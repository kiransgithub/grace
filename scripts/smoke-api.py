#!/usr/bin/env python3
"""Exercise real REST/gRPC listeners; this certifies synthetic accounting only."""
import argparse
import asyncio
from pathlib import Path
import sys
from uuid import uuid4

import aiohttp
import grpc
from grace.v1 import grace_pb2 as pb, grace_pb2_grpc as rpc

TERMINAL = {"cancelled", "expired", "released"}


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


async def run(args):
    token = Path(args.token_file).read_text().strip()
    check(len(token) >= 32, "Token file must contain at least 32 characters")
    headers = {"Authorization": "Bearer " + token}
    metadata = (("authorization", "Bearer " + token),)
    prefix = "kind-smoke-" + uuid4().hex
    records = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
        async with grpc.aio.insecure_channel(args.grpc) as channel:
            stub = rpc.ReservationServiceStub(channel)
            health_response = await http.get(args.http + "/health/ready")
            check(health_response.status == 200, "Readiness endpoint failed")
            health = await health_response.json()
            check(health.get("mode") == "simulation" and health.get("durable") is False
                  and health.get("live_execution_enabled") is False
                  and health.get("production_enabled") is False,
                  "Refusing to run synthetic capacity tests against a non-simulation runtime")
            page = ""
            while True:
                listing = await stub.ListReservations(pb.ListReservationsRequest(page_size=200, page_token=page),
                                                     metadata=metadata, timeout=10)
                check(all(item.state in {pb.RESERVATION_STATE_CANCELLED, pb.RESERVATION_STATE_EXPIRED,
                                         pb.RESERVATION_STATE_RELEASED} for item in listing.reservations),
                      "Existing active reservations found; refusing to reset or interfere with them")
                page = listing.next_page_token
                if not page:
                    break
            if args.assert_idle_only:
                print("Existing simulation is idle; no reservations were modified.")
                return

            async def create(key, fraction=250):
                body = {"tenant_id": "demo", "application_id": "kind-smoke",
                    "gpu_type": "A100-40GB", "gpu_memory_mib": 5120,
                    "gpu_millicards": fraction, "device_count": 1,
                    "environment": args.environment, "duration_seconds": 120,
                    "location": "onprem", "data_locations": ["onprem"]}
                response = await http.post(args.http + "/v1/reservations", json=body,
                    headers={**headers, "Idempotency-Key": prefix + key})
                data = await response.json()
                if response.status == 201:
                    records[data["id"]] = data
                return response.status, data

            async def cancel(item, key):
                request = pb.CancelReservationRequest(id=item["id"], etag=item["etag"],
                                                      idempotency_key=prefix + key)
                response = await stub.CancelReservation(request, metadata=metadata, timeout=10)
                replay = await stub.CancelReservation(request, metadata=metadata, timeout=10)
                check(response.id == replay.id and response.etag == replay.etag,
                      "Cancellation replay changed its result")

            async def await_release(ids):
                deadline = asyncio.get_running_loop().time() + 15
                remaining = set(ids)
                while remaining and asyncio.get_running_loop().time() < deadline:
                    for reservation_id in tuple(remaining):
                        response = await http.get(args.http + "/v1/reservations/" + reservation_id,
                                                  headers=headers)
                        check(response.status == 200, "Reservation disappeared during release")
                        if (await response.json())["state"] in TERMINAL:
                            remaining.remove(reservation_id)
                    if remaining:
                        await asyncio.sleep(0.1)
                check(not remaining, "Synthetic observer did not confirm cancellation within 15 seconds")

            try:
                unauthenticated = await http.get(args.http + "/v1/reservations")
                check(unauthenticated.status == 401, "REST accepted a missing credential")
                try:
                    await stub.ListReservations(pb.ListReservationsRequest(), timeout=10)
                except grpc.aio.AioRpcError as error:
                    check(error.code() == grpc.StatusCode.UNAUTHENTICATED, "Unexpected gRPC auth error")
                else:
                    raise RuntimeError("gRPC accepted a missing credential")
                status, first = await create("first")
                check(status == 201, f"Initial reservation failed ({status})")
                replay_status, replay = await create("first")
                check(replay_status == 201 and replay["id"] == first["id"], "Create replay allocated twice")
                found = await stub.GetReservation(pb.GetReservationRequest(id=first["id"]),
                                                  metadata=metadata, timeout=10)
                check(found.id == first["id"] and found.spec.resources.gpu_millicards == 250,
                      "REST and gRPC do not see the same reservation")
                results = await asyncio.gather(*(create(f"parallel-{index}") for index in range(8)))
                check(sum(status == 201 for status, _ in results) == 3,
                      "Concurrent 250/1000 requests did not fill exactly the remaining three shares")
                check(all(status in {201, 429} for status, _ in results), "Unexpected admission response")
                check(sum(item["request"]["gpu_millicards"] for item in records.values()) == 1000,
                      "Concurrent requests overbooked the synthetic GPU")
                for index, item in enumerate(tuple(records.values())):
                    await cancel(item, f"cancel-{index}")
                await await_release(records)
                status, full = await create("reuse", fraction=1000)
                check(status == 201, "Full GPU was not reusable after confirmed cleanup")
                await cancel(full, "cancel-reuse")
                await await_release([full["id"]])
                print("PASS: authenticated REST + gRPC; shared state; create/cancel replay; "
                      "concurrent fractional no-overbooking; confirmed cleanup; full-device reuse.")
                print("Scope: synthetic accounting in a Kubernetes pod; no SkyPilot, CUDA or HAMi runtime test.")
            finally:
                # Attempt cancellation only for records created by this invocation.
                for reservation_id in records:
                    try:
                        response = await http.get(args.http + "/v1/reservations/" + reservation_id, headers=headers)
                        item = await response.json()
                        if item.get("state") not in TERMINAL | {"releasing"}:
                            await stub.CancelReservation(pb.CancelReservationRequest(id=reservation_id,
                                etag=item["etag"], idempotency_key=prefix + "cleanup-" + reservation_id),
                                metadata=metadata, timeout=10)
                    except (aiohttp.ClientError, grpc.aio.AioRpcError, KeyError):
                        print("Cleanup incomplete for this run's reservation; inspect the demo API.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", required=True)
    parser.add_argument("--grpc", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--environment", choices=("dev", "qa"), default="dev")
    parser.add_argument("--assert-idle-only", action="store_true")
    args = parser.parse_args()
    # The helper exposes only loopback forwarding. Avoid sending credentials elsewhere.
    check(args.http.startswith("http://127.0.0.1:") and args.grpc.startswith("127.0.0.1:"),
          "Only local loopback port-forward endpoints are allowed")
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, aiohttp.ClientError, grpc.aio.AioRpcError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
