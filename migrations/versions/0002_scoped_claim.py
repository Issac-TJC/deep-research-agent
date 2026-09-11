"""Permit evaluation to claim only its own expected run without stealing unrelated work."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    op.get_bind().connection.driver_connection.execute(
        """
    DROP FUNCTION claim_next_run(text,integer);
    CREATE FUNCTION claim_next_run(worker text, ttl integer, expected uuid DEFAULT NULL)
    RETURNS TABLE(run_id uuid,tenant uuid,token bigint)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
    DECLARE selected uuid;
    BEGIN
    PERFORM pg_advisory_xact_lock(184711);
    IF EXISTS(SELECT 1 FROM research_runs WHERE status='running' AND lease_until>now()) THEN RETURN; END IF;
    SELECT id INTO selected FROM research_runs
    WHERE (status='queued' OR (status='running' AND lease_until<=now()))
      AND (expected IS NULL OR id=expected)
    ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED;
    IF selected IS NULL THEN RETURN; END IF;
    RETURN QUERY UPDATE research_runs SET status='running',worker_id=worker,fence=fence+1,
      lease_until=now()+make_interval(secs=>ttl),started_at=coalesce(started_at,now())
    WHERE id=selected RETURNING id,tenant_id,fence;
    END;
    $$;
    REVOKE ALL ON FUNCTION claim_next_run(text,integer,uuid) FROM PUBLIC;
    GRANT EXECUTE ON FUNCTION claim_next_run(text,integer,uuid) TO research_app;
    """,
        prepare=False,
    )


def downgrade():
    raise RuntimeError("Restore a backup instead of destructive downgrade")
