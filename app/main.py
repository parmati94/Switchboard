"""FastAPI surface.

Every agent-facing route resolves a bearer token to exactly one bus. There is no
way to name a bus in a request, so an agent cannot address a bus it wasn't
invited to — isolation is enforced by the auth layer, not by callers remembering
to pass a filter.

Phase 3 uses the bus bootstrap secret as the bearer token. Phase 4 replaces it
with per-agent keys minted at registration.
"""

import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .briefing import briefing_json, briefing_markdown
from .config import settings
from .db import Database
from .egress import Egress, NoWebhookConfigured
from .gateway import Gateway
from .models import KINDS, MessagesResponse, SayRequest, SayResponse

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

    gateway = Gateway(settings, db)
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


async def require_bus(request: Request) -> dict:
    """Resolve the bearer token to a bus. This is the tenancy boundary."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing credential. Send 'Authorization: Bearer <bootstrap secret>'. "
                "A server admin gets one from /switchboard enable."
            ),
        )
    bus = await request.app.state.db.bus_for_secret(header[7:].strip())
    if not bus:
        raise HTTPException(
            status_code=403,
            detail="Unknown, rotated, or disabled bootstrap secret.",
        )
    return bus


@app.get("/", response_class=PlainTextResponse)
async def briefing(request: Request):
    """The front door. No auth — the bootstrap secret gates joining, not reading.

    Defaults to Markdown because an agent reaching this with curl should get
    something it can read straight through.
    """
    base = settings.public_url.rstrip("/")
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse(briefing_json(base))
    return PlainTextResponse(
        briefing_markdown(base), media_type="text/markdown; charset=utf-8"
    )


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """503 until the gateway is connected.

    Wired to the container HEALTHCHECK and the autoheal label, so a dropped
    websocket restarts the container instead of presenting as a quiet bus.
    Deliberately says nothing about individual buses — it is unauthenticated.
    """
    snapshot = request.app.state.gateway.snapshot()
    stats = await request.app.state.db.global_stats()
    return JSONResponse(
        {"service": "switchboard", "version": __version__, **snapshot, **stats},
        status_code=200 if snapshot["ready"] else 503,
    )


@app.get("/messages", response_model=MessagesResponse)
async def messages(
    request: Request,
    after: int = Query(0, ge=0, description="Highest seq already seen."),
    limit: int = Query(50, ge=1, le=200),
    conversation_id: str | None = Query(None),
    bus: dict = Depends(require_bus),
) -> MessagesResponse:
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
    bus: dict = Depends(require_bus),
) -> SayResponse:
    egress = request.app.state.egress
    db = request.app.state.db

    # Payload validation first: a malformed request is malformed whether or not
    # the bus can currently speak, and an agent debugging against a read-only
    # instance should still learn its body is wrong.
    if body.kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {list(KINDS)}")

    if not bus["webhook_url"]:
        raise HTTPException(
            status_code=503,
            detail=(
                "This bus has no webhook. The bot likely lacks Manage Webhooks in "
                "the channel — re-run /switchboard enable after granting it."
            ),
        )

    conversation_id = body.conversation_id or f"c_{secrets.token_hex(3)}"

    # Depth is real even before budgets land in Phase 7 — it makes the
    # breadcrumb in Discord meaningful and gives the human a sense of drift.
    prior = await db.messages_after(
        bus["bus_id"], after=0, limit=200, conversation_id=conversation_id
    )
    depth = len(prior) + 1

    try:
        message_ids = await egress.send(
            webhook_url=bus["webhook_url"],
            text=body.text,
            username=body.sender,
            avatar_url=body.avatar_url,
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
            author_name=body.sender,
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
