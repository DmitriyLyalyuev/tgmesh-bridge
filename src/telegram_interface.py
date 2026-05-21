from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.parse
from typing import Any, Optional
import httpx
import staticmaps
from telegram import Bot, BotCommand, Update, ReactionTypeEmoji
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from config_manager import ConfigManager, get_logger
from storage import Storage


class TelegramInterface:
    def __init__(self, config: ConfigManager, storage: Storage, meshtastic: Any = None) -> None:
        self.config: ConfigManager = config
        self.storage: Storage = storage
        self.meshtastic: Any = meshtastic
        self.logger = get_logger(__name__)
        self.bot: Optional[Bot] = None
        self.application: Optional[Application] = None
        self.incoming_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._stop_event: asyncio.Event = asyncio.Event()
        self.chat_id: int | None = None
        self._is_polling: bool = False
        self._started_at: int = int(time.time())

    async def setup(self) -> None:
        self.logger.info("Setting up telegram interface...")
        token = self.config.get('telegram.bot_token')
        if not token:
            raise ValueError("Telegram bot token not found in configuration")
        self.chat_id = self.config.get('telegram.chat_id')
        if not self.chat_id:
            raise ValueError("Telegram chat_id not found in configuration")
        self.application = (
            Application.builder()
            .token(token)
            .connect_timeout(20.0)
            .read_timeout(20.0)
            .write_timeout(20.0)
            .pool_timeout(20.0)
            .build()
        )
        self.bot = self.application.bot
        self._register_handlers()
        self.logger.info("Telegram interface set up successfully.")

    def _register_handlers(self) -> None:
        app = self.application
        app.add_handler(CommandHandler('start', self._cmd_start))
        app.add_handler(CommandHandler('help', self._cmd_help))
        app.add_handler(CommandHandler('weather', self._cmd_weather))
        app.add_handler(CommandHandler('map', self._cmd_map))
        app.add_handler(CommandHandler('send', self._cmd_send))
        app.add_handler(CommandHandler('dm', self._cmd_send))
        app.add_handler(CommandHandler('nodes', self._cmd_nodes))
        app.add_handler(CommandHandler('health', self._cmd_health))
        app.add_handler(CommandHandler('trace', self._cmd_trace))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

    _BOT_COMMANDS = [
        BotCommand("help", "Show available commands"),
        BotCommand("weather", "Aggregate env telemetry (last 1h)"),
        BotCommand("map", "Static map of nodes with positions"),
        BotCommand("nodes", "List known mesh nodes"),
        BotCommand("send", "DM a node: /send <id|shortName> <text>"),
        BotCommand("dm", "Alias for /send"),
        BotCommand("trace", "Meshtastic traceroute: /trace <id|shortName>"),
        BotCommand("health", "Bridge and mesh health"),
    ]

    async def start(self) -> None:
        """Initialize application and run polling until stop event."""
        if not self.application:
            raise RuntimeError("TelegramInterface not set up")
        self.logger.info("Starting telegram polling...")
        await self.application.initialize()
        await self.application.start()
        try:
            await self.bot.set_my_commands(self._BOT_COMMANDS)
            self.logger.info("Registered bot commands menu.")
        except Exception as e:
            self.logger.warning(f"Failed to set bot commands: {e}")
        await self.application.updater.start_polling(drop_pending_updates=True)
        self._is_polling = True
        await self._stop_event.wait()
        await self._stop_polling()

    async def shutdown(self) -> None:
        self._stop_event.set()
        if self._is_polling:
            await self._stop_polling()

    async def _stop_polling(self) -> None:
        self.logger.info("Stopping telegram polling...")
        self._is_polling = False
        if self.application:
            try:
                await self.application.updater.stop()
                await self.application.stop()
                await self.application.shutdown()
            except Exception as e:
                self.logger.error(f"Error during telegram shutdown: {e}", exc_info=True)
        self.logger.info("Telegram polling stopped.")

    async def set_status(self, message_id: int, emoji: str) -> None:
        """Set a single emoji reaction on a message. Swallows errors."""
        try:
            await self.bot.set_message_reaction(
                chat_id=self.chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            self.logger.warning(f"Failed to set reaction on {message_id}: {e}")

    async def send_message(self, text: str, reply_to_message_id: int | None = None) -> int:
        """Send a message to the configured chat. Returns the sent message_id."""
        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
        )
        return msg.message_id

    _HELP_TEXT = (
        "tgmesh-bridge active.\n"
        "Commands:\n"
        "/start, /help — this message\n"
        "/weather — env aggregate from nearby nodes (last 1h)\n"
        "/map — node positions map (last 24h)\n"
        "/send | /dm <node_id|shortName> <text> — send DM to a specific node\n"
        "/nodes — list known nodes (last 7d) with id and shortName\n"
        "/health — bridge and mesh health summary\n"
        "/trace <node_id|shortName> — meshtastic traceroute\n"
        "Reply to a (DM) message — routes reply back to that node as DM"
    )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(self._HELP_TEXT)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(self._HELP_TEXT)

    async def _cmd_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        import time
        since = int(time.time()) - 3600  # last hour
        rows = self.storage.recent_telemetry(since)
        if not rows:
            await update.message.reply_text("Нет свежих данных от нод за последний час.")
            return

        temps = [r[1] for r in rows if r[1] is not None]
        hums = [r[2] for r in rows if r[2] is not None]
        pres = [r[3] for r in rows if r[3] is not None]
        gas = [r[4] for r in rows if r[4] is not None]

        lines = [f"Погода за последний час ({len(rows)} нод):"]
        if temps:
            lines.append(
                f"🌡 Температура: {sum(temps)/len(temps):.1f}°C"
            )
        if hums:
            lines.append(
                f"💧 Влажность: {sum(hums)/len(hums):.0f}%"
            )
        if pres:
            lines.append(
                f"📊 Давление: {sum(pres)/len(pres):.0f} hPa"
            )
        if gas:
            lines.append(
                f"🌬 Gas: {sum(gas)/len(gas):.0f}"
            )

        await update.message.reply_text("\n".join(lines))

    async def _cmd_map(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        since = int(time.time()) - 86400  # 24 hours
        rows = self.storage.recent_positions(since)
        if not rows:
            await update.message.reply_text("Нет позиций нод за последние 24ч.")
            return

        try:
            ctx = staticmaps.Context()
            ctx.set_tile_provider(staticmaps.tile_provider_OSM)
            for node_id, lat, lon, alt, updated_at in rows:
                ctx.add_object(staticmaps.Marker(
                    staticmaps.create_latlng(lat, lon),
                    color=staticmaps.RED,
                    size=12,
                ))
            image = ctx.render_pillow(800, 600)
            buf = io.BytesIO()
            image.save(buf, format='PNG')
            buf.seek(0)
        except Exception as e:
            self.logger.error(f"Failed to render map: {e}", exc_info=True)
            await update.message.reply_text(f"Не удалось отрисовать карту: {e}")
            return

        lines = [f"Узлы на карте: {len(rows)} (за последние 24ч)"]
        for node_id, lat, lon, alt, updated_at in rows[:20]:
            name_info = self.storage.lookup_node(node_id)
            name = f"{name_info[1]}" if name_info else node_id
            age_min = (int(time.time()) - updated_at) // 60
            lines.append(f"  {name}: {age_min} min ago")
        if len(rows) > 20:
            lines.append(f"  ... и ещё {len(rows) - 20}")

        await self.bot.send_photo(
            chat_id=self.chat_id,
            photo=buf,
            caption="\n".join(lines),
        )

        # Build an interactive geojson.io link with all markers
        features = []
        for node_id, lat, lon, alt, updated_at in rows:
            name_info = self.storage.lookup_node(node_id)
            name = name_info[1] if name_info else node_id
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"name": name, "node_id": node_id},
            })
        geojson = {"type": "FeatureCollection", "features": features}
        encoded = urllib.parse.quote(json.dumps(geojson, ensure_ascii=False))
        url = f"https://geojson.io/#data=data:application/json,{encoded}"
        # Try to shorten via TinyURL anonymous API; falls back to original
        short = await self._shorten_url(url)
        if short is None and len(url) > 3800:
            # Last-resort fallback if both shortener and direct URL won't fit
            avg_lat = sum(r[1] for r in rows) / len(rows)
            avg_lon = sum(r[2] for r in rows) / len(rows)
            short = f"https://www.openstreetmap.org/#map=11/{avg_lat:.4f}/{avg_lon:.4f}"
        link = short or url
        try:
            await self.bot.send_message(chat_id=self.chat_id, text=f"🗺 {link}")
        except Exception as e:
            self.logger.warning(f"Failed to send map link: {e}")

    async def _shorten_url(self, long_url: str) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    "https://tinyurl.com/api-create.php",
                    params={"url": long_url},
                )
            text = r.text.strip()
            if r.status_code == 200 and text.startswith("http"):
                return text
            self.logger.warning(f"TinyURL refused: status={r.status_code} body={text[:80]}")
        except Exception as e:
            self.logger.warning(f"TinyURL request failed: {e}")
        return None

    async def _cmd_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        if update.effective_chat.id != self.chat_id:
            return
        args = context.args or []
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /send <node_id|shortName> <text>\n"
                "Examples:\n"
                "  /send !9e771e38 hello\n"
                "  /send SLVR hello"
            )
            return
        target = args[0]
        text = ' '.join(args[1:])

        if target.startswith('!'):
            node_id = target
        else:
            candidates = self.storage.find_node_ids_by_short_name(target)
            if len(candidates) == 0:
                await update.message.reply_text(f"Node '{target}' not found.")
                return
            if len(candidates) > 1:
                await update.message.reply_text(
                    f"shortName '{target}' is ambiguous: {', '.join(candidates)}. Use node_id."
                )
                return
            node_id = candidates[0]

        user = update.message.from_user
        username = (user.username or user.first_name or 'anon') if user else 'anon'
        asyncio.create_task(self.set_status(update.message.message_id, '👀'))
        await self.incoming_queue.put({
            'text': text,
            'message_id': update.message.message_id,
            'username': username,
            'reply_to_message_id': None,
            'destination': node_id,
        })

    async def _cmd_nodes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        if update.effective_chat.id != self.chat_id:
            return
        since = int(time.time()) - 7 * 86400  # last 7 days
        rows = self.storage.list_nodes(since)
        if not rows:
            await update.message.reply_text("Известных нод нет (последние 7 дней пусты).")
            return
        lines = [f"Известные ноды ({len(rows)}, последние 7 дней):"]
        now = int(time.time())
        for node_id, long_name, short_name, updated_at in rows[:50]:
            age = now - updated_at
            if age < 3600:
                age_str = f"{age // 60}m"
            elif age < 86400:
                age_str = f"{age // 3600}h"
            else:
                age_str = f"{age // 86400}d"
            short = short_name or '—'
            long_ = long_name or ''
            lines.append(f"`{node_id}`  {short}  {long_}  ({age_str} ago)")
        if len(rows) > 50:
            lines.append(f"... и ещё {len(rows) - 50}")
        await update.message.reply_text("\n".join(lines), parse_mode='Markdown')

    @staticmethod
    def _format_uptime(seconds: int) -> str:
        """Format seconds as '1d 2h 3m' or shorter variants."""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        if update.effective_chat.id != self.chat_id:
            return

        connected_str = (
            "connected ✅" if self.meshtastic and self.meshtastic._connected
            else "disconnected ❌"
        )
        uptime_seconds = int(time.time()) - self._started_at
        uptime_str = self._format_uptime(uptime_seconds)

        own_node_str = "unknown"
        if self.meshtastic and self.meshtastic.interface and self.meshtastic._own_node_id:
            own_id = self.meshtastic._own_node_id
            user = self.meshtastic.get_node_user_info(own_id)
            if user:
                own_node_str = f"{own_id}  {user.get('shortName', '')}  ({user.get('longName', '')})"
            else:
                own_node_str = own_id

        total = len(self.storage.list_nodes(0))
        heard_1h = len(self.storage.list_nodes(int(time.time()) - 3600))
        db_path = self.storage._path

        lines = [
            "🩺 tgmesh-bridge health",
            f"Bridge: {connected_str} | uptime {uptime_str}",
            f"Own node: {own_node_str}",
            f"Mesh nodes known: {total} (heard last 1h: {heard_1h})",
            f"Storage: {db_path}",
        ]
        await update.message.reply_text("\n".join(lines))

    async def _cmd_trace(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        if update.effective_chat.id != self.chat_id:
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /trace <node_id|shortName>")
            return

        target = args[0]
        if target.startswith('!'):
            node_id = target
        else:
            candidates = self.storage.find_node_ids_by_short_name(target)
            if len(candidates) == 0:
                await update.message.reply_text(f"Node '{target}' not found.")
                return
            if len(candidates) > 1:
                await update.message.reply_text(
                    f"shortName '{target}' is ambiguous: {', '.join(candidates)}. Use node_id."
                )
                return
            node_id = candidates[0]

        if not (self.meshtastic and self.meshtastic._connected):
            await update.message.reply_text("Meshtastic disconnected")
            return

        await update.message.reply_text(f"Tracing route to {node_id}…")
        try:
            info = await self.meshtastic.send_traceroute(node_id, hop_limit=7, timeout=45.0)
        except TimeoutError as e:
            await update.message.reply_text(f"⌛ {e}")
            return
        except Exception as e:
            self.logger.error(f"Trace failed: {e}", exc_info=True)
            await update.message.reply_text(f"Trace failed: {e}")
            return

        lines = self._format_traceroute(info)
        await update.message.reply_text("\n".join(lines))

    def _format_traceroute(self, info: dict[str, Any]) -> list[str]:
        def name_for(node_id_str: str) -> str:
            n = self.meshtastic.lookup_name(node_id_str) if self.meshtastic else None
            if n:
                return f"{n[1]} ({node_id_str})"
            n = self.storage.lookup_node(node_id_str)
            if n:
                return f"{n[1]} ({node_id_str})"
            return node_id_str

        def hop(node_num: int, snr_q: int | None) -> str:
            label = name_for(self.meshtastic.node_num_to_id(node_num))
            if snr_q is None:
                return label
            return f"{label} ({snr_q / 4:.1f}dB)"

        snr_towards = info.get('snr_towards') or []
        route = info.get('route') or []
        snr_back = info.get('snr_back') or []
        route_back = info.get('route_back') or []
        src = info.get('from')
        dst = info.get('to')

        out: list[str] = []
        if src is not None:
            # Forward direction: us → … → target. Final entry of snr_towards is target's SNR.
            forward = [name_for(self.meshtastic.node_num_to_id(dst))] if dst else []
            for idx, n in enumerate(route):
                snr = snr_towards[idx] if idx < len(snr_towards) else None
                forward.append(hop(n, snr))
            tail_snr = snr_towards[-1] if snr_towards else None
            forward.append(hop(src, tail_snr))
            out.append("Route towards destination:")
            out.append("  " + " → ".join(forward))
        if route_back or snr_back:
            back = [name_for(self.meshtastic.node_num_to_id(src))] if src else []
            for idx, n in enumerate(route_back):
                snr = snr_back[idx] if idx < len(snr_back) else None
                back.append(hop(n, snr))
            tail_snr = snr_back[-1] if snr_back else None
            back.append(hop(dst, tail_snr))
            out.append("Route back to us:")
            out.append("  " + " → ".join(back))
        if not out:
            out.append("Empty traceroute response.")
        return out

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None:
            return
        # Drop messages from other chats
        if update.effective_chat.id != self.chat_id:
            return
        user = update.message.from_user
        username = (user.username or user.first_name or 'anon') if user else 'anon'
        reply_to_id: int | None = None
        if update.message.reply_to_message:
            reply_to_id = update.message.reply_to_message.message_id
        asyncio.create_task(self.set_status(update.message.message_id, '👀'))
        await self.incoming_queue.put({
            'text': update.message.text,
            'message_id': update.message.message_id,
            'username': username,
            'reply_to_message_id': reply_to_id,
            'destination': None,
        })
