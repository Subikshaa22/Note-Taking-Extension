"""RabbitMQ topology and messaging.

Queues (all durable, messages persistent, publisher confirms on):

  notes.jobs           quorum work queue. One message = one pipeline stage of one job.
                       Workers ack only after the stage's output is stored and the next
                       stage has been published, so a crashed worker's message is
                       redelivered to another worker.
  notes.retry.<N>s     one delay queue per configured backoff step. Messages wait out the
                       queue TTL and are dead-lettered back into notes.jobs.
  notes.dlq            dead-letter queue for jobs whose retry budget is exhausted, and for
                       messages RabbitMQ gives up on (malformed / delivery limit reached).
"""
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

import aio_pika
from aio_pika.abc import (
    AbstractIncomingMessage,
    AbstractRobustChannel,
    AbstractRobustConnection,
    DeliveryMode,
)

from notetaker.domain.models import JobError
from notetaker.domain.status import Stage

log = logging.getLogger(__name__)

WORK_QUEUE = "notes.jobs"
DEAD_LETTER_QUEUE = "notes.dlq"


def retry_queue_name(delay_sec: int) -> str:
    return f"notes.retry.{delay_sec}s"


@dataclass(frozen=True)
class Envelope:
    """Body of every work message. `notion_token` is encrypted (see infra.crypto)."""

    job_id: str
    stage: Stage
    notion_token: str

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_bytes(cls, body: bytes) -> "Envelope":
        data = json.loads(body)
        return cls(job_id=str(data["job_id"]), stage=Stage(data["stage"]), notion_token=str(data["notion_token"]))


MessageHandler = Callable[[AbstractIncomingMessage], Awaitable[None]]


class Broker:
    def __init__(self, amqp_url: str, retry_delays_sec: list[int], delivery_limit: int):
        self._url = amqp_url
        self._retry_delays = sorted(set(retry_delays_sec))
        self._delivery_limit = delivery_limit
        self._connection: AbstractRobustConnection | None = None
        self._publish_channel: AbstractRobustChannel | None = None
        self._consume_channel: AbstractRobustChannel | None = None
        self._consumer_tag: str | None = None
        self._work_queue = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url, client_properties={"connection_name": "notetaker"})
        self._publish_channel = await self._connection.channel(publisher_confirms=True)
        await self._declare_topology(self._publish_channel)

    async def _declare_topology(self, channel: AbstractRobustChannel) -> None:
        await channel.declare_queue(DEAD_LETTER_QUEUE, durable=True)
        await channel.declare_queue(
            WORK_QUEUE,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": self._delivery_limit,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": DEAD_LETTER_QUEUE,
            },
        )
        for delay in self._retry_delays:
            await channel.declare_queue(
                retry_queue_name(delay),
                durable=True,
                arguments={
                    "x-message-ttl": delay * 1000,
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": WORK_QUEUE,
                },
            )

    @property
    def is_connected(self) -> bool:
        return bool(self._connection and not self._connection.is_closed)

    async def _publish(self, routing_key: str, body: bytes, headers: dict | None = None) -> None:
        if self._publish_channel is None:
            raise RuntimeError("broker is not connected")
        message = aio_pika.Message(
            body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            headers=headers or {},
        )
        # With publisher confirms this returns only once RabbitMQ has persisted the message.
        await self._publish_channel.default_exchange.publish(message, routing_key=routing_key)

    async def publish_stage(self, envelope: Envelope) -> None:
        await self._publish(WORK_QUEUE, envelope.to_bytes())

    def retry_delay_tier(self, wanted_sec: float) -> int:
        """Smallest configured delay >= `wanted_sec` (or the largest one)."""
        for delay in self._retry_delays:
            if delay >= wanted_sec:
                return delay
        return self._retry_delays[-1]

    async def publish_retry(self, envelope: Envelope, delay_sec: int) -> None:
        tier = self.retry_delay_tier(delay_sec)
        await self._publish(retry_queue_name(tier), envelope.to_bytes(), {"x-retry-delay-sec": tier})

    async def publish_dead_letter(self, envelope: Envelope, error: JobError, attempts: int) -> None:
        headers = {
            "x-job-id": envelope.job_id,
            "x-stage": envelope.stage.value,
            "x-error-code": error.code,
            "x-error-message": error.message[:1000],
            "x-attempts": attempts,
        }
        await self._publish(DEAD_LETTER_QUEUE, envelope.to_bytes(), headers)

    async def consume(self, handler: MessageHandler, prefetch: int) -> None:
        self._consume_channel = await self._connection.channel()
        await self._consume_channel.set_qos(prefetch_count=prefetch)
        self._work_queue = await self._consume_channel.declare_queue(WORK_QUEUE, passive=True)
        self._consumer_tag = await self._work_queue.consume(handler)

    async def stop_consuming(self) -> None:
        if self._work_queue is not None and self._consumer_tag:
            await self._work_queue.cancel(self._consumer_tag)
            self._consumer_tag = None

    async def queue_depths(self) -> dict[str, int]:
        """Ready-message counts per queue (used by the health check)."""
        channel = await self._connection.channel()
        try:
            depths = {}
            for name in [WORK_QUEUE, DEAD_LETTER_QUEUE, *map(retry_queue_name, self._retry_delays)]:
                queue = await channel.declare_queue(name, passive=True)
                depths[name] = queue.declaration_result.message_count
            return depths
        finally:
            await channel.close()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
