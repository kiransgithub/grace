"""Explicitly non-durable dev/QA simulator, suitable for one development pod."""
import asyncio
import logging
import os
import signal
from dataclasses import replace
from datetime import datetime, timezone

from aiohttp import web
from grace.domain import Caller, Engine, GPU, State
from .service import Service
from .rest import create_app


def validate_settings(env):
    if env.get("GRACE_MODE", "simulation") != "simulation":
        raise ValueError("only simulation is implemented; durable runtime not yet delivered")
    if env.get("GRACE_STATE_BACKEND", "memory") != "memory":
        raise ValueError("PostgreSQL runtime adapter not yet delivered")
    environment = env.get("GRACE_ENVIRONMENT", "dev")
    if environment not in {"dev", "qa"} or env.get("GRACE_ALLOW_PRODUCTION", "false").lower() != "false":
        raise ValueError("production cannot be enabled in this simulation release")
    if len(env.get("GRACE_DEMO_TOKEN", "")) < 32:
        raise ValueError("set a unique GRACE_DEMO_TOKEN with at least 32 characters")
    return environment


def simulation_service(env=os.environ):
    environment = validate_settings(env)
    now = datetime.now(timezone.utc)
    tenant = env.get("GRACE_DEMO_TENANT", "demo")
    locations = frozenset({"onprem", "gcp-us-central1", "gcp-us-east1", "aks-eastus"})
    caller = Caller("demo-user", tenant, allowed_environments=frozenset({environment}),
                    allowed_locations=locations)
    gpus = tuple(GPU(f"sim-{index}", "A100-40GB", 40960, environment, location, now,
                     node_id=f"sim-node-{index}", allowed_tenants=frozenset({tenant}))
                 for index, location in enumerate(sorted(locations)))
    engine = Engine(gpus)
    service = Service(engine, token=env["GRACE_DEMO_TOKEN"], caller=caller)
    return service, gpus, replace(caller, subject="simulation-controller", controller_authorized=True)


async def simulation_observer(service, gpus, controller):
    # Synthetic empty infrastructure only: there is NO SkyPilot dispatch from this runtime.
    while True:
        now = datetime.now(timezone.utc)
        for gpu in gpus:
            service.engine.upsert_gpu(replace(gpu, observed_at=now))
        for item in service.engine.list(controller):
            if item.state is State.RESERVED and now >= item.expires_at:
                service.engine.request_release(item.id, controller, reason="expired")
            elif item.state is State.RELEASING and now > item.release_requested_at:
                # No external dispatch exists; only undispatched reservations appear here.
                service.engine.confirm_released(item.id, controller)
        await asyncio.sleep(1)


async def wait_for_stop_or_failure(observer, stop):
    """A dead inventory/lifecycle loop must not leave a healthy-looking API alive."""
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({observer, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if observer in done:
            observer.result()
            raise RuntimeError("simulation observer exited unexpectedly")
    finally:
        stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)


async def main():
    logging.basicConfig(level=logging.INFO)
    service, gpus, controller = simulation_service()
    runner = web.AppRunner(create_app(service), access_log=None)
    await runner.setup()
    http_port = int(os.getenv("GRACE_HTTP_PORT", "8080"))
    await web.TCPSite(runner, os.getenv("GRACE_HTTP_HOST", "127.0.0.1"), http_port).start()
    grpc_server = None
    if os.getenv("GRACE_GRPC_ENABLED", "true").lower() == "true":
        from .grpc_server import build_server
        grpc_server = build_server(service)
        address = os.getenv("GRACE_GRPC_HOST", "127.0.0.1") + ":" + os.getenv("GRACE_GRPC_PORT", "50051")
        if grpc_server.add_insecure_port(address) == 0:
            raise RuntimeError("cannot bind gRPC listener")
        await grpc_server.start()
    observer = asyncio.create_task(simulation_observer(service, gpus, controller))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows local development; process Ctrl+C still terminates.
    logging.info("GRACE SIMULATION: non-durable state; synthetic devices; no live dispatch or Okta")
    try:
        await wait_for_stop_or_failure(observer, stop)
    finally:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
        if grpc_server:
            await grpc_server.stop(5)
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
