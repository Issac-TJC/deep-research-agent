"""Freeze non-secret execution configuration at run creation."""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("ALTER TABLE research_runs ADD COLUMN configuration jsonb NOT NULL DEFAULT '{}'::jsonb")


def downgrade():
    raise RuntimeError("Restore a backup instead of destructive downgrade")
