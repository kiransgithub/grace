"""Behavioral tests for local queueing, placement, and trusted policy selection.

These exercise the in-memory simulator, not a durable or distributed scheduler.
"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

from grace.domain import (Caller, CapacityUnavailable, EffectivePolicy, Engine, GPU,
                          InvalidTransition, PermissionDenied, PolicyRule, Request, State,
                          Usage, ValidationError, VersionConflict)


class MvpPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        self.locations = frozenset({"onprem", "gcp-central", "gcp-east"})
        self.caller = Caller("alice", "risk", allowed_locations=self.locations,
                             business_unit_id="analytics", allowed_projects=frozenset({"forecast", "research"}))
        self.controller = replace(self.caller, subject="controller", controller_authorized=True)
        self.admin = replace(self.caller, subject="admin", admin_authorized=True)
        self.gpu = GPU("gpu-1", "A100", 40000, "dev", "onprem", self.now)
        self.request = Request("risk", "training", "A100", 10000,
                               gpu_millicards=250, data_locations=self.locations)
        self.engine = Engine((self.gpu,), clock=lambda: self.now)

    def create(self, key="key", **changes):
        return self.engine.create(replace(self.request, **changes), self.caller, key)

    def process(self, resolver=None):
        return self.engine.process_queue(self.controller, resolver or (lambda tenant, subject: self.caller))

    def full(self):
        return self.create("full", gpu_millicards=1000)

    def release(self, reservation):
        self.engine.cancel(reservation.id, self.caller)
        self.now += timedelta(seconds=1)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.engine.confirm_released(reservation.id, self.controller)

    def rule(self, *, priority=0, version=1, business_unit_id=None, project_id=None, **flags):
        policy = EffectivePolicy("configured", version, priority, **flags)
        return PolicyRule("risk", "dev", policy, business_unit_id, project_id)

    def test_default_shortage_queues_without_capacity_or_runtime_expiry(self):
        self.full()
        queued = self.create()
        self.assertEqual(queued.state, State.QUEUED)
        self.assertEqual(queued.allocations, ())
        self.assertIsNone(queued.expires_at)
        self.assertIsNone(queued.admitted_at)
        self.assertIsNone(queued.selected_location)
        self.assertEqual(queued.queue_expires_at, self.now + timedelta(hours=1))
        self.assertEqual(queued.effective_policy, EffectivePolicy())
        self.assertEqual(self.create().id, queued.id)

    def test_fail_fast_capacity_option_remains_available(self):
        self.full()
        with self.assertRaises(CapacityUnavailable):
            self.create(wait_for_capacity=False)

    def test_granted_duration_starts_on_placement_not_queue_creation(self):
        holder = self.full()
        queued = self.create(duration_seconds=60)
        self.now += timedelta(minutes=10)
        self.release(holder)
        placed = self.process()[0]
        self.assertEqual(placed.id, queued.id)
        self.assertEqual(placed.admitted_at, self.now)
        self.assertEqual(placed.expires_at, self.now + timedelta(seconds=60))
        self.assertIsNone(placed.queue_expires_at)
        self.assertEqual(placed.selected_location, "onprem")

    def test_fifo_ties_use_insertion_order_not_id_or_subject(self):
        identifiers = iter(("holder", "zzz-oldest", "aaa-newest"))
        self.engine = Engine((self.gpu,), clock=lambda: self.now, id_factory=lambda: next(identifiers))
        holder = self.full()
        oldest = self.create("oldest", gpu_millicards=1000)
        newest = self.create("newest", gpu_millicards=1000)
        self.release(holder)
        self.assertEqual([r.id for r in self.process()], [oldest.id])
        self.assertEqual(self.engine.get(newest.id, self.caller).state, State.QUEUED)

    def test_new_arrival_cannot_take_capacity_ahead_of_waiting_request(self):
        holder = self.full()
        oldest = self.create("oldest", gpu_millicards=1000)
        self.release(holder)
        arriving = self.create("arriving")
        self.assertEqual(arriving.state, State.QUEUED)
        with self.assertRaises(CapacityUnavailable):
            self.create("fail-fast", wait_for_capacity=False)
        self.assertEqual([r.id for r in self.process()], [oldest.id])

    def test_nonfit_head_may_backfill_but_retains_original_order(self):
        holder = self.full()
        large = self.create("two-gpus", device_count=2)
        small = self.create("one-gpu", gpu_millicards=1000)
        self.release(holder)
        self.assertEqual([r.id for r in self.process()], [small.id])
        self.assertEqual(self.engine.get(large.id, self.caller).state, State.QUEUED)
        self.assertEqual(self.engine.get(large.id, self.caller).created_at, large.created_at)

    def test_queue_deadline_expires_without_infrastructure_release(self):
        self.full()
        queued = self.create(queue_timeout_seconds=3)
        self.now += timedelta(seconds=3)
        expired = self.process()[0]
        self.assertEqual((expired.id, expired.state, expired.release_reason),
                         (queued.id, State.EXPIRED, "queue_expired"))
        self.assertEqual(expired.allocations, ())
        self.assertIsNone(expired.expires_at)

    def test_queued_cancellation_is_immediate_idempotent_and_never_dispatches(self):
        self.full()
        queued = self.create()
        cancelled = self.engine.cancel(queued.id, self.caller, expected_version=queued.version)
        self.assertEqual(cancelled.state, State.CANCELLED)
        self.assertEqual(self.engine.cancel(queued.id, self.caller), cancelled)
        with self.assertRaises(InvalidTransition):
            self.engine.mark_dispatching(queued.id, self.controller)
        self.assertEqual(self.process(), ())

    def test_queue_does_not_reuse_releasing_or_unknown_capacity(self):
        holder = self.full()
        queued = self.create()
        self.engine.mark_unknown(holder.id, self.controller)
        self.assertEqual(self.process(), ())
        self.engine.cancel(holder.id, self.caller)
        self.assertEqual(self.process(), ())
        self.assertEqual(self.engine.get(queued.id, self.caller).allocations, ())

    def test_concurrent_default_requests_admit_exact_capacity_and_queue_rest(self):
        request = replace(self.request, gpu_millicards=10, gpu_memory_mib=400)
        with ThreadPoolExecutor(max_workers=20) as pool:
            items = list(pool.map(lambda i: self.engine.create(request, self.caller, f"concurrent-{i}"), range(160)))
        admitted = [r for r in items if r.state is State.RESERVED]
        queued = [r for r in items if r.state is State.QUEUED]
        self.assertEqual((len(admitted), len(queued)), (100, 60))
        self.assertEqual(sum(a.gpu_millicards for r in items for a in r.allocations), 1000)
        self.assertTrue(all(not r.allocations and r.expires_at is None for r in queued))

    def test_stale_inventory_can_queue_but_cannot_promote(self):
        self.now += timedelta(seconds=61)
        queued = self.create()
        self.assertEqual(queued.queue_reason, "INVENTORY_STALE")
        self.assertEqual(self.process(), ())
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.assertEqual(self.process()[0].state, State.RESERVED)

    def test_queue_promotion_requires_controller(self):
        with self.assertRaises(PermissionDenied):
            self.engine.process_queue(self.caller, lambda tenant, subject: self.caller)

    def test_revoked_current_identity_cancels_queued_without_allocation(self):
        holder = self.full()
        queued = self.create()
        self.release(holder)
        cancelled = self.process(lambda tenant, subject: None)[0]
        self.assertEqual((cancelled.id, cancelled.state), (queued.id, State.CANCELLED))
        self.assertEqual(cancelled.release_reason, "authorization_revoked")
        self.assertEqual(cancelled.allocations, ())

    def test_identity_outage_keeps_queue_and_does_not_fake_revocation(self):
        holder = self.full()
        queued = self.create()
        self.release(holder)

        def unavailable(tenant, subject):
            raise ConnectionError("identity service unavailable")

        self.assertEqual(self.process(unavailable), ())
        item = self.engine.get(queued.id, self.caller)
        self.assertEqual((item.state, item.queue_reason), (State.QUEUED, "AUTHORIZATION_UNAVAILABLE"))
        self.assertEqual(self.process()[0].state, State.RESERVED)

    def test_changed_authorized_locations_are_rechecked_at_promotion(self):
        self.engine = Engine((), clock=lambda: self.now)
        queued = self.create(location="onprem")
        self.engine.upsert_gpu(self.gpu)
        revoked = replace(self.caller, allowed_locations=frozenset({"gcp-central"}))
        self.assertEqual(self.process(lambda tenant, subject: revoked)[0].state, State.CANCELLED)
        self.assertEqual(self.engine.get(queued.id, self.caller).allocations, ())

    def test_new_tenant_controller_cannot_promote_another_tenant_queue(self):
        self.engine = Engine((), clock=lambda: self.now)
        queued = self.create()
        self.engine.upsert_gpu(self.gpu)
        controller = replace(self.controller, tenant_id="retail")
        self.assertEqual(self.engine.process_queue(controller, lambda tenant, subject: self.caller), ())
        self.assertEqual(self.engine.get(queued.id, self.caller).state, State.QUEUED)

    def test_preferred_location_wins_before_binpacking_other_location(self):
        other = replace(self.gpu, id="other", location="gcp-central",
                        observed_allocations=(Usage("outside", 750, 30000),))
        self.engine = Engine((self.gpu, other), clock=lambda: self.now)
        reservation = self.create(location_policy="preferred", location="onprem")
        self.assertEqual(reservation.selected_location, "onprem")

    def test_preferred_spills_only_to_authorized_data_ready_same_environment(self):
        devices = (replace(self.gpu, environment="qa"),
                   replace(self.gpu, id="central", location="gcp-central"),
                   replace(self.gpu, id="east", location="gcp-east"))
        self.engine = Engine(devices, clock=lambda: self.now)
        request = replace(self.request, location_policy="preferred", location="onprem",
                          data_locations=frozenset({"onprem", "gcp-east"}))
        reservation = self.engine.create(request, self.caller, "spill")
        self.assertEqual(reservation.selected_location, "gcp-east")
        self.assertEqual(reservation.allocations[0].gpu_id, "east")

    def test_strict_location_queues_despite_other_free_locations(self):
        self.engine = Engine((replace(self.gpu, location="gcp-east"),), clock=lambda: self.now)
        self.assertEqual(self.create(location="onprem", location_policy="strict").state, State.QUEUED)

    def test_any_and_legacy_unspecified_select_valid_available_location(self):
        for policy in ("strict", "any"):
            with self.subTest(policy=policy):
                self.engine = Engine((replace(self.gpu, location="gcp-east"),), clock=lambda: self.now)
                self.assertEqual(self.create(location_policy=policy).selected_location, "gcp-east")

    def test_ambiguous_location_and_queue_values_fail_fast(self):
        cases = ({"location_policy": "preferred"}, {"location_policy": "any", "location": "onprem"},
                 {"location_policy": "fallback"}, {"location_policy": []},
                 {"wait_for_capacity": "true"}, {"queue_timeout_seconds": True},
                 {"queue_timeout_seconds": 0})
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create(**changes)

    def test_only_admin_can_configure_policy_and_scopes_are_enforced(self):
        for caller in (self.caller, self.controller, replace(self.admin, admin_authorized="true"),
                       replace(self.admin, tenant_id="retail"),
                       replace(self.admin, allowed_environments=frozenset({"qa"}))):
            with self.subTest(caller=caller), self.assertRaises(PermissionDenied):
                self.engine.configure_policy(self.rule(priority=100), caller)

    def test_policy_uses_trusted_bu_and_authorized_project_not_application_name(self):
        self.engine.configure_policy(self.rule(priority=100, business_unit_id="analytics", project_id="forecast"), self.admin)
        normal = self.create(application_id="forecast")
        elevated = self.create("project", project_id="forecast")
        self.assertEqual(normal.effective_policy.priority, 0)
        self.assertEqual(elevated.effective_policy.priority, 100)
        with self.assertRaises(PermissionDenied):
            self.create("forged-project", project_id="unapproved")

    def test_project_overrides_bu_which_overrides_tenant_policy(self):
        self.engine.configure_policy(self.rule(priority=10), self.admin)
        self.engine.configure_policy(self.rule(priority=20, business_unit_id="analytics"), self.admin)
        self.engine.configure_policy(self.rule(priority=30, business_unit_id="analytics", project_id="forecast"), self.admin)
        self.assertEqual(self.create("bu").effective_policy.priority, 20)
        self.assertEqual(self.create("project", project_id="forecast").effective_policy.priority, 30)
        other_bu = replace(self.caller, business_unit_id="other")
        self.assertEqual(self.engine.create(self.request, other_bu, "tenant").effective_policy.priority, 10)

    def test_admin_priority_orders_queue_without_preempting_existing_holder(self):
        holder = self.full()
        low = self.create("low", gpu_millicards=1000, project_id="research")
        high = self.create("high", gpu_millicards=1000, project_id="forecast")
        self.engine.configure_policy(self.rule(priority=100, business_unit_id="analytics", project_id="forecast"), self.admin)
        self.assertEqual(self.process(), ())
        self.assertEqual(self.engine.get(holder.id, self.caller).state, State.RESERVED)
        self.release(holder)
        self.assertEqual([r.id for r in self.process()], [high.id])
        self.assertEqual(self.engine.get(high.id, self.caller).effective_policy.priority, 100)
        self.assertEqual(self.engine.get(low.id, self.caller).state, State.QUEUED)

    def test_revoked_project_membership_does_not_promote_old_entitlement(self):
        holder = self.full()
        queued = self.create(project_id="forecast")
        self.release(holder)
        caller = replace(self.caller, allowed_projects=frozenset({"research"}))
        self.assertEqual(self.process(lambda tenant, subject: caller)[0].state, State.CANCELLED)
        self.assertEqual(self.engine.get(queued.id, self.caller).allocations, ())

    def test_active_effective_policy_snapshot_does_not_change_on_reconfiguration(self):
        self.engine.configure_policy(self.rule(priority=100, preemption_exempt=True), self.admin)
        granted = self.create()
        self.engine.configure_policy(self.rule(priority=0, version=2), self.admin)
        self.assertEqual(self.engine.get(granted.id, self.caller).effective_policy.priority, 100)
        self.assertTrue(granted.effective_policy.preemption_exempt)
        with self.assertRaises(FrozenInstanceError):
            granted.effective_policy.priority = 0
        with self.assertRaises(VersionConflict):
            self.engine.configure_policy(self.rule(priority=5, version=2), self.admin)

    def test_queued_visible_policy_refreshes_without_fit_and_versions_never_regress(self):
        holder = self.full()
        queued = self.create(project_id="forecast", gpu_millicards=1000)
        self.engine.configure_policy(self.rule(priority=100, business_unit_id="analytics", project_id="forecast",
                                               preemption_exempt=True), self.admin)
        self.assertEqual(self.process(), ())
        pending = self.engine.get(queued.id, self.caller)
        self.assertEqual(pending.state, State.QUEUED)
        self.assertEqual(pending.effective_policy.priority, 100)
        self.assertTrue(pending.effective_policy.preemption_exempt)
        self.assertGreater(pending.version, queued.version)
        self.assertEqual(self.process(), ())
        self.assertEqual(self.engine.get(queued.id, self.caller).version, pending.version)
        self.release(holder)
        promoted = self.process()[0]
        self.assertEqual(promoted.version, pending.version + 1)
        versions = [event.version for event in self.engine.events(queued.id, self.caller)]
        self.assertEqual(versions, sorted(set(versions)))

    def test_preemption_and_idle_protection_are_independent_and_expiry_still_applies(self):
        self.engine.configure_policy(self.rule(priority=100, preemption_enabled=True,
                                               preemption_exempt=True, idle_reclamation_exempt=True), self.admin)
        granted = self.create(duration_seconds=1)
        for reason in ("preempted", "idle"):
            with self.subTest(reason=reason), self.assertRaises(PermissionDenied):
                self.engine.request_release(granted.id, self.controller, reason=reason)
        self.now += timedelta(seconds=1)
        self.assertEqual(self.engine.request_release(granted.id, self.controller, reason="expired").state,
                         State.RELEASING)

    def test_priority_does_not_implicitly_set_idle_exemption(self):
        self.engine.configure_policy(self.rule(priority=100, preemption_exempt=True), self.admin)
        granted = self.create()
        self.assertFalse(granted.effective_policy.idle_reclamation_exempt)
        self.assertEqual(self.engine.request_release(granted.id, self.controller, reason="idle").state, State.RELEASING)

    def test_preemption_disabled_by_default_even_for_controller(self):
        granted = self.create()
        with self.assertRaises(PermissionDenied):
            self.engine.request_release(granted.id, self.controller, reason="preempted")

    def test_admin_may_enable_preemption_but_release_confirmation_still_required(self):
        self.engine.configure_policy(self.rule(preemption_enabled=True), self.admin)
        granted = self.full()
        self.assertEqual(self.engine.request_release(granted.id, self.controller, reason="preempted").state,
                         State.RELEASING)
        self.assertEqual(self.create().state, State.QUEUED)


if __name__ == "__main__":
    unittest.main()
