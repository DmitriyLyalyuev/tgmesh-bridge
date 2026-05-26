import asyncio
import logging
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Fixture: temporarily inject stub modules so tgmesh_bridge can be imported
# without Meshtastic/Telegram hardware, then restore sys.modules afterwards.
# ---------------------------------------------------------------------------

_STUB_MODS = {
    "meshtastic": {},
    "meshtastic.tcp_interface": {},
    "meshtastic.mesh_pb2": {},
    "meshtastic.portnums_pb2": {},
    "meshtastic_interface": {"MeshtasticInterface": object},
    "telegram_interface": {"TelegramInterface": object},
    "message_processor": {"MessageProcessor": object},
    "config_manager": {"ConfigManager": object, "get_logger": logging.getLogger},
    "storage": {"Storage": object},
}


@pytest.fixture()
def bridge_class():
    """Import TgmeshBridge with stub dependencies; clean up sys.modules after."""
    saved = {k: sys.modules.get(k) for k in (*_STUB_MODS, "tgmesh_bridge")}

    for mod_name, attrs in _STUB_MODS.items():
        m = types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[mod_name] = m

    # force fresh import
    sys.modules.pop("tgmesh_bridge", None)
    from tgmesh_bridge import TgmeshBridge  # noqa: PLC0415

    yield TgmeshBridge

    # restore original state
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervise_telegram_restarts_on_exception(monkeypatch, bridge_class):
    calls = {"n": 0}

    class FakeTelegram:
        def __init__(self):
            self._stop_event = asyncio.Event()

        async def start(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("boom")
            await self._stop_event.wait()

        async def shutdown(self):
            self._stop_event.set()

    _real_sleep = asyncio.sleep

    async def fake_sleep(_):
        # yield to the event loop without real delay
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bridge = bridge_class.__new__(bridge_class)
    bridge.logger = logging.getLogger("test_sup")
    bridge.telegram = FakeTelegram()

    sup_task = asyncio.create_task(bridge._supervise_telegram())
    await asyncio.sleep(0)
    for _ in range(20):
        if calls["n"] >= 3:
            break
        await asyncio.sleep(0)
    assert calls["n"] >= 3

    await bridge.telegram.shutdown()
    await asyncio.wait_for(sup_task, timeout=1.0)


@pytest.mark.asyncio
async def test_supervise_telegram_exits_on_cancel(bridge_class):
    class FakeTelegram:
        async def start(self):
            await asyncio.sleep(10)

        async def shutdown(self):
            return None

    bridge = bridge_class.__new__(bridge_class)
    bridge.logger = logging.getLogger("test_sup_cancel")
    bridge.telegram = FakeTelegram()

    sup_task = asyncio.create_task(bridge._supervise_telegram())
    await asyncio.sleep(0)
    sup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sup_task


@pytest.mark.asyncio
async def test_supervise_telegram_invokes_cleanup_between_restarts(monkeypatch, bridge_class):
    calls = {"start": 0, "cleanup": 0}

    class FakeTelegram:
        def __init__(self):
            self._stop_event = asyncio.Event()

        async def start(self):
            calls["start"] += 1
            if calls["start"] < 3:
                raise RuntimeError("boom")
            await self._stop_event.wait()

        async def restart_cleanup(self):
            calls["cleanup"] += 1

        async def shutdown(self):
            self._stop_event.set()

    _real_sleep = asyncio.sleep

    async def fake_sleep(_):
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bridge = bridge_class.__new__(bridge_class)
    bridge.logger = logging.getLogger("test_cleanup")
    bridge.telegram = FakeTelegram()

    sup_task = asyncio.create_task(bridge._supervise_telegram())
    for _ in range(50):
        if calls["start"] >= 3:
            break
        await _real_sleep(0)
    assert calls["start"] >= 3
    # cleanup runs between restarts, not on graceful exit
    assert calls["cleanup"] == 2

    await bridge.telegram.shutdown()
    await asyncio.wait_for(sup_task, timeout=1.0)


@pytest.mark.asyncio
async def test_supervise_telegram_resets_attempt_after_long_success(monkeypatch, bridge_class):
    import tgmesh_bridge as mod
    import types

    sleeps: list[int] = []
    _real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        sleeps.append(delay)
        await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # 1st crash: ran for 90s (>= threshold) -> attempt resets to 0 -> delay = 1
    # 2nd crash: ran for 1s (no reset) -> attempt = 1 -> delay = 2
    # Extra values cover the 3rd iteration (graceful exit) and any teardown calls.
    tick_values = [0.0, 90.0, 90.0, 91.0, 91.0, 91.0, 200.0, 201.0, 300.0, 301.0]
    tick_idx = {"i": 0}

    def fake_monotonic():
        idx = tick_idx["i"]
        if idx < len(tick_values):
            tick_idx["i"] += 1
            return tick_values[idx]
        # Steady value once exhausted — covers any post-test teardown calls.
        return tick_values[-1]

    # Patch time.monotonic on the tgmesh_bridge module's own 'time' reference
    # so that tgmesh_bridge.time.monotonic() uses our fake.
    fake_time = types.ModuleType("time")
    fake_time.monotonic = fake_monotonic
    monkeypatch.setattr(mod, "time", fake_time)

    starts = {"n": 0}

    class FakeTelegram:
        def __init__(self):
            self._stop_event = asyncio.Event()

        async def start(self):
            starts["n"] += 1
            if starts["n"] <= 2:
                raise RuntimeError("boom")
            await self._stop_event.wait()

        async def restart_cleanup(self):
            return None

        async def shutdown(self):
            self._stop_event.set()

    bridge = bridge_class.__new__(bridge_class)
    bridge.logger = logging.getLogger("test_reset")
    bridge.telegram = FakeTelegram()

    sup_task = asyncio.create_task(bridge._supervise_telegram())
    for _ in range(50):
        if starts["n"] >= 3:
            break
        await _real_sleep(0)
    assert starts["n"] >= 3
    # First delay after 90s success -> attempt reset to 0 -> delay = 1
    # Second delay after 1s (no reset) -> attempt = 1 -> delay = 2
    assert sleeps == [1, 2]

    await bridge.telegram.shutdown()
    await asyncio.wait_for(sup_task, timeout=1.0)
