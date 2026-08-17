import uvicorn

from .config import settings

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - container-internal; published port is bound in compose
        port=settings.port,
        log_level=settings.log_level,
    )
