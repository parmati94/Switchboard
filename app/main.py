"""FastAPI surface.

Two credentials, two scopes:

- The bus bootstrap secret registers an agent. That is all it can do.
- An `sb_live_` agent key does everything else, and resolves to exactly one
  agent on exactly one bus.

A request cannot name a bus, and it cannot name a sender. Both come from the
key, so an agent can neither reach a bus it wasn't invited to nor post as
somebody else.
"""

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .briefing import briefing_json, briefing_markdown
from .config import settings
from .db import Database, default_avatar_url, new_agent_key
from .egress import Egress, NoWebhookConfigured, ensure_agent_webhook
from .gateway import Gateway
from .models import (
    KINDS,
    MessagesResponse,
    RegisterRequest,
    RegisterResponse,
    RosterResponse,
    SayRequest,
    SayResponse,
)

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
log = logging.getLogger("switchboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.connect()

    egress = Egress(settings)
    await egress.start()

    gateway = Gateway(settings, db, egress)
    await gateway.start()

    app.state.db = db
    app.state.egress = egress
    app.state.gateway = gateway
    try:
        yield
    finally:
        await gateway.stop()
        await egress.close()
        await db.close()


app = FastAPI(title="Switchboard", version=__version__, lifespan=lifespan)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing credential. Send 'Authorization: Bearer <your agent key>'. "
                "Register first with POST /register if you don't have one."
            ),
        )
    return header[7:].strip()


async def require_agent(request: Request) -> tuple[dict, dict]:
    """Resolve an agent key to (agent, bus). The tenancy boundary."""
    found = await request.app.state.db.agent_for_key(_bearer(request))
    if not found:
        raise HTTPException(
            status_code=403,
            detail="Unknown, revoked, or rotated agent key. Register again.",
        )
    agent, bus = found
    await request.app.state.db.touch_agent(bus["bus_id"], agent["agent_id"])
    return agent, bus


@app.get("/", response_class=PlainTextResponse)
async def briefing(request: Request):
    """The front door. No auth — the bootstrap secret gates joining, not reading."""
    base = settings.public_url.rstrip("/")
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(briefing_json(base))
    return PlainTextResponse(
        briefing_markdown(base), media_type="text/markdown; charset=utf-8"
    )


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """503 until the gateway is connected. Says nothing about individual buses."""
    snapshot = request.app.state.gateway.snapshot()
    stats = await request.app.state.db.global_stats()
    return JSONResponse(
        {"service": "switchboard", "version": __version__, **snapshot, **stats},
        status_code=200 if snapshot["ready"] else 503,
    )


@app.post("/register", response_model=RegisterResponse, status_code=201)
async def register(request: Request, body: RegisterRequest) -> RegisterResponse:
    """Bootstrap secret in, agent identity out.

    Re-registering an existing name rotates that agent's key, so an agent that
    lost its credential can recover without an admin.
    """
    db = request.app.state.db
    gateway = request.app.state.gateway
    egress = request.app.state.egress

    bus = await db.bus_for_secret(body.secret)
    if not bus:
        raise HTTPException(
            status_code=403, detail="Unknown, rotated, or disabled bootstrap secret."
        )

    channel = gateway.client.get_channel(int(bus["channel_id"]))
    if channel is None:
        raise HTTPException(
            status_code=503,
            detail="Gateway is still connecting or cannot see the bus channel. Retry.",
        )

    key = new_agent_key()
    avatar = body.avatar_url or default_avatar_url(body.name)
    await db.register_agent(
        bus_id=bus["bus_id"], agent_id=body.name, key=key, avatar_url=avatar
    )

    try:
        webhook_url = await ensure_agent_webhook(
            gateway.client, db, settings, channel, bus, body.name
        )
    except Exception:  # noqa: BLE001 - fall back rather than fail registration
        log.exception("agent webhook provisioning failed for %r", body.name)
        webhook_url = None

    # Announce through the agent's own webhook so the human sees who arrived
    # and what they look like. Best-effort: a failed hello must not fail a
    # registration that otherwise succeeded.
    try:
        await egress.send(
            webhook_url=webhook_url or bus["webhook_url"],
            text=f"**{body.name}** joined the bus.",
            username=body.name,
            avatar_url=avatar,
        )
    except Exception:  # noqa: BLE001
        log.warning("join announcement failed for %r", body.name, exc_info=True)

    log.info("agent %r registered on bus %s", body.name, bus["bus_id"])
    return RegisterResponse(
        agent_id=body.name,
        bus_id=bus["bus_id"],
        bus={"guild": bus["guild_name"], "channel": bus["channel_name"]},
        key=key,
        avatar_url=avatar,
        own_webhook=webhook_url is not None,
        roster=await db.roster(bus["bus_id"]),
        protocol={
            "address_with": "@name:",
            "max_message_chars": 2000,
            "kinds": list(KINDS),
        },
    )


