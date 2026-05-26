# План: устойчивость к сетевым сбоям

> **Для агента-исполнителя:** ОБЯЗАТЕЛЬНЫЙ САБ-СКИЛЛ — superpowers:subagent-driven-development (рекомендую) либо superpowers:executing-plans. Шаги отмечаются чекбоксами `- [ ]`.

**Цель:** bridge переживает кратковременные и длительные отсутствия интернета — Telegram-сторона восстанавливается автоматически, bridge не падает при первом сетевом блипе, Docker healthcheck реально отражает состояние сервиса.

**Архитектура:**
- Обернуть последовательность старта Telegram (`initialize` → `start` → `set_my_commands` → `start_polling`) в бесконечный цикл с backoff (по аналогии с `MeshtasticInterface._reconnect_loop`). `NetworkError`/`TimedOut` считать восстановимыми, остальное — фатальным.
- Добавить супервизор вокруг telegram-таска в `TgmeshBridge.run()`, чтобы её крэш не валил `asyncio.gather` и весь bridge.
- Зарегистрировать в PTB `error_handler`, который логирует и проглатывает сетевые исключения внутри поллинга/хендлеров.
- Починить рассинхрон пути healthcheck между кодом и Dockerfile.

**Стек:** Python 3.12, `python-telegram-bot` v21+, asyncio, pytest, Docker.

---

## Структура файлов

- **Изменить:** `src/telegram_interface.py`
  - Добавить кортеж `_RECONNECT_DELAYS` (зеркалит meshtastic).
  - Добавить функцию `_resilient_call`, оборачивающую корутины в бесконечный retry с backoff по сетевым ошибкам.
  - Переписать `start()` на использование `_resilient_call`.
  - Зарегистрировать `error_handler` в Application.
- **Изменить:** `src/tgmesh_bridge.py`
  - Заменить прямой вызов `self.telegram.start()` в `asyncio.gather` на `_supervise_telegram()`, который рестартит корутину при неожиданных исключениях с backoff.
- **Изменить:** `Dockerfile`
  - Привести healthcheck-путь в соответствие коду (`/tmp/tgmesh_bridge.ready`).
- **Создать:** `tests/test_telegram_resilience.py`
  - Тесты для `_resilient_call`: ретраит `NetworkError`/`TimedOut`, прокидывает прочие исключения, применяет backoff, завершается успехом.
- **Создать:** `tests/test_bridge_supervisor.py`
  - Тесты для супервизора: рестартит корутину при exception, применяет backoff, корректно завершается при cancel.

---

## Задача 1: Helper `_resilient_call` и константы backoff

**Файлы:**
- Изменить: `src/telegram_interface.py`
- Создать: `tests/test_telegram_resilience.py`

- [ ] **Шаг 1: Написать падающий тест**

Создать `tests/test_telegram_resilience.py`:

```python
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
```

- [ ] **Шаг 2: Настроить pytest-asyncio**

Обновить `pytest.ini`:

```
[pytest]
pythonpath = src
testpaths = tests
asyncio_mode = auto
```

Проверить, что `pytest-asyncio` есть в `requirements.txt` (или dev-секции). Если нет — добавить.

- [ ] **Шаг 3: Убедиться, что тесты падают**

```
pytest tests/test_telegram_resilience.py -v
```

Ожидание: ImportError / AttributeError на `_resilient_call` и `_RECONNECT_DELAYS`.

- [ ] **Шаг 4: Реализовать helper**

В `src/telegram_interface.py` добавить на уровень модуля (после импортов, до класса):

```python
# Backoff schedule (seconds). The last entry is reused indefinitely.
_RECONNECT_DELAYS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 300)


async def _resilient_call(label: str, fn):
    """Run an async callable forever, retrying NetworkError/TimedOut with backoff.

    Other exceptions propagate immediately.
    """
    logger = get_logger(__name__)
    attempt = 0
    while True:
        try:
            return await fn()
        except (NetworkError, TimedOut) as e:
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            logger.warning(
                f"{label} failed (attempt {attempt + 1}): {e}; retrying in {delay}s"
            )
            await asyncio.sleep(delay)
            attempt += 1
```

- [ ] **Шаг 5: Прогнать тесты**

