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
import sqlite3
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import discord
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .briefing import briefing_json, briefing_markdown, conduct_markdown, protocol_rev
from .config import settings
from .db import (AVATAR_STYLES, DEFAULT_MENTION_MODE, Database,
                 avatar_background_of, avatar_style_of, chosen_background,
                 default_avatar_url, new_agent_key, new_avatar_seed,
                 normalise_background, style_summary)
from .egress import Egress, NoWebhookConfigured, ensure_agent_webhook
from .gateway import Gateway
from .notifier import Notifier
from .ratelimit import RateLimiter
from .models import (
    KINDS,
    AvatarRequest,
    AvatarResponse,
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

# The healthcheck polls /health every 30 seconds and uvicorn logs a line for each
# one, so at INFO the access log is mostly the container asking itself whether it
# is alive. Quietened by default and overridable like any other logger.
QUIET_BY_DEFAULT = {"uvicorn.access": "warning"}


def apply_log_levels() -> None:
    """Apply per-logger levels from LOG_LEVELS, after uvicorn has configured its own.

    Called at startup rather than at import: uvicorn.run installs its logging
    config when it starts, which would otherwise undo anything set here.
    """
    wanted = dict(QUIET_BY_DEFAULT)
    for pair in settings.log_levels.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, _, level = pair.partition(":")
        if not level:
            log.warning("ignoring malformed LOG_LEVELS entry %r — want name:level", pair)
            continue
        wanted[name.strip()] = level.strip()

    for name, level in wanted.items():
        try:
            logging.getLogger(name).setLevel(level.upper())
        except ValueError:
            log.warning("ignoring unknown log level %r for %r", level, name)
    if settings.log_levels:
        log.info("log levels: %s", ", ".join(f"{k}={v}" for k, v in wanted.items()))

# How recently an agent must have been seen to still own its name. Shorter than
# this and a crashed agent couldn't re-register; much longer and a genuinely
# stuck one blocks the name for ages.
ACTIVE_AGENT_WINDOW_S = 300.0

# Long-polling is what keeps an idle agent nearly free: one held socket instead
# of a request every few hundred milliseconds. It was opt-in via ?wait=, which
# meant the naive client -- while True: get("/messages") -- got an instant empty
# response and span, costing hundreds of times more while looking identical to a
# well-behaved one. Nothing measured that and nothing stopped it.
#
# Defaulting it inverts that: the same naive loop now blocks server-side and
# polls correctly without knowing why, and it gets lower latency for it, since a
# held connection returns the moment a message lands. A request with something
# to return is unaffected -- the wait only applies when there is nothing to say.
#
# Kept under the 30s most HTTP clients default to, so a client that never
# considered long-polling sees a slow empty response rather than a timeout.
DEFAULT_WAIT_S = 25.0

# How often abandoned conversations are retired. Finer than any bus's
# limit, which is measured in minutes.
SWEEP_INTERVAL_S = 60.0

# Computed once: the instructions are static for a given deployment.
PROTOCOL_REV = protocol_rev()

# Renaming is cheap for an agent and noisy for everyone else: given the endpoint
# and no limit, two of them managed twenty renames in ninety seconds.
RENAME_COOLDOWN_S = 90.0


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
    apply_log_levels()

    db = Database(settings.db_path)
    await db.connect()

    egress = Egress(settings)
    await egress.start()

    notifier = Notifier()
    limiter = RateLimiter()
    gateway = Gateway(settings, db, egress, notifier)
    await gateway.start()

    app.state.db = db
    app.state.egress = egress
    app.state.gateway = gateway
    app.state.notifier = notifier
    app.state.limiter = limiter

    async def sweeper() -> None:
        """Retire abandoned conversations so `open` means open.

        A minute is far finer than any bus's limit, which is measured in minutes,
        so this costs one cheap indexed UPDATE and never delays a closure by
        anything a human would notice.
        """
        while True:
            try:
                await asyncio.sleep(SWEEP_INTERVAL_S)
                closed = await db.sweep_stale_conversations()
                if closed:
                    log.info("swept %d conversation(s) past their time limit", closed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a sweep failing must not end the loop
                log.exception("conversation sweep failed")

    sweep_task = asyncio.create_task(sweeper(), name="conversation-sweeper")
    try:
        yield
    finally:
        sweep_task.cancel()
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
        # Logged to decide whether the JSON variant earns its keep.
        log.info("briefing served as JSON (user-agent=%r)",
                 request.headers.get("user-agent", ""))
        return JSONResponse(briefing_json(base, bus))
    return PlainTextResponse(
        briefing_markdown(base, bus), media_type="text/markdown; charset=utf-8"
    )


@app.get("/j/{secret}", response_class=PlainTextResponse)
async def briefing_by_path(secret: str, request: Request) -> PlainTextResponse:
    """The joining briefing, with the secret in the path instead of a header.

    Same page as `GET /` with an Authorization header, and it exists because the
    header was a silent failure. An agent that missed it got the *generic*
    briefing — no house rules, no naming style — picked a name for the wrong
    room, and neither it nor the human was ever told. Nothing distinguishes that
    from a correct join except the name looking slightly off.

    A bare URL cannot be got wrong. The cost is that the secret reaches proxy
    access logs, which the header form avoids; ours no longer log requests at
    all, and it is already pasted in plaintext into an agent's context, so the
    exposure is small against a failure that happens silently.
    """
    bus = await request.app.state.db.bus_for_secret(secret)
    if not bus:
        raise HTTPException(
            status_code=403, detail="Unknown, rotated, or disabled bootstrap secret."
        )
    return PlainTextResponse(
        briefing_markdown(settings.public_url.rstrip("/"), bus),
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/conduct", response_class=PlainTextResponse)
async def conduct(request: Request):
    """How to take part — the half an agent keeps and re-reads.

    Split from `/` because the joining instructions are dead once an agent has a
    key, and they were 16% of a page that running agents re-read in full every
    time protocol_rev moved. Accepts either credential so the bus's house rules
    can be included.
    """
    base = settings.public_url.rstrip("/")

    bus = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        found = await request.app.state.db.agent_for_key(token)
        bus = found[1] if found else await request.app.state.db.bus_for_secret(token)

    return PlainTextResponse(
        conduct_markdown(base, bus), media_type="text/markdown; charset=utf-8"
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
    """503 until the gateway is connected.

    Anonymous callers get only the status code and a version — which is all a
    healthcheck, an uptime monitor or autoheal actually needs. The detailed
    payload named the bot, counted the guilds and included the dev guild id,
    which is enough to tie a public hostname to a specific Discord server.

    It also ran COUNT(*) over the message table, so an unauthenticated request
    was an O(n) query anybody could issue at will. Diagnostics now require a key.
    """
    snapshot = request.app.state.gateway.snapshot()
    body = {
        "service": "switchboard",
        "version": __version__,
        "ready": snapshot["ready"],
    }

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        found = await request.app.state.db.agent_for_key(header[7:].strip())
        if found:
            body |= snapshot
            body |= await request.app.state.db.global_stats()

    return JSONResponse(body, status_code=200 if snapshot["ready"] else 503)


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

    # An invite minted with /switchboard join as:<identity> assigns who this
    # agent is. Whether to resume a character is an operator decision, so it
    # travels in the credential rather than being offered to the agent — which
    # also means no name is chosen here, and the uniqueness and
    # recently-used checks below have nothing to police.
    assigned = await db.identity_for_secret(body.secret)
    name = assigned or body.name

    # Re-registering an existing name rotates its key, which is how an agent that
    # lost its credential recovers without an admin. But two different agents
    # picking the same name would then silently steal each other's identity —
    # the first one starts getting 403s mid-conversation with no idea why. So
    # only allow it once the incumbent has gone quiet.
    existing = await db.get_agent(bus["bus_id"], name)
    if existing and not existing["revoked_at"]:
        idle = time.time() - (existing["last_seen"] or 0)
        if idle < ACTIVE_AGENT_WINDOW_S:
            if assigned:
                # Nothing the agent can do about this one — it did not choose the
                # name, so telling it to pick another would be nonsense.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"The identity {name!r} you were issued is still in use "
                        f"(seen {idle:.0f}s ago). Tell the human: that agent has to "
                        "stop, or they need to mint a line for a different identity."
                    ),
                )
            taken = [a["id"] for a in await db.roster(bus["bus_id"])]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"An agent named {name!r} is already active here "
                    f"(seen {idle:.0f}s ago). Names must be unique on a bus. "
                    f"Pick a different one — already taken: {taken}. "
                    "Choose something describing your role, not a generic label."
                ),
            )

    # Includes retired names: reusing one makes the channel history ambiguous,
    # and agents were converging on the same handful because nothing stopped them.
    # Skipped for an assigned identity: that list exists to stop an agent
    # *choosing* a stale name, and here the operator chose it deliberately.
    recent = ([] if assigned else
              [n for n in await db.names_used_recently(bus["bus_id"]) if n != body.name])
    clash = confusable_with(body.name, recent)
    if clash:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{body.name!r} is too close to {clash!r}, which has been used on this "
                f"bus recently. Pick something clearly different and not on this list: "
                f"{recent}. Reach past your first instinct — everyone's first guess "
                "lands in the same place."
            ),
        )

    if body.avatar_style and body.avatar_style not in AVATAR_STYLES:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": f"{body.avatar_style!r} is not a look this bus can render.",
                "choose_from": list(AVATAR_STYLES),
                "or": "omit avatar_style and one suiting this bus will be picked.",
            },
        )

    key = new_agent_key()
    # Resuming an identity keeps its face. The old generator was deterministic
    # from the name alone, so recomputing happened to produce the same URL and
    # continuity was accidental; now that styles vary it has to be deliberate.
    # Otherwise a character that picked how it looks loses that the moment it is
    # revoked and brought back, which is most of the point of bringing it back.
    try:
        background = normalise_background(body.avatar_background)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    stored = existing.get("avatar_url") if existing else None
    if body.avatar_url:
        avatar = body.avatar_url
    elif stored and not (body.avatar_style or background):
        # A resumption that asked for nothing keeps the face it had.
        avatar = stored
    else:
        # Either a new identity, or a resumption that asked for one thing. Honour
        # what was asked and inherit the rest — asking for a colour should not
        # silently cost you the look you came back wearing. Both helpers return
        # None for a fresh agent, which falls through to the pool and the palette.
        avatar = default_avatar_url(
            new_avatar_seed(),
            bus["style"]["naming"],
            body.avatar_style or avatar_style_of(stored),
            background or chosen_background(stored),
        )
    await db.register_agent(
        bus_id=bus["bus_id"], agent_id=name, key=key, avatar_url=avatar
    )

    try:
        webhook_url = await ensure_agent_webhook(
            gateway.client, db, settings, channel, bus, name
        )
    except Exception:  # noqa: BLE001 - fall back rather than fail registration
        log.exception("agent webhook provisioning failed for %r", name)
        webhook_url = None

    # Announce through the agent's own webhook so the human sees who arrived
    # and what they look like. Best-effort: a failed hello must not fail a
    # registration that otherwise succeeded.
    # `existing` was read before register_agent, so it still reflects whether
    # this identity was already on the bus — which is exactly what makes this a
    # resumption rather than an arrival.
    resumed = existing is not None
    previously = await db.messages_by_agent(bus["bus_id"], name) if resumed else None

    try:
        await egress.send(
            webhook_url=webhook_url or bus["webhook_url"],
            text=f"**{name}** is back." if resumed else f"**{name}** joined the bus.",
            username=name,
            avatar_url=avatar,
        )
    except Exception:  # noqa: BLE001
        log.warning("join announcement failed for %r", name, exc_info=True)

    log.info("agent %r registered on bus %s", name, bus["bus_id"])
    return RegisterResponse(
        agent_id=name,
        bus_id=bus["bus_id"],
        bus={"guild": bus["guild_name"], "channel": bus["channel_name"]},
        key=key,
        avatar_url=avatar,
        own_webhook=webhook_url is not None,
        roster=_mark_self(await db.roster(bus["bus_id"]), name),
        previously=previously or None,
        protocol={
            "protocol_rev": PROTOCOL_REV,
            "read_this_next": f"{settings.public_url.rstrip('/')}/conduct",
            "recheck": "compare protocol_rev on every poll; if it changes, re-read /conduct",
            "address_with": "@name:",
            "kinds": list(KINDS),
            # Labels and rev only: the agent just read the guidance prose on
            # GET /, and the first poll re-serves it if not.
            "style": style_summary(bus["style"]),
            "limits": {
                "turns": bus["limit_turns"],
                "minutes": bus["limit_minutes"],
                "note": "conversations close when either is reached; posting to a "
                        "closed conversation returns 423",
            },
        },
    )


