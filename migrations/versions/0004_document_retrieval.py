"""Add tenant-scoped document indexing, asynchronous uploads, and retrieval accounting."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        CREATE EXTENSION IF NOT EXISTS vector;
        ALTER TABLE research_runs ADD COLUMN retrieval_calls int NOT NULL DEFAULT 0;

        CREATE TABLE source_uploads (
          tenant_id uuid NOT NULL REFERENCES tenants(id), upload_id text NOT NULL,
          raw_hash text NOT NULL, raw_key text NOT NULL, mime text NOT NULL, title text NOT NULL,
          parser_mode text NOT NULL DEFAULT 'auto'
            CHECK(parser_mode IN ('native','auto','enhanced')),
          status text NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','processing','ready','failed')),
          source_id text, progress real NOT NULL DEFAULT 0, attempts int NOT NULL DEFAULT 0,
          lease_until timestamptz, worker_id text, error text,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,upload_id), CHECK(progress>=0 AND progress<=1), CHECK(attempts>=0)
        );

        CREATE TABLE source_index_jobs (
          tenant_id uuid NOT NULL REFERENCES tenants(id), source_id text NOT NULL,
          parsed_hash text NOT NULL, index_version text NOT NULL, chunker_version text NOT NULL,
          embedding_model text, embedding_revision text, status text NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','processing','lexical_ready','ready','failed')),
          total_chunks int NOT NULL DEFAULT 0, embedded_chunks int NOT NULL DEFAULT 0,
          attempts int NOT NULL DEFAULT 0, lease_until timestamptz, worker_id text, error text,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,source_id,index_version),
          CHECK(total_chunks>=0 AND embedded_chunks>=0 AND embedded_chunks<=total_chunks),
          CHECK(attempts>=0)
        );

        CREATE TABLE document_chunks (
          tenant_id uuid NOT NULL REFERENCES tenants(id), source_id text NOT NULL,
          parsed_hash text NOT NULL, index_version text NOT NULL, chunk_id text NOT NULL,
          ordinal int NOT NULL, start_char int NOT NULL, end_char int NOT NULL,
          page int, bbox jsonb, kind text NOT NULL, section_path jsonb NOT NULL DEFAULT '[]'::jsonb,
          block_id text NOT NULL, text text NOT NULL, lexical_text text NOT NULL,
          textsearch tsvector NOT NULL, embedding vector(768),
          embedding_model text, embedding_revision text,
          created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,index_version,chunk_id), CHECK(start_char>=0 AND end_char>start_char)
        );
        CREATE INDEX source_uploads_claim ON source_uploads(status,lease_until,created_at);
        CREATE INDEX source_index_jobs_claim ON source_index_jobs(status,lease_until,created_at);
        CREATE INDEX document_chunks_source ON document_chunks(tenant_id,source_id,index_version);
        CREATE INDEX document_chunks_textsearch ON document_chunks USING gin(textsearch);

        ALTER TABLE source_uploads ENABLE ROW LEVEL SECURITY;
        ALTER TABLE source_uploads FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON source_uploads
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE source_index_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE source_index_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON source_index_jobs
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
        ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON document_chunks
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);

        GRANT SELECT,INSERT,UPDATE,DELETE ON source_uploads,source_index_jobs,document_chunks TO research_app;

        CREATE FUNCTION claim_next_source_upload(worker text, ttl integer)
        RETURNS TABLE(upload_id text,tenant uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
        DECLARE selected_tenant uuid; selected_upload text;
        BEGIN
          UPDATE source_uploads SET status='failed',lease_until=NULL,
            error=coalesce(error,'attempts_exhausted'),updated_at=now()
          WHERE status='processing' AND lease_until<=now() AND attempts>=3;
          SELECT tenant_id,source_uploads.upload_id INTO selected_tenant,selected_upload
          FROM source_uploads
          WHERE (status='pending' OR (status='processing' AND lease_until<=now())) AND attempts<3
          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
          IF selected_upload IS NULL THEN RETURN; END IF;
          RETURN QUERY UPDATE source_uploads SET status='processing',worker_id=worker,
            lease_until=now()+make_interval(secs=>ttl),attempts=attempts+1,updated_at=now()
          WHERE tenant_id=selected_tenant AND source_uploads.upload_id=selected_upload
          RETURNING source_uploads.upload_id,source_uploads.tenant_id;
        END; $$;

        CREATE FUNCTION claim_next_index_job(worker text, ttl integer)
        RETURNS TABLE(source_id text,index_version text,tenant uuid)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
        DECLARE selected_tenant uuid; selected_source text; selected_version text;
        BEGIN
          UPDATE source_index_jobs SET
            status=CASE WHEN total_chunks>0 THEN 'lexical_ready' ELSE 'failed' END,
            lease_until=NULL,error=coalesce(error,'attempts_exhausted'),updated_at=now()
          WHERE status IN ('processing','lexical_ready') AND lease_until<=now() AND attempts>=3;
          SELECT tenant_id,source_index_jobs.source_id,source_index_jobs.index_version
          INTO selected_tenant,selected_source,selected_version
          FROM source_index_jobs
          WHERE (status='pending' OR (status IN ('processing','lexical_ready') AND lease_until<=now()))
            AND attempts<3
          ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
          IF selected_source IS NULL THEN RETURN; END IF;
          RETURN QUERY UPDATE source_index_jobs SET status='processing',worker_id=worker,
            lease_until=now()+make_interval(secs=>ttl),attempts=attempts+1,updated_at=now()
          WHERE tenant_id=selected_tenant AND source_index_jobs.source_id=selected_source
            AND source_index_jobs.index_version=selected_version
          RETURNING source_index_jobs.source_id,source_index_jobs.index_version,
            source_index_jobs.tenant_id;
        END; $$;
        REVOKE ALL ON FUNCTION claim_next_source_upload(text,integer),claim_next_index_job(text,integer)
          FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION claim_next_source_upload(text,integer),claim_next_index_job(text,integer)
          TO research_app;
        """,
        prepare=False,
    )


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
