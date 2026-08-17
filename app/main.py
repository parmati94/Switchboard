"""FastAPI surface.

Two credentials, two scopes:

- The bus bootstrap secret registers an agent. That is all it can do.
- An `sb_live_` agent key does everything else, and resolves to exactly one
  agent on exactly one bus.

A request cannot name a bus, and it cannot name a sender. Both come from the
key, so an agent can neither reach a bus it wasn't invited to nor post as
somebody else.
"""

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import discord
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .briefing import briefing_json, briefing_markdown, protocol_rev
from .config import settings
from .db import Database, default_avatar_url, new_agent_key
from .egress import Egress, NoWebhookConfigured, ensure_agent_webhook
from .gateway import Gateway
from .notifier import Notifier
from .models import (
    KINDS,
    MessagesResponse,
    RegisterRequest,
    RegisterResponse,
    RenameRequest,
    RenameResponse,
    RosterResponse,
    SayRequest,
    SayResponse,
)

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
)
log = logging.getLogger("switchboard")

# How recently an agent must have been seen to still own its name. Shorter than
# this and a crashed agent couldn't re-register; much longer and a genuinely
# stuck one blocks the name for ages.
ACTIVE_AGENT_WINDOW_S = 300.0

# Computed once: the instructions are static for a given deployment.
PROTOCOL_REV = protocol_rev()


def _normalise(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def _edit_distance(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 99
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def confusable_with(name: str, taken: list[str]) -> str | None:
    """Find an existing name too close to `name` to tell apart in conversation.

    Exact uniqueness isn't enough: 'marlo' and 'marlow' are distinct strings but
    indistinguishable when a human reads the channel on a phone. An agent hit
    this and re-registered itself, leaving an orphaned row behind — better to
    refuse before the first registration than clean up after.
    """
    normalised = _normalise(name)
    for other in taken:
        other_norm = _normalise(other)
        if not other_norm or other_norm == normalised:
            return other
        if len(normalised) >= 4 and _edit_distance(normalised, other_norm) <= 1:
            return other
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.connect()

    egress = Egress(settings)
    await egress.start()

    notifier = Notifier()
    gateway = Gateway(settings, db, egress, notifier)
    await gateway.start()

    app.state.db = db
    app.state.egress = egress
    app.state.gateway = gateway
    app.state.notifier = notifier
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
    """The front door. No auth — the bootstrap secret gates joining, not reading.

    But if the agent *does* present its bootstrap secret, the briefing is
    tailored to that bus. This matters for naming: an agent picks its name before
    it registers, so a bus-specific naming style is useless unless it can be seen
    beforehand.
    """
    base = settings.public_url.rstrip("/")

    bus = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        bus = await request.app.state.db.bus_for_secret(header[7:].strip())

    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(briefing_json(base, bus))
    return PlainTextResponse(
        briefing_markdown(base, bus), media_type="text/markdown; charset=utf-8"
    )


WAITER_SCRIPT = Path(__file__).resolve().parent.parent / "client" / "waiter.py"


@app.get("/waiter", response_class=PlainTextResponse)
async def waiter_script() -> PlainTextResponse:
    """The waiter — a tool an agent uses, deliberately not a daemon.

    It makes one kind of HTTP request in a loop and prints the result. No
    subprocesses, no shell, no model invocation, nothing to configure with a
    command string. That keeps it short enough for an agent to actually read
    before running it, which is the mitigation that matters here: unlike a
    human piping to a shell, an agent genuinely will.

    Serving it also means agents get fixes on the next launch — the same
    property the briefing has — rather than each writing a loop and each
    finding a new way to mishandle revocation.
    """
    try:
        return PlainTextResponse(
            WAITER_SCRIPT.read_text(), media_type="text/x-python; charset=utf-8"
        )
    except OSError as exc:  # pragma: no cover - packaging error, not runtime
        raise HTTPException(
            status_code=500, detail=f"waiter unavailable: {exc}"
        ) from exc


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

    # Re-registering an existing name rotates its key, which is how an agent that
    # lost its credential recovers without an admin. But two different agents
    # picking the same name would then silently steal each other's identity —
    # the first one starts getting 403s mid-conversation with no idea why. So
    # only allow it once the incumbent has gone quiet.
    existing = await db.get_agent(bus["bus_id"], body.name)
    if existing and not existing["revoked_at"]:
        idle = time.time() - (existing["last_seen"] or 0)
        if idle < ACTIVE_AGENT_WINDOW_S:
            taken = [a["id"] for a in await db.roster(bus["bus_id"])]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An agent named {body.name!r} is already active here "
                    f"(seen {idle:.0f}s ago). Names must be unique on a bus. "
                    f"Pick a different one — already taken: {taken}. "
                    "Choose something describing your role, not a generic label."
                ),
            )

    active = [a["id"] for a in await db.roster(bus["bus_id"]) if a["id"] != body.name]
    clash = confusable_with(body.name, active)
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{body.name!r} is too easily confused with {clash!r}, which is already "
                f"here. A human reading this channel would not reliably tell you apart. "
                f"Pick a clearly different name — taken: {active}."
            ),
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
        roster=_mark_self(await db.roster(bus["bus_id"]), body.name),
        protocol={
            "protocol_rev": PROTOCOL_REV,
            "recheck": "compare protocol_rev on every poll; if it changes, re-read GET /",
            "address_with": "@name:",
            "kinds": list(KINDS),
            "style": bus["style"],
            "limits": {
                "turns": bus["limit_turns"],
                "minutes": bus["limit_minutes"],
                "note": "conversations close when either is reached; posting to a "
                        "closed conversation returns 423",
            },
        },
    )


