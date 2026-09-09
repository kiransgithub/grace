"""Validated, dependency-free contracts for existing Kubernetes cluster onboarding.

This registry is a reference in-memory contract, not the durable reservation ledger.
Production code must persist it in PostgreSQL with optimistic version checks and an
audit event in the same transaction. Only platform controllers create these objects.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
import re


class AdapterError(ValueError):
    """Safe-to-display pre-dispatch error; no infrastructure side effects occurred."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise AdapterError(code, message)


def bounded_int(value: int, low: int, high: int, name: str) -> None:
    require(type(value) is int and low <= value <= high,
            "INVALID_ARGUMENT", f"{name} must be an integer in [{low}, {high}]")


def strict_bool(value: bool, name: str) -> None:
    require(type(value) is bool, "INVALID_ARGUMENT", f"{name} must be a boolean")


def label(value: str, name: str) -> None:
    require(isinstance(value, str) and re.fullmatch(
        r"[a-zA-Z0-9](?:[a-zA-Z0-9_.-]{0,61}[a-zA-Z0-9])?", value) is not None,
        "INVALID_ARGUMENT", f"{name} must be a nonempty Kubernetes label value")


def aware(value: datetime, name: str) -> None:
    require(isinstance(value, datetime) and value.tzinfo is not None and
            value.utcoffset() is not None, "INVALID_ARGUMENT", f"{name} must be timezone-aware")


class ClusterState(str, Enum):
    REGISTERED = "registered"
    DISCOVERED = "discovered"
    VALIDATING = "validating"
    CERTIFIED = "certified"
    ENABLED = "enabled"
    DRAINING = "draining"
    DISABLED = "disabled"


class FractionalIsolation(str, Enum):
    """Required software enforcement; never a hardware partition or throughput SLA."""

    KAI_ACCOUNTING = "kai_accounting"
    HAMI_MEMORY = "hami_memory"
    HAMI_MEMORY_SM = "hami_memory_sm"


