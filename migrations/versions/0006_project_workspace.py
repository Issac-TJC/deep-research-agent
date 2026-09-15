"""Add project workspaces, long-term memory, profiles, and weekly digests."""

from alembic import op

revision = "0006"
down_revision = "0005"


TABLES = (
    "users",
    "projects",
    "project_members",
    "conversations",
    "messages",
    "project_artifacts",
    "conversation_summaries",
    "project_memories",
    "memory_evidence",
    "research_profiles",
    "profile_signals",
    "research_subscriptions",
    "weekly_digests",
    "paper_candidates",
    "paper_feedback",
    "notifications",
    "audit_events",
)


def upgrade():
    connection = op.get_bind().connection.driver_connection
    connection.execute(
        """
        CREATE EXTENSION IF NOT EXISTS pgcrypto;

        CREATE TABLE users (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id),
          status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','deleted')),
          locale text NOT NULL DEFAULT 'zh', timezone text NOT NULL DEFAULT 'UTC',
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id)
        );
        CREATE TABLE projects (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), owner_user_id uuid NOT NULL,
          name text NOT NULL CHECK(char_length(name) BETWEEN 1 AND 120),
          objective text NOT NULL DEFAULT '' CHECK(char_length(objective)<=4000),
          description text NOT NULL DEFAULT '' CHECK(char_length(description)<=6000),
          language text NOT NULL DEFAULT 'zh' CHECK(language IN ('zh','en')),
          timezone text NOT NULL DEFAULT 'UTC', tags jsonb NOT NULL DEFAULT '[]'::jsonb,
          exclusions jsonb NOT NULL DEFAULT '[]'::jsonb, settings jsonb NOT NULL DEFAULT '{}'::jsonb,
          status text NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','archived','deleted_pending','purged')),
          is_default boolean NOT NULL DEFAULT false, deleted_at timestamptz, purge_after timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,owner_user_id) REFERENCES users(tenant_id,id)
        );
        CREATE UNIQUE INDEX projects_one_default_per_tenant ON projects(tenant_id) WHERE is_default;
        CREATE INDEX projects_list ON projects(tenant_id,status,updated_at DESC);
        CREATE TABLE project_members (
          tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid NOT NULL, user_id uuid NOT NULL,
          role text NOT NULL CHECK(role IN ('owner','editor','viewer')), created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,project_id,user_id),
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
          FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,id)
        );
        CREATE TABLE conversations (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid NOT NULL,
          title text NOT NULL CHECK(char_length(title) BETWEEN 1 AND 200),
          status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
          summary_id uuid, parent_conversation_id uuid, fork_message_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
        );
        CREATE INDEX conversations_project ON conversations(tenant_id,project_id,status,updated_at DESC);
        CREATE TABLE messages (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), conversation_id uuid NOT NULL,
          role text NOT NULL CHECK(role IN ('user','assistant','system')),
          content text NOT NULL, status text NOT NULL DEFAULT 'completed'
            CHECK(status IN ('pending','running','completed','failed','cancelled')),
          sequence bigint NOT NULL CHECK(sequence>0), created_by uuid, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,conversation_id,sequence),
          FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id)
        );
        CREATE INDEX messages_conversation ON messages(tenant_id,conversation_id,sequence);
        ALTER TABLE conversations ADD CONSTRAINT conversations_parent_fk
          FOREIGN KEY(tenant_id,parent_conversation_id) REFERENCES conversations(tenant_id,id);
        ALTER TABLE conversations ADD CONSTRAINT conversations_fork_message_fk
          FOREIGN KEY(tenant_id,fork_message_id) REFERENCES messages(tenant_id,id);

        CREATE TABLE project_artifacts (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid NOT NULL,
          kind text NOT NULL CHECK(kind IN ('file','url','report')), logical_id uuid NOT NULL,
          source_version_id text, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
          status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','removed')),
          removed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,project_id,source_version_id),
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
        );
        CREATE INDEX project_artifacts_project ON project_artifacts(tenant_id,project_id,status,updated_at DESC);
        CREATE TABLE conversation_summaries (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), conversation_id uuid NOT NULL,
          covered_until_message_id uuid NOT NULL, version int NOT NULL CHECK(version>0),
          content jsonb NOT NULL, quality jsonb NOT NULL DEFAULT '{}'::jsonb,
          source_summary_id uuid, model text, prompt_version text, estimated_tokens int NOT NULL DEFAULT 0,
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id),
          UNIQUE(tenant_id,conversation_id,version),
          FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id),
          FOREIGN KEY(tenant_id,covered_until_message_id) REFERENCES messages(tenant_id,id)
        );
        CREATE TABLE project_memories (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid NOT NULL,
          type text NOT NULL CHECK(type IN ('goal','scope','constraint','decision','preference','conclusion','question','entity','todo')),
          content text NOT NULL, status text NOT NULL DEFAULT 'candidate'
            CHECK(status IN ('candidate','confirmed','rejected','superseded','deleted')),
          confidence real NOT NULL DEFAULT 1 CHECK(confidence BETWEEN 0 AND 1),
          valid_from timestamptz NOT NULL DEFAULT now(), valid_to timestamptz,
          supersedes_id uuid, conflicts_with_id uuid, created_by uuid,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
          FOREIGN KEY(tenant_id,supersedes_id) REFERENCES project_memories(tenant_id,id),
          FOREIGN KEY(tenant_id,conflicts_with_id) REFERENCES project_memories(tenant_id,id)
        );
        CREATE INDEX project_memories_project ON project_memories(tenant_id,project_id,status,type,updated_at DESC);
        CREATE TABLE memory_evidence (
          tenant_id uuid NOT NULL REFERENCES tenants(id), memory_id uuid NOT NULL,
          object_type text NOT NULL CHECK(object_type IN ('user','message','claim','artifact','digest')),
          object_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,memory_id,object_type,object_id),
          FOREIGN KEY(tenant_id,memory_id) REFERENCES project_memories(tenant_id,id)
        );
        CREATE TABLE research_profiles (
          tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL,
          version int NOT NULL DEFAULT 1 CHECK(version>0), learning_enabled boolean NOT NULL DEFAULT false,
          preferences jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY(tenant_id,user_id), FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,id)
        );
        CREATE TABLE profile_signals (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), profile_user_id uuid NOT NULL,
          field text NOT NULL, value text NOT NULL,
          source text NOT NULL CHECK(source IN ('explicit','inferred','imported')),
          scope text NOT NULL CHECK(scope IN ('user','project')), project_id uuid,
          confidence real NOT NULL DEFAULT 1 CHECK(confidence BETWEEN 0 AND 1),
          state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','rejected','frozen')),
          provenance jsonb NOT NULL DEFAULT '{}'::jsonb, rejected_until timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,profile_user_id) REFERENCES research_profiles(tenant_id,user_id),
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
        );
        CREATE INDEX profile_signals_profile ON profile_signals(tenant_id,profile_user_id,state,field);

        CREATE TABLE research_subscriptions (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid NOT NULL,
          owner_user_id uuid NOT NULL, name text NOT NULL, query_config jsonb NOT NULL,
          schedule jsonb NOT NULL, status text NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','paused','deleted')),
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id),
          FOREIGN KEY(tenant_id,owner_user_id) REFERENCES users(tenant_id,id)
        );
        CREATE INDEX research_subscriptions_project ON research_subscriptions(tenant_id,project_id,status);
        CREATE TABLE weekly_digests (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), subscription_id uuid NOT NULL,
          period_start date, period_end date,
          status text NOT NULL DEFAULT 'scheduled'
            CHECK(status IN ('draft','scheduled','collecting','screening','reviewing','writing','published','needs_review','failed','cancelled')),
          revision int NOT NULL DEFAULT 1 CHECK(revision>0), report_id text,
          quality jsonb NOT NULL DEFAULT '{}'::jsonb, query_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
          selected_count int NOT NULL DEFAULT 0 CHECK(selected_count>=0),
          created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,subscription_id,period_start,revision),
          FOREIGN KEY(tenant_id,subscription_id) REFERENCES research_subscriptions(tenant_id,id)
        );
        CREATE TABLE paper_candidates (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), digest_id uuid NOT NULL,
          canonical_id text NOT NULL, metadata jsonb NOT NULL, scores jsonb NOT NULL DEFAULT '{}'::jsonb,
          evidence_scope text NOT NULL DEFAULT 'metadata_only'
            CHECK(evidence_scope IN ('full_text','abstract_only','metadata_only')),
          state text NOT NULL DEFAULT 'candidate' CHECK(state IN ('candidate','selected','rejected')),
          selection_rationale text, created_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,digest_id,canonical_id),
          FOREIGN KEY(tenant_id,digest_id) REFERENCES weekly_digests(tenant_id,id)
        );
        CREATE TABLE paper_feedback (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL,
          canonical_id text, digest_id uuid NOT NULL,
          feedback text NOT NULL CHECK(feedback IN ('useful','irrelevant','read','saved','later','never_recommend')),
          created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(tenant_id,id),
          FOREIGN KEY(tenant_id,digest_id) REFERENCES weekly_digests(tenant_id,id),
          FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,id)
        );
        CREATE TABLE notifications (
          id uuid PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), user_id uuid NOT NULL,
          digest_id uuid NOT NULL, channel text NOT NULL DEFAULT 'in_app'
            CHECK(channel IN ('in_app','email','enterprise')),
          state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','sent','failed','cancelled')),
          attempts int NOT NULL DEFAULT 0 CHECK(attempts>=0), provider_message_id text, error text,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(tenant_id,id), UNIQUE(tenant_id,user_id,digest_id,channel),
          FOREIGN KEY(tenant_id,digest_id) REFERENCES weekly_digests(tenant_id,id),
          FOREIGN KEY(tenant_id,user_id) REFERENCES users(tenant_id,id)
        );
        CREATE TABLE audit_events (
          id bigserial PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants(id), project_id uuid,
          actor_user_id uuid, event_type text NOT NULL, object_type text NOT NULL, object_id text NOT NULL,
          before_value jsonb, after_value jsonb, source jsonb NOT NULL DEFAULT '{}'::jsonb,
          created_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id)
        );
        CREATE INDEX audit_events_project ON audit_events(tenant_id,project_id,created_at DESC);

        ALTER TABLE research_runs ADD COLUMN project_id uuid;
        ALTER TABLE research_runs ADD COLUMN conversation_id uuid;
        ALTER TABLE research_runs ADD COLUMN trigger_message_id uuid;
        ALTER TABLE research_runs ADD COLUMN reply_message_id uuid;
        ALTER TABLE research_runs ADD COLUMN parent_run_id uuid;
        ALTER TABLE research_runs ADD COLUMN source_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb;

        INSERT INTO users(id,tenant_id) SELECT id,id FROM tenants ON CONFLICT(id) DO NOTHING;
        INSERT INTO research_profiles(tenant_id,user_id) SELECT id,id FROM tenants ON CONFLICT DO NOTHING;
        INSERT INTO projects(id,tenant_id,owner_user_id,name,objective,is_default)
          SELECT gen_random_uuid(),id,id,'默认研究项目','历史研究与兼容 API 自动归档于此',true FROM tenants
          ON CONFLICT DO NOTHING;
        INSERT INTO project_members(tenant_id,project_id,user_id,role)
          SELECT tenant_id,id,owner_user_id,'owner' FROM projects WHERE is_default ON CONFLICT DO NOTHING;
        INSERT INTO conversations(id,tenant_id,project_id,title)
          SELECT gen_random_uuid(),tenant_id,id,'默认对话' FROM projects WHERE is_default;
        INSERT INTO messages(id,tenant_id,conversation_id,role,content,status,sequence,created_by,created_at)
          SELECT r.id,r.tenant_id,c.id,'user',coalesce(r.brief->>'question','历史研究请求'),'completed',
            row_number() OVER(PARTITION BY r.tenant_id ORDER BY r.created_at,r.id),r.tenant_id,r.created_at
          FROM research_runs r JOIN projects p ON p.tenant_id=r.tenant_id AND p.is_default
          JOIN conversations c ON c.tenant_id=p.tenant_id AND c.project_id=p.id;
        UPDATE research_runs r SET project_id=p.id,conversation_id=c.id,trigger_message_id=r.id
          FROM projects p,conversations c
          WHERE p.tenant_id=r.tenant_id AND p.is_default AND c.tenant_id=p.tenant_id AND c.project_id=p.id;
        ALTER TABLE research_runs ALTER COLUMN project_id SET NOT NULL;
        ALTER TABLE research_runs ALTER COLUMN conversation_id SET NOT NULL;
        ALTER TABLE research_runs ALTER COLUMN trigger_message_id SET NOT NULL;
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_project_fk
          FOREIGN KEY(tenant_id,project_id) REFERENCES projects(tenant_id,id);
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_conversation_fk
          FOREIGN KEY(tenant_id,conversation_id) REFERENCES conversations(tenant_id,id);
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_trigger_message_fk
          FOREIGN KEY(tenant_id,trigger_message_id) REFERENCES messages(tenant_id,id);
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_reply_message_fk
          FOREIGN KEY(tenant_id,reply_message_id) REFERENCES messages(tenant_id,id);
        ALTER TABLE research_runs ADD CONSTRAINT research_runs_parent_run_fk
          FOREIGN KEY(tenant_id,parent_run_id) REFERENCES research_runs(tenant_id,id);
        CREATE INDEX research_runs_project ON research_runs(tenant_id,project_id,created_at DESC);
        CREATE INDEX research_runs_conversation ON research_runs(tenant_id,conversation_id,created_at DESC);
        """,
        prepare=False,
    )
    for table in TABLES:
        connection.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)
              WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid);
            GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO research_app;
            """,
            prepare=False,
        )
    connection.execute("GRANT USAGE,SELECT ON SEQUENCE audit_events_id_seq TO research_app", prepare=False)


def downgrade():
    raise RuntimeError("Destructive downgrade is intentionally unsupported; restore a backup")
