"""Initial schema. Migrations run as a separate owner; runtime role never bypasses RLS."""

DDL = """
CREATE TABLE tenants (id uuid PRIMARY KEY, name text NOT NULL);
CREATE TABLE api_keys (key_hash text PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id));
CREATE TABLE research_runs (
 id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
 idempotency_key text NOT NULL, request_hash text NOT NULL, brief jsonb NOT NULL, profile jsonb NOT NULL,
 mode text NOT NULL CHECK(mode IN ('live','fixture')), status text NOT NULL DEFAULT 'queued',
 phase text NOT NULL DEFAULT 'intake', stop_reason text, quality_status text NOT NULL DEFAULT 'unchecked',
 created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz,
 lease_until timestamptz, worker_id text, fence bigint NOT NULL DEFAULT 0, seq bigint NOT NULL DEFAULT 0,
 spent_usd numeric NOT NULL DEFAULT 0, reserved_usd numeric NOT NULL DEFAULT 0,
 tokens bigint NOT NULL DEFAULT 0, reserved_tokens bigint NOT NULL DEFAULT 0,
 model_calls int NOT NULL DEFAULT 0, tool_calls int NOT NULL DEFAULT 0, search_calls int NOT NULL DEFAULT 0,
 UNIQUE(tenant_id,idempotency_key), UNIQUE(tenant_id,id)
);
CREATE TABLE records (
 tenant_id uuid NOT NULL REFERENCES tenants(id), id text NOT NULL, run_id uuid,
 kind text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,id), FOREIGN KEY(tenant_id,run_id) REFERENCES research_runs(tenant_id,id)
);
CREATE INDEX records_run_kind ON records(tenant_id,run_id,kind);
CREATE TABLE run_events (
 tenant_id uuid NOT NULL, run_id uuid NOT NULL, seq bigint NOT NULL, event_type text NOT NULL,
 payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,run_id,seq), FOREIGN KEY(tenant_id,run_id) REFERENCES research_runs(tenant_id,id)
);
CREATE TABLE actions (
 tenant_id uuid NOT NULL, run_id uuid NOT NULL, id text NOT NULL, task_id text,
 kind text NOT NULL, status text NOT NULL, reserved_usd numeric NOT NULL, reserved_tokens bigint NOT NULL,
 usage jsonb, result jsonb, error text, started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
 PRIMARY KEY(tenant_id,run_id,id), FOREIGN KEY(tenant_id,run_id) REFERENCES research_runs(tenant_id,id)
);
CREATE TABLE campaigns (
 id text PRIMARY KEY, spent_usd numeric NOT NULL DEFAULT 0, reserved_usd numeric NOT NULL DEFAULT 0,
 limit_usd numeric NOT NULL CHECK(limit_usd > 0 AND limit_usd <= 10)
);
INSERT INTO campaigns(id,limit_usd) VALUES ('initial-live',10);
CREATE FUNCTION authenticate_api_key(digest text) RETURNS uuid
 LANGUAGE sql SECURITY DEFINER SET search_path=public,pg_temp AS $$
 SELECT tenant_id FROM api_keys WHERE key_hash=digest;
$$;
CREATE FUNCTION queue_has_room() RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
 BEGIN
 PERFORM pg_advisory_xact_lock(184710);
 RETURN (SELECT count(*) < 10 FROM research_runs WHERE status='queued');
 END;
$$;
CREATE FUNCTION claim_next_run(worker text, ttl integer)
 RETURNS TABLE(run_id uuid, tenant uuid, token bigint)
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
 DECLARE selected uuid;
 BEGIN
 PERFORM pg_advisory_xact_lock(184711);
 IF EXISTS(SELECT 1 FROM research_runs WHERE status='running' AND lease_until > now()) THEN RETURN; END IF;
 SELECT id INTO selected FROM research_runs
 WHERE status='queued' OR (status='running' AND lease_until <= now())
 ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF selected IS NULL THEN RETURN; END IF;
 RETURN QUERY UPDATE research_runs SET status='running', worker_id=worker, fence=fence+1,
 lease_until=now()+make_interval(secs=>ttl), started_at=coalesce(started_at,now())
 WHERE id=selected RETURNING id,tenant_id,fence;
 END;
$$;
REVOKE ALL ON FUNCTION authenticate_api_key(text),queue_has_room(),claim_next_run(text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION authenticate_api_key(text),queue_has_room(),claim_next_run(text,integer) TO research_app;
GRANT USAGE ON SCHEMA public TO research_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO research_app;
REVOKE ALL ON tenants,api_keys FROM research_app;
"""

TENANT_TABLES = (
    "research_runs",
    "records",
    "run_events",
    "actions",
    "source_uploads",
    "source_index_jobs",
    "document_chunks",
)
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def policy_sql(table: str) -> str:
    if table not in TENANT_TABLES + CHECKPOINT_TABLES:
        raise ValueError("unregistered RLS table")
    return f"""
    ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
    ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
    CREATE POLICY tenant_isolation ON {table}
    USING (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid)
    WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid);
    """
