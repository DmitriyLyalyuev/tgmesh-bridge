# 🌐 tgmesh-bridge

A two-way bridge between a [Meshtastic](https://meshtastic.org) mesh and a Telegram chat.
Messages on the mesh primary channel show up in your Telegram group; messages in the group
go back out to the mesh. Direct messages, replies, weather telemetry and a map of nearby
nodes work out of the box.

## ✨ Features

- 💬 **Two-way text bridge** between a Telegram chat and Meshtastic primary channel (channel 0)
- 📨 **Direct messages**: `/send` to a specific node, or just reply in Telegram to a `(DM …)` message — the bridge routes it back as a DM
- 🔁 **Reply threading** in both directions (Telegram replies ↔ mesh `replyId`)
- ✅ **Delivery status reactions** — every Telegram message you send gets a reaction reflecting its mesh delivery state
- 🌡️ `/weather` — aggregate environment telemetry (temperature / humidity / pressure / gas) from nearby nodes
- 🗺️ `/map` — rendered OSM map of nodes with positions + a shortened interactive `geojson.io` link
- 📒 `/nodes` — list known nodes with id and shortName for use with `/send`
- 💾 **Persistence**: SQLite keeps reply mappings, NodeDB, telemetry, positions across restarts
- ♻️ **Auto-reconnect** to the Meshtastic node with exponential backoff
- 🩺 Docker `HEALTHCHECK` so orchestrators can detect a stuck bridge

## 🚀 Quick start (Docker)

```bash
git clone https://github.com/DmitriyLyalyuev/tgmesh-bridge.git
cd tgmesh-bridge

cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=-1001234567890
MESHTASTIC_HOST=192.168.1.234
EOF

docker compose up -d
docker compose logs -f tgmesh-bridge
```

That's it. The image is pulled from GHCR; SQLite lives in a named volume and survives upgrades:

```bash
docker compose pull
docker compose up -d
```

## 🤖 Telegram commands

| Command | What it does |
|---------|--------------|
| `/start`, `/help` | Show the command list |
| `/send <node_id\|shortName> <text>` | Send a DM to a specific mesh node |
| `/nodes` | List known mesh nodes (last 7 days) with node id and shortName |
| `/weather` | Aggregate of environment telemetry from nearby nodes (last 1h) |
| `/map` | Static OSM map of nodes that broadcast position (last 24h) + interactive link |

**Plain text** sent into the chat is broadcast to the mesh primary channel.

**Replies** in Telegram are routed intelligently:
- Reply to a regular message → broadcast on channel 0, with a mesh `replyId` pointing at the original packet
- Reply to a `(DM …)` message → sent as a DM back to that node

## ✅ Delivery status

Every Telegram message you send gets a reaction showing its mesh delivery state:

| Reaction | Meaning |
|----------|---------|
| 👀 | Received by the bot |
| 🤝 | Sent to the mesh |
| 👌 | Acknowledged by the mesh (ACK received) |
| 😢 | Error |

`👌` appears for DM acks. Broadcast acks that report `MAX_RETRANSMIT` / `TIMEOUT` etc. are ignored — they mean "no neighbour rebroadcast", not "nobody received".

## ⚙️ Configuration

All configuration is via environment variables — set them in `.env`. Defaults live in
`docker-compose.yml`:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | yes | — | Target chat or group ID (negative for supergroups) |
| `MESHTASTIC_HOST` | yes | — | Meshtastic node hostname or IP |
| `MESHTASTIC_PORT` | no | `4403` | Meshtastic TCP API port |
| `LOG_LEVEL` | no | `info` | App log level |
| `LOG_LEVEL_TELEGRAM` | no | `warn` | Log level for the Telegram library |
| `LOG_LEVEL_HTTPX` | no | `warn` | Log level for the HTTPX client |

Logs go to stdout — read with `docker compose logs`.

## 📦 Image

Published to GHCR on every push to `master` and every `v*` tag:

- `ghcr.io/dmitriylyalyuev/tgmesh-bridge:latest` — latest master
- `ghcr.io/dmitriylyalyuev/tgmesh-bridge:vX.Y.Z` — tagged release
- `ghcr.io/dmitriylyalyuev/tgmesh-bridge:sha-<short>` — pinned to a commit

Platform: `linux/amd64`.

## 🧰 Requirements

If you want to run without Docker:

- Python 3.12+
- A Meshtastic node reachable over TCP (port 4403 by default)
- A Telegram bot token and a chat where the bot is a member

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export MESHTASTIC_HOST=...
python src/tgmesh_bridge.py
```

In this mode the SQLite database is created in the current directory (`/data/tgmesh-bridge.db`
inside the container; adjust if needed).

## 🏗️ Architecture

```
Meshtastic node ─TCP 4403─▶ MeshtasticInterface ─text/pos/telemetry queues─▶ MessageProcessor ─▶ TelegramInterface ─HTTPS─▶ Telegram Bot API
                                  ▲                                                │
                                  └──────────────── outgoing sendText ─────────────┘

                                            SQLite (/data/tgmesh-bridge.db)
                                  └─ reply_mapping, nodes, telemetry, positions,
                                     dm_origin, outbound
```

Six tables, three Telegram primitives (commands, messages, reactions), one TCP socket
to the mesh. No background workers, no external services beyond `tinyurl` (used to
shorten the `/map` link).

## 🧪 Tests

```bash
pip install pytest 'prospector[with_everything]'
PYTHONPATH=src pytest tests/ -q
prospector
```

GitHub Actions runs the same checks on every push and gates the Docker image build on
their success.

## 🛣️ Roadmap

See [`docs/plans/backlog.md`](docs/plans/backlog.md).

## 📜 License

MIT — see [`LICENSE`](LICENSE).
