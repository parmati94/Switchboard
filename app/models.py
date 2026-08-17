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


class SayRequest(BaseModel):
    """No `from` field: identity comes from the key.

    Letting a request name its own sender would mean any agent could post as any
    other, which is exactly what per-agent keys exist to prevent.
    """

    text: str = Field(min_length=1, max_length=20_000)
    to: list[str] = Field(default_factory=lambda: ["*"])
    conversation_id: str | None = None
    reply_to: str | None = None
    kind: str = "note"


class SayResponse(BaseModel):
    ok: bool
    message_ids: list[str]
    conversation_id: str
    chunks: int
    seq: int


class MessagesResponse(BaseModel):
    messages: list[dict]
    head_seq: int
    next_after: int


class RosterResponse(BaseModel):
    bus_id: str
    agents: list[dict]
