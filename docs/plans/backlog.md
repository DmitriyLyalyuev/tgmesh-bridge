# Roadmap

Features and improvements that aren't on the current path. Nothing here is
committed — open an issue or grab one if it sounds interesting.

## Bridging

- **Multi-channel support.** Today the bridge speaks only to mesh channel 0.
  Add a mapping from mesh channels to Telegram chats or forum topics so a
  single bridge can carry traffic from several mesh channels in parallel.
- **Reactions on mesh→tg messages.** The bot already reacts to outgoing
  Telegram messages with delivery state. The mirror — bot reactions on
  incoming mesh messages to denote ACK echo / signal strength / delivery
  hops — is missing.
- **Long-message splitting.** Mesh text payload caps at ~230 bytes; we
  currently truncate to 220 chars + `…`. Splitting into `[1/3]` `[2/3]`
  `[3/3]` fragments would let users send longer Telegram messages.
- **Position-only "node moved" notifications.** Optional small alert in the
  TG chat when a node's position changes by more than N meters.

## DM ergonomics

- **Per-node Telegram forum topics.** Already-explored alternative to the
  current single-chat flat model. Auto-create a forum topic per active node
  on first DM and route all subsequent messages in that topic to the
  corresponding mesh node. Requires the Telegram chat to be a supergroup
  with topics enabled and the bot to have `manage_topics` permission.
  Decided against for now to keep the UX simple — `/send`, `/dm` and reply
  routing cover the common cases.

## Out of scope (most likely never)

- Matrix transport. The codebase mirrors several ideas from
  `meshtastic-matrix-relay`, but the chosen transport is Telegram only.
- E2EE for the Telegram side. Telegram bots can't read end-to-end secret
  chats anyway.
- Community plugin system with arbitrary code loading. Adds security surface
  and operational complexity disproportionate to the use case.
