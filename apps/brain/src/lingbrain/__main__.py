from __future__ import annotations

import uvicorn

from .config import Settings

DEFAULT_DEV_TOKEN = "lingjingjing-local-dev"


def main() -> None:
    settings = Settings.from_env()
    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        if settings.auth_token == DEFAULT_DEV_TOKEN:
            raise SystemExit("非本机监听必须先在 .env 中更换 LING_AUTH_TOKEN")
    uvicorn.run(
        "lingbrain.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