@dataclass(frozen=True)
class HamiRuntimeEvidence:
    """Trusted controller observation, not values supplied by reservation callers.

    The exact workload image and libvgpu artifact are certified on real CUDA
    hardware. A version string or an injected environment variable alone is not
    evidence that interception/enforcement occurred. GRACE does not mount a
    guessed hostPath: the separately deployed isolator owns library injection.
    """

    certificate_id: str
    hami_core_version: str
    resource_isolator_version: str
    libvgpu_sha256: str
    certified_images: tuple[str, ...]
    observed_at: datetime
    binder_plugin_enabled: bool = False
    library_loaded: bool = False
    cuda_memory_limit_verified: bool = False
    cuda_sm_limit_verified: bool = False
    admission_opt_out_protected: bool = False

    def __post_init__(self):
        label(self.certificate_id, "HAMi certificate ID")
        for name in ("hami_core_version", "resource_isolator_version"):
            require(isinstance(getattr(self, name), str) and bool(getattr(self, name).strip()),
                    "INVALID_ARGUMENT", f"{name} is required")
        require(isinstance(self.libvgpu_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", self.libvgpu_sha256) is not None,
                "INVALID_ARGUMENT", "libvgpu SHA-256 must identify the tested binary")
        require(isinstance(self.certified_images, tuple) and bool(self.certified_images),
                "INVALID_ARGUMENT", "certified workload image digests are required")
        for item in self.certified_images:
            require(isinstance(item, str) and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", item) is not None
                    and not item.startswith("docker:"), "INVALID_ARGUMENT",
                    "certified images must be exact digest references without the SkyPilot docker prefix")
        aware(self.observed_at, "HAMi observed_at")
        for name in ("binder_plugin_enabled", "library_loaded", "cuda_memory_limit_verified",
                     "cuda_sm_limit_verified", "admission_opt_out_protected"):
            strict_bool(getattr(self, name), name)

    def require_certified(self, *, now: datetime, freshness_seconds: int, image: str,
                          kai_version: str, require_sm: bool) -> None:
        aware(now, "now")
        strict_bool(require_sm, "require_sm")
        version = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", kai_version)
        require(version is not None and tuple(map(int, version.groups())) >= (0, 17, 0),
                "HAMI_UNCERTIFIED", "HAMi integration requires a certified stable KAI version >=0.17.0")
        require(timedelta(0) <= now - self.observed_at <= timedelta(seconds=freshness_seconds),
                "HAMI_RUNTIME_STALE", "HAMi runtime evidence is stale or clock-skewed")
        require(image.removeprefix("docker:") in self.certified_images,
                "HAMI_IMAGE_UNCERTIFIED", "workload image was not certified with this HAMi runtime")
        require(self.binder_plugin_enabled and self.library_loaded and self.cuda_memory_limit_verified
                and self.admission_opt_out_protected, "HAMI_UNCERTIFIED",
                "HAMi needs observed library loading, memory enforcement and protected injection")
        require(not require_sm or self.cuda_sm_limit_verified, "HAMI_SM_UNCERTIFIED",
                "HAMi compute limiting has not been certified for this runtime and workload image")


@dataclass(frozen=True)
class GpuModel:
    name: str
    selector_value: str
    memory_mib: int
    fraction_quantum_millicards: int = 10

    def __post_init__(self):
        label(self.name, "GPU model")
        label(self.selector_value, "GPU selector value")
        bounded_int(self.memory_mib, 1, 2**31 - 1, "GPU memory MiB")
        bounded_int(self.fraction_quantum_millicards, 1, 1000, "fraction quantum")


@dataclass(frozen=True)
class CapabilityEvidence:
    """Controller-verified capabilities tied to exact tested software versions.

    Booleans are never accepted from an unprivileged reservation request. They are
    recorded after signed/attested certification tests, not inferred from versions.
    """

    certificate_id: str = ""
    skypilot_version: str = ""
    kai_version: str = ""
    full_gpu: bool = False
    fractional_gpu: bool = False
    fractional_runtime: bool = False
    fractional_admission: bool = False
    ownership_admission: bool = False
    inventory_verified: bool = False
    cross_namespace_sharing_reviewed: bool = False
    hami_runtime: HamiRuntimeEvidence | None = None

    def __post_init__(self):
        for name in ("full_gpu", "fractional_gpu", "fractional_runtime", "fractional_admission",
                     "ownership_admission", "inventory_verified", "cross_namespace_sharing_reviewed"):
            strict_bool(getattr(self, name), name)
        for name in ("certificate_id", "skypilot_version", "kai_version"):
            require(isinstance(getattr(self, name), str), "INVALID_ARGUMENT", f"{name} must be a string")
        require(self.hami_runtime is None or isinstance(self.hami_runtime, HamiRuntimeEvidence),
                "INVALID_ARGUMENT", "HAMi runtime evidence must be a validated controller record")

    def baseline_certified(self) -> bool:
        return bool(self.certificate_id and self.skypilot_version and self.kai_version
                    and self.full_gpu and self.ownership_admission and self.inventory_verified)

    def fractional_certified(self) -> bool:
        return (self.baseline_certified() and self.fractional_gpu and
                self.fractional_runtime and self.fractional_admission and
                self.cross_namespace_sharing_reviewed)


@dataclass(frozen=True)
class ClusterRegistration:
    cluster_id: str
    cluster_uid: str
    context: str
    namespace: str
    environment: str
    pool_id: str
    credential_ref: str
    gpu_models: tuple[GpuModel, ...]
    allowed_queues: tuple[str, ...]
    state: ClusterState = ClusterState.REGISTERED
    capabilities: CapabilityEvidence = CapabilityEvidence()
    observed_at: datetime | None = None
    freshness_seconds: int = 60
    production_enabled: bool = False
    trusted_tenant_id: str = "default"
    revision: int = 1

    def __post_init__(self):
        strict_bool(self.production_enabled, "production_enabled")
        require(isinstance(self.capabilities, CapabilityEvidence), "INVALID_ARGUMENT", "invalid capabilities")
        for name in ("cluster_id", "namespace", "pool_id", "trusted_tenant_id"):
            label(getattr(self, name), name)
        require(bool(self.cluster_uid), "INVALID_ARGUMENT", "cluster UID is required")
        require(bool(self.context) and not any(c.isspace() for c in self.context),
                "INVALID_ARGUMENT", "exact Kubernetes context is required")
        require(self.environment in {"dev", "qa", "prod"}, "INVALID_ARGUMENT", "invalid environment")
        require(isinstance(self.state, ClusterState), "INVALID_ARGUMENT", "invalid onboarding state")
        require(bool(re.fullmatch(r"(?:secret|vault|workload-identity)://[A-Za-z0-9_./:-]+",
                                  self.credential_ref)), "INVALID_ARGUMENT",
                "credential_ref must reference a secret manager or workload identity, never contain credentials")
        require(bool(self.gpu_models), "INVALID_ARGUMENT", "at least one GPU model is required")
        require(len({m.name for m in self.gpu_models}) == len(self.gpu_models),
                "INVALID_ARGUMENT", "duplicate GPU model")
        for queue in self.allowed_queues:
            label(queue, "queue")
        require(bool(self.allowed_queues), "INVALID_ARGUMENT", "allowed queues are required")
        bounded_int(self.freshness_seconds, 1, 3600, "freshness seconds")
        bounded_int(self.revision, 1, 2**63 - 1, "revision")
        if self.observed_at is not None:
            aware(self.observed_at, "observed_at")

    def require_fresh(self, now: datetime) -> None:
        aware(now, "now")
        require(self.observed_at is not None, "STALE_INVENTORY", "cluster inventory has never been observed")
        age = now - self.observed_at
        require(timedelta(0) <= age <= timedelta(seconds=self.freshness_seconds),
                "STALE_INVENTORY", "cluster inventory is stale or clock-skewed")

    def transition(self, target: ClusterState, *, now: datetime,
                   active_allocations: int = 0) -> "ClusterRegistration":
        allowed = {
            ClusterState.REGISTERED: {ClusterState.DISCOVERED, ClusterState.DISABLED},
            ClusterState.DISCOVERED: {ClusterState.VALIDATING, ClusterState.DISABLED},
            ClusterState.VALIDATING: {ClusterState.CERTIFIED, ClusterState.DISABLED},
            ClusterState.CERTIFIED: {ClusterState.ENABLED, ClusterState.VALIDATING, ClusterState.DISABLED},
            ClusterState.ENABLED: {ClusterState.DRAINING},
            ClusterState.DRAINING: {ClusterState.DISABLED},
            ClusterState.DISABLED: {ClusterState.DISCOVERED},
        }
        bounded_int(active_allocations, 0, 2**31 - 1, "active allocations")
        if target == self.state:
            return self  # Idempotent repetition does not increment revision.
        require(target in allowed[self.state], "INVALID_TRANSITION", "invalid cluster state transition")
        if target in {ClusterState.CERTIFIED, ClusterState.ENABLED}:
            self.require_fresh(now)
            require(self.capabilities.baseline_certified(), "CAPABILITY_UNCERTIFIED", "baseline certification missing")
        if target == ClusterState.DISABLED:
            require(active_allocations == 0, "ALLOCATIONS_ACTIVE", "drain and confirm releases before disabling")
        return replace(self, state=target, revision=self.revision + 1)


class ClusterRegistry:
    """Small onboarding reference with idempotence and physical-cluster identity checks."""

    def __init__(self):
        self._clusters: dict[str, ClusterRegistration] = {}
        self._requests: dict[str, ClusterRegistration] = {}

    def register(self, registration: ClusterRegistration, idempotency_key: str) -> ClusterRegistration:
        require(bool(idempotency_key), "INVALID_ARGUMENT", "idempotency key required")
        require(registration.state == ClusterState.REGISTERED,
                "INVALID_TRANSITION", "new registrations must start registered")
        existing = self._requests.get(idempotency_key)
        if existing is not None:
            require(existing == registration, "IDEMPOTENCY_CONFLICT", "key reused for another registration")
            return existing
        for item in self._clusters.values():
            require(item.cluster_uid != registration.cluster_uid and item.context != registration.context,
                    "CLUSTER_IDENTITY_CONFLICT", "cluster already onboarded; add pools under its existing identity")
        require(registration.cluster_id not in self._clusters,
                "CLUSTER_IDENTITY_CONFLICT", "cluster ID already exists")
        self._clusters[registration.cluster_id] = registration
        self._requests[idempotency_key] = registration
        return registration


@dataclass(frozen=True)
class DispatchSpec:
    """Server-owned placement decision. Do not deserialize arbitrary user Sky YAML.

    device_count is the number of devices in ONE pod/node; multi-node gang workload
    translation is a separate capability. Fractional multi-device is rejected by
    this initial translator instead of silently multiplying whole-GPU consumption.
    """

    reservation_id: str
    allocation_id: str
    fencing_token: int
    environment: str
    pool_id: str
    gpu_model: str
    gpu_millicards: int
    gpu_memory_mib: int
    device_count: int
    queue: str
    image: str
    run: str
    cpu_count: int = 1
    memory_gib: int = 3
    production_authorized: bool = False
    trusted_tenant_id: str = "default"
    dr_epoch: int = 1
    fractional_isolation: FractionalIsolation = FractionalIsolation.KAI_ACCOUNTING

    def __post_init__(self):
        strict_bool(self.production_authorized, "production_authorized")
        require(isinstance(self.fractional_isolation, FractionalIsolation), "INVALID_ARGUMENT",
                "fractional_isolation must be an explicit supported enforcement mode")
        for name in ("reservation_id", "allocation_id", "pool_id", "gpu_model", "queue", "trusted_tenant_id"):
            label(getattr(self, name), name)
        bounded_int(self.fencing_token, 1, 2**63 - 1, "fencing token")
        bounded_int(self.dr_epoch, 1, 2**63 - 1, "DR epoch")
        bounded_int(self.gpu_millicards, 1, 1000, "GPU millicards")
        bounded_int(self.gpu_memory_mib, 1, 2**31 - 1, "GPU memory MiB")
        bounded_int(self.device_count, 1, 1024, "device count")
        bounded_int(self.cpu_count, 1, 65536, "CPU count")
        bounded_int(self.memory_gib, 3, 2**20, "runtime memory GiB")
        require(self.environment in {"dev", "qa", "prod"}, "INVALID_ARGUMENT", "invalid environment")
        require(isinstance(self.image, str) and 0 < len(self.image) <= 1024 and
                not any(c.isspace() for c in self.image), "INVALID_ARGUMENT", "invalid container image")
        require(isinstance(self.run, str) and 0 < len(self.run) <= 32768 and "\x00" not in self.run,
                "INVALID_ARGUMENT", "invalid run command")


@dataclass(frozen=True)
class LeaseAuthorization:
    """Transactional authorization result; adapters compare the entire owner key.

    A DB transaction must populate this only after checking current epoch, fence,
    reservation status, attempt budget, command ownership and relevant deadlines.
    The dispatcher compares it against the immutable queued DispatchSpec. A bare
    boolean cannot establish which post-DR ownership epoch was authorized.
    """

    reservation_id: str
    allocation_id: str
    dr_epoch: int
    fencing_token: int
    authorized: bool = True

    def __post_init__(self):
        label(self.reservation_id, "reservation_id")
        label(self.allocation_id, "allocation_id")
        bounded_int(self.dr_epoch, 1, 2**63 - 1, "DR epoch")
        bounded_int(self.fencing_token, 1, 2**63 - 1, "fencing token")
        strict_bool(self.authorized, "authorized")

    def matches(self, spec: DispatchSpec) -> bool:
        return (self.authorized and self.reservation_id == spec.reservation_id and
                self.allocation_id == spec.allocation_id and self.dr_epoch == spec.dr_epoch and
                self.fencing_token == spec.fencing_token)
