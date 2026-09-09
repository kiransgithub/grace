"""Versioned gRPC adapter; same application/domain path as REST."""
import logging
from datetime import timezone
from uuid import uuid4
import grpc
from grace.domain.errors import DomainError, ValidationError
from grace.v1 import grace_pb2 as pb, grace_pb2_grpc as rpc

LOG = logging.getLogger(__name__)
ENVIRONMENT = {pb.ENVIRONMENT_DEV: "dev", pb.ENVIRONMENT_QA: "qa", pb.ENVIRONMENT_PROD: "prod"}
LOCATION_POLICY = {pb.LOCATION_POLICY_UNSPECIFIED: "strict", pb.LOCATION_POLICY_STRICT: "strict",
                   pb.LOCATION_POLICY_PREFERRED: "preferred", pb.LOCATION_POLICY_ANY: "any"}
ERROR_GRPC = {
    "INVALID_ARGUMENT": grpc.StatusCode.INVALID_ARGUMENT,
    "UNAUTHENTICATED": grpc.StatusCode.UNAUTHENTICATED,
    "PERMISSION_DENIED": grpc.StatusCode.PERMISSION_DENIED,
    "NOT_FOUND": grpc.StatusCode.NOT_FOUND,
    "IDEMPOTENCY_CONFLICT": grpc.StatusCode.ALREADY_EXISTS,
    "ABORTED": grpc.StatusCode.ABORTED,
    "UNSUPPORTED_GUARANTEE": grpc.StatusCode.FAILED_PRECONDITION,
    "INVENTORY_STALE": grpc.StatusCode.FAILED_PRECONDITION,
    "INVALID_STATE_TRANSITION": grpc.StatusCode.FAILED_PRECONDITION,
    "RELEASE_UNCONFIRMED": grpc.StatusCode.FAILED_PRECONDITION,
    "CAPACITY_UNAVAILABLE": grpc.StatusCode.RESOURCE_EXHAUSTED,
    "UNIMPLEMENTED": grpc.StatusCode.UNIMPLEMENTED,
}


