"""Lock-backed LOCAL SIMULATION repository. Not a production allocation service.

State and idempotency vanish on restart; multiple processes do NOT share locks.
The production implementation must use PostgreSQL transactions/constraints,
an outbox, fenced workers, and verified infrastructure reconciliation.
"""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Callable
from uuid import uuid4

from .errors import (IdempotencyConflict, InvalidTransition, NotFound,
                     PermissionDenied, ReleaseUnconfirmed, ValidationError, VersionConflict)
from .inventory import choose_gpus, validate_gpu
from .models import Allocation, Caller, Event, GPU, Request, Reservation, State, TERMINAL_STATES
from .policies import authorize_request, require_integer, require_text, require_timestamp


def request_fingerprint(request: Request) -> str:
    values = asdict(request)
    values["data_locations"] = sorted(request.data_locations)
    if request.start_at is not None:
        values["start_at"] = request.start_at.astimezone(timezone.utc).isoformat()
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Engine:
    """Thread-safe for one simulator process only. Do not use in production."""

    def __init__(self, gpus: tuple[GPU, ...] = (), *, clock: Callable[[], datetime] | None = None,
                 max_inventory_age_seconds: int = 60, production_enabled: bool = False,
                 id_factory: Callable[[], str] | None = None) -> None:
        require_integer(max_inventory_age_seconds, "max_inventory_age_seconds", 1, 86400)
        if type(production_enabled) is not bool:
            raise ValidationError("production_enabled must be a boolean")
        self._lock = RLock()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._max_age = max_inventory_age_seconds
        self._production_enabled = production_enabled
        self._gpus: dict[str, GPU] = {}
        self._reservations: dict[str, Reservation] = {}
        self._events: dict[str, tuple[Event, ...]] = {}
        self._idempotency: dict[tuple[str, str, str], tuple[str, str]] = {}
        for gpu in gpus:
            if gpu.id in self._gpus:
                raise ValidationError("duplicate GPU identity")
            self.upsert_gpu(gpu)

    def _now(self) -> datetime:
        now = self._clock()
        require_timestamp(now, "clock result")
        return now

    @staticmethod
    def _require_controller(caller: Caller) -> None:
        if caller.controller_authorized is not True:
            raise PermissionDenied("trusted controller authorization required")

    def upsert_gpu(self, gpu: GPU) -> None:
        """Internal reconciler input; never expose to untrusted callers."""
        validate_gpu(gpu)
        with self._lock:
            prior = self._gpus.get(gpu.id)
            if prior and gpu.observed_at < prior.observed_at:
                raise ValidationError("out-of-order inventory observation")
            if prior and (gpu.gpu_type, gpu.environment, gpu.location, gpu.cluster_id) != (
                prior.gpu_type, prior.environment, prior.location, prior.cluster_id
            ):
                raise ValidationError("GPU identity cannot move between types, environments, or locations")
            self._gpus[gpu.id] = gpu

    def create(self, request: Request, caller: Caller, idempotency_key: str) -> Reservation:
        require_text(idempotency_key, "idempotency_key", 128)
        with self._lock:
            now = self._now()
            authorize_request(request, caller, now, self._production_enabled)
            fingerprint = request_fingerprint(request)
            key = (caller.tenant_id, caller.subject, idempotency_key)
            prior = self._idempotency.get(key)
            if prior is not None:
                if prior[0] != fingerprint:
                    raise IdempotencyConflict("idempotency key already used for a different request")
                return self._reservations[prior[1]]
            ledger = tuple(allocation for reservation in self._reservations.values()
                           if reservation.state not in TERMINAL_STATES for allocation in reservation.allocations)
            devices = choose_gpus(request, caller, tuple(self._gpus.values()), ledger, now, self._max_age)
            reservation_id = self._id_factory()
            require_text(reservation_id, "generated reservation id")
            if reservation_id in self._reservations:
                raise ValidationError("id_factory produced an existing reservation id")
            allocations = tuple(Allocation(f"{reservation_id}:{gpu.id}", gpu.id,
                                           request.gpu_millicards,
                                           (gpu.memory_mib * request.gpu_millicards + 999) // 1000)
                                for gpu in devices)
            reservation = Reservation(reservation_id, request, caller.subject, State.RESERVED,
                                      now, now + timedelta(seconds=request.duration_seconds), allocations)
            self._save(reservation)
            self._idempotency[key] = (fingerprint, reservation.id)
            return reservation

    def _save(self, reservation: Reservation) -> None:
        self._reservations[reservation.id] = reservation
        event = Event(reservation.id, reservation.state, self._now(), reservation.version)
        self._events[reservation.id] = self._events.get(reservation.id, ()) + (event,)

    def _visible(self, reservation_id: str, caller: Caller) -> Reservation:
        reservation = self._reservations.get(reservation_id)
        if reservation is None or reservation.request.tenant_id != caller.tenant_id:
            raise NotFound("reservation not found")
        if reservation.owner_subject != caller.subject and caller.controller_authorized is not True:
            raise NotFound("reservation not found")
        return reservation

    def get(self, reservation_id: str, caller: Caller) -> Reservation:
        with self._lock:
            return self._visible(reservation_id, caller)

    def list(self, caller: Caller) -> tuple[Reservation, ...]:
        with self._lock:
            return tuple(item for item in self._reservations.values()
                         if item.request.tenant_id == caller.tenant_id and (
                             item.owner_subject == caller.subject or caller.controller_authorized is True))

    def events(self, reservation_id: str, caller: Caller) -> tuple[Event, ...]:
        with self._lock:
            self._visible(reservation_id, caller)
            return self._events[reservation_id]

    def cancel(self, reservation_id: str, caller: Caller, *, expected_version: int | None = None) -> Reservation:
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if expected_version is not None:
                require_integer(expected_version, "expected_version", 1, 2**63 - 1)
                if expected_version != reservation.version:
                    raise VersionConflict("reservation version changed; reload before mutating")
            if reservation.state in TERMINAL_STATES or reservation.state is State.RELEASING:
                return reservation
            updated = replace(reservation, state=State.RELEASING, release_requested_at=self._now(),
                              release_reason="cancelled", version=reservation.version + 1)
            self._save(updated)
            return updated

    def confirm_dispatch_settled(self, reservation_id: str, caller: Caller) -> Reservation:
        """Internal controller attestation that no delayed dispatch can create work.

        Production must first fence the attempt and verify SkyPilot operation/job
        handles. Never expose this control-plane method to end-user transports.
        """
        self._require_controller(caller)
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if reservation.state is not State.RELEASING:
                raise InvalidTransition("settle dispatch only while release is in progress")
            if not reservation.dispatch_uncertain:
                return reservation
            updated = replace(reservation, dispatch_uncertain=False, version=reservation.version + 1)
            self._save(updated)
            return updated

    def request_release(self, reservation_id: str, caller: Caller, *, reason: str = "completed") -> Reservation:
        self._require_controller(caller)
        if reason not in {"completed", "expired", "idle"}:
            raise ValidationError("release reason must be completed, expired, or idle")
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if reservation.state in TERMINAL_STATES or reservation.state is State.RELEASING:
                return reservation
            if reason == "expired" and self._now() < reservation.expires_at:
                raise InvalidTransition("reservation has not expired")
            updated = replace(reservation, state=State.RELEASING, release_requested_at=self._now(),
                              release_reason=reason, version=reservation.version + 1)
            self._save(updated)
            return updated

    def confirm_released(self, reservation_id: str, caller: Caller) -> Reservation:
        """Confirm from trusted complete inventory snapshots after stop request.

        Production additionally needs dispatch fencing and all pod/VM/job handles
        absent or terminal. Device snapshots alone do not fence delayed dispatch.
        The simulation disallows a subsequent dispatch transition after cancellation.
        """
        self._require_controller(caller)
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if reservation.state in TERMINAL_STATES:
                return reservation
            if reservation.state is not State.RELEASING:
                raise InvalidTransition("release must be requested before confirmation")
            if reservation.dispatch_uncertain:
                raise ReleaseUnconfirmed("dispatch outcome must be fenced and settled before releasing capacity")
            now = self._now()
            for allocation in reservation.allocations:
                gpu = self._gpus.get(allocation.gpu_id)
                if gpu is None or reservation.release_requested_at is None:
                    raise ReleaseUnconfirmed("missing release observation")
                age = (now - gpu.observed_at).total_seconds()
                if age < -5 or age > self._max_age or gpu.observed_at <= reservation.release_requested_at:
                    raise ReleaseUnconfirmed("fresh post-stop infrastructure observation required")
                if any(item.allocation_id == allocation.id for item in gpu.observed_allocations):
                    raise ReleaseUnconfirmed("allocation is still observed on infrastructure")
            state = {"cancelled": State.CANCELLED, "expired": State.EXPIRED}.get(
                reservation.release_reason, State.RELEASED)
            updated = replace(reservation, state=state, version=reservation.version + 1)
            self._save(updated)
            return updated

    def _transition(self, reservation_id: str, caller: Caller, target: State,
                    allowed: frozenset[State]) -> Reservation:
        self._require_controller(caller)
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if reservation.state is target:
                return reservation
            if reservation.state not in allowed:
                raise InvalidTransition(f"cannot transition {reservation.state.value} to {target.value}")
            if target in {State.DISPATCHING, State.RUNNING} and self._now() >= reservation.expires_at:
                raise InvalidTransition("expired reservation cannot launch or become running")
            updated = replace(reservation, state=target, version=reservation.version + 1,
                              dispatch_uncertain=target in {State.DISPATCHING, State.UNKNOWN})
            self._save(updated)
            return updated

    def mark_dispatching(self, reservation_id: str, caller: Caller) -> Reservation:
        return self._transition(reservation_id, caller, State.DISPATCHING, frozenset({State.RESERVED}))

    def mark_running(self, reservation_id: str, caller: Caller) -> Reservation:
        return self._transition(reservation_id, caller, State.RUNNING,
                                frozenset({State.DISPATCHING, State.UNKNOWN}))

    def mark_unknown(self, reservation_id: str, caller: Caller) -> Reservation:
        return self._transition(reservation_id, caller, State.UNKNOWN,
                                frozenset({State.RESERVED, State.DISPATCHING, State.RUNNING}))
