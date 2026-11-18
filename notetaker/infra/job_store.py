"""Redis-backed job state and per-job stage artifacts.

Layout (every key expires after JOB_TTL_SEC):
  job:{id}              hash   - the Job record; JSON-encoded for nested fields
  job:{id}:events       list   - append-only audit trail of transitions and attempts
  job:{id}:artifacts    hash   - stage outputs (metadata, transcript, notes, publish state)
  job:{id}:frames       hash   - per-timestamp frame outcome (JSON)
  job:{id}:frame:{ts}   string - captured JPEG bytes
  job:{id}:scan         hash   - low-res video scan samples for frame selection (short TTL,
                                 deleted once the frames are chosen)
  idem:{key}            string - Idempotency-Key -> job id

Status changes go through a Lua compare-and-set so that a transition is only applied
from one of its allowed source states (see domain.status.ALLOWED_TRANSITIONS).
"""
import base64
import json
from typing import TypeVar

from pydantic import BaseModel
from redis.asyncio import Redis

from notetaker.domain.models import Job, utc_now
from notetaker.domain.status import JobStatus, allowed_sources

T = TypeVar("T", bound=BaseModel)

_JSON_FIELDS = ("warnings", "error", "result", "retry")
_MAX_EVENTS = 100

_TRANSITION_LUA = """
local current = redis.call('HGET', KEYS[1], 'status')
if not current then return -1 end
for allowed in string.gmatch(ARGV[2], '[^,]+') do
  if allowed == current then
    redis.call('HSET', KEYS[1], 'status', ARGV[1], unpack(ARGV, 3))
    return 1
  end
end
return 0
"""


def _encode(job_fields: dict) -> dict[str, str]:
    encoded = {}
    for key, value in job_fields.items():
        if key in _JSON_FIELDS:
            encoded[key] = json.dumps(value) if value not in (None, []) else ""
        elif value is None:
            encoded[key] = ""
        else:
            encoded[key] = str(value)
    return encoded


def _decode(raw: dict[str, str]) -> dict:
    decoded: dict = {}
    for key, value in raw.items():
        if key in _JSON_FIELDS:
            decoded[key] = json.loads(value) if value else ([] if key == "warnings" else None)
        else:
            decoded[key] = value if value != "" else None
    return decoded


