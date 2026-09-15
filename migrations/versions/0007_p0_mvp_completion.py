"""Complete the P0 workspace, notification, and digest contracts."""

from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        CREATE TABLE request_idempotency (
          tenant_id uuid NOT NULL REFERENCES tenants(id), scope text NOT NULL,
          idempotency_key text NOT NULL, request_hash text NOT NULL,
          state text NOT NULL DEFAULT 'in_progress' CHECK(state IN ('in_progress','completed')),
          response jsonb, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,scope,idempotency_key)
        );
        CREATE TABLE message_attachments (
          tenant_id uuid NOT NULL REFERENCES tenants(id), message_id uuid NOT NULL,
          artifact_id uuid NOT NULL, relation text NOT NULL DEFAULT 'input'
            CHECK(relation IN ('input','output','citation')),
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,message_id,artifact_id,relation),
          FOREIGN KEY(tenant_id,message_id) REFERENCES messages(tenant_id,id),
          FOREIGN KEY(tenant_id,artifact_id) REFERENCES project_artifacts(tenant_id,id)
        );
        CREATE TABLE conversation_events (
          id bigserial PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          conversation_id uuid NOT NULL, event_type text NOT NULL, payload jsonb NOT NULL,
          occurred_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id)
        );
        CREATE INDEX conversation_events_stream ON conversation_events(tenant_id,conversation_id,id);
        CREATE TABLE connector_attempts (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), digest_id uuid NOT NULL,
          connector text NOT NULL, query jsonb NOT NULL, status text NOT NULL
            CHECK(status IN ('running','succeeded','failed','timed_out')),
          attempt int NOT NULL CHECK(attempt > 0), result_count int NOT NULL DEFAULT 0,
          latency_ms int, provider_cost_usd numeric, error text,
          started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,digest_id) REFERENCES weekly_digests(tenant_id,id)
        );
        CREATE INDEX connector_attempts_digest ON connector_attempts(tenant_id,digest_id,started_at);

        ALTER TABLE research_runs ADD COLUMN run_kind text NOT NULL DEFAULT 'research'
          CHECK(run_kind IN ('research','quick_answer','weekly_digest'));
        ALTER TABLE research_runs ADD COLUMN context_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb;
        ALTER TABLE project_memories ADD COLUMN search_vector tsvector
          GENERATED ALWAYS AS (to_tsvector('simple',content)) STORED;
        ALTER TABLE project_memories ADD COLUMN embedding vector(768);
        CREATE INDEX project_memories_search ON project_memories USING gin(search_vector);
        CREATE INDEX project_memories_embedding ON project_memories
          USING hnsw(embedding vector_cosine_ops) WHERE embedding IS NOT NULL;
        ALTER TABLE weekly_digests ADD COLUMN run_id uuid;
        UPDATE weekly_digests SET run_id=report_id::uuid
          WHERE report_id ~ '^[0-9a-fA-F-]{36}$';
        UPDATE weekly_digests SET report_id=NULL WHERE run_id IS NOT NULL;
        ALTER TABLE weekly_digests ADD CONSTRAINT weekly_digests_run_fk
          FOREIGN KEY(tenant_id,run_id) REFERENCES research_runs(tenant_id,id);
        ALTER TABLE notifications ADD COLUMN read_at timestamptz;
        ALTER TABLE notifications ADD COLUMN payload jsonb NOT NULL DEFAULT '{}'::jsonb;

        CREATE INDEX messages_created ON messages(tenant_id,conversation_id,created_at,id);
        CREATE INDEX project_memories_updated ON project_memories(tenant_id,project_id,updated_at,id);
        CREATE INDEX weekly_digests_period ON weekly_digests(tenant_id,subscription_id,period_start DESC);

        ALTER TABLE request_idempotency ENABLE ROW LEVEL SECURITY;
        ALTER TABLE request_idempotency FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON request_idempotency
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE message_attachments ENABLE ROW LEVEL SECURITY;
        ALTER TABLE message_attachments FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON message_attachments
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE conversation_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE conversation_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON conversation_events
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE connector_attempts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE connector_attempts FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON connector_attempts
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        GRANT SELECT,INSERT,UPDATE,DELETE ON request_idempotency,message_attachments,
          conversation_events,connector_attempts TO research_app;
        GRANT USAGE,SELECT ON SEQUENCE conversation_events_id_seq TO research_app;
        """,
        prepare=False,
    )


def downgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        UPDATE weekly_digests SET report_id=coalesce(report_id,run_id::text) WHERE run_id IS NOT NULL;
        DROP INDEX IF EXISTS weekly_digests_period;
        DROP INDEX IF EXISTS project_memories_updated;
        DROP INDEX IF EXISTS messages_created;
        ALTER TABLE notifications DROP COLUMN payload;
        ALTER TABLE notifications DROP COLUMN read_at;
        ALTER TABLE weekly_digests DROP CONSTRAINT weekly_digests_run_fk;
        ALTER TABLE weekly_digests DROP COLUMN run_id;
        ALTER TABLE research_runs DROP COLUMN context_snapshot;
        ALTER TABLE research_runs DROP COLUMN run_kind;
        ALTER TABLE project_memories DROP COLUMN IF EXISTS embedding;
        ALTER TABLE project_memories DROP COLUMN IF EXISTS search_vector;
        DROP TABLE connector_attempts;
        DROP TABLE IF EXISTS conversation_events;
        DROP TABLE message_attachments;
        DROP TABLE request_idempotency;
        """,
        prepare=False,
    )
