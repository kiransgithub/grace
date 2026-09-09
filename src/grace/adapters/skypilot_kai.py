"""Conservative SkyPilot/KAI task translation and optional SDK seam.

No Kubernetes create API is present: all workload dispatch goes through SkyPilot.
Fractional translation is experimental and disabled unless explicitly certified.
SDK calls are asynchronous. A returned request ID is NOT evidence of execution,
completion, cancellation, or resource release.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import hashlib
import importlib
from typing import Callable

from .contracts import (
    AdapterError, ClusterRegistration, ClusterState, DispatchSpec, FractionalIsolation, LeaseAuthorization, aware,
    bounded_int, label, require, strict_bool,
)


def deterministic_cluster_name(spec: DispatchSpec) -> str:
    # Fixed length protects cloud/Kubernetes name constraints and avoids UUID
    # truncation collisions. Same name is correlation, NOT exactly-once dispatch.
    digest = hashlib.sha256(
        f"{spec.reservation_id}:{spec.allocation_id}:{spec.dr_epoch}:{spec.fencing_token}".encode()).hexdigest()[:24]
    return f"grace-{digest}"


def build_task(spec: DispatchSpec, cluster: ClusterRegistration, *, now: datetime,
               allow_experimental_fractional: bool = False) -> dict:
    """Produce an allowlisted Sky task; does not call any external service."""
    strict_bool(allow_experimental_fractional, "allow_experimental_fractional")
    require(cluster.state == ClusterState.ENABLED, "CLUSTER_DISABLED", "cluster is not enabled")
    cluster.require_fresh(now)
    require(cluster.capabilities.baseline_certified(), "CAPABILITY_UNCERTIFIED", "cluster lacks baseline certification")
    require(spec.environment == cluster.environment, "ENVIRONMENT_DENIED", "cross-environment placement is forbidden")
    require(spec.pool_id == cluster.pool_id, "POOL_DENIED", "allocation targets another pool")
    require(spec.trusted_tenant_id == cluster.trusted_tenant_id, "TENANT_DENIED", "allocation crosses a trust boundary")
    require(spec.queue in cluster.allowed_queues, "QUEUE_DENIED", "queue is not approved for this cluster")
    if spec.environment == "prod":
        require(cluster.production_enabled and spec.production_authorized,
                "PRODUCTION_DISABLED", "production requires environment and request authorization")
    model = next((item for item in cluster.gpu_models if item.name == spec.gpu_model), None)
    require(model is not None, "GPU_MODEL_UNAVAILABLE", "GPU model is not certified in target pool")
    require(spec.gpu_memory_mib * 1000 <= model.memory_mib * spec.gpu_millicards,
            "GPU_MEMORY_EXCEEDED", "per-device memory exceeds fractional device budget")
    fractional = spec.gpu_millicards != 1000
    hami = spec.fractional_isolation != FractionalIsolation.KAI_ACCOUNTING
    require(fractional or not hami, "HAMI_TOPOLOGY_UNSUPPORTED",
            "this HAMi translator only certifies a single fractional GPU container")
    if fractional:
        require(allow_experimental_fractional and cluster.capabilities.fractional_certified(),
                "FRACTIONAL_UNCERTIFIED", "SkyPilot/KAI fractional runtime is not enabled and certified")
        require(spec.device_count == 1, "FRACTIONAL_TOPOLOGY_UNSUPPORTED",
                "fractional multi-device translation requires separate certification")
        require(spec.gpu_millicards % model.fraction_quantum_millicards == 0,
                "FRACTIONAL_PRECISION_UNSUPPORTED", "fraction is below certified scheduler precision")
    if hami:
        evidence = cluster.capabilities.hami_runtime
        require(evidence is not None, "HAMI_UNCERTIFIED", "HAMi runtime evidence is required")
        require_sm = spec.fractional_isolation == FractionalIsolation.HAMI_MEMORY_SM
        evidence.require_certified(now=now, freshness_seconds=cluster.freshness_seconds,
                                   image=spec.image, kai_version=cluster.capabilities.kai_version,
                                   require_sm=require_sm)
        require(not require_sm or spec.gpu_millicards % 10 == 0,
                "HAMI_SM_PRECISION_UNSUPPORTED", "SM limit must be an exact integer percentage; no silent rounding")
    annotations = {
        "grace.ai/fencing-token": str(spec.fencing_token),
        "grace.ai/dr-epoch": str(spec.dr_epoch),
        "grace.ai/gpu-millicards": str(spec.gpu_millicards),
        "grace.ai/gpu-memory-mib": str(spec.gpu_memory_mib),
        "grace.ai/device-count": str(spec.device_count),
        "grace.ai/fractional-isolation": spec.fractional_isolation.value,
    }
    if fractional:
        # gpu-memory and gpu-fraction are alternate KAI requests. Do not emit both.
        annotations["gpu-fraction"] = f"0.{spec.gpu_millicards:03d}".rstrip("0")
    if hami:
        # KAI v0.17's binder derives CUDA_DEVICE_MEMORY_LIMIT from the received
        # fraction. The isolator owns libvgpu/preload injection. Never inject a
        # duplicate memory variable, invent library paths, or request a hostPath.
        annotations["kai-resource-isolator.io/inject"] = "true"
        annotations["grace.ai/hami-certificate-id"] = evidence.certificate_id
        annotations["grace.ai/effective-gpu-memory-mib"] = str(
            model.memory_mib * spec.gpu_millicards // 1000)
    resources = {
        "infra": f"k8s/{cluster.context}",
        "cpus": f"{spec.cpu_count}+",
        "memory": f"{spec.memory_gib}+",
        "image_id": spec.image if spec.image.startswith("docker:") else f"docker:{spec.image}",
    }
    if not fractional:
        resources["accelerators"] = {spec.gpu_model: spec.device_count}
    task = {
        "name": deterministic_cluster_name(spec),
        "resources": resources,
        "num_nodes": 1,
        "config": {"kubernetes": {
            "namespace": cluster.namespace,
            "custom_metadata": {
                "labels": {
                    "app.kubernetes.io/managed-by": "grace",
                    "grace.ai/reservation-id": spec.reservation_id,
                    "grace.ai/allocation-id": spec.allocation_id,
                    "grace.ai/environment": spec.environment,
                    "grace.ai/pool-id": spec.pool_id,
                    "grace.ai/tenant-id": spec.trusted_tenant_id,
                    "kai.scheduler/queue": spec.queue,
                },
                "annotations": annotations,
            },
            "pod_config": {"spec": {
                "schedulerName": "kai-scheduler",
                "nodeSelector": {
                    "grace.ai/pool-id": cluster.pool_id,
                    "grace.ai/gpu-model": model.selector_value,
                    "grace.ai/tenant-id": cluster.trusted_tenant_id,
                },
            }},
        }},
        "run": spec.run,
    }
    if spec.fractional_isolation == FractionalIsolation.HAMI_MEMORY_SM:
        # HAMi-core supports this variable; KAI v0.17 does NOT derive it. This is
        # the certified SkyPilot task/child-process environment, not a guarantee
        # that arbitrary hostile commands cannot alter their own environment.
        task["envs"] = {"CUDA_DEVICE_SM_LIMIT": str(spec.gpu_millicards // 10)}
    return task


def requires_gpu_admission(pod: dict) -> bool:
    """Detection contract for webhook tests, NOT a substitute for admission.

    Also protect GPU node selectors/runtime classes and pod updates in the actual
    webhook. A plain CPU resource request can still be a KAI fractional GPU pod.
    """
    annotations = pod.get("metadata", {}).get("annotations", {}) or {}
    if any(key in annotations for key in (
            "gpu-fraction", "gpu-memory", "gpu-fraction-container-name",
            "grace.ai/allocation-id", "grace.ai/fencing-token", "grace.ai/dr-epoch",
            "kai-resource-isolator.io/inject", "grace.ai/fractional-isolation")):
        return True
    spec = pod.get("spec", {})
    if spec.get("schedulerName") == "kai-scheduler":
        return True
    if spec.get("runtimeClassName") in {"nvidia", "nvidia-cdi"}:
        return True
    selectors = spec.get("nodeSelector", {}) or {}
    if any(key.startswith(("nvidia.com/", "grace.ai/")) for key in selectors):
        return True
    for container in spec.get("containers", []) + spec.get("initContainers", []) + spec.get("ephemeralContainers", []):
        if any(item.get("name") in {"CUDA_DEVICE_MEMORY_LIMIT", "CUDA_DEVICE_SM_LIMIT", "NVIDIA_VISIBLE_DEVICES"}
               for item in container.get("env", [])):
            return True
        resources = container.get("resources", {})
        if any(key.startswith("nvidia.com/") for kind in ("requests", "limits")
               for key in resources.get(kind, {})):
            return True
    return False


class Outcome(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    RELEASE_CONFIRMED = "release_confirmed"


@dataclass(frozen=True)
class AdapterResult:
    outcome: Outcome
    cluster_name: str
    code: str
    operation_id: str | None = None

    @property
    def may_release(self) -> bool:
        return self.outcome == Outcome.RELEASE_CONFIRMED

    @property
    def may_spillover(self) -> bool:
        # Another reservation transaction and placement policy evaluation are
        # still required. UNKNOWN is never permission to launch elsewhere.
        return self.outcome == Outcome.RELEASE_CONFIRMED


def map_dispatch_error(error: Exception, cluster_name: str, *, submitted: bool) -> AdapterResult:
    if not submitted:
        code = error.code if isinstance(error, AdapterError) else "ADAPTER_PREFLIGHT_FAILED"
        return AdapterResult(Outcome.REJECTED, cluster_name, code)
    # Never leak provider exceptions (which can contain credentials) to users.
    # Even an apparently definitive error can follow a partially-created pod.
    return AdapterResult(Outcome.UNKNOWN, cluster_name, "DISPATCH_OUTCOME_UNKNOWN")


@dataclass(frozen=True)
class AbsenceEvidence:
    """Trusted reconciler evidence, not user input and not sky.status alone."""

    allocation_id: str
    fencing_token: int
    cluster_uid: str
    observed_at: datetime
    ownership_fenced: bool
    termination_requested: bool
    authoritative_read_succeeded: bool
    all_workload_resources_absent: bool
    scheduler_share_released: bool
    fenced_at: datetime | None = None
    stop_requested_at: datetime | None = None
    dr_epoch: int = 1
    dispatch_reconciled: bool = False

    def __post_init__(self):
        label(self.allocation_id, "allocation_id")
        bounded_int(self.fencing_token, 1, 2**63 - 1, "fencing_token")
        bounded_int(self.dr_epoch, 1, 2**63 - 1, "DR epoch")
        require(isinstance(self.cluster_uid, str) and bool(self.cluster_uid),
                "INVALID_ARGUMENT", "cluster_uid must be a string")
        aware(self.observed_at, "observed_at")
        for name in ("ownership_fenced", "termination_requested", "authoritative_read_succeeded",
                     "all_workload_resources_absent", "scheduler_share_released", "dispatch_reconciled"):
            strict_bool(getattr(self, name), name)
        for name in ("fenced_at", "stop_requested_at"):
            value = getattr(self, name)
            if value is not None:
                aware(value, name)


def confirm_release(spec: DispatchSpec, cluster: ClusterRegistration,
                    evidence: AbsenceEvidence, *, now: datetime) -> AdapterResult:
    aware(now, "now")
    aware(evidence.observed_at, "observed_at")
    same_owner = (evidence.allocation_id == spec.allocation_id and
                  evidence.dr_epoch == spec.dr_epoch and
                  evidence.fencing_token == spec.fencing_token and
                  evidence.cluster_uid == cluster.cluster_uid)
    fresh = timedelta(0) <= now - evidence.observed_at <= timedelta(seconds=cluster.freshness_seconds)
    causal = (evidence.fenced_at is not None and evidence.stop_requested_at is not None and
              evidence.observed_at > max(evidence.fenced_at, evidence.stop_requested_at))
    confirmed = (same_owner and fresh and causal and evidence.dispatch_reconciled and evidence.ownership_fenced and
                 evidence.termination_requested and evidence.authoritative_read_succeeded and
                 evidence.all_workload_resources_absent and evidence.scheduler_share_released)
    return AdapterResult(Outcome.RELEASE_CONFIRMED if confirmed else Outcome.UNKNOWN,
                         deterministic_cluster_name(spec),
                         "RELEASE_CONFIRMED" if confirmed else "RELEASE_NOT_CONFIRMED")


class SkyPilotAdapter:
    """Optional SDK seam. Caller owns durable outbox, single-owner and deadline rules.

    authorize_lease must transactionally recheck the current DR epoch and fencing token,
    reservation status, expiry, attempt budget, and command ownership. A caller
    must NOT use this seam with a permissive callback in production. There is no
    automatic retry or recovery launch, and no direct Kubernetes submission.
    """

    def __init__(self, *, live_enabled: bool = False,
                 allow_experimental_fractional: bool = False,
                 authorize_lease: Callable[[DispatchSpec], LeaseAuthorization | None] | None = None,
                 sdk=None):
        strict_bool(live_enabled, "live_enabled")
        strict_bool(allow_experimental_fractional, "allow_experimental_fractional")
        self.live_enabled = live_enabled
        self.allow_experimental_fractional = allow_experimental_fractional
        self.authorize_lease = authorize_lease
        self._sdk = sdk

    def _load_sdk(self):
        if self._sdk is None:
            self._sdk = importlib.import_module("sky")
        return self._sdk

    def _authorize(self, spec: DispatchSpec) -> None:
        require(self.live_enabled, "LIVE_DISPATCH_DISABLED", "live dispatch is disabled")
        authorization = self.authorize_lease(spec) if self.authorize_lease is not None else None
        require(isinstance(authorization, LeaseAuthorization) and authorization.matches(spec),
                "LEASE_NOT_AUTHORIZED", "current allocation lease is not authorized")

    def submit(self, spec: DispatchSpec, cluster: ClusterRegistration, *, now: datetime) -> AdapterResult:
        name = deterministic_cluster_name(spec)
        submitted = False
        try:
            task_config = build_task(spec, cluster, now=now,
                                     allow_experimental_fractional=self.allow_experimental_fractional)
            sdk = self._load_sdk() if self.live_enabled else None
            require(sdk is not None, "LIVE_DISPATCH_DISABLED", "live dispatch is disabled")
            require(getattr(sdk, "__version__", None) == cluster.capabilities.skypilot_version,
                    "SDK_VERSION_UNCERTIFIED", "SDK version differs from the cluster capability certificate")
            task = sdk.Task.from_yaml_config(task_config)
            self._authorize(spec)
            submitted = True
            # Remote SDK returns request ID; do not wait inside the public API.
            request_id = sdk.launch(task, cluster_name=name, detach_run=True,
                                    retry_until_up=False)
            if not isinstance(request_id, str) or not request_id or request_id == "None":
                return AdapterResult(Outcome.UNKNOWN, name, "INVALID_OPERATION_RECEIPT")
            return AdapterResult(Outcome.ACCEPTED, name, "DISPATCH_ACCEPTED", request_id)
        except Exception as error:
            return map_dispatch_error(error, name, submitted=submitted)

    def terminate(self, spec: DispatchSpec) -> AdapterResult:
        name = deterministic_cluster_name(spec)
        submitted = False
        try:
            self._authorize(spec)
            sdk = self._load_sdk()
            submitted = True
            request_id = sdk.down(name)
            if not isinstance(request_id, str) or not request_id or request_id == "None":
                return AdapterResult(Outcome.UNKNOWN, name, "INVALID_OPERATION_RECEIPT")
            # ACCEPTED only. Wait for infrastructure and scheduler evidence before
            # returning capacity, including fraction accounting on reservation pods.
            return AdapterResult(Outcome.ACCEPTED, name, "TERMINATION_ACCEPTED", request_id)
        except Exception as error:
            return map_dispatch_error(error, name, submitted=submitted)