class JobStore:
    def __init__(self, redis: Redis, ttl_sec: int):
        self._redis = redis
        self._ttl = ttl_sec
        self._transition = redis.register_script(_TRANSITION_LUA)

    @staticmethod
    def _key(job_id: str, suffix: str = "") -> str:
        return f"job:{job_id}{':' + suffix if suffix else ''}"

    async def _touch(self, job_id: str) -> None:
        pipe = self._redis.pipeline(transaction=False)
        for suffix in ("", "events", "artifacts", "frames"):
            pipe.expire(self._key(job_id, suffix), self._ttl)
        await pipe.execute()

    # ---- Job record ------------------------------------------------------------------

    async def create(self, job: Job) -> None:
        await self._redis.hset(self._key(job.job_id), mapping=_encode(job.model_dump(mode="json")))
        await self.add_event(job.job_id, status=job.status, note="accepted")

    async def get(self, job_id: str) -> Job | None:
        raw = await self._redis.hgetall(self._key(job_id))
        if not raw:
            return None
        return Job.model_validate(_decode(raw))

    async def update(self, job_id: str, **fields) -> None:
        """Update non-status fields (progress, detail, title...) of an existing job."""
        if "status" in fields:
            raise ValueError("use transition() to change status")
        fields["updated_at"] = utc_now()
        await self._redis.hset(self._key(job_id), mapping=_encode(self._dump(fields)))

    async def transition(self, job_id: str, target: JobStatus, **fields) -> bool:
        """Move the job to `target` if allowed from its current status; returns success."""
        fields["updated_at"] = utc_now()
        sources = ",".join(s.value for s in allowed_sources(target))
        flat: list[str] = []
        for key, value in _encode(self._dump(fields)).items():
            flat.extend((key, value))
        applied = await self._transition(keys=[self._key(job_id)], args=[target.value, sources, *flat])
        if applied == 1:
            await self.add_event(job_id, status=target, note=fields.get("detail"))
        return applied == 1

    async def increment_attempt(self, job_id: str) -> int:
        return int(await self._redis.hincrby(self._key(job_id), "attempt", 1))

    async def refund_attempt(self, job_id: str, max_refunds: int) -> bool:
        """Give back an attempt that was interrupted (not failed), at most `max_refunds` per job.

        The cap keeps a stage that *always* loses its lease from looping forever.
        """
        key = self._key(job_id)
        if int(await self._redis.hincrby(key, "interruptions", 1)) > max_refunds:
            return False
        await self._redis.hincrby(key, "attempt", -1)
        return True

    @staticmethod
    def _dump(fields: dict) -> dict:
        dumped = {}
        for key, value in fields.items():
            if isinstance(value, BaseModel):
                dumped[key] = value.model_dump(mode="json")
            elif isinstance(value, list):
                dumped[key] = [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
            else:
                dumped[key] = value
        return dumped

    # ---- Audit trail -----------------------------------------------------------------

    async def add_event(self, job_id: str, **event) -> None:
        event = {"at": utc_now(), **{k: v for k, v in event.items() if v is not None}}
        key = self._key(job_id, "events")
        pipe = self._redis.pipeline(transaction=False)
        pipe.rpush(key, json.dumps(event, default=str))
        pipe.ltrim(key, -_MAX_EVENTS, -1)
        await pipe.execute()
        await self._touch(job_id)

    async def events(self, job_id: str) -> list[dict]:
        return [json.loads(e) for e in await self._redis.lrange(self._key(job_id, "events"), 0, -1)]

    # ---- Stage artifacts -------------------------------------------------------------

    async def save_artifact(self, job_id: str, name: str, value: BaseModel | dict) -> None:
        payload = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
        await self._redis.hset(self._key(job_id, "artifacts"), name, payload)
        await self._redis.expire(self._key(job_id, "artifacts"), self._ttl)

    async def load_artifact(self, job_id: str, name: str, model: type[T]) -> T | None:
        raw = await self._redis.hget(self._key(job_id, "artifacts"), name)
        return model.model_validate_json(raw) if raw else None

    async def load_artifact_dict(self, job_id: str, name: str) -> dict | None:
        raw = await self._redis.hget(self._key(job_id, "artifacts"), name)
        return json.loads(raw) if raw else None

    # ---- Frames ----------------------------------------------------------------------

    async def save_frame(self, job_id: str, timestamp_sec: int, image: bytes) -> None:
        # The client decodes responses as text, so binary frames are stored base64-encoded.
        key = self._key(job_id, f"frame:{timestamp_sec}")
        await self._redis.set(key, base64.b64encode(image).decode("ascii"), ex=self._ttl)
        await self.record_frame_outcome(job_id, timestamp_sec, {"ok": True, "bytes": len(image)})

    async def load_frame(self, job_id: str, timestamp_sec: int) -> bytes | None:
        encoded = await self._redis.get(self._key(job_id, f"frame:{timestamp_sec}"))
        return base64.b64decode(encoded) if encoded else None

    async def record_frame_outcome(self, job_id: str, timestamp_sec: int, outcome: dict) -> None:
        await self._redis.hset(self._key(job_id, "frames"), str(timestamp_sec), json.dumps(outcome))
        await self._redis.expire(self._key(job_id, "frames"), self._ttl)

    async def frame_outcomes(self, job_id: str) -> dict[int, dict]:
        raw = await self._redis.hgetall(self._key(job_id, "frames"))
        return {int(ts): json.loads(v) for ts, v in raw.items()}

    # ---- Video scan samples (kept briefly so a retried selection call skips the rescan) ---

    async def save_scan(self, job_id: str, frames: dict[int, bytes], ttl_sec: int) -> None:
        key = self._key(job_id, "scan")
        await self._redis.delete(key)
        if frames:
            await self._redis.hset(key, mapping={str(ts): base64.b64encode(img).decode("ascii") for ts, img in frames.items()})
            await self._redis.expire(key, ttl_sec)

    async def load_scan(self, job_id: str) -> dict[int, bytes]:
        raw = await self._redis.hgetall(self._key(job_id, "scan"))
        return {int(ts): base64.b64decode(v) for ts, v in raw.items()}

    async def delete_scan(self, job_id: str) -> None:
        await self._redis.delete(self._key(job_id, "scan"))

    # ---- Idempotent submission ---------------------------------------------------------

    async def claim_idempotency_key(self, key: str, job_id: str, ttl_sec: int) -> str | None:
        """Bind `key` to `job_id`. Returns the existing job id if the key was already used."""
        redis_key = f"idem:{key}"
        if await self._redis.set(redis_key, job_id, nx=True, ex=ttl_sec):
            return None
        return await self._redis.get(redis_key)

    async def release_idempotency_key(self, key: str) -> None:
        await self._redis.delete(f"idem:{key}")

    async def ping(self) -> bool:
        return bool(await self._redis.ping())
