"""Environment-driven settings.

Deliberately small. Per-server configuration lives in Discord via slash commands,
not here — env vars can't express per-guild anything, and a second way of naming
a channel is a footgun.

Instantiating Settings at import time is deliberate: a missing token should stop
the container immediately with a clear error, not surface later as a gateway that
silently never connects.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The only Discord credential in the system. Never handed to an agent.
    discord_bot_token: str

    # Advertised to agents in /enable and the briefing, so it must be reachable
    # from wherever they run — not localhost, once anyone else is using this.
    public_url: str = "http://localhost:5585"

    # Name for the webhooks Switchboard creates. Admin-facing: it appears in
    # Channel Settings → Integrations, not in the channel itself.
    webhook_name: str = "Switchboard"

    port: int = 5585
    log_level: str = "info"

    # Bind-mounted to ./data on the host so the ledger survives a rebuild.
    db_path: str = "/app/data/switchboard.db"


settings = Settings()
