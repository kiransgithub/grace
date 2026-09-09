"""Independent adversarial checks for the single-process MVP queue contract.

These tests exercise observable invariants; they do not certify PostgreSQL,
Okta, KAI execution, hardware isolation, or cross-tenant scheduling fairness.
"""

import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from grace.domain import Caller, EffectivePolicy, Engine, GPU, PolicyRule, Request, State
from grace.domain.errors import PermissionDenied, ReleaseUnconfirmed, ValidationError
from grace.transport.service import parse_request


class MvpSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        self.owner = Caller(
            "alice", "enterprise", allowed_locations=frozenset({"onprem", "gcp-central"}),
            business_unit_id="retail", allowed_projects=frozenset({"forecasting", "research"}),
        )
        self.controller = replace(self.owner, subject="controller", controller_authorized=True)
        self.admin = replace(self.owner, subject="admin", admin_authorized=True)
        self.request = Request(
            "enterprise", "forecast-model", "A100-40GB", 4096,
            gpu_millicards=250, location_policy="any",
            data_locations=frozenset({"onprem", "gcp-central"}),
            duration_seconds=300, queue_timeout_seconds=600, project_id="forecasting",
        )
        self.gpu = GPU("physical-1", "A100-40GB", 40960, "dev", "onprem", self.now,
                       cluster_id="onprem-cluster", allowed_tenants=frozenset({"enterprise"}))
        self.engine = Engine(clock=lambda: self.now)

    def create(self, key, **changes):
        return self.engine.create(replace(self.request, **changes), self.owner, key)

    def resolve(self, tenant, subject):
        return self.owner if (tenant, subject) == (self.owner.tenant_id, self.owner.subject) else None

    def process(self, resolver=None):
        return self.engine.process_queue(self.controller, resolver or self.resolve)

    def policy(self, policy, *, project="forecasting"):
        return self.engine.configure_policy(
            PolicyRule("enterprise", "dev", policy, business_unit_id="retail", project_id=project),
            self.admin,
        )

    def test_parallel_default_requests_queue_without_overbooking_or_partial_holds(self):
        self.engine.upsert_gpu(self.gpu)
        with ThreadPoolExecutor(max_workers=16) as executor:
            items = list(executor.map(lambda i: self.create(f"parallel-{i}"), range(64)))
        grants = [item for item in items if item.state is State.RESERVED]
        waiting = [item for item in items if item.state is State.QUEUED]
        self.assertEqual(len(grants), 4)
        self.assertEqual(len(waiting), 60)
        self.assertEqual(sum(a.gpu_millicards for item in items for a in item.allocations), 1000)
        self.assertEqual(sum(a.memory_mib for item in items for a in item.allocations), 40960)
        self.assertTrue(all(not item.allocations and item.expires_at is None for item in waiting))

    def test_new_arrival_does_not_take_free_gpu_before_older_same_tenant_queue(self):
        first = self.create("older", gpu_millicards=1000)
        self.engine.upsert_gpu(self.gpu)
        newcomer = self.create("newer", gpu_millicards=1000)
        self.assertEqual(newcomer.state, State.QUEUED)
        self.process()
        self.assertEqual(self.engine.get(first.id, self.owner).state, State.RESERVED)
        self.assertEqual(self.engine.get(newcomer.id, self.owner).state, State.QUEUED)

    def test_fifo_uses_arrival_order_even_when_clock_and_priority_tie(self):
        ids = iter(("z-first", "a-second"))
        self.engine = Engine(clock=lambda: self.now, id_factory=lambda: next(ids))
        first = self.create("first", gpu_millicards=1000)
        second = self.create("second", gpu_millicards=1000)
        self.engine.upsert_gpu(self.gpu)
        self.process()
        self.assertEqual(self.engine.get(first.id, self.owner).state, State.RESERVED)
        self.assertEqual(self.engine.get(second.id, self.owner).state, State.QUEUED)

    def test_queue_wait_does_not_consume_grant_duration(self):
        queued = self.create("wait", duration_seconds=90)
        self.assertIsNone(queued.expires_at)
        self.assertIsNone(queued.admitted_at)
        self.now += timedelta(seconds=120)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.process()
        grant = self.engine.get(queued.id, self.owner)
        self.assertEqual(grant.admitted_at, self.now)
        self.assertEqual(grant.expires_at, self.now + timedelta(seconds=90))
        self.assertIsNone(grant.queue_expires_at)

    def test_exact_queue_deadline_expires_even_if_capacity_just_appeared(self):
        queued = self.create("deadline", queue_timeout_seconds=10)
        self.now += timedelta(seconds=10)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.process()
        expired = self.engine.get(queued.id, self.owner)
        self.assertEqual(expired.state, State.EXPIRED)
        self.assertFalse(expired.allocations)
        self.assertIsNone(expired.admitted_at)
        self.assertIsNone(expired.expires_at)

    def test_confirmed_revocation_cancels_but_identity_outage_preserves_queue(self):
        queued = self.create("auth")
        self.engine.upsert_gpu(self.gpu)

        def unavailable(tenant, subject):
            raise TimeoutError("identity catalog unavailable")

        self.process(unavailable)
        retained = self.engine.get(queued.id, self.owner)
        self.assertEqual(retained.state, State.QUEUED)
        self.assertEqual(retained.queue_reason, "AUTHORIZATION_UNAVAILABLE")
        self.assertFalse(retained.allocations)
        self.process(lambda tenant, subject: None)
        revoked = self.engine.get(queued.id, self.owner)
        self.assertEqual(revoked.state, State.CANCELLED)
        self.assertFalse(revoked.allocations)

    def test_resolver_cannot_substitute_different_user_or_tenant(self):
        for owner in (replace(self.owner, subject="mallory"),
                      replace(self.owner, tenant_id="another-enterprise")):
            with self.subTest(subject=owner.subject, tenant=owner.tenant_id):
                self.engine = Engine(clock=lambda: self.now)
                queued = self.create("auth")
                self.engine.upsert_gpu(self.gpu)
                self.process(lambda tenant, subject: owner)
                self.assertFalse(self.engine.get(queued.id, self.owner).allocations)

    def test_queued_project_permission_is_rechecked_at_admission(self):
        queued = self.create("project")
        self.engine.upsert_gpu(self.gpu)
        self.process(lambda tenant, subject: replace(self.owner, allowed_projects=frozenset()))
        revoked = self.engine.get(queued.id, self.owner)
        self.assertEqual(revoked.state, State.CANCELLED)
        self.assertFalse(revoked.allocations)

    def test_location_allowlist_shrink_cannot_admit_with_old_grants(self):
        queued = self.create("location", data_locations=frozenset({"onprem"}))
        self.engine.upsert_gpu(self.gpu)
        owner = replace(self.owner, allowed_locations=frozenset({"gcp-central"}))
        self.process(lambda tenant, subject: owner)
        retained = self.engine.get(queued.id, self.owner)
        self.assertEqual(retained.state, State.QUEUED)
        self.assertFalse(retained.allocations)

    def test_cancelled_queue_idempotency_replay_never_claims_new_capacity(self):
        queued = self.create("original")
        self.assertEqual(self.engine.cancel(queued.id, self.owner).state, State.CANCELLED)
        self.engine.upsert_gpu(self.gpu)
        self.process()
        replay = self.create("original")
        self.assertEqual(replay.id, queued.id)
        self.assertEqual(replay.state, State.CANCELLED)
        self.assertFalse(replay.allocations)

    def test_unconfirmed_cancellation_does_not_promote_waiting_reservation(self):
        self.engine.upsert_gpu(self.gpu)
        grant = self.create("grant", gpu_millicards=1000)
        queued = self.create("queued", gpu_millicards=1000)
        self.engine.cancel(grant.id, self.owner)
        with self.assertRaises(ReleaseUnconfirmed):
            self.engine.confirm_released(grant.id, self.controller)
        self.process()
        self.assertEqual(self.engine.get(queued.id, self.owner).state, State.QUEUED)
        self.now += timedelta(seconds=1)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.engine.confirm_released(grant.id, self.controller)
        self.process()
        self.assertEqual(self.engine.get(queued.id, self.owner).state, State.RESERVED)

    def test_waiting_policy_visibility_refreshes_without_admitting_or_changing_grant_snapshot(self):
        self.engine.upsert_gpu(self.gpu)
        grant = self.create("grant", gpu_millicards=1000)
        queued = self.create("queued", gpu_millicards=1000)
        policy = EffectivePolicy("priority-change", version=2, priority=80, preemption_exempt=True)
        self.policy(policy)
        self.process()
        refreshed = self.engine.get(queued.id, self.owner)
        self.assertEqual(refreshed.state, State.QUEUED)
        self.assertEqual(refreshed.effective_policy, policy)
        self.assertGreater(refreshed.version, queued.version)
        self.assertEqual(self.engine.get(grant.id, self.owner).effective_policy, grant.effective_policy)
        self.assertFalse(refreshed.allocations)

    def test_admin_priority_orders_waiting_projects_without_preempting_existing_grant(self):
        self.engine.upsert_gpu(self.gpu)
        grant = self.create("grant", gpu_millicards=1000)
        low = self.create("low", gpu_millicards=1000, project_id="research")
        high = self.create("high", gpu_millicards=1000)
        self.policy(EffectivePolicy("priority", priority=10))
        self.process()
        self.assertEqual(self.engine.get(grant.id, self.owner).state, State.RESERVED)
        self.assertEqual(self.engine.get(high.id, self.owner).state, State.QUEUED)
        self.engine.cancel(grant.id, self.owner)
        self.now += timedelta(seconds=1)
        self.engine.upsert_gpu(replace(self.gpu, observed_at=self.now))
        self.engine.confirm_released(grant.id, self.controller)
        self.process()
        self.assertEqual(self.engine.get(high.id, self.owner).state, State.RESERVED)
        self.assertEqual(self.engine.get(low.id, self.owner).state, State.QUEUED)

    def test_project_rule_is_complete_override_and_cannot_cross_business_unit(self):
        self.policy(EffectivePolicy("bu", priority=10, idle_reclamation_exempt=True), project=None)
        project = EffectivePolicy("project", priority=0)
        self.policy(project)
        own = self.create("own")
        self.assertEqual(own.effective_policy, project)
        foreign = self.engine.create(self.request, replace(self.owner, business_unit_id="risk"), "foreign")
        self.assertEqual(foreign.effective_policy, EffectivePolicy())

    def test_idle_and_preemption_exemptions_are_independent(self):
        for idle_exempt, preempt_exempt, allowed_reason, denied_reason in (
            (False, True, "idle", "preempted"),
            (True, False, "preempted", "idle"),
        ):
            with self.subTest(idle_exempt=idle_exempt, preempt_exempt=preempt_exempt):
                self.engine = Engine((self.gpu,), clock=lambda: self.now)
                self.policy(EffectivePolicy("separate", preemption_enabled=True,
                    preemption_exempt=preempt_exempt, idle_reclamation_exempt=idle_exempt))
                grant = self.create("grant", gpu_millicards=1000)
                with self.assertRaises(PermissionDenied):
                    self.engine.request_release(grant.id, self.controller, reason=denied_reason)
                self.assertEqual(self.engine.request_release(grant.id, self.controller,
                    reason=allowed_reason).state, State.RELEASING)

    def test_preferred_spill_requires_both_current_authorization_and_data_attestation(self):
        remote = replace(self.gpu, id="gcp-gpu", location="gcp-central", cluster_id="gke-central")
        for allowed, data in ((frozenset({"onprem"}), self.request.data_locations),
                              (self.owner.allowed_locations, frozenset({"onprem"}))):
            with self.subTest(allowed=allowed, data=data):
                engine = Engine((remote,), clock=lambda: self.now)
                queued = engine.create(replace(self.request, location="onprem", location_policy="preferred",
                    data_locations=data), replace(self.owner, allowed_locations=allowed), "spill")
                self.assertEqual(queued.state, State.QUEUED)
                self.assertFalse(queued.allocations)

    def test_request_body_cannot_supply_trusted_admin_or_owner_context(self):
        body = dict(tenant_id="enterprise", application_id="model", gpu_type="A100-40GB",
                    gpu_memory_mib=4096, data_locations=["onprem"])
        for field, value in (("priority", 100), ("admin_authorized", True),
                             ("business_unit_id", "retail"), ("owner_subject", "admin"),
                             ("effective_policy", {"preemption_exempt": True})):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                parse_request({**body, field: value})


if __name__ == "__main__":
    unittest.main()
