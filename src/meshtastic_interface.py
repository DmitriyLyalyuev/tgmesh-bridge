from __future__ import annotations

import asyncio
from typing import Any, Optional
from meshtastic import tcp_interface
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub
from config_manager import ConfigManager, get_logger


class MeshtasticInterface:
    def __init__(self, config: ConfigManager) -> None:
        self.config: ConfigManager = config
        self.logger = get_logger(__name__)
        self.interface: Optional[TCPInterface] = None
        self.text_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.ack_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.telemetry_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.position_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._own_node_id: Optional[str] = None
        self._connected: bool = False
        self._reconnect_task: Optional[asyncio.Task] = None

    async def setup(self) -> None:
        self.logger.info("Setting up meshtastic interface...")
        self._loop = asyncio.get_running_loop()
        host: str = self.config.get('meshtastic.host')
        port: int = self.config.get('meshtastic.port', 4403)
        self.interface = await asyncio.to_thread(
            tcp_interface.TCPInterface, hostname=host, portNumber=port
        )
        node_info = await asyncio.to_thread(self.interface.getMyNodeInfo)
        self._own_node_id = node_info['user']['id']
        self.logger.info(f"Own node id: {self._own_node_id}")
        self._connected = True
        pub.subscribe(self._on_receive_text, "meshtastic.receive.text")
        pub.subscribe(self._on_receive_telemetry, "meshtastic.receive.telemetry")
        pub.subscribe(self._on_receive_position, "meshtastic.receive.position")
        pub.subscribe(self._on_connection_lost, "meshtastic.connection.lost")
        self.logger.info("Meshtastic interface setup complete.")

    async def shutdown(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        try:
            pub.unsubscribe(self._on_receive_text, "meshtastic.receive.text")
        except Exception:
            pass
        try:
            pub.unsubscribe(self._on_receive_telemetry, "meshtastic.receive.telemetry")
        except Exception:
            pass
        try:
            pub.unsubscribe(self._on_receive_position, "meshtastic.receive.position")
        except Exception:
            pass
        try:
            pub.unsubscribe(self._on_connection_lost, "meshtastic.connection.lost")
        except Exception:
            pass
        if self.interface:
            try:
                await asyncio.to_thread(self.interface.close)
            except Exception as e:
                self.logger.error(f"Error closing meshtastic interface: {e}", exc_info=True)
        self.logger.info("Meshtastic interface closed.")

    @property
    def own_node_id(self) -> str:
        if self._own_node_id is None:
            raise RuntimeError("MeshtasticInterface not set up yet")
        return self._own_node_id

    def _on_connection_lost(self, interface: Any, topic: Any = pub.AUTO_TOPIC) -> None:
        # Called from meshtastic thread — schedule disconnect handling on asyncio loop
        self.logger.warning("Meshtastic connection lost")
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._handle_disconnect)

    def _handle_disconnect(self) -> None:
        # Runs in asyncio thread
        self._connected = False
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        host: str = self.config.get('meshtastic.host')
        port: int = self.config.get('meshtastic.port', 4403)

        # Backoff sequence capped at 300 seconds
        delays = [1, 2, 4, 8, 16, 32, 64, 128, 256, 300]

        attempt = 0
        while True:
            delay = delays[min(attempt, len(delays) - 1)]
            self.logger.info(f"Reconnect attempt {attempt + 1} in {delay}s...")
            await asyncio.sleep(delay)
            attempt += 1

            # Close existing interface best-effort
            if self.interface is not None:
                try:
                    await asyncio.to_thread(self.interface.close)
                except Exception:
                    pass
                self.interface = None

            try:
                self.logger.info("Attempting reconnect to Meshtastic node...")
                new_interface = await asyncio.to_thread(
                    tcp_interface.TCPInterface, hostname=host, portNumber=port
                )
                node_info = await asyncio.to_thread(new_interface.getMyNodeInfo)
                self._own_node_id = node_info['user']['id']
                self.interface = new_interface

                # Re-subscribe to receive texts (unsubscribe first to avoid duplicates)
                try:
                    pub.unsubscribe(self._on_receive_text, "meshtastic.receive.text")
                except Exception:
                    pass
                pub.subscribe(self._on_receive_text, "meshtastic.receive.text")

                try:
                    pub.unsubscribe(self._on_receive_telemetry, "meshtastic.receive.telemetry")
                except Exception:
                    pass
                pub.subscribe(self._on_receive_telemetry, "meshtastic.receive.telemetry")

                try:
                    pub.unsubscribe(self._on_receive_position, "meshtastic.receive.position")
                except Exception:
                    pass
                pub.subscribe(self._on_receive_position, "meshtastic.receive.position")

                self._connected = True
                self._reconnect_task = None
                self.logger.info(f"Reconnected successfully. Own node id: {self._own_node_id}")
                return
            except Exception as e:
                self.logger.warning(f"Reconnect attempt {attempt} failed: {e}")

    def _on_receive_text(self, packet: dict[str, Any], interface: Any) -> None:
        # Called from meshtastic thread — push to async queue safely
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.text_queue.put_nowait, packet)

    def _on_receive_telemetry(self, packet: dict[str, Any], interface: Any) -> None:
        # Called from meshtastic thread — push to async queue safely
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.telemetry_queue.put_nowait, packet)

    def _on_receive_position(self, packet: dict[str, Any], interface: Any) -> None:
        # Called from meshtastic thread — push to async queue safely
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.position_queue.put_nowait, packet)

    def _on_send_response(self, packet: dict[str, Any]) -> None:
        # Called from meshtastic thread when ACK/NAK arrives for a sent packet
        try:
            decoded = packet.get('decoded', {})
            request_id = decoded.get('requestId') or packet.get('requestId')
            if request_id is None:
                return
            routing = decoded.get('routing', {})
            error_reason = routing.get('errorReason', 'NONE')
            is_failure = (error_reason != 'NONE')
            event = {
                'request_id': request_id,
                'is_ack': True,
                'is_failure': is_failure,
                'error_reason': error_reason,
            }
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self.ack_queue.put_nowait, event)
        except Exception as e:
            self.logger.warning(f"Error in _on_send_response: {e}")

    async def send_text(
        self,
        text: str,
        reply_id: int | None = None,
        destination: str = '^all',
    ) -> int:
        if not self._connected:
            raise RuntimeError("Meshtastic disconnected")
        if len(text) > 220:
            text = text[:220] + "…"
        result = await asyncio.to_thread(
            self.interface.sendText,
            text,
            destinationId=destination,
            channelIndex=0,
            replyId=reply_id,
            wantAck=True,
            onResponse=self._on_send_response,
        )
        packet_id: int = result.id
        self.logger.info(
            f"Sent mesh text, packet_id={packet_id} reply_id={reply_id} destination={destination}"
        )
        return packet_id

    def lookup_name(self, node_id: str) -> tuple[str, str] | None:
        """Return (longName, shortName) for a node, or None if unknown."""
        nodes = getattr(self.interface, 'nodes', {}) or {}
        node = nodes.get(node_id)
        if node is None:
            return None
        user = node.get('user', {})
        long_name = user.get('longName')
        short_name = user.get('shortName')
        if long_name and short_name:
            return (long_name, short_name)
        return None

    def get_node_user_info(self, node_id: str) -> dict | None:
        """Return raw user dict for a node (contains longName, shortName), or None."""
        nodes = getattr(self.interface, 'nodes', {}) or {}
        node = nodes.get(node_id)
        if not node:
            return None
        return node.get('user')
