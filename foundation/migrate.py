"""Schema migrations (Alembic), applied automatically at startup.

Two migration branches share one database:
  * ``foundation`` (foundation/migrations/versions) for the tables every project has, and
  * ``app``        (app/migrations/versions) for the tables of one project.
Keeping them apart means a project can pull Foundation updates without migration conflicts.

Create a project migration after changing app/models.py:
    python scripts/manage.py makemigration "add petitions table"
"""
from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
FOUNDATION_VERSIONS = ROOT / "foundation" / "migrations" / "versions"
APP_VERSIONS = ROOT / "app" / "migrations" / "versions"


def alembic_config(engine: Engine, connection=None, target_metadata=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "foundation" / "migrations"))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("version_locations", os.pathsep.join([str(FOUNDATION_VERSIONS), str(APP_VERSIONS)]))
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    if connection is not None:
        cfg.attributes["connection"] = connection
    if target_metadata is not None:
        cfg.attributes["target_metadata"] = target_metadata
    return cfg


def upgrade_to_head(engine: Engine) -> None:
    with engine.begin() as conn:
        command.upgrade(alembic_config(engine, conn), "heads")


def make_migration(engine: Engine, message: str, branch: str = "app") -> None:
    """Autogenerate a migration on the app branch (or the foundation branch, for Foundation itself)."""
    upgrade_to_head(engine)
    with engine.begin() as conn:
        cfg = alembic_config(engine, conn)
        heads = ScriptDirectory.from_config(cfg).get_heads()
        branch_heads = [h for h in heads
                        if branch in (ScriptDirectory.from_config(cfg).get_revision(h).branch_labels or set())
                        or _in_branch(cfg, h, branch)]
        path = str(APP_VERSIONS if branch == "app" else FOUNDATION_VERSIONS)
        if branch_heads:
            command.revision(cfg, message=message, autogenerate=True, head=f"{branch}@head", version_path=path)
        else:
            command.revision(cfg, message=message, autogenerate=True, head="base", branch_label=branch,
                             version_path=path)


def _in_branch(cfg: Config, rev: str, branch: str) -> bool:
    script = ScriptDirectory.from_config(cfg)
    for r in script.iterate_revisions(rev, "base"):
        if branch in (r.branch_labels or set()):
            return True
    return False
