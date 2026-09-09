"""Independent adversarial tests of the single-process safety foundation.

These checks prove local invariants only. They do not claim DB isolation,
real admission enforcement, or GPU/cluster integration.
"""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from grace.domain import Allocation, Caller, Engine, GPU, Request, State, Usage
from grace.domain.errors import (
    CapacityUnavailable,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    ReleaseUnconfirmed,
    ValidationError,
)
from grace.domain.inventory import remaining_capacity


class SystemSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.caller = Caller(
            "alice", "tenant-a", allowed_locations=frozenset({"onprem-a"})
        )
        self.controller = replace(self.caller, controller_authorized=True)
        self.gpu = GPU(
            "gpu-a", "A100-40GB", 40960, "dev", "onprem-a", self.now,
            allowed_tenants=frozenset({"tenant-a"}),
        )
        self.request = Request(
            "tenant-a", "application-a", "A100-40GB", 4096,
            data_locations=frozenset({"onprem-a"}), duration_seconds=300,
            wait_for_capacity=False,  # Preserve explicit shortage/fail-fast assertions.
        )
        self.engine = Engine((self.gpu,), clock=lambda: self.now)

    def test_equal_timestamp_snapshot_does_not_confirm_stopped_workload(self):
        reservation = self.engine.create(self.request, self.caller, "create")
        self.engine.mark_dispatching(reservation.id, self.controller)
        self.engine.mark_running(reservation.id, self.controller)
        self.engine.cancel(reservation.id, self.caller)
        # No post-stop observation has occurred. Equal timestamp precision must
        # not turn the pre-launch snapshot into evidence of remote termination.
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)

    def test_equal_timestamp_snapshot_cannot_release_even_without_dispatch(self):
        reservation = self.engine.create(self.request, self.caller, "create")
        self.engine.cancel(reservation.id, self.caller)
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)

    def test_same_region_different_clusters_cannot_form_one_multi_gpu_job(self):
        engine = Engine((
            replace(self.gpu, cluster_id="cluster-a"),
            replace(self.gpu, id="gpu-b", cluster_id="cluster-b"),
        ), clock=lambda: self.now)
        with self.assertRaises(CapacityUnavailable):
            engine.create(replace(self.request, device_count=2), self.caller, "cross-cluster")

    def test_unknown_result_and_expired_end_time_keep_capacity(self):
        reservation = self.engine.create(self.request, self.caller, "first")
        self.engine.mark_unknown(reservation.id, self.controller)
        self.now += timedelta(seconds=301)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        with self.assertRaises(CapacityUnavailable):
            self.engine.create(self.request, self.caller, "second")
        self.assertEqual(self.engine.get(reservation.id, self.caller).state, State.UNKNOWN)

    def test_tenant_and_same_tenant_other_subject_cannot_read_or_cancel(self):
        reservation = self.engine.create(self.request, self.caller, "owned")
        for caller in (
            replace(self.caller, tenant_id="tenant-b"),
            replace(self.caller, subject="mallory"),
        ):
            with self.subTest(caller=caller.subject, tenant=caller.tenant_id):
                with self.assertRaises(NotFound):
                    self.engine.get(reservation.id, caller)
                with self.assertRaises(NotFound):
                    self.engine.cancel(reservation.id, caller)
                self.assertEqual(self.engine.list(caller), ())
        self.assertEqual(self.engine.get(reservation.id, self.caller).state, State.RESERVED)

    def test_client_cannot_assert_controller_role_through_request(self):
        reservation = self.engine.create(self.request, self.caller, "owned")
        with self.assertRaises(PermissionDenied):
            self.engine.mark_running(reservation.id, self.caller)
        with self.assertRaises(PermissionDenied):
            self.engine.confirm_released(reservation.id, self.caller)

    def test_truthy_string_cannot_become_controller_read_authority(self):
        reservation = self.engine.create(self.request, self.caller, "owned")
        malformed_caller = replace(self.caller, subject="mallory", controller_authorized="false")
        with self.assertRaises(NotFound):
            self.engine.get(reservation.id, malformed_caller)
        with self.assertRaises(NotFound):
            self.engine.cancel(reservation.id, malformed_caller)
        self.assertEqual(self.engine.list(malformed_caller), ())

    def test_cancellation_prevents_late_dispatch_and_late_adoption(self):
        reservation = self.engine.create(self.request, self.caller, "owned")
        self.engine.mark_unknown(reservation.id, self.controller)
        self.engine.cancel(reservation.id, self.caller)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_dispatching(reservation.id, self.controller)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_running(reservation.id, self.controller)

    def test_snapshot_still_observing_allocation_cannot_release(self):
        reservation = self.engine.create(self.request, self.caller, "owned")
        self.engine.cancel(reservation.id, self.caller)
        self.now += timedelta(seconds=1)
        allocation = reservation.allocations[0]
        self.engine.upsert_gpu(replace(
            self.gpu, observed_at=self.now,
            observed_allocations=(Usage(allocation.id, 1000, 4096),),
        ))
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(reservation.id, self.controller)

    def test_multi_device_failure_does_not_leave_partial_capacity_claim(self):
        with self.assertRaises(CapacityUnavailable):
            self.engine.create(replace(self.request, device_count=2), self.caller, "too-large")
        self.assertEqual(self.engine.list(self.caller), ())
        reservation = self.engine.create(self.request, self.caller, "fits")
        self.assertEqual(len(reservation.allocations), 1)

    def test_observation_union_uses_max_per_resource_and_counts_foreign_claims(self):
        gpu = replace(self.gpu, observed_allocations=(
            Usage("managed", 200, 8000), Usage("outside", 300, 4000),
        ))
        free_share, free_memory = remaining_capacity(gpu, (
            Allocation("managed", self.gpu.id, 400, 2000),
            Allocation("pending", self.gpu.id, 100, 1000),
        ))
        self.assertEqual(free_share, 200)
        self.assertEqual(free_memory, 40960 - 8000 - 4000 - 1000)

    def test_empty_authorized_location_set_is_not_wildcard(self):
        with self.assertRaises(PermissionDenied):
            self.engine.create(
                self.request, replace(self.caller, allowed_locations=frozenset()), "denied"
            )

    def test_environment_container_types_raise_safe_validation_error(self):
        for invalid in ({}, [], None, 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.engine.create(replace(self.request, environment=invalid), self.caller, "invalid")

    def test_out_of_order_observation_cannot_replace_current_gpu(self):
        with self.assertRaises(ValidationError):
            self.engine.upsert_gpu(replace(
                self.gpu, observed_at=self.now - timedelta(seconds=1), healthy=False
            ))

    def test_production_requires_each_independent_gate(self):
        request = replace(self.request, environment="prod", production_opt_in=True)
        caller = replace(
            self.caller, allowed_environments=frozenset({"prod"}),
            production_authorized=True,
        )
        gpu = replace(self.gpu, environment="prod")
        for global_enabled, request_enabled, caller_enabled in (
            (False, True, True), (True, False, True), (True, True, False)
        ):
            with self.subTest(platform=global_enabled, request=request_enabled, role=caller_enabled):
                engine = Engine((gpu,), clock=lambda: self.now, production_enabled=global_enabled)
                with self.assertRaises(PermissionDenied):
                    engine.create(
                        replace(request, production_opt_in=request_enabled),
                        replace(caller, production_authorized=caller_enabled), "prod",
                    )


if __name__ == "__main__":
    unittest.main()
