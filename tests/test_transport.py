"""Actual HTTP and gRPC transport tests, not mocked network success."""
import asyncio
import unittest
from aiohttp.test_utils import TestClient, TestServer
import grpc
from grace.transport.server import simulation_service, validate_settings, wait_for_stop_or_failure
from grace.transport.rest import create_app
from grace.transport.grpc_server import build_server
from grace.v1 import grace_pb2 as pb, grace_pb2_grpc as rpc

TOKEN = "local-unit-test-token-not-for-deployment-1234"
ENV = {"GRACE_DEMO_TOKEN": TOKEN}


def request_body(**extra):
    return {"tenant_id": "demo", "application_id": "demo-app", "gpu_type": "A100-40GB",
        "gpu_memory_mib": 5120, "gpu_millicards": 250, "device_count": 1,
        "environment": "dev", "duration_seconds": 3600, "data_locations": ["onprem"],
        "wait_for_capacity": False, **extra}


class SettingsTests(unittest.TestCase):
    def test_production_not_enableable(self):
        for change in ({"GRACE_ENVIRONMENT": "prod"}, {"GRACE_ALLOW_PRODUCTION": "true"}):
            with self.assertRaises(ValueError):
                validate_settings({**ENV, **change})

    def test_no_silent_db_fallback(self):
        with self.assertRaises(ValueError):
            validate_settings({**ENV, "GRACE_STATE_BACKEND": "postgres"})

    def test_no_default_secret(self):
        with self.assertRaises(ValueError):
            validate_settings({})


class SupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_failure_terminates_wait(self):
        async def fail():
            raise ValueError("injected observer failure")
        with self.assertRaisesRegex(ValueError, "injected observer failure"):
            await asyncio.wait_for(wait_for_stop_or_failure(asyncio.create_task(fail()), asyncio.Event()), 1)

    async def test_stop_does_not_wait_for_observer(self):
        stop = asyncio.Event()
        stop.set()
        observer = asyncio.create_task(asyncio.Event().wait())
        try:
            await asyncio.wait_for(wait_for_stop_or_failure(observer, stop), 1)
        finally:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service, self.gpus, self.controller = simulation_service(ENV)
        self.client = TestClient(TestServer(create_app(self.service)))
        await self.client.start_server()
        self.grpc = build_server(self.service)
        port = self.grpc.add_insecure_port("127.0.0.1:0")
        await self.grpc.start()
        self.channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        self.stub = rpc.ReservationServiceStub(self.channel)
        self.metadata = (("authorization", "Bearer " + TOKEN),)
        self.headers = {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "create-1"}

    async def asyncTearDown(self):
        await self.channel.close()
        await self.grpc.stop(0)
        await self.client.close()

    async def test_health_honest_simulation(self):
        response = await self.client.get("/health/ready")
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body["durable"])
        self.assertFalse(body["live_execution_enabled"])

    async def test_default_queue_is_visible_over_grpc_without_a_gpu_hold(self):
        held = await self.client.post("/v1/reservations", json=request_body(gpu_millicards=1000),
                                      headers=self.headers)
        self.assertEqual(held.status, 201)
        body = request_body()
        body.pop("wait_for_capacity")
        queued = await self.client.post("/v1/reservations", json=body,
            headers={**self.headers, "Idempotency-Key": "queued-default"})
        item = await queued.json()
        self.assertEqual(queued.status, 201)
        self.assertEqual(item["state"], "queued")
        self.assertIsNone(item["expires_at"])
        self.assertEqual(item["allocations"], [])
        self.assertEqual(item["effective_policy"]["priority"], 0)
        self.assertFalse(item["effective_policy"]["preemption_enabled"])
        result = await self.stub.GetReservation(pb.GetReservationRequest(id=item["id"]),
            metadata=self.metadata, timeout=2)
        self.assertEqual(result.state, pb.RESERVATION_STATE_QUEUED)
        self.assertFalse(result.HasField("expires_at"))
        self.assertTrue(result.HasField("queue_expires_at"))
        cancelled = await self.stub.CancelReservation(pb.CancelReservationRequest(id=item["id"],
            etag=item["etag"], idempotency_key="cancel-queued"), metadata=self.metadata, timeout=2)
        self.assertEqual(cancelled.state, pb.RESERVATION_STATE_CANCELLED)
        self.assertEqual(len(cancelled.allocations), 0)

    async def test_preferred_location_spills_and_reports_actual_location(self):
        await self.client.post("/v1/reservations", json=request_body(gpu_millicards=1000), headers=self.headers)
        response = await self.client.post("/v1/reservations",
            json=request_body(location="onprem", location_policy="preferred",
                              data_locations=["onprem", "gcp-us-central1"]),
            headers={**self.headers, "Idempotency-Key": "preferred"})
        item = await response.json()
        self.assertEqual(response.status, 201)
        self.assertEqual(item["selected_location"], "gcp-us-central1")
        result = await self.stub.GetReservation(pb.GetReservationRequest(id=item["id"]),
            metadata=self.metadata, timeout=2)
        self.assertEqual(result.spec.placement.location_policy, pb.LOCATION_POLICY_PREFERRED)
        self.assertEqual(result.spec.placement.location, "onprem")
        self.assertEqual(result.selected_location, "gcp-us-central1")

    async def test_user_cannot_set_priority_or_exemptions(self):
        for extra in ({"priority": 100}, {"preemption_exempt": True},
                      {"idle_reclamation_exempt": True}, {"effective_policy": {"priority": 100}},
                      {"admin_authorized": True}, {"business_unit_id": "executive"}):
            response = await self.client.post("/v1/reservations", json=request_body(**extra), headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post("/v1/reservations", json=request_body(project_id="unapproved"),
                                          headers=self.headers)
        self.assertEqual(response.status, 403)

    async def test_grpc_rejects_conflicting_location_and_unknown_mode(self):
        for placement in (pb.PlacementPolicy(location="onprem", required_location="gcp-us-central1"),
                          pb.PlacementPolicy(location_policy=99)):
            spec = pb.ReservationSpec(tenant_id="demo", application_id="demo-app", environment=pb.ENVIRONMENT_DEV,
                resources=pb.ResourceShape(gpu_type="A100-40GB", device_count=1,
                    gpu_millicards=250, gpu_memory_mib=5120), placement=placement, duration_seconds=120)
            with self.assertRaises(grpc.aio.AioRpcError) as error:
                await self.stub.CreateReservation(pb.CreateReservationRequest(idempotency_key="invalid-mode",
                    spec=spec), metadata=self.metadata, timeout=2)
            self.assertEqual(error.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)

    async def test_admin_policy_api_is_explicitly_unimplemented(self):
        stub = rpc.PolicyServiceStub(self.channel)
        with self.assertRaises(grpc.aio.AioRpcError) as error:
            await stub.GetPolicy(pb.GetPolicyRequest(id="test"), metadata=self.metadata, timeout=2)
        self.assertEqual(error.exception.code(), grpc.StatusCode.UNIMPLEMENTED)
        response = await self.client.get("/v1/policies/test", headers=self.headers)
        self.assertEqual(response.status, 501)

    async def test_missing_rest_auth(self):
        response = await self.client.post("/v1/reservations", json=request_body())
        self.assertEqual(response.status, 401)

    async def test_duplicate_credentials_rejected_in_both_transports(self):
        good, bad = "Bearer " + TOKEN, "Bearer wrong"
        for values in ((good, bad), (bad, good), (good, good)):
            response = await self.client.get("/v1/reservations",
                headers=[("Authorization", value) for value in values])
            self.assertEqual(response.status, 401)
            with self.assertRaises(grpc.aio.AioRpcError) as error:
                await self.stub.ListReservations(pb.ListReservationsRequest(),
                    metadata=[("authorization", value) for value in values], timeout=2)
            self.assertEqual(error.exception.code(), grpc.StatusCode.UNAUTHENTICATED)
            trailers = {key: value for key, value in error.exception.trailing_metadata()}
            self.assertTrue(trailers["x-correlation-id"])

    async def test_pathological_etag_rejected_without_internal_error(self):
        for etag in ("9" * 5000, "9223372036854775808", "١", "0", "-1"):
            with self.assertRaises(grpc.aio.AioRpcError) as error:
                await self.stub.CancelReservation(pb.CancelReservationRequest(id="not-found",
                    idempotency_key="bounded-etag", etag=etag), metadata=self.metadata, timeout=2)
            self.assertEqual(error.exception.code(), grpc.StatusCode.INVALID_ARGUMENT)

    async def test_invalid_grpc_auth(self):
        with self.assertRaises(grpc.aio.AioRpcError) as error:
            await self.stub.ListReservations(pb.ListReservationsRequest(), timeout=2)
        self.assertEqual(error.exception.code(), grpc.StatusCode.UNAUTHENTICATED)

    async def test_http_create_grpc_get_same_state(self):
        response = await self.client.post("/v1/reservations", json=request_body(), headers=self.headers)
        self.assertEqual(response.status, 201, await response.text())
        result = await response.json()
        rpc_result = await self.stub.GetReservation(pb.GetReservationRequest(id=result["id"]),
            metadata=self.metadata, timeout=2)
        self.assertEqual(rpc_result.id, result["id"])
        self.assertEqual(rpc_result.spec.resources.gpu_millicards, 250)
        self.assertTrue(rpc_result.simulation)
        self.assertEqual(rpc_result.allocations[0].fencing_token, 0)

    async def test_grpc_create_and_idempotent_replay(self):
        request = pb.CreateReservationRequest(idempotency_key="rpc-create",
            spec=pb.ReservationSpec(tenant_id="demo", application_id="demo-app",
                environment=pb.ENVIRONMENT_DEV, duration_seconds=3600,
                resources=pb.ResourceShape(gpu_type="A100-40GB", device_count=1,
                    gpu_millicards=250, gpu_memory_mib=5120),
                placement=pb.PlacementPolicy(data_locations=["onprem"])))
        first = await self.stub.CreateReservation(request, metadata=self.metadata, timeout=2)
        second = await self.stub.CreateReservation(request, metadata=self.metadata, timeout=2)
        self.assertEqual(first.id, second.id)
        request.spec.resources.gpu_millicards = 500
        with self.assertRaises(grpc.aio.AioRpcError) as error:
            await self.stub.CreateReservation(request, metadata=self.metadata, timeout=2)
        self.assertEqual(error.exception.code(), grpc.StatusCode.ALREADY_EXISTS)

    async def test_request_cannot_grant_production(self):
        result = await self.client.post("/v1/reservations", headers=self.headers,
            json=request_body(environment="prod", production_opt_in=True))
        self.assertEqual(result.status, 403)

    async def test_boolean_and_float_fraction_rejected(self):
        for invalid in (True, 0.25, "250"):
            result = await self.client.post("/v1/reservations", headers=self.headers,
                json=request_body(gpu_millicards=invalid))
            self.assertEqual(result.status, 400)

    async def test_tenant_forgery_and_identity_field_rejected(self):
        result = await self.client.post("/v1/reservations", headers=self.headers,
            json=request_body(tenant_id="other"))
        self.assertEqual(result.status, 403)
        result = await self.client.post("/v1/reservations", headers=self.headers,
            json=request_body(controller_authorized=True))
        self.assertEqual(result.status, 400)

    async def test_cancel_retains_capacity_and_retry_is_idempotent(self):
        response = await self.client.post("/v1/reservations", headers=self.headers,
            json=request_body(gpu_millicards=1000))
        created = await response.json()
        cancelled = await self.stub.CancelReservation(pb.CancelReservationRequest(id=created["id"],
            idempotency_key="cancel-1", etag=created["etag"]), metadata=self.metadata, timeout=2)
        self.assertEqual(cancelled.state, pb.RESERVATION_STATE_RELEASING)
        replay = await self.stub.CancelReservation(pb.CancelReservationRequest(id=created["id"],
            idempotency_key="cancel-1", etag=created["etag"]), metadata=self.metadata, timeout=2)
        self.assertEqual(replay.etag, cancelled.etag)
        result = await self.client.post("/v1/reservations", headers={**self.headers,"Idempotency-Key":"create-2"},
            json=request_body(gpu_millicards=1000))
        self.assertEqual(result.status, 429)

    async def test_cancel_stale_etag(self):
        response = await self.client.post("/v1/reservations", headers=self.headers, json=request_body())
        created = await response.json()
        result = await self.client.post(f"/v1/reservations/{created['id']}:cancel",
            headers={**self.headers,"Idempotency-Key":"cancel-2","If-Match":"99"})
        self.assertEqual(result.status, 409)

    async def test_signed_pagination(self):
        for index in range(3):
            response = await self.client.post("/v1/reservations", json=request_body(),
                headers={**self.headers,"Idempotency-Key":f"page-{index}"})
            self.assertEqual(response.status, 201)
        first = await self.stub.ListReservations(pb.ListReservationsRequest(page_size=2),
                                                metadata=self.metadata, timeout=2)
        self.assertEqual(len(first.reservations), 2)
        second = await self.stub.ListReservations(pb.ListReservationsRequest(page_size=2,
            page_token=first.next_page_token), metadata=self.metadata, timeout=2)
        self.assertEqual(len(second.reservations), 1)
        with self.assertRaises(grpc.aio.AioRpcError):
            await self.stub.ListReservations(pb.ListReservationsRequest(page_token=first.next_page_token+"x"),
                                            metadata=self.metadata, timeout=2)

    async def test_unsupported_features_do_not_fake_success(self):
        with self.assertRaises(grpc.aio.AioRpcError) as error:
            await self.stub.ActivateReservation(pb.ActivateReservationRequest(),
                                               metadata=self.metadata, timeout=2)
        self.assertEqual(error.exception.code(), grpc.StatusCode.UNIMPLEMENTED)
        result = await self.client.get("/v1/usage", headers=self.headers)
        self.assertEqual(result.status, 501)

    async def test_body_size_and_malformed_json(self):
        result = await self.client.post("/v1/reservations", headers=self.headers, data="{")
        self.assertEqual(result.status, 400)
        result = await self.client.post("/v1/reservations", headers=self.headers, data="x" * 70000)
        self.assertEqual(result.status, 413)


if __name__ == "__main__":
    unittest.main()