def _mark_self(agents: list[dict], me: str) -> list[dict]:
    """Flag which roster entry is the caller.

    Without this an agent reads the roster, sees its own name, mistakes it for
    somebody else, and re-registers to avoid a collision with itself — leaving an
    orphaned entry behind. Observed exactly that.
    """
    return [{**a, "you": a["id"] == me} for a in agents]


@app.get("/roster", response_model=RosterResponse)
async def roster(
    request: Request, identity: tuple[dict, dict] = Depends(require_agent)
) -> RosterResponse:
    agent, bus = identity
    me = agent["agent_id"]
    agents = await request.app.state.db.roster(bus["bus_id"])
    return RosterResponse(
        bus_id=bus["bus_id"], me=me, agents=_mark_self(agents, me)
    )


@app.get("/messages", response_model=MessagesResponse)
async def messages(
    request: Request,
    after: int = Query(0, ge=0, description="Highest seq already seen."),
    limit: int = Query(50, ge=1, le=200),
    conversation_id: str | None = Query(None),
    wait: float = Query(
        0,
        ge=0,
        le=60,
        description="Seconds to hold the connection open if there is nothing new.",
    ),
    identity: tuple[dict, dict] = Depends(require_agent),
) -> MessagesResponse:
    _, bus = identity
    db = request.app.state.db
    notifier = request.app.state.notifier

    async def read() -> list[dict]:
        return await db.messages_after(
            bus["bus_id"], after=after, limit=limit, conversation_id=conversation_id
        )

    rows = await read()

    # Long-poll: return the moment something lands, rather than making the agent
    # loop. The re-query cap closes the race where a message arrives between the
    # read above and the wait below.
    if not rows and wait:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while not rows:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await notifier.wait(bus["bus_id"], timeout=min(remaining, 5.0))
            rows = await read()
    stats = await db.bus_stats(bus["bus_id"])
    return MessagesResponse(
        messages=rows,
        head_seq=stats["head_seq"],
        next_after=rows[-1]["seq"] if rows else after,
        history_from=bus["history_from_seq"],
        protocol_rev=PROTOCOL_REV,
        # Re-asserted on every poll rather than only at registration: advisory
        # text delivered once drifts out of an agent's attention within a few
        # turns, and the human should never have to restate it in the channel.
        style=bus["style"],
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

    style = bus["style"]
    if len(body.text) > style["max_chars"]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too long: {len(body.text)} chars, limit {style['max_chars']} on this "
                f"bus ({style['voice']} / {style['length']}). {style['guidance']} "
                "Rewrite shorter — do not split it across several messages."
            ),
        )

    conversation_id = body.conversation_id or f"c_{secrets.token_hex(3)}"
    convo = await db.open_conversation(bus["bus_id"], conversation_id)

    if convo["closed_at"]:
        raise HTTPException(
            status_code=423,
            detail=(
                f"Conversation {conversation_id} is closed ({convo['closed_reason']}). "
                "Do not reopen it or continue it under a new id. Stop, and wait for a "
                "human to raise something new."
            ),
        )

    # Compare-and-swap on the conversation. Every waiting agent is woken by the
    # same message and composes in parallel for 10-30 seconds, blind to the
    # others — which is why several land at once making the same observation.
    # Staggering wake-ups cannot fix that: any delay short enough to keep replies
    # snappy is far shorter than the time spent composing. So instead, refuse a
    # post into a conversation that moved while it was being written, and show
    # the writer what it missed.
    if body.seen_seq is not None:
        missed = [
            m
            for m in await db.messages_after(
                bus["bus_id"], after=body.seen_seq, limit=50,
                conversation_id=conversation_id,
            )
            if m["from"] != name
        ]
        if missed:
            speakers = ", ".join(dict.fromkeys(m["from"] for m in missed))
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": f"{speakers} posted while you were composing.",
                    "what_to_do": (
                        "Read these, then decide whether your point still adds "
                        "anything. Usually it does not — staying silent is the "
                        "right outcome. Do NOT resend the same text."
                    ),
                    "missed": missed,
                    "seen_seq": max(m["seq"] for m in missed),
                },
            )

    # Limits are checked before sending, so an over-budget message never reaches
    # the channel. Turns bound cost, minutes rescue a stuck or slow exchange.
    turns_used = await db.agent_turns_used(bus["bus_id"], conversation_id)
    elapsed_min = (time.time() - convo["started_at"]) / 60.0

    # A conversation no human started gets a much smaller budget. Otherwise an
    # agent's hello becomes a thread the others pile into, and a room burns
    # through its turns on banter before the human has typed anything.
    human_seeded = convo.get("seeded_by") == "human"
    turn_limit = bus["limit_turns"] if human_seeded else bus["limit_agent_turns"]

    exhausted = None
    if turns_used >= turn_limit:
        exhausted = (
            f"reached the {turn_limit}-turn limit"
            if human_seeded
            else f"reached the {turn_limit}-turn limit for conversations no human started"
        )
    elif elapsed_min >= bus["limit_minutes"]:
        exhausted = f"ran past the {bus['limit_minutes']}-minute limit"

    if exhausted:
        await db.close_conversation(conversation_id, exhausted)
        try:
            await egress.send(
                webhook_url=bus["webhook_url"],
                text=f"🛑 Conversation `{conversation_id}` closed — {exhausted}.",
                username=settings.webhook_name,
            )
        except Exception:  # noqa: BLE001 - closing matters more than announcing it
            log.warning("closure notice failed for %s", conversation_id, exc_info=True)
        raise HTTPException(
            status_code=423,
            detail=(
                f"Conversation {conversation_id} just closed — {exhausted}. Your message "
                "was not sent. Stop posting and wait for a human."
            ),
        )

    prior = await db.messages_after(
        bus["bus_id"], after=0, limit=200, conversation_id=conversation_id
    )
    depth = len(prior) + 1
    budget_left = max(0, turn_limit - turns_used - 1)

    # Who this agent may actually ping. Enforced on the wire, so an agent writing
    # <@someone-else> renders a mention that notifies nobody.
    mention_ids: list[str] = []
    if bus["mentions_enabled"]:
        try:
            mention_ids = [
                str(u["id"]) for u in json.loads(convo.get("mentionable") or "[]")
            ]
        except (json.JSONDecodeError, TypeError, KeyError):
            mention_ids = []

    async def _send(url: str) -> list[str]:
        return await egress.send(
            webhook_url=url,
            text=body.text,
            username=name,
            avatar_url=agent.get("avatar_url"),
            footer=f"{conversation_id} · turn {depth} · {budget_left} left",
            mention_user_ids=mention_ids,
        )

    try:
        try:
            message_ids = await _send(webhook_url)
        except discord.NotFound:
            # The webhook was deleted out from under us — revoked, or removed in
            # the Discord UI. Re-provision and retry rather than 404ing forever:
            # an agent has no way to fix this itself, since re-registering is
            # blocked by its own activity keeping the name marked live.
            log.warning("webhook gone for %r; re-provisioning", name)
            await db.clear_agent_webhook(bus["bus_id"], name)
            channel = request.app.state.gateway.client.get_channel(int(bus["channel_id"]))
            fresh = None
            if channel is not None:
                fresh = await ensure_agent_webhook(
                    request.app.state.gateway.client, db, settings, channel, bus, name
                )
            message_ids = await _send(fresh or bus["webhook_url"])
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
            budget_left=budget_left,
            reply_to=body.reply_to,
            kind=body.kind,
        )

    # Wake long-pollers now rather than waiting for the gateway to observe this
    # message coming back from Discord — a round trip they shouldn't pay for.
    request.app.state.notifier.notify(bus["bus_id"])

    # Agents that registered before seen_seq existed keep working, but get no
    # protection. Telling them in the response propagates the change without
    # breaking them or requiring a restart.
    hint = None
    if body.seen_seq is None:
        hint = (
            "Send `seen_seq` (the highest seq you had seen when you started "
            "composing) on future posts. Without it, you and the other agents "
            "reply blind to each other and duplicate the same point."
        )

    return SayResponse(
        ok=True,
        message_ids=message_ids,
        conversation_id=conversation_id,
        chunks=len(message_ids),
        seq=seq,
        hint=hint,
    )


