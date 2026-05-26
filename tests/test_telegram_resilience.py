import asyncio
import os
import sys

import pytest
from telegram.error import NetworkError, TimedOut

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.mark.asyncio
async def test_resilient_call_retries_on_network_error(monkeypatch):
    from telegram_interface import _resilient_call

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkError("boom")
        return "ok"

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await _resilient_call("test", flaky)
    assert result == "ok"
    assert calls["n"] == 3
    assert sleeps[0] == 1
    assert sleeps[1] == 2


@pytest.mark.asyncio
async def test_resilient_call_retries_on_timed_out(monkeypatch):
    from telegram_interface import _resilient_call

    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimedOut("slow")
        return "ok"

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await _resilient_call("test", flaky)
    assert result == "ok"


@pytest.mark.asyncio
async def test_resilient_call_propagates_non_network_errors(monkeypatch):
    from telegram_interface import _resilient_call

    async def boom():
        raise ValueError("fatal")

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(ValueError):
        await _resilient_call("test", boom)


@pytest.mark.asyncio
async def test_resilient_call_caps_backoff(monkeypatch):
    from telegram_interface import _resilient_call, _RECONNECT_DELAYS

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] <= len(_RECONNECT_DELAYS) + 2:
            raise NetworkError("nope")
        return "done"

    result = await _resilient_call("test", flaky)
    assert result == "done"
    assert sleeps[-1] == _RECONNECT_DELAYS[-1]


@pytest.mark.asyncio
async def test_start_retries_initialize(monkeypatch):
    """TelegramInterface.start retries initialize() on NetworkError."""
    from telegram_interface import TelegramInterface

    ti = TelegramInterface.__new__(TelegramInterface)
    ti.logger = __import__("logging").getLogger("test")
    ti._stop_event = asyncio.Event()
    ti._is_polling = False

    init_calls = {"n": 0}

    async def _async_noop(*_a, **_k):
        return None

    class FakeUpdater:
        async def start_polling(self, **_):
            return None

        async def stop(self):
            return None

    class FakeApp:
        def __init__(self):
            self.updater = FakeUpdater()
            self.bot = type("B", (), {
                "set_my_commands": staticmethod(_async_noop)
            })()

        async def initialize(self):
            init_calls["n"] += 1
            if init_calls["n"] < 3:
                from telegram.error import NetworkError
                raise NetworkError("dns")

        async def start(self):
            return None

        async def stop(self):
            return None

        async def shutdown(self):
            return None

    ti.application = FakeApp()
    ti.bot = ti.application.bot

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async def stopper():
        await asyncio.sleep(0)
        ti._stop_event.set()

    await asyncio.gather(ti.start(), stopper())
    assert init_calls["n"] == 3


@pytest.mark.asyncio
async def test_error_handler_swallows_network_error(caplog):
    from telegram.error import NetworkError
    from telegram_interface import TelegramInterface
    import logging

    ti = TelegramInterface.__new__(TelegramInterface)
    ti.logger = logging.getLogger("test_eh")

    class FakeCtx:
        error = NetworkError("flap")

    caplog.set_level(logging.WARNING, logger="test_eh")
    await ti._on_error(update=None, context=FakeCtx())
    assert any(
        "NetworkError" in rec.message or "flap" in rec.message
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_restart_cleanup_resets_polling_and_calls_lifecycle(caplog):
    from telegram_interface import TelegramInterface
    import logging

    ti = TelegramInterface.__new__(TelegramInterface)
    ti.logger = logging.getLogger("test_rc")
    ti._is_polling = True

    calls: list[str] = []

    class FakeUpdater:
        async def stop(self):
            calls.append("updater.stop")

    class FakeApp:
        def __init__(self):
            self.updater = FakeUpdater()

        async def stop(self):
            calls.append("application.stop")

        async def shutdown(self):
            calls.append("application.shutdown")

    ti.application = FakeApp()
    await ti.restart_cleanup()
    assert calls == ["updater.stop", "application.stop", "application.shutdown"]
    assert ti._is_polling is False


@pytest.mark.asyncio
async def test_restart_cleanup_tolerates_failures(caplog):
    from telegram_interface import TelegramInterface
    import logging

    ti = TelegramInterface.__new__(TelegramInterface)
    ti.logger = logging.getLogger("test_rc2")
    ti._is_polling = True

    class FakeUpdater:
        async def stop(self):
            raise RuntimeError("updater dead")

    class FakeApp:
        def __init__(self):
            self.updater = FakeUpdater()

        async def stop(self):
            raise RuntimeError("app dead")

        async def shutdown(self):
            return None

    ti.application = FakeApp()
    caplog.set_level(logging.WARNING, logger="test_rc2")
    await ti.restart_cleanup()  # must not raise
    assert ti._is_polling is False
