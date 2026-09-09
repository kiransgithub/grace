"""Independent negative tests of authorization and remote-release evidence."""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from grace.adapters import (
    AbsenceEvidence, AdapterError, CapabilityEvidence, ClusterRegistration,
    ClusterState, DispatchSpec, GpuModel, confirm_release, deterministic_cluster_name,
)


class AdapterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.spec = DispatchSpec(
            reservation_id="res-qa", allocation_id="alloc-qa", fencing_token=42,
            environment="dev", pool_id="pool-qa", gpu_model="A100", gpu_millicards=1000,
            gpu_memory_mib=40960, device_count=1, queue="qa",
            image="registry.example/gpu@sha256:" + "a" * 64, run="python task.py",
        )
        self.cluster = ClusterRegistration(
            cluster_id="cluster-qa", cluster_uid="uid-qa", context="onprem-qa",
            namespace="grace-dev", environment="dev", pool_id="pool-qa",
            credential_ref="workload-identity://grace/controller",
            gpu_models=(GpuModel("A100", "a100-40gb", 40960),), allowed_queues=("qa",),
            state=ClusterState.ENABLED, observed_at=self.now,
        )
        self.evidence = AbsenceEvidence(
            allocation_id="alloc-qa", fencing_token=42, cluster_uid="uid-qa",
            observed_at=self.now, ownership_fenced=True, termination_requested=True,
            authoritative_read_succeeded=True, all_workload_resources_absent=True,
            scheduler_share_released=True, dispatch_reconciled=True,
            fenced_at=self.now - timedelta(seconds=2),
            stop_requested_at=self.now - timedelta(seconds=1),
        )

    def test_strings_cannot_become_production_authorization(self):
        for invalid in ("false", "true", 0, 1, None):
            with self.subTest(value=invalid):
                with self.assertRaises(AdapterError):
                    replace(self.spec, production_authorized=invalid)
                with self.assertRaises(AdapterError):
                    replace(self.cluster, production_enabled=invalid)

    def test_strings_cannot_certify_cluster_capabilities(self):
        for flag in (
            "full_gpu", "fractional_gpu", "fractional_runtime", "fractional_admission",
            "ownership_admission", "inventory_verified", "cross_namespace_sharing_reviewed",
        ):
            with self.subTest(flag=flag), self.assertRaises(AdapterError):
                CapabilityEvidence(**{flag: "false"})

    def test_truthy_string_is_not_release_evidence(self):
        for flag in (
            "ownership_fenced", "termination_requested", "authoritative_read_succeeded",
            "all_workload_resources_absent", "scheduler_share_released", "dispatch_reconciled",
        ):
            with self.subTest(flag=flag), self.assertRaises(AdapterError):
                replace(self.evidence, **{flag: "false"})

    def test_release_observation_must_follow_fence_and_stop(self):
        for changes in (
            {"observed_at": self.now - timedelta(seconds=1)},
            {"fenced_at": None},
            {"stop_requested_at": None},
            {"fenced_at": self.now},
            {"stop_requested_at": self.now + timedelta(seconds=1)},
            {"dispatch_reconciled": False},
        ):
            with self.subTest(changes=changes):
                result = confirm_release(
                    self.spec, self.cluster, replace(self.evidence, **changes), now=self.now
                )
                self.assertFalse(result.may_release)
                self.assertFalse(result.may_spillover)

    def test_stop_intent_may_precede_fence_but_snapshot_must_follow_both(self):
        # SQL cancellation persists stop intent before the controller confirms
        # ownership fencing. Neither timestamp alone establishes safe release.
        evidence = replace(
            self.evidence, stop_requested_at=self.now - timedelta(seconds=3),
            fenced_at=self.now - timedelta(seconds=1),
        )
        self.assertTrue(confirm_release(
            self.spec, self.cluster, evidence, now=self.now
        ).may_release)
        self.assertFalse(confirm_release(
            self.spec, self.cluster,
            replace(evidence, observed_at=evidence.fenced_at), now=self.now
        ).may_release)

    def test_fresh_ordered_evidence_matches_exact_allocation_and_epoch(self):
        self.assertTrue(confirm_release(
            self.spec, self.cluster, self.evidence, now=self.now
        ).may_release)
        for changes in ({"fencing_token": 41}, {"cluster_uid": "wrong-cluster"},
                        {"allocation_id": "wrong-allocation"}):
            with self.subTest(changes=changes):
                self.assertFalse(confirm_release(
                    self.spec, self.cluster, replace(self.evidence, **changes), now=self.now
                ).may_release)

    def test_dr_epoch_cannot_reuse_prior_dispatch_or_release_evidence(self):
        restored = replace(self.spec, dr_epoch=self.spec.dr_epoch + 1)
        self.assertNotEqual(deterministic_cluster_name(self.spec), deterministic_cluster_name(restored))
        # Simulate a restored counter: the same allocation and token still
        # cannot accept prior-authority evidence after the DR epoch changes.
        self.assertFalse(confirm_release(
            restored, self.cluster, self.evidence, now=self.now
        ).may_release)
        self.assertTrue(confirm_release(
            restored, self.cluster, replace(self.evidence, dr_epoch=restored.dr_epoch), now=self.now
        ).may_release)


if __name__ == "__main__":
    unittest.main()