def spec_dict(spec):
    if spec.environment not in ENVIRONMENT:
        raise ValidationError("explicit environment required")
    if spec.guarantee not in (pb.GUARANTEE_UNSPECIFIED, pb.GUARANTEE_BEST_EFFORT, pb.GUARANTEE_POOL_BACKED):
        raise ValidationError("unknown guarantee enum")
    if spec.placement.location_policy not in LOCATION_POLICY:
        raise ValidationError("unknown location policy enum")
    old_location = spec.placement.required_location if spec.placement.HasField("required_location") else None
    location = spec.placement.location if spec.placement.HasField("location") else old_location
    if old_location is not None and location != old_location:
        raise ValidationError("location and legacy required_location disagree")
    result = {
        "tenant_id": spec.tenant_id, "application_id": spec.application_id,
        "environment": ENVIRONMENT[spec.environment], "gpu_type": spec.resources.gpu_type,
        "device_count": spec.resources.device_count, "gpu_millicards": spec.resources.gpu_millicards,
        "gpu_memory_mib": spec.resources.gpu_memory_mib,
        "duration_seconds": spec.duration_seconds,
        "location": location,
        "location_policy": LOCATION_POLICY[spec.placement.location_policy],
        "data_locations": list(spec.placement.data_locations),
        "production_opt_in": spec.placement.production_opt_in,
        "guarantee": "pool_backed" if spec.guarantee == pb.GUARANTEE_POOL_BACKED else "best_effort",
        "wait_for_capacity": spec.wait_for_capacity if spec.HasField("wait_for_capacity") else True,
        "queue_timeout_seconds": spec.queue_timeout_seconds if spec.HasField("queue_timeout_seconds") else 3600,
        "project_id": spec.project_id if spec.HasField("project_id") else None,
    }
    if spec.HasField("start_at"):
        try:
            result["start_at"] = spec.start_at.ToDatetime(tzinfo=timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise ValidationError("start_at timestamp is out of range") from None
    return result


def as_proto(item):
    request = item.request
    spec = pb.ReservationSpec(tenant_id=request.tenant_id, application_id=request.application_id,
        environment={v: k for k, v in ENVIRONMENT.items()}[request.environment],
        resources=pb.ResourceShape(gpu_type=request.gpu_type, device_count=request.device_count,
            gpu_millicards=request.gpu_millicards, gpu_memory_mib=request.gpu_memory_mib),
        placement=pb.PlacementPolicy(data_locations=sorted(request.data_locations),
                                    production_opt_in=request.production_opt_in,
                                    location_policy=pb.LocationPolicy.Value("LOCATION_POLICY_" +
                                        request.location_policy.upper())),
        duration_seconds=request.duration_seconds, guarantee=pb.GUARANTEE_BEST_EFFORT,
        wait_for_capacity=request.wait_for_capacity, queue_timeout_seconds=request.queue_timeout_seconds)
    if request.location is not None:
        spec.placement.location = request.location
        if request.location_policy == "strict":
            spec.placement.required_location = request.location
    if request.project_id is not None:
        spec.project_id = request.project_id
    if request.start_at is not None:
        spec.start_at.FromDatetime(request.start_at)
    result = pb.Reservation(id=item.id, spec=spec,
        state=pb.ReservationState.Value("RESERVATION_STATE_" + item.state.value.upper()),
        etag=str(item.version), simulation=True,
        effective_location_policy=pb.LocationPolicy.Value("LOCATION_POLICY_" +
            (request.location_policy if request.location else "any").upper()))
    result.created_at.FromDatetime(item.created_at)
    if item.expires_at is not None:
        result.expires_at.FromDatetime(item.expires_at)
    if item.queue_expires_at is not None:
        result.queue_expires_at.FromDatetime(item.queue_expires_at)
    if item.admitted_at is not None:
        result.admitted_at.FromDatetime(item.admitted_at)
    policy = item.effective_policy
    result.effective_policy.CopyFrom(pb.EffectivePolicy(policy_id=policy.policy_id,
        version=policy.version, priority=policy.priority, preemption_enabled=policy.preemption_enabled,
        preemption_exempt=policy.preemption_exempt, idle_reclamation_exempt=policy.idle_reclamation_exempt))
    if item.selected_location is not None:
        result.selected_location = item.selected_location
    if item.queue_reason is not None:
        result.queue_reason = item.queue_reason
    for allocation in item.allocations:
        result.allocations.add(allocation_id=allocation.id, gpu_id=allocation.gpu_id,
            gpu_millicards=allocation.gpu_millicards, memory_mib=allocation.memory_mib,
            fencing_token=0)  # No fictitious distributed fence in memory simulator.
    return result


class Reservations(rpc.ReservationServiceServicer):
    def __init__(self, service):
        self.service = service

    async def _call(self, context, action):
        correlation = str(uuid4())
        context.set_trailing_metadata((("x-correlation-id", correlation),))
        try:
            metadata = context.invocation_metadata()
            headers = [item.value for item in metadata if item.key == "authorization"]
            caller = self.service.authenticate(headers[0] if len(headers) == 1 else "")
            return action(caller)
        except DomainError as exc:
            await context.abort(ERROR_GRPC.get(exc.code, grpc.StatusCode.INTERNAL), f"{exc.code}: {exc}")
        except Exception:
            LOG.exception("gRPC request failed correlation=%s", correlation)
            await context.abort(grpc.StatusCode.INTERNAL, "INTERNAL: internal error")

    async def CreateReservation(self, request, context):
        return await self._call(context, lambda caller: as_proto(
            self.service.create(spec_dict(request.spec), caller, request.idempotency_key)))

    async def GetReservation(self, request, context):
        return await self._call(context, lambda caller: as_proto(self.service.get(request.id, caller)))

    async def ListReservations(self, request, context):
        def action(caller):
            items, token = self.service.list(caller, page_size=request.page_size or 50,
                                             page_token=request.page_token)
            return pb.ListReservationsResponse(reservations=[as_proto(item) for item in items],
                                               next_page_token=token)
        return await self._call(context, action)

    async def CancelReservation(self, request, context):
        return await self._call(context, lambda caller: as_proto(self.service.cancel(
            request.id, caller, request.idempotency_key, request.etag)))

    async def RenewReservation(self, request, context):
        return await self._call(context, lambda caller: self.service.unavailable_feature())

    async def ActivateReservation(self, request, context):
        return await self._call(context, lambda caller: self.service.unavailable_feature())

    async def WatchReservation(self, request, context):
        await self._call(context, lambda caller: self.service.unavailable_feature())
        if False:
            yield pb.ReservationEvent()


def build_server(service):
    server = grpc.aio.server(options=[("grpc.max_receive_message_length", 64 * 1024),
                                      ("grpc.max_send_message_length", 1024 * 1024)],
                             maximum_concurrent_rpcs=200)
    rpc.add_ReservationServiceServicer_to_server(Reservations(service), server)
    # Contract-only enterprise services return UNIMPLEMENTED until registered.
    return server
