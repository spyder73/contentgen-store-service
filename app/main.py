from __future__ import annotations

import logging
import os

import uvicorn

from .fastapi_app import create_fastapi_app


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8300"))
    logging.getLogger(__name__).info("store-service listening on %s:%s", host, port)
    uvicorn.run(
        create_fastapi_app(),
        host=host,
        port=port,
        log_level=log_level.lower(),
    )


if __name__ == "__main__":
    main()
