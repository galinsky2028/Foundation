"""Alembic environment, run programmatically by foundation.migrate (no alembic.ini)."""
from alembic import context

config = context.config
target_metadata = config.attributes.get("target_metadata")
if target_metadata is None:
    from foundation.db import Base
    import foundation.models  # noqa: F401

    try:
        import app.models  # noqa: F401
    except ImportError:
        pass
    target_metadata = Base.metadata

connection = config.attributes["connection"]
context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
with context.begin_transaction():
    context.run_migrations()
