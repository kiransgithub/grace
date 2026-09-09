"""Stable domain errors; transport adapters map codes without leaking internals."""


class DomainError(Exception):
    code = "DOMAIN_ERROR"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ValidationError(DomainError):
    code = "INVALID_ARGUMENT"


class PermissionDenied(DomainError):
    code = "PERMISSION_DENIED"


class UnsupportedGuarantee(DomainError):
    code = "UNSUPPORTED_GUARANTEE"


class CapacityUnavailable(DomainError):
    code = "CAPACITY_UNAVAILABLE"


class InventoryStale(CapacityUnavailable):
    code = "INVENTORY_STALE"


class IdempotencyConflict(DomainError):
    code = "IDEMPOTENCY_CONFLICT"


class NotFound(DomainError):
    code = "NOT_FOUND"


class InvalidTransition(DomainError):
    code = "INVALID_STATE_TRANSITION"


class ReleaseUnconfirmed(DomainError):
    code = "RELEASE_UNCONFIRMED"


class VersionConflict(DomainError):
    code = "ABORTED"
