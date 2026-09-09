"""Immutable value objects. Integer units avoid rounding capacity into existence.

Caller is a trusted authentication-context value, NEVER a user request body.
gpu_millicards is a scheduling entitlement, not a CUDA compute guarantee.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class State(str, Enum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    UNKNOWN = "unknown"
    RELEASING = "releasing"
    RELEASED = "released"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset({State.RELEASED, State.CANCELLED, State.EXPIRED})


@dataclass(frozen=True)
class Caller:
    subject: str
    tenant_id: str
    allowed_environments: frozenset[str] = frozenset({"dev", "qa"})
    allowed_locations: frozenset[str] = frozenset()
    production_authorized: bool = False
    controller_authorized: bool = False


@dataclass(frozen=True)
class Request:
    tenant_id: str
    application_id: str
    gpu_type: str
    gpu_memory_mib: int
    environment: str = "dev"
    device_count: int = 1
    gpu_millicards: int = 1000
    location: str | None = None
    data_locations: frozenset[str] = frozenset()
    duration_seconds: int = 3600
    guarantee: str = "best_effort"
    start_at: datetime | None = None
    production_opt_in: bool = False


@dataclass(frozen=True)
class Usage:
    """One observed allocation on one physical GPU; identity must be stable."""

    allocation_id: str
    gpu_millicards: int
    memory_mib: int


@dataclass(frozen=True)
class GPU:
    id: str
    gpu_type: str
    memory_mib: int
    environment: str
    location: str
    observed_at: datetime
    healthy: bool = True
    observed_allocations: tuple[Usage, ...] = ()
    node_id: str = ""
    allowed_tenants: frozenset[str] = frozenset()
    cluster_id: str = ""


@dataclass(frozen=True)
class Allocation:
    id: str
    gpu_id: str
    gpu_millicards: int
    memory_mib: int


@dataclass(frozen=True)
class Reservation:
    id: str
    request: Request
    owner_subject: str
    state: State
    created_at: datetime
    expires_at: datetime
    allocations: tuple[Allocation, ...] = ()
    release_requested_at: datetime | None = None
    release_reason: str | None = None
    version: int = 1
    dispatch_uncertain: bool = False


@dataclass(frozen=True)
class Event:
    reservation_id: str
    state: State
    occurred_at: datetime
    version: int


@dataclass(frozen=True)
class Telemetry:
    observed_at: datetime
    gpu_utilization_percent: float | None = None
    active_job: bool | None = None
    cuda_process_present: bool | None = None
    interactive_heartbeat_present: bool | None = None
    data_loading: bool | None = None


class IdleDecision(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    SUSPECTED_IDLE = "suspected_idle"
