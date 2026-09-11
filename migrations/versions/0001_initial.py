"""Initial domain schema and tenant policies."""

from alembic import op

from research_agent.schema import DDL, TENANT_TABLES, policy_sql

revision = "0001"
down_revision = None


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(DDL, prepare=False)
    for table in TENANT_TABLES:
        connection.execute(policy_sql(table), prepare=False)


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
