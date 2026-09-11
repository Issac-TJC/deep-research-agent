from alembic import context
from sqlalchemy import create_engine

from research_agent.settings import settings

engine = create_engine(settings().admin_database_url.replace("postgresql://", "postgresql+psycopg://"))
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