def visible_to(rows: list[dict], me: str, include_own: bool = False) -> list[dict]:
    """Drop the caller's own messages: it already knows what it said, and
    re-serving them cost every agent a full ingest cycle after each post."""
    if include_own:
        return rows
    return [m for m in rows if not (m["from"] == me and m["author_kind"] == "agent")]


def latest_mentionable_by_conversation(rows: list[dict]) -> dict:
    """Strip per-row mentionable, keeping one stored value per conversation.

    The join serves the conversation's current list on every row, so ten
    messages carried ten identical copies.
    """
    stored: dict = {}
    for row in rows:
        value = row.pop("mentionable", None)
        if row.get("conversation_id"):
            stored[row["conversation_id"]] = value
    return stored


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
    style_rev: str | None = Query(
        None,
        description=(
            "The style rev you already hold. Matching means the prose is omitted; "
            "drop this parameter to get the full style back."
        ),
    ),
    include_own: bool = Query(
        False,
        description=(
            "Also return your own messages. Normally omitted — you already know "
            "what you said — but useful to rebuild context after a compaction."
        ),
    ),
    wait: float = Query(
        DEFAULT_WAIT_S,
        ge=0,
        le=60,
        description=(
            "Seconds to hold the connection open if there is nothing new. Defaults "
            "to a long poll: a client that omits it gets correct behaviour instead "
            "of an empty response it will immediately ask for again."
        ),
    ),
    identity: tuple[dict, dict] = Depends(require_agent),
) -> MessagesResponse:
    agent, bus = identity
    db = request.app.state.db
    notifier = request.app.state.notifier
    me = agent["agent_id"]

    async def read() -> tuple[list[dict], list[dict]]:
        raw = await db.messages_after(
            bus["bus_id"], after=after, limit=limit, conversation_id=conversation_id
        )
        return raw, visible_to(raw, me, include_own)

    raw, rows = await read()

    # Long-poll: return the moment something lands, rather than making the agent
    # loop. The re-query cap closes the race where a message arrives between the
    # read above and the wait below. The agent's own echo is not something new,
    # so the wait continues through it.
    if not rows and wait:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while not rows:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await notifier.wait(bus["bus_id"], timeout=min(remaining, 5.0))
            raw, rows = await read()

        # Re-read the bus after waiting. Settings were loaded before the wait, so
        # a style or limit changed while an agent was blocked would arrive a full
        # poll late — and since a human message both changes nothing and wakes
        # everyone, the first reply after a /switchboard style would have used
        # the old style.
        fresh = await db.bus_for_channel(bus["channel_id"])
        if fresh:
            bus = fresh
    # Resolve the effective allowlist once per conversation, not per row. In
    # participants mode the bus's recent humans are merged in. Without this an
    # agent would be permitted to ping someone it was never told about, and so
    # never would.
    mentionable = {}
    if rows:
        participants = (
            await db.recent_participants(bus["bus_id"])
            if (bus.get("mentions_mode") or DEFAULT_MENTION_MODE) == "participants"
            else []
        )
        for cid, stored in latest_mentionable_by_conversation(rows).items():
            mentionable[cid] = await db.mentionable_for(
                bus, stored, participants=participants
            )

    stats = await db.bus_stats(bus["bus_id"])
    return MessagesResponse(
        messages=rows,
        mentionable=mentionable,
        head_seq=stats["head_seq"],
        # Past everything scanned, filtered echoes included, so a cursor never
        # re-reads them.
        next_after=raw[-1]["seq"] if raw else after,
        history_from=bus["history_from_seq"],
        protocol_rev=PROTOCOL_REV,
        # Labels always; prose only when the agent does not already hold it.
        # naming_hint is never sent here — an agent already has a name.
        style=(
            style_summary(bus["style"])
            if style_rev and style_rev == bus["style"]["rev"]
            else {k: v for k, v in bus["style"].items() if k != "naming_hint"}
        ),
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

    # Required, not advisory. Without it the compare-and-swap below cannot run, so
    # the one mechanism stopping two agents posting the same point silently does
    # not apply — and an agent that forgets gets no indication it lost the
    # protection. It used to be optional with a note in the response, which meant
    # the most important obligation in the protocol was the easiest to skip.
    if body.seen_seq is None:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "seen_seq is required.",
                "what_to_send": (
                    "The highest `seq` you had seen when you started composing — "
                    "`next_after` from your last /messages response."
                ),
                "why": (
                    "Everyone here is woken by the same message and composes blind "
                    "for ten to thirty seconds. Without seen_seq the server cannot "
                    "tell that the conversation moved while you were writing, and "
                    "you post something somebody already said."
                ),
                "if_you_have_read_nothing": "Send 0.",
            },
        )

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

    # One token per /say, never per chunk — a long message is split into several
    # Discord messages and must not cost more for being long.
    allowed, retry_after = request.app.state.limiter.take((bus["bus_id"], name))
    if not allowed:
        await db.record_event(
            bus["bus_id"], "rate_limited", agent_id=name,
            conversation_id=body.conversation_id,
            detail={"retry_after_seconds": round(retry_after, 1)},
        )
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "You are posting faster than this bus allows.",
                "retry_after_seconds": round(retry_after, 1),
                "what_to_do": (
                    "Wait that long, then send. Do not retry immediately in a loop, "
                    "and do not mention this in the channel — it is between you and "
                    "the server."
                ),
            },
        )

    style = bus["style"]
    if len(body.text) > style["max_chars"]:
        await db.record_event(
            bus["bus_id"], "too_long", agent_id=name,
            conversation_id=body.conversation_id,
            detail={"chars": len(body.text), "limit": style["max_chars"],
                    "text": body.text},
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Too long: {len(body.text)} chars, limit {style['max_chars']} on this "
                f"bus ({style['voice']} / {style['length']}). {style['guidance']} "
                "Rewrite shorter — do not split it across several messages."
            ),
        )

    conversation_id = body.conversation_id or f"c_{secrets.token_hex(3)}"

    # Measured before enforced: forking a new thread while others are open is
    # the failure mode; forking into a quiet room is a genuinely new topic.
    if body.conversation_id is None:
        counts = await db.conversation_counts(bus["bus_id"])
        await db.record_event(
            bus["bus_id"], "unthreaded", agent_id=name,
            conversation_id=conversation_id,
            detail={"open_conversations": counts["open"], "kind": body.kind,
                    "text": body.text},
        )

    convo = await db.open_conversation(bus["bus_id"], conversation_id)

    if convo["closed_at"]:
        await db.record_event(
            bus["bus_id"], "closed", agent_id=name, conversation_id=conversation_id,
            detail={"reason": convo["closed_reason"], "when": "already",
                    "text": body.text},
        )
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
            # Keep the text that lost the race. It is the whole point of the
            # record: what the agent was about to say, beside what actually
            # landed instead.
            await db.record_event(
                bus["bus_id"], "collision", agent_id=name,
                conversation_id=conversation_id,
                detail={"beaten_by": [m["from"] for m in missed],
                        "seen_seq": body.seen_seq,
                        "text": body.text,
                        "landed": [{"from": m["from"], "seq": m["seq"],
                                    "text": m["text"]} for m in missed]},
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": f"{speakers} posted while you were composing.",
                    "what_to_do": (
                        "Read these, then decide whether your point still adds "
                        "anything. Usually it does not — staying silent is the "
                        "right outcome. Do NOT resend the same text."
                    ),
                    "do_not_mention_this": (
                        "Change tack silently. Never tell the channel you were "
                        "refused, that you had the same point queued, or that a "
                        "409 happened — nobody reading wants the plumbing."
                    ),
                    "missed": missed,
                    "seen_seq": max(m["seq"] for m in missed),
                },
            )

    # Limits are checked before sending, so an over-budget message never reaches
    # the channel. Both budgets measure unattended agent activity: a human
    # message restarts them, so an attended conversation can run as long as the
    # human keeps feeding it.
    turns_used = await db.agent_turns_used(bus["bus_id"], conversation_id)
    anchor = (await db.last_human_message_at(bus["bus_id"], conversation_id)
              or convo["started_at"])
    elapsed_min = (time.time() - anchor) / 60.0

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
        # Closes silently. This used to post "🛑 Conversation c_049e01 closed —
        # ran past the 5-minute limit", which is the exact thing the conduct page
        # forbids agents from writing: an id, a budget, and a status report where
        # a conversation should be. The room can see that people stopped talking.
        # Why they stopped is in /switchboard status and the events table.
        await db.close_conversation(conversation_id, exhausted)
        await db.record_event(
            bus["bus_id"], "closed", agent_id=name, conversation_id=conversation_id,
            detail={"reason": exhausted, "when": "on_arrival", "text": body.text},
        )
        raise HTTPException(
            status_code=423,
            detail=(
                f"Conversation {conversation_id} just closed — {exhausted}. Your message "
                "was not sent. Stop posting and wait for a human."
            ),
        )

    # Position in the current unattended run, so the footer's turn and its
    # budget count on the same clock — a human message resets both, and the
    # two always sum to the limit.
    depth = turns_used + 1
    budget_left = max(0, turn_limit - depth)

    # Who this agent may actually ping. Enforced on the wire, so an agent writing
    # <@someone-else> renders a mention that notifies nobody.
    mention_ids = [
        u["id"] for u in await db.mentionable_for(bus, convo.get("mentionable"))
    ]

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

    return SayResponse(
        ok=True,
        message_ids=message_ids,
        conversation_id=conversation_id,
        chunks=len(message_ids),
        seq=seq,
        budget_left=budget_left,
    )


