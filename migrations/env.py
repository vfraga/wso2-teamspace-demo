"""Alembic environment for the Teamspace Business API.

The database URL is not stored in alembic.ini. It comes from
`api.config.settings.DATABASE_URL`, the same value the service itself reads, so
a migration can never be applied to a different database than the app uses.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

from api.config import settings
from api.database import get_engine
from api.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    # An explicit -x url=... wins, so a one-off migration can target another DB.
    return context.get_x_argument(as_dictionary=True).get("url") or settings.DATABASE_URL


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most columns in place; batch mode rewrites the
        # table instead. Harmless on Postgres/MySQL, essential on the default.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        from sqlalchemy import create_engine

        connectable = create_engine(override, poolclass=pool.NullPool)
    else:
        # Reuse api/database.py's engine factory so the SQLite pragmas and
        # connect_args stay consistent with the running service.
        connectable = get_engine()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
