from __future__ import annotations

import logging
import os

import uvicorn

from .fastapi_app import create_fastapi_app
from .logging_config import init_logging
from .migration_state import set_migration_status

logger = logging.getLogger(__name__)

# Repo root holds alembic.ini and the alembic/ script dir; it is /app in the
# container image. main.py lives in app/, so the root is one level up.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_APP_DIR)

# Revision the live schema corresponds to when the database predates
# alembic-managed deploys (tables exist but alembic_version does not). The
# production database was provisioned before startup migrations existed, with a
# schema matching this revision.
_LEGACY_BASELINE = "0013"


def _database_state() -> str:
    """Classify the database: 'stamped' (alembic_version exists), 'legacy'
    (tables exist without alembic_version), or 'empty'."""
    import asyncio

    from sqlalchemy import inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    async def probe() -> str:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with engine.connect() as conn:
                def classify(sync_conn) -> str:
                    inspector = inspect(sync_conn)
                    if inspector.has_table("alembic_version"):
                        return "stamped"
                    return "legacy" if inspector.get_table_names() else "empty"

                return await conn.run_sync(classify)
        finally:
            await engine.dispose()

    return asyncio.run(probe())


def run_migrations() -> None:
    """Apply Alembic migrations to head before serving.

    The container CMD (python3 -m app.main) never ran alembic, so the production
    schema exists but was never stamped. A plain `upgrade head` would replay
    migration 0001 onto existing tables and crash-loop the container, so:

    - DATABASE_URL unset: skip with a log (local tooling / tests run without a DB).
    - alembic_version present: incremental upgrade to head.
    - tables without alembic_version: stamp the legacy baseline first, then
      upgrade (applies only the migrations newer than the baseline).
    - empty database: full upgrade from scratch.
    - Any failure: log CRITICAL and serve anyway — the store must not crash-loop
      production; the outcome is exposed on /healthz as `migrations`.
    """
    if not os.environ.get("DATABASE_URL", "").strip():
        logger.info("alembic: DATABASE_URL unset, skipping migrations")
        set_migration_status("skipped (no DATABASE_URL)")
        return

    try:
        from alembic import command
        from alembic.config import Config

        ini_path = os.path.join(_REPO_ROOT, "alembic.ini")
        config = Config(ini_path)
        # Pin the script location to an absolute path so migrations resolve
        # regardless of the process working directory.
        config.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

        state = _database_state()
        if state == "legacy":
            logger.warning(
                "alembic: unmanaged schema detected, stamping baseline %s",
                _LEGACY_BASELINE,
            )
            command.stamp(config, _LEGACY_BASELINE)

        logger.info("alembic: upgrading database to head (state=%s)", state)
        command.upgrade(config, "head")
        logger.info("alembic: database is at head")
        set_migration_status("ok")
    except Exception as exc:  # noqa: BLE001 — never crash-loop the store on migration failure
        logger.critical("alembic: migration failed, serving anyway", exc_info=True)
        set_migration_status(f"failed: {type(exc).__name__}: {str(exc)[:300]}")


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
