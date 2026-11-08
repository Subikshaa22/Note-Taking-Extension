import pytest
from fakeredis import FakeAsyncRedis

from notetaker.infra.job_store import JobStore


@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def store(redis):
    return JobStore(redis, ttl_sec=3600)
