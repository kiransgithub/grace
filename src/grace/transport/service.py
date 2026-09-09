"""Shared application facade for real REST/gRPC simulation transports.

Fixed bearer authentication is deliberately demo-only, not an Okta implementation.
Unimplemented enterprise operations fail explicitly, rather than return success.
"""
import base64
import hashlib
import hmac
import json
from dataclasses import asdict, fields
from datetime import datetime
from enum import Enum
from threading import RLock

from grace.domain import Caller, Engine, Request
from grace.domain.errors import DomainError, ValidationError


class Unauthenticated(DomainError):
    code = "UNAUTHENTICATED"


class Unimplemented(DomainError):
    code = "UNIMPLEMENTED"


class Conflict(DomainError):
    code = "ABORTED"


ERROR_HTTP = {
    "INVALID_ARGUMENT": 400, "UNAUTHENTICATED": 401, "PERMISSION_DENIED": 403,
    "NOT_FOUND": 404, "IDEMPOTENCY_CONFLICT": 409, "ABORTED": 409,
    "UNSUPPORTED_GUARANTEE": 412, "INVENTORY_STALE": 412,
    "INVALID_STATE_TRANSITION": 412, "RELEASE_UNCONFIRMED": 412,
    "CAPACITY_UNAVAILABLE": 429, "UNIMPLEMENTED": 501,
}


def plain(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def reservation_dict(reservation):
    result = plain(asdict(reservation))
    result["etag"] = str(reservation.version)
    result["simulation"] = True
    return result


def parse_request(body: dict) -> Request:
    if type(body) is not dict:
        raise ValidationError("reservation request must be an object")
    allowed = {field.name for field in fields(Request)}
    unknown = set(body) - allowed
    if unknown:
        raise ValidationError("unknown request fields: " + ", ".join(sorted(unknown)))
    values = dict(body)
    data = values.get("data_locations", [])
    if type(data) is not list or not all(type(item) is str for item in data):
        raise ValidationError("data_locations must be a list of location strings")
    values["data_locations"] = frozenset(data)
    if values.get("start_at") is not None:
        try:
            values["start_at"] = datetime.fromisoformat(values["start_at"])
        except (ValueError, TypeError):
            raise ValidationError("start_at must be an RFC3339 timestamp") from None
    try:
        return Request(**values)
    except TypeError:
        raise ValidationError("required reservation fields are missing") from None


class Service:
    def __init__(self, engine: Engine, *, token: str, caller: Caller):
        if len(token) < 32:
            raise ValueError("GRACE_DEMO_TOKEN must have at least 32 characters")
        self.engine = engine
        self._token = token
        self._caller = caller
        self._cancel_lock = RLock()
        self._cancel_replays = {}

    def authenticate(self, authorization: str) -> Caller:
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + self._token).encode()):
            raise Unauthenticated("valid demo bearer credential required")
        return self._caller

    def create(self, body: dict, caller: Caller, key: str):
        return self.engine.create(parse_request(body), caller, key)

    def get(self, reservation_id: str, caller: Caller):
        return self.engine.get(reservation_id, caller)

    def cancel(self, reservation_id: str, caller: Caller, key: str, etag: str):
        if not key or len(key) > 128:
            raise ValidationError("cancel requires idempotency key of 1..128 characters")
        if (type(etag) is not str or not 1 <= len(etag) <= 19
                or not etag.isascii() or not etag.isdecimal()
                or not 1 <= int(etag) <= 2**63 - 1):
            raise ValidationError("cancel requires the current numeric etag")
        scope = (caller.tenant_id, caller.subject, key)
        fingerprint = (reservation_id, etag)
        with self._cancel_lock:
            prior = self._cancel_replays.get(scope)
            if prior:
                if prior[0] != fingerprint:
                    from grace.domain.errors import IdempotencyConflict
                    raise IdempotencyConflict("cancel key used for different request")
                return prior[1]
            result = self.engine.cancel(reservation_id, caller, expected_version=int(etag))
            self._cancel_replays[scope] = (fingerprint, result)
            return result

    def list(self, caller: Caller, *, page_size: int = 50, page_token: str = ""):
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValidationError("page_size must be between 1 and 200")
        cursor = None
        if page_token:
            try:
                raw, signature = page_token.split(".", 1)
                expected = hmac.new(self._token.encode(), raw.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected):
                    raise ValueError()
                payload = json.loads(base64.urlsafe_b64decode(raw))
                if payload["tenant"] != caller.tenant_id or payload["subject"] != caller.subject:
                    raise ValueError()
                cursor = tuple(payload["cursor"])
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValidationError("invalid or foreign page token") from None
        sort_key = lambda item: (item.created_at.isoformat(), item.id)
        records = sorted(self.engine.list(caller), key=sort_key)
        if cursor:
            records = [item for item in records if sort_key(item) > cursor]
        selected = records[:page_size]
        token = ""
        if len(records) > page_size:
            payload = {"tenant": caller.tenant_id, "subject": caller.subject,
                       "cursor": sort_key(selected[-1])}
            raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
            token = raw + "." + hmac.new(self._token.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return selected, token

    @staticmethod
    def unavailable_feature():
        raise Unimplemented("requires durable service and certified execution; not enabled in v0.1")
