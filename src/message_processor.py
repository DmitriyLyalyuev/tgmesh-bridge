from __future__ import annotations

import asyncio
from typing import Any
from meshtastic_interface import MeshtasticInterface
from telegram_interface import TelegramInterface
from config_manager import ConfigManager, get_logger
from storage import Storage


class MessageProcessor:
    def __init__(
        self,
        meshtastic: MeshtasticInterface,
        telegram: TelegramInterface,
        config: ConfigManager,
        storage: Storage,
    ) -> None:
        self.meshtastic = meshtastic
        self.telegram = telegram
        self.config = config
        self.storage = storage
        self.logger = get_logger(__name__)

    async def run(self) -> None:
        await asyncio.gather(
            self._mesh_to_telegram(),
            self._telegram_to_mesh(),
            self._ack_loop(),
            self._telemetry_loop(),
            self._position_loop(),
        )

    async def _mesh_to_telegram(self) -> None:
        while True:
            packet: dict[str, Any] = await self.meshtastic.text_queue.get()
            try:
                await self._handle_mesh_packet(packet)
            except Exception as e:
                self.logger.error(f"Error processing mesh packet: {e}", exc_info=True)

    async def _handle_mesh_packet(self, packet: dict[str, Any]) -> None:
        to_id: str = packet.get('toId', '')
        own = self.meshtastic.own_node_id

        is_broadcast = to_id in ('!ffffffff', '^all') or to_id == own
        if not is_broadcast and to_id != own:
            self.logger.debug(f"Dropping packet to {to_id}, not for us")
            return

        from_id: str = packet.get('fromId', 'unknown')
        text: str = packet.get('decoded', {}).get('text', '')
        packet_id: int = packet.get('id', 0)

        name_info = self.meshtastic.lookup_name(from_id)
        if name_info is None:
            name_info = self.storage.lookup_node(from_id)
        else:
            # cache fresh node data in persistent storage
            self.storage.upsert_node(from_id, name_info[0], name_info[1])

        if name_info:
            sender = f"{name_info[0]} ({name_info[1]})"
        else:
            sender = from_id

        dm_prefix = "(DM) " if to_id == own else ""
        tg_text = f"{dm_prefix}{sender}: {text}"

        reply_id_mesh: int | None = packet.get('decoded', {}).get('replyId')
        reply_to_tg: int | None = None
        if reply_id_mesh is not None:
            reply_to_tg = self.storage.tg_for_mesh(reply_id_mesh)

        tg_msg_id = await self.telegram.send_message(tg_text, reply_to_message_id=reply_to_tg)
        self.storage.add_reply(packet_id, tg_msg_id)
        if to_id == own:
            self.storage.record_dm_origin(packet_id, from_id)
        self.logger.info(f"Forwarded mesh→tg: mesh_id={packet_id} tg_id={tg_msg_id}")

    async def _telegram_to_mesh(self) -> None:
        while True:
            message: dict[str, Any] = await self.telegram.incoming_queue.get()
            try:
                await self._handle_tg_message(message)
            except Exception as e:
                self.logger.error(f"Error processing telegram message: {e}", exc_info=True)

    async def _handle_tg_message(self, message: dict[str, Any]) -> None:
        text: str = message.get('text', '')
        message_id: int = message.get('message_id', 0)
        username: str = message.get('username', 'anon')
        reply_to_tg_id: int | None = message.get('reply_to_message_id')

        mesh_text = f"[TG:{username}] {text}"

        # Determine destination: explicit from /send, or implicit from reply-to-DM
        destination: str = message.get('destination') or '^all'
        reply_id_mesh: int | None = None
        if reply_to_tg_id is not None:
            reply_id_mesh = self.storage.mesh_for_tg(reply_to_tg_id)
            if reply_id_mesh is not None:
                self.logger.info(
                    f"TG reply detected: reply_to_tg_id={reply_to_tg_id} → mesh_reply_id={reply_id_mesh}"
                )
                # If replying to an incoming DM and no explicit destination set, route back to that node
                if destination == '^all':
                    dm_node = self.storage.dm_origin(reply_id_mesh)
                    if dm_node is not None:
                        destination = dm_node

        try:
            mesh_id = await self.meshtastic.send_text(
                mesh_text, reply_id=reply_id_mesh, destination=destination
            )
            self.storage.add_reply(mesh_id, message_id)
            self.storage.record_outbound(mesh_id, destination)
            await self.telegram.set_status(message_id, '🤝')
            self.logger.info(
                f"Forwarded tg→mesh: tg_id={message_id} mesh_id={mesh_id} "
                f"reply_id={reply_id_mesh} destination={destination}"
            )
        except Exception as e:
            self.logger.error(f"Failed to send tg→mesh: {e}", exc_info=True)
            await self.telegram.set_status(message_id, '😢')

    async def _telemetry_loop(self) -> None:
        while True:
            packet: dict[str, Any] = await self.meshtastic.telemetry_queue.get()
            try:
                self._handle_telemetry_packet(packet)
            except Exception as e:
                self.logger.error(f"Error in telemetry loop: {e}", exc_info=True)

    def _handle_telemetry_packet(self, packet: dict[str, Any]) -> None:
        from_id: str = packet.get('fromId', '')
        if not from_id:
            return
        telemetry = packet.get('decoded', {}).get('telemetry', {})
        env = telemetry.get('environmentMetrics')
        if not env:
            return  # device/power metrics — ignore
        self.storage.upsert_telemetry(
            node_id=from_id,
            temperature=env.get('temperature'),
            humidity=env.get('relativeHumidity'),
            pressure=env.get('barometricPressure'),
            gas_resistance=env.get('gasResistance'),
        )
        self.logger.debug(f"Stored env telemetry for {from_id}")

    async def _position_loop(self) -> None:
        while True:
            packet: dict[str, Any] = await self.meshtastic.position_queue.get()
            try:
                self._handle_position_packet(packet)
            except Exception as e:
                self.logger.error(f"Error in position loop: {e}", exc_info=True)

    def _handle_position_packet(self, packet: dict[str, Any]) -> None:
        from_id: str = packet.get('fromId', '')
        if not from_id:
            return
        pos = packet.get('decoded', {}).get('position', {})
        lat = pos.get('latitude')
        lon = pos.get('longitude')
        alt = pos.get('altitude')
        if lat is None or lon is None:
            return
        self.storage.upsert_position(from_id, lat, lon, alt)
        self.logger.debug(f"Stored position for {from_id}: {lat},{lon}")

    async def _ack_loop(self) -> None:
        while True:
            event: dict[str, Any] = await self.meshtastic.ack_queue.get()
            try:
                request_id: int = event.get('request_id', 0)
                is_failure: bool = event.get('is_failure', False)
                error_reason: str = event.get('error_reason', 'NONE')
                tg_id = self.storage.tg_for_mesh(request_id)
                if tg_id is None:
                    self.logger.debug(f"ACK for unknown mesh_id {request_id}, ignoring")
                    continue
                destination = self.storage.outbound_destination(request_id) or '^all'
                is_broadcast = destination in ('^all', '!ffffffff')
                if is_broadcast and is_failure:
                    # Broadcast acks failing with MAX_RETRANSMIT/TIMEOUT/etc just mean
                    # neighbours did not rebroadcast — message may still have been received.
                    # Keep the 🤝 status; log it for visibility.
                    self.logger.info(
                        f"Broadcast ack reported error (ignored): mesh_id={request_id} "
                        f"tg_id={tg_id} error_reason={error_reason}"
                    )
                    continue
                emoji = '😢' if is_failure else '👌'
                await self.telegram.set_status(tg_id, emoji)
                self.logger.info(
                    f"ACK applied: mesh_id={request_id} tg_id={tg_id} "
                    f"emoji={emoji} destination={destination} error_reason={error_reason}"
                )
            except Exception as e:
                self.logger.error(f"Error in _ack_loop: {e}", exc_info=True)
