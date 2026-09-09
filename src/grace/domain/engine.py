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

from .errors import (CapacityUnavailable, IdempotencyConflict, InvalidTransition, NotFound,
                     PermissionDenied, ReleaseUnconfirmed, ValidationError, VersionConflict)
from .inventory import choose_gpus, validate_gpu
from .models import (Allocation, Caller, EffectivePolicy, Event, GPU, PolicyRule,
                     Request, Reservation, State, TERMINAL_STATES)
from .policies import (authorize_request, require_integer, require_text, require_timestamp,
                       validate_policy_rule)


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
        self._policy_rules: dict[tuple[str, str, str | None, str | None], PolicyRule] = {}
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
            # Only the controller can reauthorize older queued owners. A new
            # arrival cannot leapfrog them using the arriving caller's grants.
            waiting = any(item.state is State.QUEUED
                          and item.request.tenant_id == request.tenant_id
                          and item.request.environment == request.environment
                          for item in self._reservations.values())
            queue_reason = None
            try:
                if waiting:
                    raise CapacityUnavailable("earlier queue entries await authorized placement")
                devices = self._choose(request, caller, now)
            except CapacityUnavailable as exc:
                if not request.wait_for_capacity:
                    raise
                devices = ()
                queue_reason = exc.code
            reservation_id = self._id_factory()
            require_text(reservation_id, "generated reservation id")
            if reservation_id in self._reservations:
                raise ValidationError("id_factory produced an existing reservation id")
            reservation = Reservation(
                reservation_id, request, caller.subject, State.QUEUED, now, None,
                queue_expires_at=now + timedelta(seconds=request.queue_timeout_seconds),
                queue_reason=queue_reason, effective_policy=self._effective_policy(request, caller))
            if devices:
                reservation = self._admit(reservation, devices, caller, now)
            self._save(reservation)
            self._idempotency[key] = (fingerprint, reservation.id)
            return reservation

    def configure_policy(self, rule: PolicyRule, caller: Caller) -> PolicyRule:
        """Internal trusted admin interface; no end-user transport is wired.

        Rules replace the entire policy snapshot; project > business unit >
        tenant/environment > default. Existing grants retain their snapshot.
        Queued grants refresh it during each authorized controller queue scan.
        """
        if caller.admin_authorized is not True:
            raise PermissionDenied("trusted administrator authorization required")
        validate_policy_rule(rule)
        if rule.tenant_id != caller.tenant_id:
            raise PermissionDenied("administrator cannot configure another tenant")
        if rule.environment not in caller.allowed_environments:
            raise PermissionDenied("administrator cannot configure an unauthorized environment")
        key = (rule.tenant_id, rule.environment, rule.business_unit_id, rule.project_id)
        with self._lock:
            prior = self._policy_rules.get(key)
            if prior is not None and rule.effective_policy.version <= prior.effective_policy.version:
                raise VersionConflict("policy version must increase")
            self._policy_rules[key] = rule
        return rule

    def _effective_policy(self, request: Request, caller: Caller) -> EffectivePolicy:
        prefix = (request.tenant_id, request.environment)
        for suffix in ((caller.business_unit_id, request.project_id),
                       (caller.business_unit_id, None), (None, None)):
            rule = self._policy_rules.get(prefix + suffix)
            if rule is not None:
                return rule.effective_policy
        return EffectivePolicy()

    def _choose(self, request: Request, caller: Caller, now: datetime) -> tuple[GPU, ...]:
        ledger = tuple(allocation for reservation in self._reservations.values()
                       if reservation.state not in TERMINAL_STATES for allocation in reservation.allocations)
        return choose_gpus(request, caller, tuple(self._gpus.values()), ledger, now, self._max_age)

    def _admit(self, reservation: Reservation, devices: tuple[GPU, ...], caller: Caller,
               now: datetime) -> Reservation:
        request = reservation.request
        allocations = tuple(Allocation(f"{reservation.id}:{gpu.id}", gpu.id,
                                       request.gpu_millicards,
                                       (gpu.memory_mib * request.gpu_millicards + 999) // 1000)
                            for gpu in devices)
        return replace(reservation, state=State.RESERVED, allocations=allocations,
                       admitted_at=now, expires_at=now + timedelta(seconds=request.duration_seconds),
                       queue_expires_at=None, queue_reason=None, selected_location=devices[0].location,
                       effective_policy=self._effective_policy(request, caller))

    def process_queue(self, caller: Caller,
                      resolve_caller: Callable[[str, str], Caller | None]) -> tuple[Reservation, ...]:
        """Controller-driven, tenant-scoped queue admission for this simulator.

        The synchronous resolver reads CURRENT trusted identity configuration;
        it must not perform remote I/O while this simulator lock is held. None
        confirms revocation. Exceptions mean unavailable identity information:
        keep the reservation queued and do not allocate. Default priority zero
        gives insertion FIFO, including clock ties. Non-fitting or auth-blocked
        entries may backfill; this is not a future start-time guarantee.
        """
        self._require_controller(caller)
        if not callable(resolve_caller):
            raise ValidationError("resolve_caller must be callable")
        with self._lock:
            now = self._now()
            changed: list[Reservation] = []
            eligible: list[tuple[Reservation, Caller]] = []
            # dict insertion order is the process-local monotonic FIFO sequence.
            for reservation in tuple(self._reservations.values()):
                if (reservation.state is not State.QUEUED
                        or reservation.request.tenant_id != caller.tenant_id
                        or reservation.request.environment not in caller.allowed_environments):
                    continue
                if reservation.queue_expires_at is None or now >= reservation.queue_expires_at:
                    updated = replace(reservation, state=State.EXPIRED, release_reason="queue_expired",
                                      queue_reason="QUEUE_TIMEOUT", version=reservation.version + 1)
                    self._save(updated)
                    changed.append(updated)
                    continue
                try:
                    owner = resolve_caller(reservation.request.tenant_id, reservation.owner_subject)
                except Exception:
                    # An identity dependency outage is never evidence of revocation.
                    self._queue_reason(reservation, "AUTHORIZATION_UNAVAILABLE")
                    continue
                try:
                    if (owner is None or not isinstance(owner, Caller)
                            or owner.subject != reservation.owner_subject
                            or owner.tenant_id != reservation.request.tenant_id):
                        raise PermissionDenied("current reservation owner authorization is absent")
                    authorize_request(reservation.request, owner, now, self._production_enabled)
                except (PermissionDenied, ValidationError):
                    updated = replace(reservation, state=State.CANCELLED, release_reason="authorization_revoked",
                                      queue_reason="AUTHORIZATION_REVOKED", version=reservation.version + 1)
                    self._save(updated)
                    changed.append(updated)
                    continue
                policy = self._effective_policy(reservation.request, owner)
                if policy != reservation.effective_policy:
                    reservation = replace(reservation, effective_policy=policy,
                                          version=reservation.version + 1)
                    self._save(reservation)
                eligible.append((reservation, owner))
            # Stable sort preserves FIFO among equal priorities. No priority is
            # accepted from the requester; configured priorities do not preempt.
            eligible.sort(key=lambda item: -item[0].effective_policy.priority)
            for reservation, owner in eligible:
                try:
                    devices = self._choose(reservation.request, owner, now)
                except CapacityUnavailable as exc:
                    self._queue_reason(reservation, exc.code)
                    continue
                updated = self._admit(reservation, devices, owner, now)
                updated = replace(updated, version=reservation.version + 1)
                self._save(updated)
                changed.append(updated)
            return tuple(changed)

    def _queue_reason(self, reservation: Reservation, reason: str) -> None:
        if reservation.queue_reason != reason:
            self._save(replace(reservation, queue_reason=reason, version=reservation.version + 1))

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
            if reservation.state is State.QUEUED:
                updated = replace(reservation, state=State.CANCELLED, release_reason="cancelled",
                                  queue_reason=None, version=reservation.version + 1)
                self._save(updated)
                return updated
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
        if reason not in {"completed", "expired", "idle", "preempted"}:
            raise ValidationError("release reason must be completed, expired, idle, or preempted")
        with self._lock:
            reservation = self._visible(reservation_id, caller)
            if reservation.state in TERMINAL_STATES or reservation.state is State.RELEASING:
                return reservation
            if reservation.state is State.QUEUED:
                raise InvalidTransition("queued requests use cancellation or queue timeout, not resource release")
            if reason == "idle" and reservation.effective_policy.idle_reclamation_exempt:
                raise PermissionDenied("effective policy exempts this reservation from idle reclamation")
            if reason == "preempted" and (not reservation.effective_policy.preemption_enabled
                                          or reservation.effective_policy.preemption_exempt):
                raise PermissionDenied("effective policy protects this reservation from preemption")
            if reason == "expired" and (reservation.expires_at is None or self._now() < reservation.expires_at):
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
            if target in {State.DISPATCHING, State.RUNNING} and (
                reservation.expires_at is None or self._now() >= reservation.expires_at
            ):
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
