"""FastAPI application factory.

Run with: uvicorn notetaker.gateway.app:create_app --factory --host 0.0.0.0 --port 8000
"""
import contextlib
import logging

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from notetaker.config import Settings, get_settings
from notetaker.gateway.errors import install_error_handlers
from notetaker.gateway.routes_jobs import router as jobs_router
from notetaker.gateway.routes_system import router as system_router
from notetaker.infra.broker import Broker
from notetaker.infra.crypto import TokenCipher
from notetaker.infra.job_store import JobStore
from notetaker.infra.redis_client import create_redis
from notetaker.infra.startup import retry_until_ready
from notetaker.logging_setup import configure_logging

log = logging.getLogger("notetaker.gateway")


def create_app(
    settings: Settings | None = None,
    *,
    store: JobStore | None = None,
    broker: Broker | None = None,
    http: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the app. Dependencies can be injected (tests); otherwise they connect on startup."""
    settings = settings or get_settings()
    configure_logging("gateway")
    cipher = TokenCipher(settings.token_encryption_key)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        owned = []
        if store is None:
            redis = create_redis(settings.redis_url)
            await retry_until_ready("redis", redis.ping)
            app.state.store = JobStore(redis, settings.job_ttl_sec)
            owned.append(redis.aclose)
        if broker is None:
            app.state.broker = Broker(settings.amqp_url, settings.retry_delays_sec, settings.queue_delivery_limit)
            await retry_until_ready("rabbitmq", app.state.broker.connect)
            owned.append(app.state.broker.close)
        if http is None:
            app.state.http = httpx.AsyncClient(timeout=15)
            owned.append(app.state.http.aclose)
        log.info("gateway ready")
        yield
        for close in reversed(owned):
            with contextlib.suppress(Exception):
                await close()

    app = FastAPI(title="Video Note-Taking Gateway", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.cipher = cipher
    app.state.store = store
    app.state.broker = broker
    app.state.http = http

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_origin_regex=r"chrome-extension://.*",
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["Location"],
    )
    install_error_handlers(app)
    app.include_router(jobs_router)
    app.include_router(system_router)
    return app
