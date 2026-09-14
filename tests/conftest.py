import os
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio

from research_agent.api import app
from research_agent.db import Database
from research_agent.service import ResearchService
from research_agent.settings import Settings
from research_agent.storage import ObjectStore


@pytest_asyncio.fixture
async def env():
    if os.environ.get("RUN_INTEGRATION") != "1":
        pytest.skip("Set RUN_INTEGRATION=1 after starting the project PostgreSQL/MinIO services")
    import hashlib

    s = Settings(research_mode="fixture", _env_file=None)
    tenant, other = str(uuid4()), str(uuid4())
    key, other_key = "test_" + str(uuid4()), "test_" + str(uuid4())
    with psycopg.connect(s.admin_database_url) as conn:
        for t, k in [(tenant, key), (other, other_key)]:
            conn.execute("INSERT INTO tenants(id,name) VALUES (%s,'integration fixture')", (t,))
            conn.execute("INSERT INTO api_keys VALUES (%s,%s)", (hashlib.sha256(k.encode()).hexdigest(), t))
    db, store = Database(s), ObjectStore(s)
    await db.open()
    await store.setup()
    service = ResearchService(db, store)
    app.state.service = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer " + key},
    ) as client:
        yield {
            "db": db,
            "store": store,
            "service": service,
            "tenant": tenant,
            "other": other,
            "key": key,
            "other_key": other_key,
            "client": client,
            "settings": s,
        }
    await db.close()
    with psycopg.connect(s.admin_database_url) as conn:
        for table in [
            "checkpoint_writes",
            "checkpoint_blobs",
            "checkpoints",
            "document_chunks",
            "source_index_jobs",
            "source_uploads",
            "run_events",
            "actions",
            "records",
            "research_runs",
            "api_keys",
        ]:
            conn.execute(f"DELETE FROM {table} WHERE tenant_id IN (%s,%s)", (tenant, other))
        conn.execute("DELETE FROM tenants WHERE id IN (%s,%s)", (tenant, other))