```
pytest tests/test_telegram_resilience.py -v
```

Ожидание: все 4 теста PASS.

- [ ] **Шаг 6: Коммит**

```bash
git add src/telegram_interface.py tests/test_telegram_resilience.py pytest.ini requirements.txt
git commit -m "add resilient_call helper with backoff for telegram network errors"
```

---

## Задача 2: Использовать `_resilient_call` в старте Telegram

**Файлы:**
- Изменить: `src/telegram_interface.py:77-92` (метод `start()`)

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_telegram_resilience.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тест падает**

```
pytest tests/test_telegram_resilience.py::test_start_retries_initialize -v
```

Ожидание: FAIL — `start()` сейчас падает на первом `NetworkError`.

- [ ] **Шаг 3: Переписать `start()`**

Заменить тело `TelegramInterface.start` в `src/telegram_interface.py:77-92`:

```python
async def start(self) -> None:
    """Initialize application and run polling until stop event.

    The initialize/start/start_polling sequence is wrapped in an infinite
    backoff loop: transient network failures (DNS, timeouts) on startup or
    early polling no longer crash the bridge.
    """
    if not self.application:
        raise RuntimeError("TelegramInterface not set up")
    self.logger.info("Starting telegram polling...")

    await _resilient_call("application.initialize", self.application.initialize)
    await self.application.start()
    try:
        await _resilient_call(
            "set_my_commands",
            lambda: self.bot.set_my_commands(self._BOT_COMMANDS),
        )
        self.logger.info("Registered bot commands menu.")
    except Exception as e:
        self.logger.warning(f"Failed to set bot commands: {e}")

    await _resilient_call(
        "updater.start_polling",
        lambda: self.application.updater.start_polling(drop_pending_updates=True),
    )
    self._is_polling = True
    await self._stop_event.wait()
    await self._stop_polling()
```

- [ ] **Шаг 4: Прогнать тесты**

```
pytest tests/test_telegram_resilience.py -v
```

Ожидание: все тесты PASS.

- [ ] **Шаг 5: Коммит**

```bash
git add src/telegram_interface.py tests/test_telegram_resilience.py
git commit -m "retry telegram initialize and polling startup on network errors"
```

---

## Задача 3: PTB `error_handler` для сетевых ошибок в рантайме

**Файлы:**
- Изменить: `src/telegram_interface.py` — добавить регистрацию в `_register_handlers` и метод.

- [ ] **Шаг 1: Написать падающий тест**

Дописать в `tests/test_telegram_resilience.py`:

```python
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
```

- [ ] **Шаг 2: Убедиться, что тест падает**

```
pytest tests/test_telegram_resilience.py::test_error_handler_swallows_network_error -v
```

Ожидание: FAIL — `_on_error` отсутствует.

- [ ] **Шаг 3: Реализовать `_on_error` и зарегистрировать**

Добавить в класс `TelegramInterface`:

```python
async def _on_error(self, update, context) -> None:
    """Catch-all handler for exceptions raised in polling/handlers.

    Network errors are expected during outages; log and swallow.
    """
    err = getattr(context, "error", None)
    if isinstance(err, (NetworkError, TimedOut)):
        self.logger.warning(f"Telegram network error (swallowed): {err}")
        return
    self.logger.error(f"Unhandled telegram error: {err}", exc_info=err)
```

В `_register_handlers` после последнего `add_handler` добавить:

```python
app.add_error_handler(self._on_error)
```

- [ ] **Шаг 4: Прогнать тесты**

```
pytest tests/test_telegram_resilience.py -v
```

Ожидание: все тесты PASS.

- [ ] **Шаг 5: Коммит**

```bash
git add src/telegram_interface.py tests/test_telegram_resilience.py
git commit -m "swallow network errors in telegram error_handler instead of crashing"
```

---

## Задача 4: Супервизор telegram-таска в `TgmeshBridge.run`

**Файлы:**
- Изменить: `src/tgmesh_bridge.py:58-77` (метод `run()`)
- Создать: `tests/test_bridge_supervisor.py`

- [ ] **Шаг 1: Написать падающие тесты**

Создать `tests/test_bridge_supervisor.py`:

