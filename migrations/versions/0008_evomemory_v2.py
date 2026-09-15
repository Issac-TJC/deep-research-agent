"""Add governed EvoMemory v2 conversation and observation storage."""

from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        ALTER TABLE conversations
          ADD COLUMN memory_system_version int NOT NULL DEFAULT 2,
          ADD COLUMN memory_cutover_sequence bigint NOT NULL DEFAULT 0,
          ADD COLUMN memory_revision bigint NOT NULL DEFAULT 0;
        UPDATE conversations c SET memory_cutover_sequence=coalesce(
          (SELECT max(m.sequence) FROM messages m WHERE m.conversation_id=c.id),0
        );

        ALTER TABLE conversation_summaries ADD COLUMN memory_version int NOT NULL DEFAULT 1;
        ALTER TABLE conversation_summaries ALTER COLUMN memory_version SET DEFAULT 2;
        ALTER TABLE project_memories ADD COLUMN memory_version int NOT NULL DEFAULT 1;
        ALTER TABLE project_memories ALTER COLUMN memory_version SET DEFAULT 2;
        ALTER TABLE memory_evidence DROP CONSTRAINT memory_evidence_object_type_check;
        ALTER TABLE memory_evidence ADD CONSTRAINT memory_evidence_object_type_check
          CHECK(object_type IN ('user','message','run','claim','artifact','digest'));
        ALTER TABLE profile_signals ADD COLUMN memory_version int NOT NULL DEFAULT 1;
        ALTER TABLE profile_signals ALTER COLUMN memory_version SET DEFAULT 2;
        ALTER TABLE profile_signals ADD COLUMN category text NOT NULL DEFAULT 'user_profile'
          CHECK(category IN ('assistant_style','user_profile','research_taste','project_profile'));
        ALTER TABLE profile_signals DROP CONSTRAINT profile_signals_state_check;
        ALTER TABLE profile_signals ADD CONSTRAINT profile_signals_state_check
          CHECK(state IN ('candidate','active','rejected','frozen'));

        CREATE TABLE observations (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          owner_user_id uuid NOT NULL, project_id uuid,
          scope text NOT NULL CHECK(scope IN ('user_global','project')),
          memory_type text NOT NULL CHECK(memory_type IN ('semantic','procedural','episodic')),
          summary text NOT NULL CHECK(char_length(summary) BETWEEN 1 AND 500),
          body text NOT NULL CHECK(char_length(body) BETWEEN 1 AND 8000),
          why_it_matters text NOT NULL DEFAULT '' CHECK(char_length(why_it_matters)<=2000),
          status text NOT NULL DEFAULT 'candidate'
            CHECK(status IN ('candidate','confirmed','rejected','superseded','deleted')),
          confidence real NOT NULL DEFAULT 0.8 CHECK(confidence BETWEEN 0 AND 1),
          fingerprint text NOT NULL, source_type text NOT NULL
            CHECK(source_type IN ('user','message','run','claim','artifact','digest','worker')),
          source_id text NOT NULL, source_digest text NOT NULL,
          supersedes_id uuid, embedding vector(768),
          search_vector tsvector GENERATED ALWAYS AS
            (to_tsvector('simple',summary||' '||body||' '||why_it_matters)) STORED,
          memory_version int NOT NULL DEFAULT 2,
          created_by uuid, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,owner_user_id,fingerprint,memory_version),
          FOREIGN KEY(tenant_id,owner_user_id) REFERENCES users(tenant_id,id),
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
          FOREIGN KEY(tenant_id,supersedes_id) REFERENCES observations(tenant_id,id),
          CHECK((scope='project' AND project_id IS NOT NULL) OR
                (scope='user_global' AND project_id IS NULL))
        );
        CREATE INDEX observations_project ON observations
          (tenant_id,owner_user_id,project_id,status,memory_type,updated_at DESC);
        CREATE INDEX observations_search ON observations USING gin(search_vector);
        CREATE INDEX observations_embedding ON observations
          USING hnsw(embedding vector_cosine_ops) WHERE embedding IS NOT NULL;

        CREATE TABLE observation_evidence (
          tenant_id uuid NOT NULL REFERENCES tenants(id), observation_id uuid NOT NULL,
          object_type text NOT NULL CHECK(object_type IN ('user','message','run','claim','artifact','digest')),
          object_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,observation_id,object_type,object_id),
          FOREIGN KEY(tenant_id,observation_id) REFERENCES observations(tenant_id,id)
        );

        CREATE TABLE observation_relations (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          source_observation_id uuid NOT NULL, target_observation_id uuid NOT NULL,
          relation text NOT NULL CHECK(relation IN ('complements','contradicts','supersedes')),
          reason text NOT NULL CHECK(char_length(reason) BETWEEN 1 AND 500),
          created_by text NOT NULL DEFAULT 'worker', created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,source_observation_id,target_observation_id,relation),
          FOREIGN KEY(tenant_id,source_observation_id) REFERENCES observations(tenant_id,id),
          FOREIGN KEY(tenant_id,target_observation_id) REFERENCES observations(tenant_id,id),
          CHECK(source_observation_id<>target_observation_id)
        );

        CREATE TABLE memory_jobs (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          project_id uuid NOT NULL, conversation_id uuid,
          kind text NOT NULL CHECK(kind IN
            ('conversation_compaction','turn_distillation','run_distillation',
             'observation_linking','embedding_backfill')),
          source_type text NOT NULL, source_id text NOT NULL, source_digest text NOT NULL,
          status text NOT NULL DEFAULT 'queued'
            CHECK(status IN ('queued','running','completed','failed','dead_letter')),
          payload jsonb NOT NULL DEFAULT '{}'::jsonb, result jsonb NOT NULL DEFAULT '{}'::jsonb,
          usage jsonb NOT NULL DEFAULT '{}'::jsonb, attempts int NOT NULL DEFAULT 0,
          error text, available_at timestamptz NOT NULL DEFAULT now(),
          lease_until timestamptz, worker_id text,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,kind,source_type,source_id,source_digest),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
          FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id)
        );
        CREATE INDEX memory_jobs_queue ON memory_jobs(status,available_at,created_at);
        CREATE INDEX memory_jobs_project ON memory_jobs(tenant_id,project_id,created_at DESC);

        CREATE FUNCTION claim_next_memory_job(worker text, ttl integer)
         RETURNS TABLE(job_id uuid, tenant uuid)
         LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
         DECLARE selected uuid;
         BEGIN
           SELECT id INTO selected FROM memory_jobs
           WHERE (status='queued' AND available_at<=now())
              OR (status='running' AND lease_until<=now())
           ORDER BY available_at,created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
           IF selected IS NULL THEN RETURN; END IF;
           RETURN QUERY UPDATE memory_jobs SET status='running',worker_id=worker,
             attempts=attempts+1,lease_until=now()+make_interval(secs=>ttl),updated_at=now()
             WHERE id=selected RETURNING id,tenant_id;
         END;
         $$;
        REVOKE ALL ON FUNCTION claim_next_memory_job(text,integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION claim_next_memory_job(text,integer) TO research_app;

        ALTER TABLE observations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE observations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON observations
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE observation_evidence ENABLE ROW LEVEL SECURITY;
        ALTER TABLE observation_evidence FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON observation_evidence
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE observation_relations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE observation_relations FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON observation_relations
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
        ALTER TABLE memory_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON memory_jobs
          USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
          WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);

        GRANT SELECT,INSERT,UPDATE,DELETE ON observations,observation_evidence,
          observation_relations,memory_jobs TO research_app;
        """
    )


def downgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        DROP FUNCTION claim_next_memory_job(text,integer);
        DROP TABLE memory_jobs,observation_relations,observation_evidence,observations;
        ALTER TABLE profile_signals DROP COLUMN category;
        ALTER TABLE profile_signals DROP COLUMN memory_version;
        ALTER TABLE profile_signals DROP CONSTRAINT profile_signals_state_check;
        ALTER TABLE profile_signals ADD CONSTRAINT profile_signals_state_check
          CHECK(state IN ('active','rejected','frozen'));
        ALTER TABLE project_memories DROP COLUMN memory_version;
        ALTER TABLE memory_evidence DROP CONSTRAINT memory_evidence_object_type_check;
        ALTER TABLE memory_evidence ADD CONSTRAINT memory_evidence_object_type_check
          CHECK(object_type IN ('user','message','claim','artifact','digest'));
        ALTER TABLE conversation_summaries DROP COLUMN memory_version;
        ALTER TABLE conversations DROP COLUMN memory_revision;
        ALTER TABLE conversations DROP COLUMN memory_cutover_sequence;
        ALTER TABLE conversations DROP COLUMN memory_system_version;
        """
    )