@app.post("/me/rename", response_model=RenameResponse)
async def rename(
    request: Request,
    body: RenameRequest,
    identity: tuple[dict, dict] = Depends(require_agent),
) -> RenameResponse:
    """Change name in place, keeping the same key and webhook.

    Previously the only route was registering again, which orphaned the old
    roster entry and rotated the key — so agents correctly told the human that
    renaming was technically possible and practically not worth it.
    """
    agent, bus = identity
    db = request.app.state.db
    old_name = agent["agent_id"]
    new_name = body.name

    if new_name == old_name:
        raise HTTPException(status_code=422, detail="That is already your name.")

    others = [a["id"] for a in await db.roster(bus["bus_id"]) if a["id"] != old_name]
    if new_name in others:
        raise HTTPException(
            status_code=409, detail=f"{new_name!r} is taken. Taken here: {others}."
        )
    clash = confusable_with(new_name, others)
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{new_name!r} is too easily confused with {clash!r}. "
                f"Pick something clearly different. Taken here: {others}."
            ),
        )

    # Only regenerate the face if it was the generated one — a custom avatar the
    # agent chose should survive a rename.
    avatar = None
    if agent.get("avatar_url") == default_avatar_url(old_name):
        avatar = default_avatar_url(new_name)

    updated = await db.rename_agent(bus["bus_id"], old_name, new_name, avatar)
    if not updated:
        raise HTTPException(status_code=500, detail="Rename failed; you are unchanged.")

    # Keep the admin-facing webhook label in step, and tell the channel, so the
    # humans can follow who just became whom.
    if updated.get("webhook_url"):
        try:
            hook = discord.Webhook.from_url(
                updated["webhook_url"], session=request.app.state.egress._session
            )
            await hook.edit(name=f"{settings.webhook_name} · {new_name}")
        except Exception:  # noqa: BLE001 - cosmetic
            log.warning("could not rename webhook for %r", new_name, exc_info=True)
        try:
            await request.app.state.egress.send(
                webhook_url=updated["webhook_url"],
                text=f"**{old_name}** is now **{new_name}**.",
                username=new_name,
                avatar_url=updated.get("avatar_url"),
            )
        except Exception:  # noqa: BLE001
            log.warning("rename announcement failed for %r", new_name, exc_info=True)

    log.info("agent %r renamed to %r on bus %s", old_name, new_name, bus["bus_id"])
    return RenameResponse(
        ok=True,
        was=old_name,
        now=new_name,
        avatar_url=updated.get("avatar_url") or "",
        note="Your key is unchanged. Messages you already posted keep the old name.",
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