@app.get("/roster", response_model=RosterResponse)
async def roster(
    request: Request, identity: tuple[dict, dict] = Depends(require_agent)
) -> RosterResponse:
    _, bus = identity
    return RosterResponse(
        bus_id=bus["bus_id"], agents=await request.app.state.db.roster(bus["bus_id"])
    )


@app.get("/messages", response_model=MessagesResponse)
async def messages(
    request: Request,
    after: int = Query(0, ge=0, description="Highest seq already seen."),
    limit: int = Query(50, ge=1, le=200),
    conversation_id: str | None = Query(None),
    identity: tuple[dict, dict] = Depends(require_agent),
) -> MessagesResponse:
    _, bus = identity
    db = request.app.state.db
    rows = await db.messages_after(
        bus["bus_id"], after=after, limit=limit, conversation_id=conversation_id
    )
    stats = await db.bus_stats(bus["bus_id"])
    return MessagesResponse(
        messages=rows,
        head_seq=stats["head_seq"],
        next_after=rows[-1]["seq"] if rows else after,
    )


@app.post("/say", response_model=SayResponse)
async def say(
    request: Request,
    body: SayRequest,
    identity: tuple[dict, dict] = Depends(require_agent),
) -> SayResponse:
    agent, bus = identity
    egress = request.app.state.egress
    db = request.app.state.db
    name = agent["agent_id"]

    if body.kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(KINDS)}")

    # An agent with no webhook of its own shares the bus webhook — still a
    # distinct identity in the channel, just not individually revocable.
    webhook_url = agent.get("webhook_url") or bus["webhook_url"]
    if not webhook_url:
        raise HTTPException(
            status_code=503,
            detail=(
                "This bus has no webhook. The bot likely lacks Manage Webhooks in "
                "the channel — re-run /switchboard enable after granting it."
            ),
        )

    conversation_id = body.conversation_id or f"c_{secrets.token_hex(3)}"
    prior = await db.messages_after(
        bus["bus_id"], after=0, limit=200, conversation_id=conversation_id
    )
    depth = len(prior) + 1

    try:
        message_ids = await egress.send(
            webhook_url=webhook_url,
            text=body.text,
            username=name,
            avatar_url=agent.get("avatar_url"),
            footer=f"{conversation_id} · turn {depth}",
        )
    except NoWebhookConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface Discord failures as 502
        log.exception("egress failed for bus %s", bus["bus_id"])
        raise HTTPException(
            status_code=502, detail=f"Discord rejected the send: {exc}"
        ) from exc

    seq = 0
    for message_id in message_ids:
        seq = await db.record_sent_metadata(
            bus_id=bus["bus_id"],
            discord_id=message_id,
            channel_id=bus["channel_id"],
            author_name=name,
            content=body.text,
            conversation_id=conversation_id,
            to_agents=body.to,
            depth=depth,
            reply_to=body.reply_to,
            kind=body.kind,
        )

    return SayResponse(
        ok=True,
        message_ids=message_ids,
        conversation_id=conversation_id,
        chunks=len(message_ids),
        seq=seq,
    )


@app.delete("/me")
async def deregister(
    request: Request, identity: tuple[dict, dict] = Depends(require_agent)
) -> dict:
    agent, bus = identity
    db = request.app.state.db
    revoked = await db.revoke_agent(bus["bus_id"], agent["agent_id"])
    if revoked and revoked.get("webhook_url"):
        await request.app.state.egress.delete_webhook(revoked["webhook_url"])
    return {"ok": True, "agent_id": agent["agent_id"], "bus_id": bus["bus_id"]}
