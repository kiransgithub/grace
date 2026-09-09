"""Bounded async HTTP adapter for the simulation service."""
import logging
from uuid import uuid4
from aiohttp import web
from grace.domain.errors import DomainError, ValidationError
from .service import ERROR_HTTP, reservation_dict

LOG = logging.getLogger(__name__)


def create_app(service):
    @web.middleware
    async def errors(request, handler):
        correlation = str(uuid4())
        try:
            response = await handler(request)
        except DomainError as exc:
            response = web.json_response({"code": exc.code, "detail": str(exc),
                                          "correlation_id": correlation},
                                         status=ERROR_HTTP.get(exc.code, 500))
        except web.HTTPException:
            raise
        except Exception:
            LOG.exception("request failed correlation=%s", correlation)
            response = web.json_response({"code": "INTERNAL", "detail": "internal error",
                                          "correlation_id": correlation}, status=500)
        response.headers["X-Correlation-ID"] = correlation
        response.headers["Cache-Control"] = "no-store"
        return response

    @web.middleware
    async def auth(request, handler):
        if request.path not in {"/health/live", "/health/ready"}:
            credentials = request.headers.getall("Authorization", [])
            request["caller"] = service.authenticate(credentials[0] if len(credentials) == 1 else "")
        return await handler(request)

    app = web.Application(middlewares=[errors, auth], client_max_size=64 * 1024)

    async def health(request):
        return web.json_response({"status": "simulation", "durable": False,
            "mode": "simulation", "production_enabled": False,
            "live_execution_enabled": False, "version": "0.1.0"})

    async def body(request):
        try:
            value = await request.json()
        except (ValueError, UnicodeError):
            raise ValidationError("valid JSON body required") from None
        if type(value) is not dict:
            raise ValidationError("JSON body must be an object")
        return value

    async def create(request):
        result = service.create(await body(request), request["caller"],
                                request.headers.get("Idempotency-Key", ""))
        return web.json_response(reservation_dict(result), status=201)

    async def get(request):
        return web.json_response(reservation_dict(service.get(request.match_info["id"], request["caller"])))

    async def list_records(request):
        try:
            size = int(request.query.get("page_size", "50"))
        except ValueError:
            raise ValidationError("page_size must be an integer") from None
        results, token = service.list(request["caller"], page_size=size,
                                      page_token=request.query.get("page_token", ""))
        return web.json_response({"reservations": [reservation_dict(item) for item in results],
                                  "next_page_token": token, "simulation": True})

    async def cancel(request):
        result = service.cancel(request.match_info["id"], request["caller"],
                                request.headers.get("Idempotency-Key", ""),
                                request.headers.get("If-Match", "").strip('"'))
        return web.json_response(reservation_dict(result), status=202)

    async def not_implemented(request):
        service.unavailable_feature()

    app.router.add_get("/health/live", health)
    app.router.add_get("/health/ready", health)
    app.router.add_post("/v1/reservations", create)
    app.router.add_get("/v1/reservations", list_records)
    app.router.add_get("/v1/reservations/{id}", get)
    app.router.add_post("/v1/reservations/{id}:cancel", cancel)
    app.router.add_post("/v1/reservations/{id}:renew", not_implemented)
    app.router.add_post("/v1/reservations/{id}:activate", not_implemented)
    app.router.add_get("/v1/reservations/{id}/events", not_implemented)
    app.router.add_get("/v1/capacity", not_implemented)
    app.router.add_get("/v1/usage", not_implemented)
    app.router.add_get("/v1/policies/{id}", not_implemented)
    app.router.add_put("/v1/policies/{id}", not_implemented)
    return app