```python
import asyncio
import os
import sys
import logging

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


@pytest.mark.asyncio
async def test_supervise_telegram_restarts_on_exception(monkeypatch):
    from tgmesh_bridge import TgmeshBridge

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

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    bridge = TgmeshBridge.__new__(TgmeshBridge)
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
async def test_supervise_telegram_exits_on_cancel(monkeypatch):
    from tgmesh_bridge import TgmeshBridge

    class FakeTelegram:
        async def start(self):
            await asyncio.sleep(10)

        async def shutdown(self):
            return None

    bridge = TgmeshBridge.__new__(TgmeshBridge)
    bridge.logger = logging.getLogger("test_sup_cancel")
    bridge.telegram = FakeTelegram()

    sup_task = asyncio.create_task(bridge._supervise_telegram())
    await asyncio.sleep(0)
    sup_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sup_task
```

- [ ] **Шаг 2: Убедиться, что тесты падают**

```
pytest tests/test_bridge_supervisor.py -v
```

Ожидание: FAIL — `_supervise_telegram` отсутствует.

- [ ] **Шаг 3: Реализовать супервизор и использовать в `run()`**

В `src/tgmesh_bridge.py` добавить в класс `TgmeshBridge`:

```python
_TELEGRAM_RESTART_DELAYS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 300)

async def _supervise_telegram(self) -> None:
    """Run telegram.start() forever, restarting on unexpected exceptions.

    NetworkError/TimedOut are already handled inside TelegramInterface;
    anything that escapes here is a programming bug or a pathological
    condition we still want the bridge to survive.
    """
    attempt = 0
    while True:
        try:
            await self.telegram.start()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            delay = self._TELEGRAM_RESTART_DELAYS[
                min(attempt, len(self._TELEGRAM_RESTART_DELAYS) - 1)
            ]
            self.logger.error(
                f"Telegram task crashed (attempt {attempt + 1}): {e}; "
                f"restarting in {delay}s",
                exc_info=True,
            )
            await asyncio.sleep(delay)
            attempt += 1
```

Заменить блок `asyncio.gather` в `run()` (текущие строки 67-71):

```python
await asyncio.gather(
    self.processor.run(),
    self._supervise_telegram(),
    self._health_loop(),
)
```

- [ ] **Шаг 4: Прогнать тесты**

```
pytest tests/test_bridge_supervisor.py -v
```

Ожидание: все тесты PASS.

- [ ] **Шаг 5: Коммит**

```bash
git add src/tgmesh_bridge.py tests/test_bridge_supervisor.py
git commit -m "supervise telegram task so its crashes don't tear down the bridge"
```

---

## Задача 5: Починить рассинхрон путей healthcheck

**Файлы:**
- Изменить: `Dockerfile:33-34`

- [ ] **Шаг 1: Изучить текущее состояние**

Код в `src/tgmesh_bridge.py:48`:

```python
path = Path('/tmp/tgmesh_bridge.ready')
```

Текущий `Dockerfile:33-34`:

```
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD find /tmp/tgmesh-bridge.ready -mmin -2 | grep -q . || exit 1
```

Имена расходятся (`_` vs `-`). Стандартизируем на `tgmesh_bridge.ready` — этот вариант уже пишет рабочий код.

- [ ] **Шаг 2: Поправить Dockerfile**

Заменить HEALTHCHECK-строку:

```
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
    CMD find /tmp/tgmesh_bridge.ready -mmin -2 | grep -q . || exit 1
```

- [ ] **Шаг 3: Коммит**

```bash
git add Dockerfile
git commit -m "fix healthcheck path mismatch"
```

---

## Self-review

- **Покрытие:**
  - (1) Retry старта Telegram → задачи 1+2.
  - (2) Супервизор telegram-таска → задача 4.
  - (3) Сетевые ошибки в рантайме → задача 3.
  - (4) Рассинхрон healthcheck → задача 5.
- **Заглушки:** нет — все блоки кода полные.
- **Согласованность типов:** `_RECONNECT_DELAYS` и `_TELEGRAM_RESTART_DELAYS` намеренно разнесены (первая — в `telegram_interface.py`, для сетевых ретраев; вторая — в `tgmesh_bridge.py`, для рестартов любых exception). Значения сейчас идентичны, общий константный модуль — YAGNI.
