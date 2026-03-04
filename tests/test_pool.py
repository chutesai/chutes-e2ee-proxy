import asyncio

import pytest

from chutes_e2ee_proxy.pool import TransportPool


class FakeTransport:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pool_reuses_transport_for_same_key() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", transport_factory=factory)
    one = await pool.get("k1")
    two = await pool.get("k1")

    assert one is two
    assert len(created) == 1

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_evicts_oldest_when_max_size_reached() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", max_size=2, transport_factory=factory)

    t1 = await pool.get("k1")
    await asyncio.sleep(0)
    _t2 = await pool.get("k2")
    _t3 = await pool.get("k3")

    assert t1.closed is True

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_cleanup_evicts_idle_entries() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", idle_ttl=0.0, transport_factory=factory)
    _t1 = await pool.get("k1")
    await pool.cleanup()

    assert created[0].closed is True

    await pool.close_all()


@pytest.mark.asyncio
async def test_pool_close_all_closes_every_transport() -> None:
    created: list[FakeTransport] = []

    def factory(_api_key: str, _upstream: str) -> FakeTransport:
        transport = FakeTransport()
        created.append(transport)
        return transport

    pool = TransportPool("https://llm.chutes.ai", transport_factory=factory)
    await pool.get("k1")
    await pool.get("k2")

    await pool.close_all()

    assert all(t.closed for t in created)
