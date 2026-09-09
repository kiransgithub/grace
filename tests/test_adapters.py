"""Contract/fault tests; no credentials, SkyPilot install, or GPUs required."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from grace.adapters import (
    AdapterError, CapabilityEvidence, ClusterRegistration, ClusterRegistry,
    ClusterState, DispatchSpec, GpuModel, LeaseAuthorization, AbsenceEvidence, Outcome,
    FractionalIsolation, HamiRuntimeEvidence,
    SkyPilotAdapter, build_task, confirm_release, deterministic_cluster_name,
    requires_gpu_admission,
)
from grace.adapters.skypilot_kai import map_dispatch_error


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def spec(**changes):
    base = DispatchSpec(
        reservation_id="reservation-1", allocation_id="allocation-1", fencing_token=7,
        environment="dev", pool_id="onprem-dev", gpu_model="A100", gpu_millicards=1000,
        gpu_memory_mib=40000, device_count=1, queue="research",
        image="registry.example.org/approved@sha256:" + "a" * 64, run="python train.py",
    )
    return replace(base, **changes)


def cluster(**changes):
    base = ClusterRegistration(
        cluster_id="onprem-a", cluster_uid="kubernetes-uid-a", context="onprem-a",
        namespace="grace-workloads-dev", environment="dev", pool_id="onprem-dev",
        credential_ref="workload-identity://grace/skypilot-dispatcher",
        gpu_models=(GpuModel("A100", "a100-40gb", 40960),), allowed_queues=("research",),
        state=ClusterState.ENABLED, observed_at=NOW,
        capabilities=CapabilityEvidence(
            certificate_id="ci-gpu-cert-001", skypilot_version="0.13.0", kai_version="0.17.0",
            full_gpu=True, fractional_gpu=True, fractional_runtime=True,
            fractional_admission=True, ownership_admission=True, inventory_verified=True,
            cross_namespace_sharing_reviewed=True,
        ),
    )
    return replace(base, **changes)


def authorize(item):
    return LeaseAuthorization(item.reservation_id, item.allocation_id,
                              item.dr_epoch, item.fencing_token)


def hami_evidence(**changes):
    return replace(HamiRuntimeEvidence(
        certificate_id="hami-cuda-cert-001", hami_core_version="certified-core-commit",
        resource_isolator_version="1.0.0-chart", libvgpu_sha256="b" * 64,
        certified_images=(spec().image,), observed_at=NOW,
        binder_plugin_enabled=True, library_loaded=True, cuda_memory_limit_verified=True,
        cuda_sm_limit_verified=True, admission_opt_out_protected=True,
    ), **changes)


class FakeSky:
    __version__ = "0.13.0"

    class Task:
        @staticmethod
        def from_yaml_config(config):
            return config

    def __init__(self, error=None, request_id="remote-request-id"):
        self.error = error
        self.request_id = request_id
        self.launches = []
        self.terminations = []

    def launch(self, task, **options):
        self.launches.append((task, options))
        if self.error:
            raise self.error
        return self.request_id

    def down(self, name):
        self.terminations.append(name)
        if self.error:
            raise self.error
        return self.request_id


class TaskTranslationTests(unittest.TestCase):
    def test_native_full_gpu_is_exact_existing_context(self):
        task = build_task(spec(device_count=2), cluster(), now=NOW)
        self.assertEqual(task["resources"]["infra"], "k8s/onprem-a")
        self.assertEqual(task["resources"]["accelerators"], {"A100": 2})
        k8s = task["config"]["kubernetes"]
        self.assertEqual(k8s["pod_config"]["spec"]["schedulerName"], "kai-scheduler")
        self.assertEqual(k8s["custom_metadata"]["annotations"]["grace.ai/fencing-token"], "7")
        self.assertEqual(k8s["custom_metadata"]["annotations"]["grace.ai/dr-epoch"], "1")
        self.assertNotIn("gpu-fraction", k8s["custom_metadata"]["annotations"])
        self.assertNotIn("disk_size", task["resources"])

    def test_fractional_disabled_by_default_even_when_cluster_certified(self):
        with self.assertRaises(AdapterError) as caught:
            build_task(spec(gpu_millicards=250, gpu_memory_mib=10000), cluster(), now=NOW)
        self.assertEqual(caught.exception.code, "FRACTIONAL_UNCERTIFIED")

    def test_fractional_never_requests_native_gpu(self):
        task = build_task(spec(gpu_millicards=250, gpu_memory_mib=10000), cluster(),
                          now=NOW, allow_experimental_fractional=True)
        self.assertNotIn("accelerators", task["resources"])
        self.assertNotIn("nvidia.com/gpu", str(task))
        annotations = task["config"]["kubernetes"]["custom_metadata"]["annotations"]
        self.assertEqual(annotations["gpu-fraction"], "0.25")
        self.assertNotIn("gpu-memory", annotations)
        self.assertNotIn("NVIDIA_VISIBLE_DEVICES", str(task))

    def test_fractional_precision_and_multidevice_fail_fast(self):
        for changes, code in [
            ({"gpu_millicards": 251, "gpu_memory_mib": 10000}, "FRACTIONAL_PRECISION_UNSUPPORTED"),
            ({"gpu_millicards": 250, "gpu_memory_mib": 10000, "device_count": 2},
             "FRACTIONAL_TOPOLOGY_UNSUPPORTED"),
        ]:
            with self.subTest(code=code), self.assertRaises(AdapterError) as caught:
                build_task(spec(**changes), cluster(), now=NOW, allow_experimental_fractional=True)
            self.assertEqual(caught.exception.code, code)

    def test_fractional_allows_millicard_precision_only_if_certified(self):
        c = cluster(gpu_models=(GpuModel("A100", "a100-40gb", 40960, 1),))
        task = build_task(spec(gpu_millicards=1, gpu_memory_mib=40), c,
                          now=NOW, allow_experimental_fractional=True)
        self.assertEqual(task["config"]["kubernetes"]["custom_metadata"]["annotations"]["gpu-fraction"], "0.001")

    def test_uncertified_runtime_cannot_enable_fractional(self):
        c = cluster(capabilities=replace(cluster().capabilities, fractional_runtime=False))
        with self.assertRaises(AdapterError):
            build_task(spec(gpu_millicards=250, gpu_memory_mib=10000), c,
                       now=NOW, allow_experimental_fractional=True)

    def test_memory_budget_is_per_device_and_exact_integer(self):
        with self.assertRaises(AdapterError) as caught:
            build_task(spec(gpu_millicards=250, gpu_memory_mib=10241), cluster(),
                       now=NOW, allow_experimental_fractional=True)
        self.assertEqual(caught.exception.code, "GPU_MEMORY_EXCEEDED")

    def test_unapproved_environment_pool_queue_tenant_model_fail(self):
        for changes, code in [
            ({"environment": "qa"}, "ENVIRONMENT_DENIED"),
            ({"pool_id": "gcp-dev"}, "POOL_DENIED"),
            ({"queue": "admin"}, "QUEUE_DENIED"),
            ({"trusted_tenant_id": "other"}, "TENANT_DENIED"),
            ({"gpu_model": "H100"}, "GPU_MODEL_UNAVAILABLE"),
        ]:
            with self.subTest(code=code), self.assertRaises(AdapterError) as caught:
                build_task(spec(**changes), cluster(), now=NOW)
            self.assertEqual(caught.exception.code, code)

    def test_stale_and_future_inventory_denied(self):
        for instant in [NOW - timedelta(seconds=61), NOW + timedelta(seconds=1)]:
            with self.subTest(instant=instant), self.assertRaises(AdapterError) as caught:
                build_task(spec(), cluster(observed_at=instant), now=NOW)
            self.assertEqual(caught.exception.code, "STALE_INVENTORY")

    def test_draining_and_production_require_explicit_gates(self):
        with self.assertRaises(AdapterError):
            build_task(spec(), cluster(state=ClusterState.DRAINING), now=NOW)
        prod = spec(environment="prod")
        target = cluster(environment="prod", production_enabled=True)
        with self.assertRaises(AdapterError):
            build_task(prod, target, now=NOW)
        self.assertIsInstance(build_task(replace(prod, production_authorized=True), target, now=NOW), dict)

    def test_validation_rejects_bool_fraction_and_invalid_identifiers(self):
        for changes in [{"gpu_millicards": True}, {"gpu_millicards": 0},
                        {"gpu_millicards": 1001}, {"memory_gib": 1},
                        {"allocation_id": "$(shell)"}, {"fencing_token": -1}]:
            with self.subTest(changes=changes), self.assertRaises(AdapterError):
                spec(**changes)

    def test_string_policy_flags_are_never_truthy_authorizations(self):
        for make in [lambda: spec(production_authorized="false"),
                     lambda: cluster(production_enabled="false"),
                     lambda: CapabilityEvidence(full_gpu="false"),
                     lambda: SkyPilotAdapter(live_enabled="false")]:
            with self.assertRaises(AdapterError):
                make()

    def test_name_tracks_allocation_and_epoch_not_user_command(self):
        self.assertEqual(deterministic_cluster_name(spec()), deterministic_cluster_name(spec(run="other")))
        self.assertNotEqual(deterministic_cluster_name(spec()), deterministic_cluster_name(spec(fencing_token=8)))
        self.assertNotEqual(deterministic_cluster_name(spec()), deterministic_cluster_name(spec(dr_epoch=2)))
        self.assertLess(len(deterministic_cluster_name(spec())), 63)


class OnboardingTests(unittest.TestCase):
    def test_state_machine_requires_discovery_and_certification(self):
        c = cluster(state=ClusterState.REGISTERED)
        with self.assertRaises(AdapterError):
            c.transition(ClusterState.ENABLED, now=NOW)
        for state in [ClusterState.DISCOVERED, ClusterState.VALIDATING, ClusterState.CERTIFIED,
                      ClusterState.ENABLED, ClusterState.DRAINING]:
            c = c.transition(state, now=NOW)
        with self.assertRaises(AdapterError):
            c.transition(ClusterState.DISABLED, now=NOW, active_allocations=1)
        c = c.transition(ClusterState.DISABLED, now=NOW)
        self.assertIs(c, c.transition(ClusterState.DISABLED, now=NOW))

    def test_missing_certification_cannot_enable(self):
        c = cluster(state=ClusterState.VALIDATING, capabilities=CapabilityEvidence())
        with self.assertRaises(AdapterError):
            c.transition(ClusterState.CERTIFIED, now=NOW)

    def test_idempotent_onboarding_and_cluster_identity_no_double_count(self):
        registry = ClusterRegistry()
        c = cluster(state=ClusterState.REGISTERED)
        self.assertEqual(registry.register(c, "key-1"), registry.register(c, "key-1"))
        with self.assertRaises(AdapterError) as caught:
            registry.register(replace(c, namespace="other"), "key-1")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(AdapterError) as caught:
            registry.register(replace(c, cluster_id="alias", environment="qa"), "key-2")
        self.assertEqual(caught.exception.code, "CLUSTER_IDENTITY_CONFLICT")

    def test_inline_credentials_disallowed(self):
        with self.assertRaises(AdapterError):
            cluster(credential_ref="Bearer eyJhbGc...")


class HamiContractTests(unittest.TestCase):
    def hami_spec(self, **changes):
        return spec(gpu_millicards=250, gpu_memory_mib=10000,
                    fractional_isolation=FractionalIsolation.HAMI_MEMORY, **changes)

    def hami_cluster(self, **changes):
        return cluster(capabilities=replace(cluster().capabilities,
                       hami_runtime=hami_evidence(**changes)))

    def build(self, request=None, target=None):
        return build_task(request or self.hami_spec(), target or self.hami_cluster(),
                          now=NOW, allow_experimental_fractional=True)

    def test_accounting_mode_does_not_claim_enforcement_when_hami_is_installed(self):
        task = self.build(spec(gpu_millicards=250, gpu_memory_mib=10000))
        annotations = task["config"]["kubernetes"]["custom_metadata"]["annotations"]
        self.assertEqual(annotations["grace.ai/fractional-isolation"], "kai_accounting")
        self.assertNotIn("kai-resource-isolator.io/inject", annotations)
        self.assertNotIn("envs", task)

    def test_memory_mode_requests_verified_webhook_without_duplicate_memory_or_hostpath(self):
        task = self.build()
        annotations = task["config"]["kubernetes"]["custom_metadata"]["annotations"]
        self.assertEqual(annotations["kai-resource-isolator.io/inject"], "true")
        self.assertEqual(annotations["grace.ai/effective-gpu-memory-mib"], "10240")
        self.assertEqual(annotations["grace.ai/hami-certificate-id"], "hami-cuda-cert-001")
        for value in ["CUDA_DEVICE_MEMORY_LIMIT", "LD_PRELOAD", "hostPath", "CUDA_DEVICE_SM_LIMIT"]:
            self.assertNotIn(value, str(task))

    def test_sm_mode_adds_integer_cuda_cap_to_certified_job_environment(self):
        request = replace(self.hami_spec(), fractional_isolation=FractionalIsolation.HAMI_MEMORY_SM)
        self.assertEqual(self.build(request)["envs"], {"CUDA_DEVICE_SM_LIMIT": "25"})

    def test_sm_cap_never_silently_rounds_finer_accounting_precision(self):
        target = replace(self.hami_cluster(), gpu_models=(GpuModel("A100", "a100-40gb", 40960, 1),))
        request = replace(self.hami_spec(), gpu_millicards=251,
                          fractional_isolation=FractionalIsolation.HAMI_MEMORY_SM)
        with self.assertRaises(AdapterError) as caught:
            self.build(request, target)
        self.assertEqual(caught.exception.code, "HAMI_SM_PRECISION_UNSUPPORTED")

    def test_enforcement_does_not_follow_from_kai_version_or_caller_mode_alone(self):
        with self.assertRaises(AdapterError) as caught:
            self.build(target=cluster())
        self.assertEqual(caught.exception.code, "HAMI_UNCERTIFIED")
        for missing in ["binder_plugin_enabled", "library_loaded", "cuda_memory_limit_verified",
                        "admission_opt_out_protected"]:
            with self.subTest(missing=missing), self.assertRaises(AdapterError) as caught:
                self.build(target=self.hami_cluster(**{missing: False}))
            self.assertEqual(caught.exception.code, "HAMI_UNCERTIFIED")

    def test_sm_evidence_is_separate_from_memory_evidence(self):
        target = self.hami_cluster(cuda_sm_limit_verified=False)
        self.assertIsInstance(self.build(target=target), dict)
        request = replace(self.hami_spec(), fractional_isolation=FractionalIsolation.HAMI_MEMORY_SM)
        with self.assertRaises(AdapterError) as caught:
            self.build(request, target)
        self.assertEqual(caught.exception.code, "HAMI_SM_UNCERTIFIED")

    def test_stale_future_and_changed_image_fail_closed(self):
        for instant in [NOW - timedelta(seconds=61), NOW + timedelta(seconds=1)]:
            with self.subTest(instant=instant), self.assertRaises(AdapterError) as caught:
                self.build(target=self.hami_cluster(observed_at=instant))
            self.assertEqual(caught.exception.code, "HAMI_RUNTIME_STALE")
        with self.assertRaises(AdapterError) as caught:
            self.build(self.hami_spec(image="registry.example.org/approved:latest"))
        self.assertEqual(caught.exception.code, "HAMI_IMAGE_UNCERTIFIED")
        self.assertIsInstance(self.build(self.hami_spec(image="docker:" + spec().image)), dict)

    def test_hami_does_not_accept_unsupported_or_unstable_kai_versions(self):
        for version in ["0.16.9", "0.17.0-rc1", "latest", "0.17"]:
            target = self.hami_cluster()
            target = replace(target, capabilities=replace(target.capabilities, kai_version=version))
            with self.subTest(version=version), self.assertRaises(AdapterError) as caught:
                self.build(target=target)
            self.assertEqual(caught.exception.code, "HAMI_UNCERTIFIED")
        target = self.hami_cluster()
        target = replace(target, capabilities=replace(target.capabilities, kai_version="v0.17.0"))
        self.assertIsInstance(self.build(target=target), dict)

    def test_hami_does_not_expand_certification_to_full_or_multidevice_requests(self):
        for request, code in [
            (replace(self.hami_spec(), gpu_millicards=1000), "HAMI_TOPOLOGY_UNSUPPORTED"),
            (replace(self.hami_spec(), device_count=2), "FRACTIONAL_TOPOLOGY_UNSUPPORTED"),
        ]:
            with self.subTest(code=code), self.assertRaises(AdapterError) as caught:
                self.build(request)
            self.assertEqual(caught.exception.code, code)

    def test_evidence_rejects_truthy_strings_and_mutable_image_tags(self):
        for changes in [{"library_loaded": "true"}, {"cuda_sm_limit_verified": 1},
                        {"certified_images": ("registry.example.org/approved:latest",)},
                        {"libvgpu_sha256": "unknown"}]:
            with self.subTest(changes=changes), self.assertRaises(AdapterError):
                hami_evidence(**changes)
        with self.assertRaises(AdapterError):
            spec(fractional_isolation="hami_memory")
        with self.assertRaises(AdapterError):
            CapabilityEvidence(hami_runtime={"library_loaded": True})

    def test_cuda_env_and_hami_optout_are_subject_to_gpu_admission(self):
        self.assertTrue(requires_gpu_admission({"metadata": {"annotations": {
            "kai-resource-isolator.io/inject": "false"}}}))
        for kind in ["containers", "initContainers", "ephemeralContainers"]:
            for name in ["CUDA_DEVICE_MEMORY_LIMIT", "CUDA_DEVICE_SM_LIMIT", "NVIDIA_VISIBLE_DEVICES"]:
                with self.subTest(kind=kind, name=name):
                    self.assertTrue(requires_gpu_admission({"spec": {kind: [
                        {"name": "cpu", "env": [{"name": name, "value": "25"}]}]}}))


class DispatchFaultTests(unittest.TestCase):
    def adapter(self, fake, **kwargs):
        return SkyPilotAdapter(live_enabled=True, authorize_lease=authorize, sdk=fake, **kwargs)

    def test_disabled_sdk_is_not_called(self):
        fake = FakeSky()
        result = SkyPilotAdapter(sdk=fake).submit(spec(), cluster(), now=NOW)
        self.assertEqual(result.outcome, Outcome.REJECTED)
        self.assertEqual(fake.launches, [])

    def test_fenced_owner_not_dispatched(self):
        fake = FakeSky()
        result = SkyPilotAdapter(live_enabled=True, authorize_lease=lambda _: False,
                                sdk=fake).submit(spec(), cluster(), now=NOW)
        self.assertEqual(result.code, "LEASE_NOT_AUTHORIZED")
        self.assertEqual(fake.launches, [])

    def test_authorization_requires_exact_dr_epoch_and_fence(self):
        for authorization in [
            replace(authorize(spec()), dr_epoch=2),
            replace(authorize(spec()), fencing_token=8),
            replace(authorize(spec()), allocation_id="another"),
            replace(authorize(spec()), reservation_id="another"),
            replace(authorize(spec()), authorized=False), True, "true", None,
        ]:
            with self.subTest(authorization=authorization):
                fake = FakeSky()
                adapter = SkyPilotAdapter(live_enabled=True,
                    authorize_lease=lambda _: authorization, sdk=fake)
                result = adapter.submit(spec(), cluster(), now=NOW)
                self.assertEqual(result.code, "LEASE_NOT_AUTHORIZED")
                self.assertEqual(fake.launches, [])

    def test_dr_epoch_requires_positive_nonboolean_integer(self):
        for invalid in [0, -1, True, "1", None]:
            with self.subTest(epoch=invalid):
                with self.assertRaises(AdapterError):
                    spec(dr_epoch=invalid)
                with self.assertRaises(AdapterError):
                    replace(authorize(spec()), dr_epoch=invalid)
                with self.assertRaises(AdapterError):
                    AbsenceEvidence("allocation-1", 7, "uid", NOW,
                        True, True, True, True, True, dr_epoch=invalid)

    def test_uncertified_sdk_version_is_rejected_before_launch(self):
        fake = FakeSky()
        fake.__version__ = "unvalidated"
        result = self.adapter(fake).submit(spec(), cluster(), now=NOW)
        self.assertEqual(result.code, "SDK_VERSION_UNCERTIFIED")
        self.assertEqual(fake.launches, [])

    def test_launch_receipt_is_only_accepted(self):
        fake = FakeSky()
        result = self.adapter(fake).submit(spec(), cluster(), now=NOW)
        self.assertEqual(result.outcome, Outcome.ACCEPTED)
        self.assertFalse(result.may_release)
        self.assertFalse(result.may_spillover)
        self.assertFalse(fake.launches[0][1]["retry_until_up"])

    def test_every_post_submit_error_is_unknown_no_blind_retry(self):
        for error in [TimeoutError("secret token"), PermissionError("forbidden"),
                      ConnectionError("connection reset"), ValueError("internal error")]:
            with self.subTest(error=type(error)):
                fake = FakeSky(error)
                result = self.adapter(fake).submit(spec(), cluster(), now=NOW)
                self.assertEqual(result.outcome, Outcome.UNKNOWN)
                self.assertFalse(result.may_spillover)
                self.assertFalse(result.may_release)
                self.assertEqual(len(fake.launches), 1)
                self.assertNotIn("secret", str(result))

    def test_invalid_receipt_never_means_success(self):
        for receipt in [None, "None", "", (1, "handle")]:
            result = self.adapter(FakeSky(request_id=receipt)).submit(spec(), cluster(), now=NOW)
            self.assertEqual(result.outcome, Outcome.UNKNOWN)

    def test_termination_does_not_release(self):
        fake = FakeSky()
        result = self.adapter(fake).terminate(spec())
        self.assertEqual(result.code, "TERMINATION_ACCEPTED")
        self.assertFalse(result.may_release)
        self.assertEqual(fake.terminations, [deterministic_cluster_name(spec())])

    def test_error_mapping_before_and_after_submission(self):
        error = AdapterError("INVALID_ARGUMENT", "bad input")
        self.assertEqual(map_dispatch_error(error, "name", submitted=False).code, "INVALID_ARGUMENT")
        self.assertEqual(map_dispatch_error(error, "name", submitted=True).outcome, Outcome.UNKNOWN)

    def test_release_needs_all_authoritative_evidence(self):
        evidence = AbsenceEvidence("allocation-1", 7, "kubernetes-uid-a", NOW,
                                   True, True, True, True, True,
                                   NOW - timedelta(seconds=2), NOW - timedelta(seconds=1),
                                   dispatch_reconciled=True)
        self.assertTrue(confirm_release(spec(), cluster(), evidence, now=NOW).may_release)
        for changes in [
            {"fencing_token": 6}, {"allocation_id": "other"}, {"cluster_uid": "other"},
            {"dr_epoch": 2},
            {"observed_at": NOW - timedelta(seconds=61)}, {"ownership_fenced": False},
            {"termination_requested": False}, {"authoritative_read_succeeded": False},
            {"all_workload_resources_absent": False}, {"scheduler_share_released": False},
            {"fenced_at": None}, {"stop_requested_at": None},
            {"stop_requested_at": NOW}, {"fenced_at": NOW},
            {"dispatch_reconciled": False},
        ]:
            with self.subTest(changes=changes):
                self.assertFalse(confirm_release(spec(), cluster(), replace(evidence, **changes),
                                                 now=NOW).may_release)

    def test_stop_intent_may_precede_fence_but_absence_must_follow_both(self):
        evidence = AbsenceEvidence("allocation-1", 7, "kubernetes-uid-a", NOW,
                                   True, True, True, True, True,
                                   fenced_at=NOW - timedelta(seconds=1),
                                   stop_requested_at=NOW - timedelta(seconds=2),
                                   dispatch_reconciled=True)
        self.assertTrue(confirm_release(spec(), cluster(), evidence, now=NOW).may_release)
        for observed_at in [evidence.fenced_at, evidence.stop_requested_at]:
            self.assertFalse(confirm_release(spec(), cluster(), replace(evidence, observed_at=observed_at),
                                             now=NOW).may_release)
        with self.assertRaises(AdapterError):
            replace(evidence, dispatch_reconciled="true")


class AdmissionContractTests(unittest.TestCase):
    def test_cpu_like_fractional_pod_must_be_checked(self):
        for key in ["gpu-fraction", "gpu-memory", "gpu-fraction-container-name", "grace.ai/dr-epoch"]:
            self.assertTrue(requires_gpu_admission({"metadata": {"annotations": {key: "0.5"}}}))

    def test_init_gpu_resources_and_runtime_are_checked(self):
        self.assertTrue(requires_gpu_admission({"spec": {"initContainers": [
            {"resources": {"limits": {"nvidia.com/gpu": 1}}}]} }))
        self.assertTrue(requires_gpu_admission({"spec": {"runtimeClassName": "nvidia"}}))
        self.assertTrue(requires_gpu_admission({"spec": {"schedulerName": "kai-scheduler"}}))
        self.assertFalse(requires_gpu_admission({"spec": {"containers": [{"name": "cpu"}]}}))


if __name__ == "__main__":
    unittest.main()
