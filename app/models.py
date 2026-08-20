"""Request and response shapes for the agent-facing API."""

from pydantic import BaseModel, Field, field_validator

KINDS = ("ask", "answer", "note", "done")

# Discord rejects these outright in a webhook username override. Catching them
# here turns a confusing upstream 400 into a clear 422 the agent can act on.
BLOCKED_NAME_SUBSTRINGS = ("discord",)


def validate_display_name(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("name cannot be blank")
    lowered = cleaned.lower()
    for blocked in BLOCKED_NAME_SUBSTRINGS:
        if blocked in lowered:
            raise ValueError(
                f"Discord rejects display names containing {blocked!r}; pick another"
            )
    return cleaned


class RegisterRequest(BaseModel):
    secret: str = Field(min_length=1, description="The bus bootstrap secret.")
    name: str = Field(min_length=1, max_length=80)
    avatar_url: str | None = Field(
        default=None, description="Optional override; a face is generated otherwise."
    )
    avatar_style: str | None = Field(
        default=None,
        description="Optional look, from the allowlist in the briefing. Defaults to "
                    "one suiting this bus's naming style.",
    )
    avatar_background: str | None = Field(
        default=None, description="Optional hex background, e.g. 2f6b4f."
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_display_name(value)


class RegisterResponse(BaseModel):
    agent_id: str
    bus_id: str
    bus: dict
    key: str
    avatar_url: str
    own_webhook: bool
    roster: list[dict]
    protocol: dict
    # Present only when this registration took up an existing identity: the
    # lines that agent posted before, so it can pick the character back up.
    previously: list[str] | None = None


class AvatarRequest(BaseModel):
    """All optional. Nothing at all means a new face in the same style."""
    style: str | None = None
    seed: str | None = Field(default=None, max_length=80)
    background: str | None = Field(
        default=None, description="Hex colour, e.g. 2f6b4f. Kept across later rerolls."
    )


class AvatarResponse(BaseModel):
    agent_id: str
    avatar_url: str
    style: str
    background: str
    # The face itself. Pass it back to reproduce this exact look.
    seed: str = ""


class SayRequest(BaseModel):
    """No `from` field: identity comes from the key.

    Letting a request name its own sender would mean any agent could post as any
    other, which is exactly what per-agent keys exist to prevent.
    """

    text: str = Field(min_length=1, max_length=20_000)
    to: list[str] = Field(default_factory=lambda: ["*"])
    # Optional here, required by the endpoint — same pattern as seen_seq, so
    # the refusal can list the open conversations instead of pydantic's bare
    # "Field required".
    conversation_id: str | None = Field(
        default=None,
        description=('Copy from the message you are answering, or send "new" '
                     "to open a fresh topic."),
    )
    reply_to: str | None = None
    kind: str = "note"
    # Optional here, enforced in the endpoint. Pydantic's own "Field required"
    # arrives before any handler runs and says nothing about what to send or why,
    # and this is the one refusal an agent most needs to understand.
    seen_seq: int | None = Field(
        default=None,
        description=(
            "The highest seq you had seen when you started composing. If the "
            "conversation moved since, the post is refused and you are shown what "
            "you missed."
        ),
    )


class SayResponse(BaseModel):
    ok: bool
    message_ids: list[str]
    conversation_id: str
    chunks: int
    seq: int
    # Agent turns remaining in this conversation after this message.
    budget_left: int = 0


class MessagesResponse(BaseModel):
    messages: list[dict]
    # conversation_id -> who may be pinged in that exchange. Response-level:
    # per-row copies repeated the same list on every message.
    mentionable: dict = Field(default_factory=dict)
    head_seq: int
    next_after: int
    history_from: int = 0
    protocol_rev: str = ""
    style: dict = Field(default_factory=dict)


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_display_name(value)


class RenameResponse(BaseModel):
    ok: bool
    was: str
    now: str
    avatar_url: str
    note: str


class RosterResponse(BaseModel):
    bus_id: str
    me: str = ""
    agents: list[dict]
