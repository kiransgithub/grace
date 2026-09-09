"""Governed execution adapters; importing this package never provisions resources."""

from .contracts import (
    AdapterError,
    CapabilityEvidence,
    ClusterRegistration,
    ClusterRegistry,
    ClusterState,
    DispatchSpec,
    FractionalIsolation,
    GpuModel,
    HamiRuntimeEvidence,
    LeaseAuthorization,
)
from .skypilot_kai import (
    AbsenceEvidence,
    AdapterResult,
    Outcome,
    SkyPilotAdapter,
    build_task,
    confirm_release,
    deterministic_cluster_name,
    requires_gpu_admission,
)

__all__ = [
    "AdapterError", "CapabilityEvidence", "ClusterRegistration", "ClusterRegistry",
    "ClusterState", "DispatchSpec", "FractionalIsolation", "GpuModel", "HamiRuntimeEvidence", "LeaseAuthorization", "AbsenceEvidence", "AdapterResult",
    "Outcome", "SkyPilotAdapter", "build_task", "confirm_release",
    "deterministic_cluster_name", "requires_gpu_admission",
]
