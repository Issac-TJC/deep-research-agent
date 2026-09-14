"""Initial domain schema and tenant policies."""

from alembic import op

from research_agent.schema import DDL, policy_sql

revision = "0001"
down_revision = None


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(DDL, prepare=False)
    # Keep the initial migration independent from tables added by later migrations.
    # Importing the live TENANT_TABLES registry here breaks fresh installs whenever
    # a future migration extends that registry.
    for table in ("research_runs", "records", "run_events", "actions"):
        connection.execute(policy_sql(table), prepare=False)


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
