"""Executable safety properties for the local reference kernel, not E2E proof."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from grace.domain import (Allocation, Caller, CapacityUnavailable, Engine, GPU,
                          IdempotencyConflict, IdleDecision, InvalidTransition,
                          InventoryStale, NotFound, PermissionDenied, ReleaseUnconfirmed,
                          Request, State, Telemetry, UnsupportedGuarantee, Usage,
                          ValidationError, VersionConflict, idle_decision, remaining_capacity)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
        self.locations = frozenset({"onprem", "gcp-central", "gcp-east"})
        self.caller = Caller("alice", "risk", allowed_locations=self.locations)
        self.controller = replace(self.caller, subject="controller", controller_authorized=True)
        self.gpu = GPU("gpu-1", "A100", 40000, "dev", "onprem", self.now)
        self.request = Request("risk", "training", "A100", 10000,
                               gpu_millicards=250, data_locations=self.locations)
        self.engine = Engine((self.gpu,), clock=lambda: self.now)

    def create(self, key="key-1", **changes):
        return self.engine.create(replace(self.request, **changes), self.caller, key)

    def fresh_empty_snapshot(self):
        self.now += timedelta(seconds=1)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))

    def test_fraction_allocation_and_integer_units(self):
        reservation = self.create()
        self.assertEqual(reservation.state, State.RESERVED)
        self.assertEqual(reservation.allocations[0].gpu_millicards, 250)
        self.assertEqual(reservation.allocations[0].memory_mib, 10000)
        self.assertEqual(reservation.version, 1)

    def test_fraction_reserves_entire_fraction_memory_not_only_minimum(self):
        reservation = self.create(gpu_memory_mib=1)
        self.assertEqual(reservation.allocations[0].memory_mib, 10000)

    def test_memory_larger_than_fraction_cannot_fit(self):
        with self.assertRaises(CapacityUnavailable):
            self.create(gpu_memory_mib=10001)

    def test_float_bool_and_invalid_fraction_rejected(self):
        for value in (0, 1001, 0.5, True, "250"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.create(gpu_millicards=value)

    def test_invalid_integer_fields_rejected(self):
        for field, value in (("device_count", False), ("device_count", 0),
                             ("gpu_memory_mib", 1.5), ("duration_seconds", -1)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.create(**{field: value})

    def test_empty_data_attestation_fails_fast(self):
        with self.assertRaises(ValidationError):
            self.create(data_locations=frozenset())

    def test_caller_location_allowlist_empty_denies(self):
        caller = replace(self.caller, allowed_locations=frozenset())
        with self.assertRaises(PermissionDenied):
            self.engine.create(self.request, caller, "x")

    def test_strict_location_never_spills(self):
        with self.assertRaises(CapacityUnavailable):
            self.create(location="gcp-central")

    def test_flexible_location_uses_other_data_ready_location(self):
        self.engine = Engine((replace(self.gpu, location="gcp-east"),), clock=lambda: self.now)
        self.assertEqual(self.create().allocations[0].gpu_id, "gpu-1")

    def test_location_data_mismatch_fails_fast(self):
        with self.assertRaises(ValidationError):
            self.create(location="onprem", data_locations=frozenset({"gcp-east"}))

    def test_caller_cannot_choose_unauthorized_location(self):
        caller = replace(self.caller, allowed_locations=frozenset({"onprem"}))
        with self.assertRaises(PermissionDenied):
            self.engine.create(replace(self.request, location="gcp-east"), caller, "x")

    def test_cross_tenant_create_denied(self):
        with self.assertRaises(PermissionDenied):
            self.create(tenant_id="retail")

    def test_other_tenant_and_other_owner_cannot_read_or_cancel(self):
        reservation = self.create()
        for caller in (replace(self.caller, tenant_id="retail"), replace(self.caller, subject="bob")):
            with self.subTest(caller=caller):
                with self.assertRaises(NotFound):
                    self.engine.get(reservation.id, caller)
                with self.assertRaises(NotFound):
                    self.engine.cancel(reservation.id, caller)
                self.assertEqual(self.engine.list(caller), ())

    def test_controller_cannot_cross_tenant(self):
        reservation = self.create()
        with self.assertRaises(NotFound):
            self.engine.get(reservation.id, replace(self.controller, tenant_id="retail"))

    def test_gpu_tenant_allowlist_respected(self):
        self.engine.upsert_gpu(replace(self.gpu, allowed_tenants=frozenset({"retail"})))
        with self.assertRaises(CapacityUnavailable):
            self.create()

    def test_dev_request_does_not_spill_to_qa_or_prod(self):
        for environment in ("qa", "prod"):
            self.engine = Engine((replace(self.gpu, environment=environment),), clock=lambda: self.now)
            with self.subTest(environment=environment), self.assertRaises(CapacityUnavailable):
                self.create()

    def test_production_requires_all_three_gates(self):
        caller = replace(self.caller, allowed_environments=frozenset({"dev", "qa", "prod"}),
                         production_authorized=True)
        request = replace(self.request, environment="prod", production_opt_in=True)
        gpu = replace(self.gpu, environment="prod")
        for platform, authorized, opted in ((False, True, True), (True, False, True), (True, True, False)):
            engine = Engine((gpu,), clock=lambda: self.now, production_enabled=platform)
            with self.subTest(platform=platform, authorized=authorized, opted=opted):
                with self.assertRaises(PermissionDenied):
                    engine.create(replace(request, production_opt_in=opted),
                                  replace(caller, production_authorized=authorized), "x")
        engine = Engine((gpu,), clock=lambda: self.now, production_enabled=True)
        self.assertEqual(engine.create(request, caller, "x").request.environment, "prod")

    def test_guaranteed_and_future_reservations_unsupported(self):
        for changes in ({"guarantee": "guaranteed"}, {"start_at": self.now + timedelta(seconds=1)}):
            with self.subTest(changes=changes), self.assertRaises(UnsupportedGuarantee):
                self.create(**changes)

    def test_naive_timestamps_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(start_at=datetime(2026, 9, 9))
        with self.assertRaises(ValidationError):
            self.engine.upsert_gpu(replace(self.gpu, observed_at=datetime(2026, 9, 9)))

    def test_stale_inventory_fail_closed(self):
        self.now += timedelta(seconds=61)
        with self.assertRaises(InventoryStale):
            self.create()

    def test_future_dated_inventory_fail_closed(self):
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now + timedelta(seconds=6)))
        with self.assertRaises(InventoryStale):
            self.create()

    def test_unhealthy_gpu_not_allocatable(self):
        self.engine.upsert_gpu(replace(self.gpu, healthy=False))
        with self.assertRaises(CapacityUnavailable):
            self.create()

    def test_fraction_full_device_conflict(self):
        self.create()
        with self.assertRaises(CapacityUnavailable):
            self.create("full", gpu_millicards=1000, gpu_memory_mib=40000)

    def test_fragmentation_never_sums_across_devices(self):
        used = (Usage("outside", 750, 30000),)
        devices = (replace(self.gpu, observed_allocations=used),
                   replace(self.gpu, id="gpu-2", observed_allocations=used))
        self.engine = Engine(devices, clock=lambda: self.now)
        with self.assertRaises(CapacityUnavailable):
            self.create(gpu_millicards=500, gpu_memory_mib=20000)

    def test_multi_gpu_atomic_admission_requires_distinct_devices(self):
        with self.assertRaises(CapacityUnavailable):
            self.create(device_count=2)
        self.assertEqual(self.engine.list(self.caller), ())

    def test_multi_gpu_cannot_span_regions(self):
        self.engine = Engine((self.gpu, replace(self.gpu, id="gpu-2", location="gcp-east")),
                             clock=lambda: self.now)
        with self.assertRaises(CapacityUnavailable):
            self.create(device_count=2)

    def test_multi_gpu_cannot_span_clusters_in_same_region(self):
        self.engine = Engine((replace(self.gpu, cluster_id="cluster-a"),
                              replace(self.gpu, id="gpu-2", cluster_id="cluster-b")), clock=lambda: self.now)
        with self.assertRaises(CapacityUnavailable):
            self.create(device_count=2)

    def test_multi_gpu_same_cluster_allocates_atomically(self):
        self.engine = Engine((replace(self.gpu, cluster_id="cluster-a"),
                              replace(self.gpu, id="gpu-2", cluster_id="cluster-a")), clock=lambda: self.now)
        self.assertEqual(len(self.create(device_count=2).allocations), 2)

    def test_binpack_prefers_used_compatible_device(self):
        self.engine = Engine((self.gpu, replace(self.gpu, id="gpu-2",
                              observed_allocations=(Usage("outside", 500, 20000),))), clock=lambda: self.now)
        self.assertEqual(self.create().allocations[0].gpu_id, "gpu-2")

    def test_distinct_observed_and_ledger_claims_both_consume(self):
        self.create(gpu_millicards=500)
        self.engine.upsert_gpu(replace(self.gpu, observed_allocations=(Usage("outside", 500, 20000),)))
        with self.assertRaises(CapacityUnavailable):
            self.create("another")

    def test_same_observed_and_ledger_claim_is_not_double_counted(self):
        reservation = self.create(gpu_millicards=500)
        observation = Usage(reservation.allocations[0].id, 500, 20000)
        self.engine.upsert_gpu(replace(self.gpu, observed_allocations=(observation,)))
        self.assertEqual(self.create("another", gpu_millicards=500).state, State.RESERVED)

    def test_mismatched_mirrored_claim_uses_resource_wise_maximum(self):
        gpu = replace(self.gpu, observed_allocations=(Usage("a", 750, 10000),))
        ledger = (Allocation("a", self.gpu.id, 500, 20000),)
        self.assertEqual(remaining_capacity(gpu, ledger), (250, 20000))

    def test_duplicate_observation_identity_rejected(self):
        usage = Usage("a", 250, 10000)
        with self.assertRaises(ValidationError):
            self.engine.upsert_gpu(replace(self.gpu, observed_allocations=(usage, usage)))

    def test_out_of_order_inventory_rejected(self):
        with self.assertRaises(ValidationError):
            self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now - timedelta(seconds=1)))

    def test_inventory_identity_cannot_move_between_environments(self):
        with self.assertRaises(ValidationError):
            self.engine.upsert_gpu(replace(self.gpu, environment="prod"))

    def test_thousand_concurrent_requests_never_overbook(self):
        request = replace(self.request, gpu_millicards=10, gpu_memory_mib=400)

        def attempt(index):
            try:
                return self.engine.create(request, self.caller, f"request-{index}")
            except CapacityUnavailable:
                return None

        with ThreadPoolExecutor(max_workers=32) as pool:
            outcomes = list(pool.map(attempt, range(1000)))
        admitted = [result for result in outcomes if result is not None]
        self.assertEqual(len(admitted), 100)
        self.assertEqual(sum(r.allocations[0].gpu_millicards for r in admitted), 1000)
        self.assertEqual(sum(r.allocations[0].memory_mib for r in admitted), 40000)

    def test_thousand_concurrent_same_key_requests_create_once(self):
        with ThreadPoolExecutor(max_workers=32) as pool:
            outcomes = list(pool.map(lambda _: self.create(), range(1000)))
        self.assertEqual(len({reservation.id for reservation in outcomes}), 1)
        self.assertEqual(len(self.engine.list(self.caller)), 1)
        self.assertEqual(len(self.engine.events(outcomes[0].id, self.caller)), 1)

    def test_idempotency_shape_mismatch_conflicts(self):
        self.create()
        with self.assertRaises(IdempotencyConflict):
            self.create(duration_seconds=60)

    def test_idempotency_scope_includes_caller_identity(self):
        first = self.create()
        second = self.engine.create(self.request, replace(self.caller, subject="bob"), "key-1")
        self.assertNotEqual(first.id, second.id)

    def test_unknown_outcome_does_not_release_or_duplicate(self):
        reservation = self.create(gpu_millicards=1000)
        self.engine.mark_dispatching(reservation.id, self.controller)
        self.engine.mark_unknown(reservation.id, self.controller)
        self.assertEqual(self.create(gpu_millicards=1000).id, reservation.id)
        with self.assertRaises(CapacityUnavailable):
            self.create("different", gpu_millicards=1000)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_dispatching(reservation.id, self.controller)

    def test_cancellation_always_holds_until_confirmed(self):
        reservation = self.create(gpu_millicards=1000)
        self.assertEqual(self.engine.cancel(reservation.id, self.caller).state, State.RELEASING)
        with self.assertRaises(CapacityUnavailable):
            self.create("new")
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)
        self.fresh_empty_snapshot()
        self.assertEqual(self.engine.confirm_released(reservation.id, self.controller).state, State.CANCELLED)
        self.assertEqual(self.create("new").state, State.RESERVED)

    def test_user_cannot_assert_release_confirmation(self):
        reservation = self.create()
        self.engine.cancel(reservation.id, self.caller)
        with self.assertRaises(PermissionDenied):
            self.engine.confirm_released(reservation.id, self.caller)

    def test_cancel_inflight_needs_settled_dispatch_and_new_snapshot(self):
        reservation = self.create(gpu_millicards=1000)
        self.engine.mark_dispatching(reservation.id, self.controller)
        self.engine.cancel(reservation.id, self.caller)
        self.fresh_empty_snapshot()
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_running(reservation.id, self.controller)
        self.engine.confirm_dispatch_settled(reservation.id, self.controller)
        self.assertEqual(self.engine.confirm_released(reservation.id, self.controller).state, State.CANCELLED)

    def test_observed_live_allocation_blocks_release(self):
        reservation = self.create()
        self.engine.cancel(reservation.id, self.caller)
        self.now += timedelta(seconds=1)
        usage = Usage(reservation.allocations[0].id, 250, 10000)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now, observed_allocations=(usage,)))
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)

    def test_cancel_expected_version_is_atomic(self):
        reservation = self.create()
        self.engine.mark_dispatching(reservation.id, self.controller)
        with self.assertRaises(VersionConflict):
            self.engine.cancel(reservation.id, self.caller, expected_version=reservation.version)
        self.assertEqual(self.engine.get(reservation.id, self.caller).state, State.DISPATCHING)

    def test_expiration_does_not_free_on_wall_clock_alone(self):
        reservation = self.create(duration_seconds=1, gpu_millicards=1000)
        self.now += timedelta(seconds=2)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        with self.assertRaises(CapacityUnavailable):
            self.create("new")
        self.engine.request_release(reservation.id, self.controller, reason="expired")
        self.fresh_empty_snapshot()
        self.assertEqual(self.engine.confirm_released(reservation.id, self.controller).state, State.EXPIRED)

    def test_expired_reservation_cannot_start_dispatch(self):
        reservation = self.create(duration_seconds=1)
        self.now += timedelta(seconds=1)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_dispatching(reservation.id, self.controller)

    def test_expiry_reason_rejected_before_expiration(self):
        reservation = self.create()
        with self.assertRaises(InvalidTransition):
            self.engine.request_release(reservation.id, self.controller, reason="expired")

    def test_idempotent_retry_after_cancel_does_not_create_new_allocation(self):
        reservation = self.create()
        self.engine.cancel(reservation.id, self.caller)
        self.fresh_empty_snapshot()
        self.engine.confirm_released(reservation.id, self.controller)
        result = self.create()
        self.assertEqual(result.id, reservation.id)
        self.assertEqual(result.state, State.CANCELLED)

    def test_history_and_returned_values_are_immutable(self):
        reservation = self.create()
        history = self.engine.events(reservation.id, self.caller)
        self.engine.cancel(reservation.id, self.caller)
        self.assertEqual(len(history), 1)
        self.assertEqual(len(self.engine.events(reservation.id, self.caller)), 2)
        with self.assertRaises(FrozenInstanceError):
            reservation.state = State.CANCELLED

    def test_missing_or_stale_telemetry_not_idle(self):
        self.assertEqual(idle_decision(None, self.now), IdleDecision.UNKNOWN)
        self.assertEqual(idle_decision(Telemetry(self.now, 0), self.now), IdleDecision.UNKNOWN)
        sample = Telemetry(self.now - timedelta(seconds=61), 0, False, False, False, False)
        self.assertEqual(idle_decision(sample, self.now), IdleDecision.UNKNOWN)

    def test_single_low_utilization_sample_only_suspects_idle(self):
        sample = Telemetry(self.now, 0, False, False, False, False)
        self.assertEqual(idle_decision(sample, self.now), IdleDecision.SUSPECTED_IDLE)
        self.assertEqual(idle_decision(replace(sample, data_loading=True), self.now), IdleDecision.ACTIVE)

    def test_nan_or_invalid_telemetry_does_not_imply_idle(self):
        for value in (float("nan"), float("inf"), -1, 101, True):
            sample = Telemetry(self.now, value, False, False, False, False)
            with self.subTest(value=value):
                self.assertEqual(idle_decision(sample, self.now), IdleDecision.UNKNOWN)

    def test_exception_has_transport_safe_code_and_detail(self):
        error = VersionConflict("reload reservation")
        self.assertEqual(error.code, "ABORTED")
        self.assertEqual(error.detail, "reload reservation")


if __name__ == "__main__":
    unittest.main()
