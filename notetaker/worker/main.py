"""Worker entrypoint: `python -m notetaker.worker.main`.

Wires the dependencies, consumes from the work queue with prefetch = WORKER_CONCURRENCY,
and on SIGTERM stops taking new messages and lets in-flight stages finish (anything not
finished before the container is killed is redelivered to another worker).
"""
import asyncio
import logging
import os
import signal
import socket

import httpx

from notetaker.config import Settings, get_settings
from notetaker.domain.errors import FatalError
from notetaker.infra.broker import Broker
from notetaker.infra.crypto import TokenCipher, TokenCipherError
from notetaker.infra.job_store import JobStore
from notetaker.infra.rate_limiter import RateLimiter, ServiceLimits
from notetaker.infra.redis_client import create_redis
from notetaker.logging_setup import configure_logging
from notetaker.pipeline.context import Services
from notetaker.services.frames import FrameGrabber
from notetaker.services.llm import GeminiClient
from notetaker.services.transcripts import TranscriptClient
from notetaker.services.youtube import YouTubeClient
from notetaker.worker.processor import StageProcessor

log = logging.getLogger("notetaker.worker")

_SHUTDOWN_GRACE_SEC = 25


async def retry_until_ready(name: str, connect, attempts: int = 30, delay_sec: float = 2.0):
    for attempt in range(1, attempts + 1):
        try:
            return await connect()
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("%s not reachable yet (%s); retrying in %.0fs", name, type(exc).__name__, delay_sec)
            await asyncio.sleep(delay_sec)


def build_limits(redis, settings: Settings) -> ServiceLimits:
    return ServiceLimits(
        RateLimiter(redis),
        llm_rpm=settings.llm_rpm,
        llm_tpm=settings.llm_tpm,
        notion_rps=settings.notion_rps,
        youtube_rpm=settings.youtube_rpm,
    )


async def run() -> None:
    settings = get_settings()
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    cipher = TokenCipher(settings.token_encryption_key)

    redis = create_redis(settings.redis_url)
    await retry_until_ready("redis", redis.ping)
    limits = build_limits(redis, settings)
    http = httpx.AsyncClient(timeout=settings.notion_timeout_sec)
    services = Services(
        youtube=YouTubeClient(settings, limits),
        transcripts=TranscriptClient(settings, limits),
        frames=FrameGrabber(settings.frame_timeout_sec),
        llm=GeminiClient(settings, limits),
        limits=limits,
        http=http,
        settings=settings,
    )

    broker = Broker(settings.amqp_url, settings.retry_delays_sec, settings.queue_delivery_limit)
    await retry_until_ready("rabbitmq", broker.connect)

    processor = StageProcessor(
        redis=redis,
        store=JobStore(redis, settings.job_ttl_sec),
        broker=broker,
        services=services,
        settings=settings,
        cipher=cipher,
        worker_id=worker_id,
    )
    await broker.consume(processor.handle, prefetch=settings.worker_concurrency)
    log.info("worker %s consuming (concurrency=%d)", worker_id, settings.worker_concurrency)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass
    await stop.wait()

    log.info("shutting down: no new messages, waiting for %d in-flight stage(s)", processor.in_flight)
    await broker.stop_consuming()
    for _ in range(_SHUTDOWN_GRACE_SEC * 10):
        if processor.in_flight == 0:
            break
        await asyncio.sleep(0.1)
    await broker.close()
    await http.aclose()
    await redis.aclose()


def main() -> None:
    configure_logging("worker")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except (FatalError, TokenCipherError) as exc:
        log.error("configuration error: %s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
