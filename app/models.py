"""Request and response shapes for the agent-facing API."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

KINDS = ("ask", "answer", "note", "done")

# Discord rejects these outright in a webhook username override. Catching it
# here turns a confusing upstream 400 into a clear 422 the agent can act on.
BLOCKED_NAME_SUBSTRINGS = ("discord",)


class SayRequest(BaseModel):
    # `from` is a Python keyword, so the field is `sender` and aliased.
    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(min_length=1, max_length=20_000)
    sender: str = Field(
        alias="from",
        min_length=1,
        max_length=80,
        description="Display name for this agent in the channel.",
    )
    to: list[str] = Field(default_factory=lambda: ["*"])
    conversation_id: str | None = None
    reply_to: str | None = None
    kind: str = "note"
    avatar_url: str | None = None

    @field_validator("sender")
    @classmethod
    def _usable_as_webhook_username(cls, value: str) -> str:
        lowered = value.lower()
        for blocked in BLOCKED_NAME_SUBSTRINGS:
            if blocked in lowered:
                raise ValueError(
                    f"Discord rejects display names containing {blocked!r}; pick another"
                )
        if not value.strip():
            raise ValueError("display name cannot be blank")
        return value.strip()


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
