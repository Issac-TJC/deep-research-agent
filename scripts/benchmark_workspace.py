"""Rollback-only capacity gate for the P0 project workspace."""

import json
import os
import statistics
import time
from uuid import uuid4

import psycopg


def measure(cursor, sql: str, parameters: tuple, repeats: int = 20) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        cursor.execute(sql, parameters).fetchall()
        samples.append((time.perf_counter() - started) * 1000)
    return statistics.quantiles(samples, n=20)[18]


def main() -> None:
    database_url = os.environ.get(
        "ADMIN_DATABASE_URL", "postgresql://postgres:postgres_local@localhost:15432/research"
    )
    tenant, project, conversation = uuid4(), uuid4(), uuid4()
    with psycopg.connect(database_url) as connection:
        cursor = connection.cursor()
        cursor.execute("INSERT INTO tenants(id,name) VALUES (%s,'capacity rollback fixture')", (tenant,))
        cursor.execute("INSERT INTO users(id,tenant_id) VALUES (%s,%s)", (tenant, tenant))
        cursor.execute(
            "INSERT INTO research_profiles(tenant_id,user_id) VALUES (%s,%s)", (tenant, tenant)
        )
        cursor.execute(
            """INSERT INTO projects(id,tenant_id,owner_user_id,name,is_default)
            VALUES (%s,%s,%s,'Capacity root',true)""",
            (project, tenant, tenant),
        )
        cursor.execute(
            """INSERT INTO projects(id,tenant_id,owner_user_id,name)
            SELECT gen_random_uuid(),%s,%s,'Capacity project '||n FROM generate_series(1,999) n""",
            (tenant, tenant),
        )
        cursor.execute(
            """INSERT INTO project_members(tenant_id,project_id,user_id,role)
            SELECT %s,id,%s,'owner' FROM projects WHERE tenant_id=%s""",
            (tenant, tenant, tenant),
        )
        cursor.execute(
            """INSERT INTO conversations(id,tenant_id,project_id,title)
            VALUES (%s,%s,%s,'Capacity conversation')""",
            (conversation, tenant, project),
        )
        cursor.execute(
            """INSERT INTO conversations(id,tenant_id,project_id,title)
            SELECT gen_random_uuid(),%s,%s,'Capacity conversation '||n FROM generate_series(1,999) n""",
            (tenant, project),
        )
        cursor.execute(
            """INSERT INTO messages(id,tenant_id,conversation_id,role,content,status,sequence)
            SELECT gen_random_uuid(),%s,%s,CASE WHEN n%%2=0 THEN 'assistant' ELSE 'user' END,
              'capacityneedle message '||n,'completed',n FROM generate_series(1,100000) n""",
            (tenant, conversation),
        )
        cursor.execute(
            """INSERT INTO project_artifacts
            (id,tenant_id,project_id,kind,logical_id,metadata)
            SELECT gen_random_uuid(),%s,%s,'file',gen_random_uuid(),
              jsonb_build_object('title','capacity asset '||n,'tags',jsonb_build_array('capacity'))
            FROM generate_series(1,10000) n""",
            (tenant, project),
        )
        cursor.execute(
            """INSERT INTO project_memories
            (id,tenant_id,project_id,type,content,status,confidence)
            SELECT gen_random_uuid(),%s,%s,'constraint','capacityneedle memory '||n,'confirmed',0.9
            FROM generate_series(1,5000) n""",
            (tenant, project),
        )
        results = {
            "dataset": {"projects": 1000, "conversations": 1000, "assets": 10000, "messages": 100000, "memories": 5000},
            "p95_ms": {
                "project_list": measure(
                    cursor,
                    "SELECT id FROM projects WHERE tenant_id=%s AND status='active' ORDER BY updated_at DESC,id LIMIT 101",
                    (tenant,),
                ),
                "conversation_list": measure(
                    cursor,
                    "SELECT id FROM conversations WHERE tenant_id=%s AND project_id=%s ORDER BY updated_at DESC,id LIMIT 101",
                    (tenant, project),
                ),
                "message_page": measure(
                    cursor,
                    "SELECT id FROM messages WHERE tenant_id=%s AND conversation_id=%s AND sequence>50000 ORDER BY sequence LIMIT 101",
                    (tenant, conversation),
                ),
                "memory_search": measure(
                    cursor,
                    "SELECT id FROM project_memories WHERE tenant_id=%s AND project_id=%s AND content ILIKE '%%capacityneedle%%' ORDER BY updated_at DESC LIMIT 100",
                    (tenant, project),
                ),
            },
        }
        connection.rollback()
    results["passed"] = (
        results["p95_ms"]["project_list"] < 500
        and results["p95_ms"]["conversation_list"] < 500
        and results["p95_ms"]["message_page"] < 1000
        and results["p95_ms"]["memory_search"] < 2000
    )
    print(json.dumps(results, indent=2))
    if not results["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
