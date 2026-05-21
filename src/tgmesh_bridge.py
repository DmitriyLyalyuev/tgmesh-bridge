import asyncio
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
                self.telegram.start(),
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
