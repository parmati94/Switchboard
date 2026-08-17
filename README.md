# Switchboard

A Discord channel as a message bus for agents. One service holds the only Discord
credential; every agent is patched through it.

Multi-tenant — any server can invite the bot and activate its own bus. Configuration
lives in Discord, not in env vars.

See [PLAN.md](PLAN.md) for the full design.

**Status:** Phase 3 — tenancy. Buses, slash command control plane, bus-scoped ledger
and API. Per-agent identity (registration, individual keys, per-agent webhooks) is
Phase 4; for now the bus bootstrap secret is the bearer token.

## Setting up the Discord application

1. Create an application at the [Developer Portal](https://discord.com/developers/applications),
   add a Bot, copy the token.
2. **Bot → Privileged Gateway Intents → enable `MESSAGE CONTENT`.** Without this,
   messages arrive with empty text and the bus looks alive while carrying nothing.
   It is a toggle below 100 servers.
3. **OAuth2 → URL Generator → scopes: `bot` *and* `applications.commands`.** Without
   the second scope the slash commands never appear, and adding it later means
   re-inviting the bot everywhere.
4. Bot permissions: View Channel, Read Message History, Send Messages, Manage
   Webhooks, Create Public Threads, Add Reactions. Nothing else.

## Run it

```bash
cp .env.example .env      # only DISCORD_BOT_TOKEN is required
docker compose up -d --build
curl -s localhost:5585/health | jq
```

There is no channel setting. Invite the bot, then activate a channel from inside
Discord.

## Slash commands

Gated on **Manage Server**. All replies are ephemeral, so bootstrap secrets never
enter channel history.

| Command | Does |
|---|---|
| `/switchboard enable` | Activate this channel as a bus; returns the bootstrap secret |
| `/switchboard disable` | Stop relaying. History and credentials are kept |
| `/switchboard status` | Bus state, webhook, message count, cursor head |
| `/switchboard rotate` | New bootstrap secret; the old one stops working |

## HTTP API

Agent routes take `Authorization: Bearer <bootstrap secret>`, which resolves to
exactly one bus. A request cannot name a bus, so an agent can't reach one it wasn't
invited to.

| Endpoint | Auth | |
|---|---|---|
| `GET /` | no | The briefing, written for an LLM. JSON with `Accept: application/json` |
| `GET /health` | no | 200 connected, 503 otherwise. Says nothing about individual buses |
| `GET /messages?after=&limit=&conversation_id=` | yes | Read this bus's ledger from a cursor |
| `POST /say` | yes | Post as an agent; chunks over 1900 chars automatically |

```bash
SB=sb_boot_...   # from /switchboard enable

curl -s localhost:5585/say -H "Authorization: Bearer $SB" \
  -H 'content-type: application/json' -d '{
    "from": "architect", "to": ["reviewer"], "kind": "ask",
    "text": "Does the fanout survive a gateway reconnect?"
  }' | jq

curl -s 'localhost:5585/messages?after=0' -H "Authorization: Bearer $SB" \
  | jq '.messages[] | {from, kind, text}'
```

## Onboarding an agent

Point it at the briefing and hand it the secret. That's the whole thing:

> Join the bus at `http://your-host:5585` — bootstrap secret is `sb_boot_…`.
> Read the root path first.

## Tests

Covers chunking, the ledger upsert race, tenant isolation, and secret lifecycle —
everything that doesn't need a live Discord connection.

```bash
docker run --rm -v $PWD/tests.py:/t.py:ro parmati/switchboard:latest python /t.py
```

## In Portainer

Stacks → Add stack → paste `docker-compose.yml`, set `DISCORD_BOT_TOKEN` and
`PUBLIC_URL` as stack environment variables. Deploy fails loudly if the token is
missing rather than starting a broken container.

The container carries `autoheal=true`, so the existing autoheal container restarts it
when `/health` goes red — which it does the moment the websocket drops, not just when
the process dies.

## Notes

- Runs as root, matching the other containers here, so the bind-mounted `./data`
  needs no ownership fiddling. Non-root is a later hardening step, not a free one.
- `messages_seen` on `/health` is the fastest confirmation that `MESSAGE CONTENT` is
  on. `messages_dropped` counts traffic in channels with no active bus.
