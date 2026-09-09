"""Physical-device fitting with observed/ledger identity union, not scalar minima."""

from datetime import datetime

from .errors import CapacityUnavailable, InventoryStale, ValidationError
from .models import Allocation, Caller, GPU, Request, Usage
from .policies import ENVIRONMENTS, require_integer, require_string_set, require_text, require_timestamp


def validate_gpu(gpu: GPU) -> None:
    for value, name in ((gpu.id, "GPU id"), (gpu.gpu_type, "GPU type"), (gpu.location, "GPU location")):
        require_text(value, name)
    require_integer(gpu.memory_mib, "GPU memory_mib", 1, 2**31 - 1)
    require_timestamp(gpu.observed_at, "GPU observed_at")
    require_string_set(gpu.allowed_tenants, "allowed_tenants")
    require_text(gpu.environment, "GPU environment")
    if gpu.cluster_id:
        require_text(gpu.cluster_id, "cluster_id")
    if gpu.environment not in ENVIRONMENTS:
        raise ValidationError("GPU environment must be dev, qa, or prod")
    if type(gpu.healthy) is not bool:
        raise ValidationError("GPU healthy must be a boolean")
    if not isinstance(gpu.observed_allocations, tuple):
        raise ValidationError("observed_allocations must be a tuple")
    seen = set()
    for usage in gpu.observed_allocations:
        if not isinstance(usage, Usage):
            raise ValidationError("observed_allocations must contain Usage values")
        require_text(usage.allocation_id, "allocation id")
        require_integer(usage.gpu_millicards, "observed gpu_millicards", 1, 1000)
        require_integer(usage.memory_mib, "observed memory_mib", 1, 2**31 - 1)
        if usage.allocation_id in seen:
            raise ValidationError("duplicate observed allocation identity on one GPU")
        seen.add(usage.allocation_id)


def remaining_capacity(gpu: GPU, ledger_allocations: tuple[Allocation, ...]) -> tuple[int, int]:
    """Union by allocation identity, using each resource's max if reports differ.

    A ledger-only hold AND a distinct observed workload both consume capacity.
    A mirrored observation of the same allocation must not count twice.
    """
    claims = {u.allocation_id: (u.gpu_millicards, u.memory_mib) for u in gpu.observed_allocations}
    for allocation in ledger_allocations:
        if allocation.gpu_id != gpu.id:
            continue
        previous = claims.get(allocation.id, (0, 0))
        claims[allocation.id] = (max(previous[0], allocation.gpu_millicards),
                                 max(previous[1], allocation.memory_mib))
    return (max(0, 1000 - sum(value[0] for value in claims.values())),
            max(0, gpu.memory_mib - sum(value[1] for value in claims.values())))


def choose_gpus(request: Request, caller: Caller, gpus: tuple[GPU, ...],
                ledger_allocations: tuple[Allocation, ...], now: datetime,
                max_age_seconds: int) -> tuple[GPU, ...]:
    candidates: list[tuple[int, int, str, GPU]] = []
    saw_stale = False
    for gpu in gpus:
        if gpu.gpu_type != request.gpu_type or gpu.environment != request.environment:
            continue
        if request.location is not None and gpu.location != request.location:
            continue
        if gpu.location not in request.data_locations:
            continue
        if gpu.location not in caller.allowed_locations:
            continue
        if gpu.allowed_tenants and request.tenant_id not in gpu.allowed_tenants:
            continue
        age = (now - gpu.observed_at).total_seconds()
        if age > max_age_seconds or age < -5:
            saw_stale = True
            continue
        if not gpu.healthy:
            continue
        # Fractional KAI sharing budgets device memory, not guaranteed SM time.
        # The caller's memory value is a minimum. Charge the entire fraction's
        # memory entitlement even when a tiny memory minimum was requested.
        effective_memory = (gpu.memory_mib * request.gpu_millicards + 999) // 1000
        if request.gpu_memory_mib > gpu.memory_mib * request.gpu_millicards // 1000:
            continue
        remaining_share, remaining_memory = remaining_capacity(gpu, ledger_allocations)
        if remaining_share >= request.gpu_millicards and remaining_memory >= effective_memory:
            candidates.append((remaining_share - request.gpu_millicards,
                               remaining_memory - effective_memory, gpu.id, gpu))
    # A multi-device job is admitted wholly within ONE execution location.
    # Regional spread is not a substitute for distributed-training topology.
    by_location: dict[tuple[str, str], list[tuple[int, int, str, GPU]]] = {}
    for candidate in candidates:
        gpu = candidate[3]
        # Empty cluster_id is a local-simulator compatibility default: one
        # execution cluster per location. Production inventory must populate it.
        by_location.setdefault((gpu.location, gpu.cluster_id or gpu.location), []).append(candidate)
    fitting = [sorted(group, key=lambda item: item[:3])[:request.device_count]
               for group in by_location.values() if len(group) >= request.device_count]
    if not fitting:
        if saw_stale:
            raise InventoryStale("fresh compatible capacity is insufficient; stale inventory cannot be promised")
        raise CapacityUnavailable("no single data-ready location fits all requested physical GPU slices")
    selected = min(fitting, key=lambda group: (sum(item[0] for item in group),
                                              sum(item[1] for item in group),
                                              tuple(item[2] for item in group)))
    return tuple(item[3] for item in selected)
