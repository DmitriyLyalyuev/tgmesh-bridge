import asyncio
import time
from pathlib import Path
from typing import Optional
from meshtastic_interface import MeshtasticInterface
from telegram_interface import TelegramInterface
from message_processor import MessageProcessor
from config_manager import ConfigManager, get_logger
from storage import Storage


class TgmeshBridge:
    def __init__(self, config: ConfigManager) -> None:
        self.config: ConfigManager = config
        self.logger = get_logger(__name__)
        self.meshtastic: Optional[MeshtasticInterface] = None
        self.telegram: Optional[TelegramInterface] = None
        self.processor: Optional[MessageProcessor] = None
        self.storage: Optional[Storage] = None

    async def setup(self) -> None:
        self.logger.info("Setting up tgmesh_bridge...")
        self.storage = Storage('/data/tgmesh_bridge.db')
        self.storage.init()
        self.meshtastic = MeshtasticInterface(self.config)
        await self.meshtastic.setup()
        self.telegram = TelegramInterface(self.config, self.storage, self.meshtastic)
        await self.telegram.setup()
        self.processor = MessageProcessor(self.meshtastic, self.telegram, self.config, self.storage)
        self.logger.info("TgmeshBridge setup complete.")

    async def shutdown(self) -> None:
        self.logger.info("Shutting down tgmesh_bridge...")
        if self.telegram:
            try:
                await self.telegram.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down telegram: {e}", exc_info=True)
        if self.meshtastic:
            try:
                await self.meshtastic.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down meshtastic: {e}", exc_info=True)
        if self.storage:
            self.storage.close()
        self.logger.info("TgmeshBridge shutdown complete.")

    _TELEGRAM_RESTART_DELAYS: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128, 256, 300)
    _TELEGRAM_RESTART_RESET_AFTER_SECONDS: float = 60.0

    async def _supervise_telegram(self) -> None:
        """Run telegram.start() forever, restarting on unexpected exceptions.

        NetworkError/TimedOut are already handled inside TelegramInterface;
        anything that escapes here is a programming bug or a pathological
        condition we still want the bridge to survive.

        If start() ran longer than the reset threshold before crashing, the
        backoff counter resets so independent failures hours apart don't
        drift up to the 300s cap.
        """
        attempt = 0
        while True:
            started_at = time.monotonic()
            try:
                await self.telegram.start()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                ran_for = time.monotonic() - started_at
                if ran_for >= self._TELEGRAM_RESTART_RESET_AFTER_SECONDS:
                    attempt = 0
                delay = self._TELEGRAM_RESTART_DELAYS[
                    min(attempt, len(self._TELEGRAM_RESTART_DELAYS) - 1)
                ]
                self.logger.error(
                    f"Telegram task crashed after {ran_for:.1f}s "
                    f"(attempt {attempt + 1}): {e}; restarting in {delay}s",
                    exc_info=True,
                )
                try:
                    await self.telegram.restart_cleanup()
                except Exception as cleanup_err:
                    self.logger.warning(
                        f"restart_cleanup raised: {cleanup_err}"
                    )
                await asyncio.sleep(delay)
                attempt += 1

    async def _health_loop(self) -> None:
        path = Path('/tmp/tgmesh_bridge.ready')
        while True:
            # touch only if meshtastic is connected
            if self.meshtastic and self.meshtastic._connected:
                try:
                    path.touch()
                except Exception as e:
                    self.logger.warning(f"Healthcheck touch failed: {e}")
            await asyncio.sleep(30)

    async def run(self) -> None:
        try:
            await self.setup()
        except Exception as e:
            self.logger.error(f"Failed to set up TgmeshBridge: {e}", exc_info=True)
            return

        self.logger.info("TgmeshBridge is running.")
        try:
            await asyncio.gather(
                self.processor.run(),
                self._supervise_telegram(),
                self._health_loop(),
            )
        except asyncio.CancelledError:
            self.logger.info("Received cancellation signal.")
        except Exception as e:
            self.logger.error(f"Unexpected error: {e}", exc_info=True)
        finally:
            await self.shutdown()


async def main() -> None:
    config = ConfigManager()
    logger = get_logger(__name__)

    app = TgmeshBridge(config)
    try:
        await app.run()
    except ExceptionGroup as eg:
        for i, e in enumerate(eg.exceptions, 1):
            logger.error(f"Exception {i}: {e}", exc_info=e)
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt.")
    finally:
        await app.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
