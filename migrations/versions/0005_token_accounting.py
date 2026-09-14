"""Add model-role token diagnostics and degradation audit metadata."""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        ALTER TABLE actions ADD COLUMN role text NOT NULL DEFAULT 'legacy';
        ALTER TABLE actions ADD COLUMN budget_group text NOT NULL DEFAULT 'legacy';
        ALTER TABLE actions ADD COLUMN serialized_bytes bigint NOT NULL DEFAULT 0;
        ALTER TABLE actions ADD COLUMN estimated_input_tokens bigint NOT NULL DEFAULT 0;
        ALTER TABLE actions ADD COLUMN output_token_ceiling bigint NOT NULL DEFAULT 0;
        ALTER TABLE actions ADD COLUMN request jsonb;
        CREATE INDEX actions_run_budget_group ON actions(tenant_id,run_id,budget_group);
        """,
        prepare=False,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
