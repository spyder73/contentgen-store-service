from __future__ import annotations

import logging
import os

import uvicorn

from .fastapi_app import create_fastapi_app
from .logging_config import init_logging

logger = logging.getLogger(__name__)

# Repo root holds alembic.ini and the alembic/ script dir; it is /app in the
# container image. main.py lives in app/, so the root is one level up.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_APP_DIR)


def run_migrations() -> None:
    """Apply Alembic migrations to head before serving.

    The container CMD (python3 -m app.main) never ran alembic, so migrations had
    to be applied by hand. Run them programmatically here.

    - DATABASE_URL unset: skip with a log (local tooling / tests run without a DB).
    - Migration failure: let the exception propagate so startup crashes (fail
      fast) rather than serving against a schema that is behind the code.

    The alembic env (alembic/env.py) builds an async engine from DATABASE_URL, so
    the asyncpg URL the rest of the service uses works unchanged.
    """
    if not os.environ.get("DATABASE_URL", "").strip():
        logger.info("alembic: DATABASE_URL unset, skipping migrations")
        return

    from alembic import command
    from alembic.config import Config

    ini_path = os.path.join(_REPO_ROOT, "alembic.ini")
    config = Config(ini_path)
    # Pin the script location to an absolute path so migrations resolve
    # regardless of the process working directory.
    config.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    logger.info("alembic: upgrading database to head")
    command.upgrade(config, "head")
    logger.info("alembic: database is at head")


def main() -> None:
    init_logging("store")
    run_migrations()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8300"))
    logger.info(
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
