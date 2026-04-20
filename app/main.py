from __future__ import annotations

import logging
import os

import uvicorn

from .fastapi_app import create_fastapi_app
from .logging_config import init_logging


def main() -> None:
    init_logging("store")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8300"))
    logging.getLogger(__name__).info(
        "store-service listening",
        extra={"host": host, "port": port},
    )
    uvicorn.run(
        create_fastapi_app(),
        host=host,
        port=port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
