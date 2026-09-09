"""Fail-closed policy checks independent of persistence and SkyPilot."""

import math
from datetime import datetime

from .errors import PermissionDenied, UnsupportedGuarantee, ValidationError
from .models import Caller, IdleDecision, Request, Telemetry

ENVIRONMENTS = frozenset({"dev", "qa", "prod"})


def require_integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer in [{minimum}, {maximum}]")


def require_text(value: object, name: str, maximum: int = 256) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationError(f"{name} must be a nonempty string of at most {maximum} characters")


def require_timestamp(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{name} must be a timezone-aware datetime")


def require_string_set(value: object, name: str) -> None:
    if not isinstance(value, frozenset):
        raise ValidationError(f"{name} must be a frozenset of strings")
    for item in value:
        require_text(item, name)


def authorize_request(request: Request, caller: Caller, now: datetime, production_enabled: bool) -> None:
    require_timestamp(now, "now")
    for value, name in ((caller.subject, "subject"), (caller.tenant_id, "caller tenant"),
                        (request.tenant_id, "tenant_id"), (request.application_id, "application_id"),
                        (request.gpu_type, "gpu_type")):
        require_text(value, name)
    require_string_set(caller.allowed_environments, "allowed_environments")
    require_string_set(caller.allowed_locations, "allowed_locations")
    require_string_set(request.data_locations, "data_locations")
    for value in (caller.production_authorized, caller.controller_authorized,
                  request.production_opt_in, production_enabled):
        if type(value) is not bool:
            raise ValidationError("authorization and feature flags must be booleans")
    if request.tenant_id != caller.tenant_id:
        raise PermissionDenied("request tenant differs from authenticated tenant")
    require_text(request.environment, "environment")
    if request.environment not in ENVIRONMENTS:
        raise ValidationError("environment must be dev, qa, or prod")
    if request.environment not in caller.allowed_environments:
        raise PermissionDenied("environment is not authorized")
    if not caller.allowed_locations:
        raise PermissionDenied("an explicit nonempty location allowlist is required")
    if request.environment == "prod" and not (
        production_enabled and caller.production_authorized and request.production_opt_in
    ):
        raise PermissionDenied("production needs the platform switch, token authorization, and request opt-in")
    require_integer(request.device_count, "device_count", 1, 4096)
    require_integer(request.gpu_millicards, "gpu_millicards", 1, 1000)
    require_integer(request.gpu_memory_mib, "gpu_memory_mib", 1, 2**31 - 1)
    require_integer(request.duration_seconds, "duration_seconds", 1, 604800)
    if request.guarantee != "best_effort":
        raise UnsupportedGuarantee("the simulation supports immediate best_effort admission only")
    if request.start_at is not None:
        require_timestamp(request.start_at, "start_at")
        if request.start_at > now:
            raise UnsupportedGuarantee("future capacity reservations require the durable capacity-calendar implementation")
    if request.location is not None:
        require_text(request.location, "location")
        if request.location not in caller.allowed_locations:
            raise PermissionDenied("location is not authorized")
        if request.location not in request.data_locations:
            raise ValidationError("data availability must be confirmed in the requested location")
    if not request.data_locations:
        raise ValidationError("at least one data-available location must be attested")


def idle_decision(telemetry: Telemetry | None, now: datetime, *, max_age_seconds: int = 60,
                  threshold_percent: float = 10) -> IdleDecision:
    """A single sample can suggest idleness, NEVER authorize reclamation.

    The caller must implement sustained windows, warning/grace, exemptions, and
    verified termination. Missing/stale telemetry is UNKNOWN, not zero usage.
    """
    require_timestamp(now, "now")
    require_integer(max_age_seconds, "max_age_seconds", 1, 86400)
    if isinstance(threshold_percent, bool) or not isinstance(threshold_percent, (int, float)):
        raise ValidationError("threshold_percent must be numeric")
    if not math.isfinite(threshold_percent) or not 0 <= threshold_percent <= 100:
        raise ValidationError("threshold_percent must be finite and within [0,100]")
    if telemetry is None:
        return IdleDecision.UNKNOWN
    require_timestamp(telemetry.observed_at, "telemetry observed_at")
    age = (now - telemetry.observed_at).total_seconds()
    if age < -5 or age > max_age_seconds:
        return IdleDecision.UNKNOWN
    signals = (telemetry.active_job, telemetry.cuda_process_present,
               telemetry.interactive_heartbeat_present, telemetry.data_loading)
    if any(value is True for value in signals):
        return IdleDecision.ACTIVE
    if any(type(value) is not bool for value in signals):
        return IdleDecision.UNKNOWN
    utilization = telemetry.gpu_utilization_percent
    if isinstance(utilization, bool) or not isinstance(utilization, (int, float)):
        return IdleDecision.UNKNOWN
    if not math.isfinite(utilization) or not 0 <= utilization <= 100:
        return IdleDecision.UNKNOWN
    return (IdleDecision.SUSPECTED_IDLE if utilization < threshold_percent else IdleDecision.ACTIVE)