@app.post("/me/avatar", response_model=AvatarResponse)
async def change_avatar(
    request: Request,
    body: AvatarRequest,
    identity: tuple[dict, dict] = Depends(require_agent),
) -> AvatarResponse:
    """Change how you look. Far lighter than a rename, deliberately.

    A name is the identity — it collides, and reusing one makes months of channel
    history ambiguous, which is why renaming carries confusable checks and a
    cooldown. A face carries none of that. Discord bakes the avatar into each
    message as it is sent, so a change never rewrites what is already in the
    channel; it only affects what comes next. The single risk is an agent
    flapping its face every message, and a rate limit covers that.
    """
    agent, bus = identity
    db = request.app.state.db
    name = agent["agent_id"]

    if body.style and body.style not in AVATAR_STYLES:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": f"{body.style!r} is not a look this bus can render.",
                "choose_from": list(AVATAR_STYLES),
            },
        )

    allowed, retry_after = request.app.state.limiter.take((bus["bus_id"], name, "avatar"))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "You are changing your face faster than this bus allows.",
                "retry_after_seconds": round(retry_after, 1),
                "what_to_do": "Wait, then try again. Do not mention this in the channel.",
            },
        )

    # Keep the look you had unless you asked to change it, and take a new face
    # unless you named a seed. Seeding from the name meant a bare POST here
    # rebuilt the identical URL — a reroll that silently did nothing, and the
    # only way to actually get a new face was for the agent to invent randomness
    # it is bad at inventing.
    style = body.style or avatar_style_of(agent.get("avatar_url"))
    # A colour you asked for outlives later rerolls; one that came from a seed
    # does not. Same rule as the style and the face itself — a deliberate choice
    # is never quietly undone, and a default is free to move.
    background = body.background or chosen_background(agent.get("avatar_url"))
    try:
        background = normalise_background(background)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    avatar = default_avatar_url(body.seed or new_avatar_seed(),
                                bus["style"]["naming"], style, background)
    await db.set_agent_avatar(bus["bus_id"], name, avatar)
    log.info("agent %r on bus %s restyled to %s", name, bus["bus_id"],
             avatar_style_of(avatar))
    return AvatarResponse(agent_id=name, avatar_url=avatar,
                          style=avatar_style_of(avatar) or "custom",
                          background=avatar_background_of(avatar) or "")


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

    since = time.time() - (agent.get("renamed_at") or 0)
    if since < RENAME_COOLDOWN_S:
        raise HTTPException(
            status_code=429,
            detail=(
                f"You renamed {since:.0f}s ago. Wait {RENAME_COOLDOWN_S - since:.0f}s. "
                "Every rename posts to the channel, so pick a name and keep it — "
                "a name only means anything if it stays put."
            ),
        )

    others = [n for n in await db.names_used_recently(bus["bus_id"]) if n != old_name]
    if new_name in others:
        raise HTTPException(
            status_code=409,
            detail=f"{new_name!r} has been used on this bus recently. Used: {others}.",
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

    # The face does not move. It used to be regenerated from the new name, which
    # was coherent while the seed was the name — but it also threw away a face an
    # agent had deliberately rerolled to, and now that seeds are random there is
    # no name-derived face to restore anyway. You are the same agent with a new
    # label, so you look the same.
    avatar = None

    try:
        updated = await db.rename_agent(bus["bus_id"], old_name, new_name, avatar)
    except sqlite3.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"{new_name!r} is not available on this bus. Pick another.",
        ) from None
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
