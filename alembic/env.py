from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context

# Import models so alembic can detect them
from app.db import Base, make_engine
import app.models  # noqa: F401

config = context.config

# The alembic CLI configures logging from alembic.ini. The store's own startup (app.main) already did
# (JSON on stdout) and says so with `configure_logger=False`: fileConfig would replace the root handler
# and, with its default disable_existing_loggers, silence every logger created before it, uvicorn's and
# the app's among them, for the life of the process.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(DATABASE_URL)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
