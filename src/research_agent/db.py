from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic_core import to_jsonable_python

from research_agent.context import (
    BUDGET_CUMULATIVE_CAPS,
    BUDGET_GROUPS,
    SOFT_TOKEN_TARGET,
    degradation_level,
)
from research_agent.contracts import CreateRun, RunProfile, uid
from research_agent.settings import Settings


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class StaleLease(Exception):
    pass


class BudgetExceeded(Exception):
    pass


SENSITIVE_PROFILE_FIELDS = {
    "race", "ethnicity", "religion", "politics", "political", "health", "medical",
    "sexual_orientation", "biometric", "financial", "民族", "种族", "宗教", "政治",
    "健康", "医疗", "性取向", "生物识别", "财务",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def json_safe(value: Any) -> Any:
    return to_jsonable_python(value, fallback=str)


def vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.9g}" for value in values) + "]"


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = AsyncConnectionPool(
            settings.database_url, min_size=1, max_size=12, open=False, kwargs={"row_factory": dict_row}
        )

    async def open(self):
        await self.pool.open()
        await self.pool.wait()
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            ).fetchone()
            if row["rolsuper"] or row["rolbypassrls"]:
                raise RuntimeError("Runtime database role must not be superuser or BYPASSRLS")

    async def close(self):
        await self.pool.close()

    @asynccontextmanager
    async def tx(self, tenant: str):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',%s,true)", (tenant,))
                yield conn

    async def _project_row(self, conn, project_id: str, *, writable: bool = False) -> dict:
        row = await (await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))).fetchone()
        if not row or row["status"] in {"deleted_pending", "purged"}:
            raise NotFound(project_id)
        if writable and row["status"] != "active":
            raise Conflict("project_not_active")
        return row

    async def execute_idempotent(
        self,
        tenant: str,
        scope: str,
        key: str,
        payload: Any,
        operation: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Serialize create requests and replay their persisted JSON response."""
        request_hash = digest(payload)
        async with self.tx(tenant) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{tenant}:{scope}:{key}",)
            )
            old = await (
                await conn.execute(
                    "SELECT * FROM request_idempotency WHERE scope=%s AND idempotency_key=%s",
                    (scope, key),
                )
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused with different request")
                if old["state"] == "completed":
                    return old["response"], False
                raise Conflict("idempotent request is still in progress")
            await conn.execute(
                """INSERT INTO request_idempotency
                (tenant_id,scope,idempotency_key,request_hash) VALUES (%s,%s,%s,%s)""",
                (tenant, scope, key, request_hash),
            )
        try:
            result = await operation()
        except Exception:
            async with self.tx(tenant) as conn:
                await conn.execute(
                    """DELETE FROM request_idempotency WHERE scope=%s AND idempotency_key=%s
                    AND request_hash=%s AND state='in_progress'""",
                    (scope, key, request_hash),
                )
            raise
        safe = json_safe(result)
        async with self.tx(tenant) as conn:
            await conn.execute(
                """UPDATE request_idempotency SET state='completed',response=%s,updated_at=now()
                WHERE scope=%s AND idempotency_key=%s AND request_hash=%s""",
                (Jsonb(safe), scope, key, request_hash),
            )
        return safe, True

    async def auth(self, api_key: str) -> str | None:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        async with self.pool.connection() as conn:
            r = await (
                await conn.execute("SELECT authenticate_api_key(%s) AS tenant", (key_hash,))
            ).fetchone()
            return str(r["tenant"]) if r["tenant"] else None

    async def guard(self, conn, run_id: str, fence: int):
        r = await (
            await conn.execute("SELECT * FROM research_runs WHERE id=%s FOR UPDATE", (run_id,))
        ).fetchone()
        if not r or r["fence"] != fence or r["status"] != "running":
            raise StaleLease("run cancelled, completed, or ownership changed")
        valid = await (
            await conn.execute("SELECT %s::timestamptz > now() AS ok", (r["lease_until"],))
        ).fetchone()
        if not valid["ok"]:
            raise StaleLease("lease expired")
        return r

    async def event(self, conn, tenant: str, run_id: str, kind: str, payload: dict):
        r = await (
            await conn.execute("UPDATE research_runs SET seq=seq+1 WHERE id=%s RETURNING seq", (run_id,))
        ).fetchone()
        if not r:
            raise NotFound(run_id)
        await conn.execute(
            "INSERT INTO run_events VALUES (%s,%s,%s,%s,%s,now())",
            (tenant, run_id, r["seq"], kind, Jsonb(payload)),
        )

    async def emit_event(self, tenant: str, run_id: str, kind: str, payload: dict):
        async with self.tx(tenant) as conn:
            await self.event(conn, tenant, run_id, kind, payload)

    async def record_degradation(self, tenant: str, run_id: str, level: int, reason: str):
        async with self.tx(tenant) as conn:
            exists = await (
                await conn.execute(
                    """SELECT 1 FROM run_events WHERE run_id=%s AND event_type='budget.degraded'
                    AND payload->>'level'=%s LIMIT 1""",
                    (run_id, str(level)),
                )
            ).fetchone()
            if not exists:
                await self.event(
                    conn,
                    tenant,
                    run_id,
                    "budget.degraded",
                    {"level": level, "reason": reason, "soft_target": SOFT_TOKEN_TARGET},
                )

    async def _ensure_workspace(self, conn, tenant: str) -> tuple[dict, dict, dict]:
        await conn.execute(
            """INSERT INTO users(id,tenant_id) VALUES (%s,%s)
            ON CONFLICT(id) DO NOTHING""",
            (tenant, tenant),
        )
        await conn.execute(
            """INSERT INTO research_profiles(tenant_id,user_id) VALUES (%s,%s)
            ON CONFLICT(tenant_id,user_id) DO NOTHING""",
            (tenant, tenant),
        )
        project = await (
            await conn.execute("SELECT * FROM projects WHERE is_default ORDER BY created_at LIMIT 1")
        ).fetchone()
        if not project:
            project_id = uid()
            project = await (
                await conn.execute(
                    """INSERT INTO projects
                    (id,tenant_id,owner_user_id,name,objective,is_default)
                    VALUES (%s,%s,%s,'默认研究项目','兼容单次研究与长期项目工作区',true)
                    RETURNING *""",
                    (project_id, tenant, tenant),
                )
            ).fetchone()
            await conn.execute(
                """INSERT INTO project_members(tenant_id,project_id,user_id,role)
                VALUES (%s,%s,%s,'owner') ON CONFLICT DO NOTHING""",
                (tenant, project_id, tenant),
            )
        conversation = await (
            await conn.execute(
                """SELECT * FROM conversations WHERE project_id=%s AND title='默认对话'
                ORDER BY created_at LIMIT 1""",
                (project["id"],),
            )
        ).fetchone()
        if not conversation:
            conversation = await (
                await conn.execute(
                    """INSERT INTO conversations(id,tenant_id,project_id,title)
                    VALUES (%s,%s,%s,'默认对话') RETURNING *""",
                    (uid(), tenant, project["id"]),
                )
            ).fetchone()
        user = await (await conn.execute("SELECT * FROM users WHERE id=%s", (tenant,))).fetchone()
        return user, project, conversation

    async def _append_message(
        self,
        conn,
        tenant: str,
        conversation_id: str,
        role: str,
        content: str,
        status: str = "completed",
        metadata: dict | None = None,
        message_id: str | None = None,
    ) -> dict:
        await conn.execute("SELECT id FROM conversations WHERE id=%s FOR UPDATE", (conversation_id,))
        sequence = await (
            await conn.execute(
                "SELECT coalesce(max(sequence),0)+1 AS sequence FROM messages WHERE conversation_id=%s",
                (conversation_id,),
            )
        ).fetchone()
        return await (
            await conn.execute(
                """INSERT INTO messages
                (id,tenant_id,conversation_id,role,content,status,sequence,created_by,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    message_id or uid(),
                    tenant,
                    conversation_id,
                    role,
                    content,
                    status,
                    sequence["sequence"],
                    tenant if role == "user" else None,
                    Jsonb(metadata or {}),
                ),
            )
        ).fetchone()

    async def _conversation_event(
        self, conn, tenant: str, conversation_id: str, event_type: str, payload: dict
    ) -> None:
        await conn.execute(
            """INSERT INTO conversation_events(tenant_id,conversation_id,event_type,payload)
            VALUES (%s,%s,%s,%s)""",
            (tenant, conversation_id, event_type, Jsonb(payload)),
        )

    async def conversation_events(
        self, tenant: str, conversation_id: str, after_id: int = 0
    ) -> list[dict]:
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    """SELECT c.id FROM conversations c JOIN projects p ON p.id=c.project_id
                    WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation:
                raise NotFound(conversation_id)
            return await (
                await conn.execute(
                    """SELECT * FROM conversation_events WHERE conversation_id=%s AND id>%s
                    ORDER BY id LIMIT 200""",
                    (conversation_id, after_id),
                )
            ).fetchall()

    async def create_run(self, tenant: str, request: CreateRun, key: str) -> tuple[dict, bool]:
        payload = request.model_dump(mode="json")
        request_hash = digest(payload)
        async with self.tx(tenant) as conn:
            # The queue advisory lock also serializes concurrent idempotency-key creation.
            room = await (await conn.execute("SELECT queue_has_room() AS room")).fetchone()
            old = await (
                await conn.execute("SELECT * FROM research_runs WHERE idempotency_key=%s", (key,))
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused with different request")
                return old, False
            if not room["room"]:
                raise Conflict("run queue is full")
            for source_id in request.brief.upload_ids:
                r = await (
                    await conn.execute("SELECT id FROM records WHERE id=%s AND kind='source'", (source_id,))
                ).fetchone()
                if not r:
                    raise NotFound("upload not accessible")
            run_id = uid()
            _, default_project, default_conversation = await self._ensure_workspace(conn, tenant)
            project_id = request.project_id or str(default_project["id"])
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))
            ).fetchone()
            if not project:
                raise NotFound("project not accessible")
            if project["status"] != "active":
                raise Conflict("project_not_active")
            if request.conversation_id:
                conversation = await (
                    await conn.execute(
                        "SELECT * FROM conversations WHERE id=%s AND project_id=%s",
                        (request.conversation_id, project_id),
                    )
                ).fetchone()
                if not conversation:
                    raise NotFound("conversation not accessible")
                if conversation["status"] != "active":
                    raise Conflict("conversation_not_active")
            elif project_id == str(default_project["id"]):
                conversation = default_conversation
            else:
                conversation = await (
                    await conn.execute(
                        """INSERT INTO conversations(id,tenant_id,project_id,title)
                        VALUES (%s,%s,%s,%s) RETURNING *""",
                        (uid(), tenant, project_id, request.brief.question[:120]),
                    )
                ).fetchone()
            if request.trigger_message_id:
                trigger = await (
                    await conn.execute(
                        """SELECT * FROM messages WHERE id=%s AND conversation_id=%s AND role='user'""",
                        (request.trigger_message_id, conversation["id"]),
                    )
                ).fetchone()
                if not trigger:
                    raise NotFound("trigger message not accessible")
            else:
                trigger = await self._append_message(
                    conn, tenant, str(conversation["id"]), "user", request.brief.question
                )
            reply = await self._append_message(
                conn,
                tenant,
                str(conversation["id"]),
                "assistant",
                "研究任务已进入队列。",
                "running",
                {"run_id": run_id},
            )
            await self._conversation_event(
                conn,
                tenant,
                str(conversation["id"]),
                "message.created",
                {"message_id": str(trigger["id"]), "sequence": trigger["sequence"], "role": "user"},
            )
            await self._conversation_event(
                conn,
                tenant,
                str(conversation["id"]),
                "message.created",
                {"message_id": str(reply["id"]), "sequence": reply["sequence"], "role": "assistant"},
            )
            r = await (
                await conn.execute(
                    """INSERT INTO research_runs
                (id,tenant_id,idempotency_key,request_hash,brief,profile,mode,configuration,
                 project_id,conversation_id,trigger_message_id,reply_message_id,parent_run_id,source_snapshot,
                 run_kind,context_snapshot)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        run_id,
                        tenant,
                        key,
                        request_hash,
                        Jsonb(payload["brief"]),
                        Jsonb(payload["profile"]),
                        self.settings.research_mode,
                        Jsonb(self.settings.execution_snapshot()),
                        project_id,
                        conversation["id"],
                        trigger["id"],
                        reply["id"],
                        request.parent_run_id,
                        Jsonb(request.brief.upload_ids),
                        request.run_kind,
                        Jsonb(request.context_snapshot),
                    ),
                )
            ).fetchone()
            await conn.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (conversation["id"],))
            await conn.execute("UPDATE projects SET updated_at=now() WHERE id=%s", (project_id,))
            await self.event(conn, tenant, run_id, "run.queued", {"mode": self.settings.research_mode})
            await self._conversation_event(
                conn,
                tenant,
                str(conversation["id"]),
                "run.linked",
                {"run_id": run_id, "message_id": str(reply["id"]), "status": "queued"},
            )
            return r, True

    async def resolve_upload_ids(self, tenant: str, upload_ids: list[str]) -> list[str]:
        resolved = []
        async with self.tx(tenant) as conn:
            for item in upload_ids:
                source = await (
                    await conn.execute("SELECT id FROM records WHERE id=%s AND kind='source'", (item,))
                ).fetchone()
                if source:
                    resolved.append(item)
                    continue
                upload = await (
                    await conn.execute(
                        "SELECT status,source_id FROM source_uploads WHERE upload_id=%s", (item,)
                    )
                ).fetchone()
                if not upload:
                    raise NotFound("upload not accessible")
                if upload["status"] != "ready" or not upload["source_id"]:
                    raise Conflict("upload_not_ready")
                resolved.append(upload["source_id"])
        return resolved

    async def run(self, tenant: str, run_id: str) -> dict:
        async with self.tx(tenant) as conn:
            r = await (
                await conn.execute(
                    """SELECT r.* FROM research_runs r JOIN projects p ON p.id=r.project_id
                    WHERE r.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (run_id,),
                )
            ).fetchone()
            if not r:
                raise NotFound(run_id)
            return r

    async def runs(self, tenant: str, project_id: str | None = None) -> list[dict]:
        async with self.tx(tenant) as conn:
            if project_id:
                await self._project_row(conn, project_id)
                return await (
                    await conn.execute(
                        """SELECT * FROM research_runs WHERE project_id=%s
                        ORDER BY created_at DESC LIMIT 100""",
                        (project_id,),
                    )
                ).fetchall()
            return await (
                await conn.execute(
                    """SELECT r.* FROM research_runs r JOIN projects p ON p.id=r.project_id
                    WHERE p.status NOT IN ('deleted_pending','purged') ORDER BY r.created_at DESC LIMIT 100"""
                )
            ).fetchall()

    async def _audit(
        self,
        conn,
        tenant: str,
        project_id: str | None,
        event_type: str,
        object_type: str,
        object_id: str,
        before: dict | None = None,
        after: dict | None = None,
        source: dict | None = None,
    ):
        await conn.execute(
            """INSERT INTO audit_events
            (tenant_id,project_id,actor_user_id,event_type,object_type,object_id,before_value,after_value,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                tenant,
                project_id,
                tenant,
                event_type,
                object_type,
                object_id,
                Jsonb(json_safe(before)) if before is not None else None,
                Jsonb(json_safe(after)) if after is not None else None,
                Jsonb(json_safe(source or {})),
            ),
        )

    async def create_project(self, tenant: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            project_id, conversation_id = uid(), uid()
            row = await (
                await conn.execute(
                    """INSERT INTO projects
                    (id,tenant_id,owner_user_id,name,objective,description,language,timezone,tags,exclusions)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        project_id,
                        tenant,
                        tenant,
                        data["name"],
                        data.get("objective", ""),
                        data.get("description", ""),
                        data.get("language", "zh"),
                        data.get("timezone", "UTC"),
                        Jsonb(list(dict.fromkeys(data.get("tags", [])))),
                        Jsonb(list(dict.fromkeys(data.get("exclusions", [])))),
                    ),
                )
            ).fetchone()
            await conn.execute(
                "INSERT INTO project_members(tenant_id,project_id,user_id,role) VALUES (%s,%s,%s,'owner')",
                (tenant, project_id, tenant),
            )
            await conn.execute(
                """INSERT INTO conversations(id,tenant_id,project_id,title)
                VALUES (%s,%s,%s,'研究对话')""",
                (conversation_id, tenant, project_id),
            )
            for memory_type, content in [
                ("goal", data.get("objective", "")),
                *[("constraint", item) for item in data.get("exclusions", [])],
            ]:
                if content:
                    memory_id = uid()
                    await conn.execute(
                        """INSERT INTO project_memories
                        (id,tenant_id,project_id,type,content,status,confidence,created_by)
                        VALUES (%s,%s,%s,%s,%s,'confirmed',1,%s)""",
                        (memory_id, tenant, project_id, memory_type, content, tenant),
                    )
                    await conn.execute(
                        """INSERT INTO memory_evidence
                        (tenant_id,memory_id,object_type,object_id) VALUES (%s,%s,'user',%s)""",
                        (tenant, memory_id, tenant),
                    )
            await self._audit(
                conn, tenant, project_id, "project.created", "project", project_id, after=dict(row)
            )
            return row

    async def projects(
        self,
        tenant: str,
        query: str | None = None,
        tag: str | None = None,
        status: str = "active",
    ) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            rows = await (
                await conn.execute(
                    """SELECT p.*,
                    (SELECT count(*) FROM research_runs r WHERE r.project_id=p.id AND r.status IN ('queued','running')) AS active_runs,
                    (SELECT count(*) FROM weekly_digests d JOIN research_subscriptions s ON s.id=d.subscription_id
                     JOIN notifications n ON n.digest_id=d.id AND n.user_id=%s AND n.channel='in_app'
                     WHERE s.project_id=p.id AND d.status='published' AND n.read_at IS NULL) AS unread_digests
                    FROM projects p
                    WHERE p.status=%s
                      AND (%s::text IS NULL OR p.name ILIKE '%%'||%s||'%%' OR p.objective ILIKE '%%'||%s||'%%')
                      AND (%s::text IS NULL OR p.tags ? %s)
                    ORDER BY p.updated_at DESC,p.created_at DESC LIMIT 1000""",
                    (tenant, status, query, query, query, tag, tag),
                )
            ).fetchall()
            return rows

    async def project(self, tenant: str, project_id: str, include_deleted: bool = False) -> dict:
        async with self.tx(tenant) as conn:
            row = await (await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))).fetchone()
            if not row or (not include_deleted and row["status"] in {"deleted_pending", "purged"}):
                raise NotFound(project_id)
            return row

    async def update_project(self, tenant: str, project_id: str, changes: dict) -> dict:
        allowed = {"name", "objective", "description", "language", "timezone", "tags", "exclusions", "status"}
        changes = {key: value for key, value in changes.items() if key in allowed and value is not None}
        async with self.tx(tenant) as conn:
            before = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s FOR UPDATE", (project_id,))
            ).fetchone()
            if not before or before["status"] == "purged":
                raise NotFound(project_id)
            if before["status"] == "deleted_pending" and changes.get("status") != "active":
                raise Conflict("deleted_project_can_only_be_restored")
            if changes.get("status") == "active" and before["status"] == "deleted_pending":
                changes["deleted_at"], changes["purge_after"] = None, None
                allowed.update({"deleted_at", "purge_after"})
            if not changes:
                return before
            values = []
            assignments = []
            for key, value in changes.items():
                assignments.append(f"{key}=%s")
                values.append(Jsonb(value) if key in {"tags", "exclusions"} else value)
            values.append(project_id)
            after = await (
                await conn.execute(
                    f"UPDATE projects SET {','.join(assignments)},updated_at=now() WHERE id=%s RETURNING *",
                    values,
                )
            ).fetchone()
            if after["status"] == "archived":
                await conn.execute(
                    "UPDATE research_subscriptions SET status='paused',updated_at=now() WHERE project_id=%s AND status='active'",
                    (project_id,),
                )
            await self._audit(
                conn,
                tenant,
                project_id,
                "project.updated",
                "project",
                project_id,
                dict(before),
                dict(after),
            )
            return after

    async def delete_project(self, tenant: str, project_id: str) -> None:
        async with self.tx(tenant) as conn:
            before = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s FOR UPDATE", (project_id,))
            ).fetchone()
            if not before or before["status"] in {"deleted_pending", "purged"}:
                raise NotFound(project_id)
            if before["is_default"]:
                raise Conflict("default_project_cannot_be_deleted")
            after = await (
                await conn.execute(
                    """UPDATE projects SET status='deleted_pending',deleted_at=now(),
                    purge_after=now()+interval '30 days',updated_at=now() WHERE id=%s RETURNING *""",
                    (project_id,),
                )
            ).fetchone()
            await conn.execute(
                "UPDATE research_subscriptions SET status='paused',updated_at=now() WHERE project_id=%s",
                (project_id,),
            )
            await conn.execute(
                """UPDATE research_runs SET status='cancelled',stop_reason='project_deleted',
                finished_at=now(),lease_until=NULL WHERE project_id=%s AND status IN ('queued','running','interrupted')""",
                (project_id,),
            )
            await conn.execute(
                """UPDATE notifications n SET state='cancelled',updated_at=now()
                FROM weekly_digests d,research_subscriptions s
                WHERE n.digest_id=d.id AND d.subscription_id=s.id AND s.project_id=%s
                  AND n.state IN ('pending','sending')""",
                (project_id,),
            )
            await self._audit(
                conn,
                tenant,
                project_id,
                "project.deleted",
                "project",
                project_id,
                dict(before),
                dict(after),
            )

    async def restore_project(self, tenant: str, project_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """UPDATE projects SET status='active',deleted_at=NULL,purge_after=NULL,updated_at=now()
                    WHERE id=%s AND status='deleted_pending' AND purge_after>now() RETURNING *""",
                    (project_id,),
                )
            ).fetchone()
            if not row:
                raise NotFound(project_id)
            await self._audit(
                conn, tenant, project_id, "project.restored", "project", project_id, after=dict(row)
            )
            return row

    async def purge_expired_projects(self, tenant: str) -> list[dict]:
        """Permanently remove projects whose 30-day recovery window has expired."""
        purged: list[dict] = []
        async with self.tx(tenant) as conn:
            projects = await (
                await conn.execute(
                    """SELECT id,name FROM projects WHERE status='deleted_pending'
                    AND purge_after<=now() AND is_default=false FOR UPDATE"""
                )
            ).fetchall()
            for project in projects:
                project_id = str(project["id"])
                sources = await (
                    await conn.execute(
                        """SELECT DISTINCT a.source_version_id,r.data FROM project_artifacts a
                        JOIN records r ON r.id=a.source_version_id AND r.kind='source'
                        WHERE a.project_id=%s AND a.source_version_id IS NOT NULL
                        AND NOT EXISTS (
                          SELECT 1 FROM project_artifacts other
                          WHERE other.source_version_id=a.source_version_id AND other.project_id<>%s
                            AND other.status='active'
                        )
                        AND NOT EXISTS (
                          SELECT 1 FROM research_runs other_run JOIN projects other_project
                            ON other_project.id=other_run.project_id
                          WHERE other_run.project_id<>%s
                            AND other_project.status NOT IN ('deleted_pending','purged')
                            AND other_run.source_snapshot ? a.source_version_id
                        )""",
                        (project_id, project_id, project_id),
                    )
                ).fetchall()
                source_ids = [row["source_version_id"] for row in sources]
                object_keys = list(
                    dict.fromkeys(
                        key
                        for row in sources
                        for key in (row["data"].get("raw_key"), row["data"].get("parsed_key"))
                        if key
                    )
                )
                await conn.execute(
                    "DELETE FROM connector_attempts WHERE digest_id IN (SELECT d.id FROM weekly_digests d JOIN research_subscriptions s ON s.id=d.subscription_id WHERE s.project_id=%s)",
                    (project_id,),
                )
                for table in ("paper_feedback", "notifications", "paper_candidates"):
                    await conn.execute(
                        f"DELETE FROM {table} WHERE digest_id IN (SELECT d.id FROM weekly_digests d JOIN research_subscriptions s ON s.id=d.subscription_id WHERE s.project_id=%s)",
                        (project_id,),
                    )
                await conn.execute(
                    "DELETE FROM weekly_digests WHERE subscription_id IN (SELECT id FROM research_subscriptions WHERE project_id=%s)",
                    (project_id,),
                )
                await conn.execute("DELETE FROM research_subscriptions WHERE project_id=%s", (project_id,))
                await conn.execute(
                    "DELETE FROM message_attachments WHERE message_id IN (SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.project_id=%s)",
                    (project_id,),
                )
                await conn.execute(
                    "DELETE FROM memory_evidence WHERE memory_id IN (SELECT id FROM project_memories WHERE project_id=%s)",
                    (project_id,),
                )
                await conn.execute("DELETE FROM project_memories WHERE project_id=%s", (project_id,))
                await conn.execute(
                    """DELETE FROM observation_relations WHERE source_observation_id IN
                    (SELECT id FROM observations WHERE project_id=%s) OR target_observation_id IN
                    (SELECT id FROM observations WHERE project_id=%s)""",
                    (project_id, project_id),
                )
                await conn.execute(
                    """DELETE FROM observation_evidence WHERE observation_id IN
                    (SELECT id FROM observations WHERE project_id=%s)""",
                    (project_id,),
                )
                await conn.execute("DELETE FROM observations WHERE project_id=%s", (project_id,))
                await conn.execute("DELETE FROM memory_jobs WHERE project_id=%s", (project_id,))
                await conn.execute(
                    "DELETE FROM conversation_summaries WHERE conversation_id IN (SELECT id FROM conversations WHERE project_id=%s)",
                    (project_id,),
                )
                await conn.execute(
                    "DELETE FROM conversation_events WHERE conversation_id IN (SELECT id FROM conversations WHERE project_id=%s)",
                    (project_id,),
                )
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    await conn.execute(
                        f"""DELETE FROM {table} WHERE thread_id IN
                        (SELECT id::text FROM research_runs WHERE project_id=%s)
                        OR thread_id IN (SELECT 'conversation:'||id::text||':v2' FROM conversations WHERE project_id=%s)""",
                        (project_id, project_id),
                    )
                for table in ("actions", "run_events", "records"):
                    await conn.execute(
                        f"DELETE FROM {table} WHERE run_id IN (SELECT id FROM research_runs WHERE project_id=%s)",
                        (project_id,),
                    )
                await conn.execute("DELETE FROM research_runs WHERE project_id=%s", (project_id,))
                await conn.execute(
                    "UPDATE conversations SET fork_message_id=NULL,parent_conversation_id=NULL WHERE project_id=%s",
                    (project_id,),
                )
                await conn.execute(
                    "DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE project_id=%s)",
                    (project_id,),
                )
                await conn.execute("DELETE FROM conversations WHERE project_id=%s", (project_id,))
                await conn.execute("DELETE FROM project_artifacts WHERE project_id=%s", (project_id,))
                await conn.execute("DELETE FROM profile_signals WHERE project_id=%s", (project_id,))
                await conn.execute("DELETE FROM audit_events WHERE project_id=%s", (project_id,))
                await conn.execute("DELETE FROM project_members WHERE project_id=%s", (project_id,))
                await conn.execute(
                    "DELETE FROM request_idempotency WHERE response::text LIKE %s OR scope LIKE %s",
                    (f"%{project_id}%", f"%{project_id}%"),
                )
                if source_ids:
                    await conn.execute("DELETE FROM document_chunks WHERE source_id=ANY(%s)", (source_ids,))
                    await conn.execute("DELETE FROM source_index_jobs WHERE source_id=ANY(%s)", (source_ids,))
                    await conn.execute(
                        "DELETE FROM source_uploads WHERE source_id=ANY(%s) OR upload_id=ANY(%s)",
                        (source_ids, source_ids),
                    )
                    await conn.execute(
                        "DELETE FROM records WHERE id=ANY(%s) OR data->>'source_id'=ANY(%s)",
                        (source_ids, source_ids),
                    )
                await conn.execute("DELETE FROM projects WHERE id=%s", (project_id,))
                purged.append(
                    {"project_id": project_id, "name": project["name"], "object_keys": object_keys}
                )
        return purged

    async def create_conversation(self, tenant: str, project_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))
            ).fetchone()
            if not project:
                raise NotFound(project_id)
            if project["status"] != "active":
                raise Conflict("project_not_active")
            if data.get("parent_conversation_id"):
                parent = await (
                    await conn.execute(
                        "SELECT id FROM conversations WHERE id=%s AND project_id=%s",
                        (data["parent_conversation_id"], project_id),
                    )
                ).fetchone()
                if not parent:
                    raise NotFound("parent conversation not accessible")
            row = await (
                await conn.execute(
                    """INSERT INTO conversations
                    (id,tenant_id,project_id,title,parent_conversation_id,fork_message_id)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        project_id,
                        data.get("title", "新对话"),
                        data.get("parent_conversation_id"),
                        data.get("fork_message_id"),
                    ),
                )
            ).fetchone()
            if data.get("fork_message_id"):
                source = await (
                    await conn.execute(
                        """SELECT m.* FROM messages m JOIN conversations c ON c.id=m.conversation_id
                        WHERE m.id=%s AND c.project_id=%s""",
                        (data["fork_message_id"], project_id),
                    )
                ).fetchone()
                if not source:
                    raise NotFound("fork message not accessible")
                old_messages = await (
                    await conn.execute(
                        """SELECT m.* FROM messages m WHERE m.conversation_id=%s AND m.sequence<=%s
                        ORDER BY m.sequence""",
                        (source["conversation_id"], source["sequence"]),
                    )
                ).fetchall()
                for item in old_messages:
                    await self._append_message(
                        conn,
                        tenant,
                        str(row["id"]),
                        item["role"],
                        item["content"],
                        item["status"],
                        {**item["metadata"], "forked_from_message_id": str(item["id"])},
                    )
            await self._audit(
                conn,
                tenant,
                project_id,
                "conversation.created",
                "conversation",
                str(row["id"]),
                after=dict(row),
            )
            return row

    async def conversations(self, tenant: str, project_id: str, include_archived: bool = False) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """SELECT c.*,
                    (SELECT count(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count,
                    (SELECT max(status) FROM research_runs r WHERE r.conversation_id=c.id AND r.status IN ('queued','running')) AS active_run_status
                    FROM conversations c WHERE c.project_id=%s AND (%s OR c.status='active')
                    ORDER BY c.updated_at DESC,c.created_at DESC""",
                    (project_id, include_archived),
                )
            ).fetchall()

    async def update_conversation(self, tenant: str, conversation_id: str, changes: dict) -> dict:
        allowed = {
            key: value for key, value in changes.items() if key in {"title", "status"} and value is not None
        }
        async with self.tx(tenant) as conn:
            before = await (
                await conn.execute(
                    """SELECT c.* FROM conversations c JOIN projects p ON p.id=c.project_id
                    WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged') FOR UPDATE OF c""",
                    (conversation_id,),
                )
            ).fetchone()
            if not before:
                raise NotFound(conversation_id)
            if not allowed:
                return before
            fields, values = [], []
            for key, value in allowed.items():
                fields.append(f"{key}=%s")
                values.append(value)
            values.append(conversation_id)
            after = await (
                await conn.execute(
                    f"UPDATE conversations SET {','.join(fields)},updated_at=now() WHERE id=%s RETURNING *",
                    values,
                )
            ).fetchone()
            await self._audit(
                conn,
                tenant,
                str(before["project_id"]),
                "conversation.updated",
                "conversation",
                conversation_id,
                dict(before),
                dict(after),
            )
            return after

    async def messages(
        self,
        tenant: str,
        conversation_id: str,
        after_sequence: int = 0,
        limit: int = 200,
        cursor_id: str | None = None,
    ) -> list[dict]:
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    """SELECT c.id FROM conversations c JOIN projects p ON p.id=c.project_id
                    WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation:
                raise NotFound(conversation_id)
            cursor_sequence = 0
            if cursor_id:
                cursor_row = await (
                    await conn.execute(
                        "SELECT sequence FROM messages WHERE id=%s AND conversation_id=%s",
                        (cursor_id, conversation_id),
                    )
                ).fetchone()
                if not cursor_row:
                    raise ValueError("cursor is not valid for this conversation")
                cursor_sequence = cursor_row["sequence"]
            return await (
                await conn.execute(
                    """SELECT m.*,r.id AS run_id,r.status AS run_status,r.quality_status
                    FROM messages m LEFT JOIN research_runs r ON r.reply_message_id=m.id
                    WHERE m.conversation_id=%s AND m.sequence>%s ORDER BY m.sequence LIMIT %s""",
                    (conversation_id, max(after_sequence, cursor_sequence), limit),
                )
            ).fetchall()

    async def conversation_project(self, tenant: str, conversation_id: str) -> str:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """SELECT c.project_id,c.status AS conversation_status,p.status AS project_status
                    FROM conversations c JOIN projects p ON p.id=c.project_id
                    WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (conversation_id,),
                )
            ).fetchone()
            if not row:
                raise NotFound(conversation_id)
            if row["project_status"] != "active" or row["conversation_status"] != "active":
                raise Conflict("conversation_not_active")
            return str(row["project_id"])

    async def create_user_message(
        self, tenant: str, conversation_id: str, content: str, attachment_ids: list[str]
    ) -> dict:
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    """SELECT c.*,p.status AS project_status FROM conversations c
                    JOIN projects p ON p.id=c.project_id WHERE c.id=%s""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation:
                raise NotFound(conversation_id)
            if conversation["status"] != "active" or conversation["project_status"] != "active":
                raise Conflict("conversation_not_active")
            active_run = await (
                await conn.execute(
                    """SELECT id FROM research_runs WHERE conversation_id=%s
                    AND status IN ('queued','running') LIMIT 1""",
                    (conversation_id,),
                )
            ).fetchone()
            if active_run:
                raise Conflict("conversation_busy")
            for source_id in attachment_ids:
                source = await (
                    await conn.execute(
                        """SELECT id FROM project_artifacts WHERE project_id=%s AND source_version_id=%s
                        AND status='active'""",
                        (conversation["project_id"], source_id),
                    )
                ).fetchone()
                if not source:
                    raise NotFound("attachment not in project")
            row = await self._append_message(
                conn, tenant, conversation_id, "user", content, metadata={"attachment_ids": attachment_ids}
            )
            await conn.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
            await conn.execute(
                "UPDATE projects SET updated_at=now() WHERE id=%s", (conversation["project_id"],)
            )
            return {**row, "project_id": conversation["project_id"]}

    async def create_message_run(
        self,
        tenant: str,
        conversation_id: str,
        content: str,
        attachment_ids: list[str],
        request: CreateRun,
        idempotency_key: str,
        semantic_payload: dict,
    ) -> tuple[dict, bool]:
        """Atomically create the user message, attachment links, reply placeholder, and Run."""
        scope = f"conversation-message:{conversation_id}"
        request_hash = digest(semantic_payload)
        async with self.tx(tenant) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"{tenant}:{scope}:{idempotency_key}",),
            )
            old = await (
                await conn.execute(
                    "SELECT * FROM request_idempotency WHERE scope=%s AND idempotency_key=%s",
                    (scope, idempotency_key),
                )
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise Conflict("idempotency key reused with different request")
                return old["response"], False

            conversation = await (
                await conn.execute(
                    """SELECT c.*,p.status AS project_status FROM conversations c
                    JOIN projects p ON p.id=c.project_id WHERE c.id=%s FOR UPDATE OF c""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation or conversation["project_status"] in {"deleted_pending", "purged"}:
                raise NotFound(conversation_id)
            if conversation["status"] != "active" or conversation["project_status"] != "active":
                raise Conflict("conversation_not_active")
            active_run = await (
                await conn.execute(
                    """SELECT id FROM research_runs WHERE conversation_id=%s
                    AND status IN ('queued','running') LIMIT 1""",
                    (conversation_id,),
                )
            ).fetchone()
            if active_run:
                raise Conflict("conversation_busy")
            artifacts = []
            for source_id in attachment_ids:
                artifact = await (
                    await conn.execute(
                        """SELECT id FROM project_artifacts WHERE project_id=%s AND source_version_id=%s
                        AND status='active'""",
                        (conversation["project_id"], source_id),
                    )
                ).fetchone()
                if not artifact:
                    raise NotFound("attachment not in project")
                artifacts.append(artifact["id"])
            for source_id in request.brief.upload_ids:
                source = await (
                    await conn.execute("SELECT id FROM records WHERE id=%s AND kind='source'", (source_id,))
                ).fetchone()
                if not source:
                    raise NotFound("upload not accessible")
            room = await (await conn.execute("SELECT queue_has_room() AS room")).fetchone()
            if not room["room"]:
                raise Conflict("run queue is full")

            run_id = uid()
            trigger = await self._append_message(
                conn,
                tenant,
                conversation_id,
                "user",
                content,
                metadata={"attachment_ids": attachment_ids},
            )
            for artifact_id in artifacts:
                await conn.execute(
                    """INSERT INTO message_attachments(tenant_id,message_id,artifact_id,relation)
                    VALUES (%s,%s,%s,'input')""",
                    (tenant, trigger["id"], artifact_id),
                )
            reply = await self._append_message(
                conn,
                tenant,
                conversation_id,
                "assistant",
                "快速回答已进入队列。" if request.run_kind == "quick_answer" else "研究任务已进入队列。",
                "running",
                {"run_id": run_id, "run_kind": request.run_kind},
            )
            await self._conversation_event(
                conn,
                tenant,
                conversation_id,
                "message.created",
                {"message_id": str(trigger["id"]), "sequence": trigger["sequence"], "role": "user"},
            )
            await self._conversation_event(
                conn,
                tenant,
                conversation_id,
                "message.created",
                {"message_id": str(reply["id"]), "sequence": reply["sequence"], "role": "assistant"},
            )
            payload = request.model_dump(mode="json")
            run = await (
                await conn.execute(
                    """INSERT INTO research_runs
                    (id,tenant_id,idempotency_key,request_hash,brief,profile,mode,configuration,
                     project_id,conversation_id,trigger_message_id,reply_message_id,parent_run_id,
                     source_snapshot,run_kind,context_snapshot)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        run_id,
                        tenant,
                        f"message:{conversation_id}:{idempotency_key}",
                        digest(payload),
                        Jsonb(payload["brief"]),
                        Jsonb(payload["profile"]),
                        self.settings.research_mode,
                        Jsonb(self.settings.execution_snapshot()),
                        conversation["project_id"],
                        conversation_id,
                        trigger["id"],
                        reply["id"],
                        request.parent_run_id,
                        Jsonb(request.brief.upload_ids),
                        request.run_kind,
                        Jsonb(request.context_snapshot),
                    ),
                )
            ).fetchone()
            await conn.execute("UPDATE conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
            await conn.execute(
                "UPDATE projects SET updated_at=now() WHERE id=%s", (conversation["project_id"],)
            )
            await self.event(
                conn,
                tenant,
                run_id,
                "run.queued",
                {"mode": self.settings.research_mode, "run_kind": request.run_kind},
            )
            await self._conversation_event(
                conn,
                tenant,
                conversation_id,
                "run.linked",
                {"run_id": run_id, "message_id": str(reply["id"]), "status": "queued"},
            )
            result = {
                "message": {**trigger, "project_id": conversation["project_id"]},
                "assistant_message": reply,
                "run": run,
                "created": True,
            }
            await conn.execute(
                """INSERT INTO request_idempotency
                (tenant_id,scope,idempotency_key,request_hash,state,response)
                VALUES (%s,%s,%s,%s,'completed',%s)""",
                (tenant, scope, idempotency_key, request_hash, Jsonb(json_safe(result))),
            )
            return json_safe(result), True

    async def add_project_artifact(self, tenant: str, project_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))
            ).fetchone()
            if not project:
                raise NotFound(project_id)
            if project["status"] != "active":
                raise Conflict("project_not_active")
            source = await (
                await conn.execute(
                    "SELECT data FROM records WHERE id=%s AND kind='source'", (data["source_version_id"],)
                )
            ).fetchone()
            if not source:
                raise NotFound("source not accessible")
            metadata = {
                "title": data.get("title") or source["data"].get("title", ""),
                "authors": data.get("authors", []),
                "year": data.get("year"),
                "doi": data.get("doi"),
                "tags": data.get("tags", []),
                "notes": data.get("notes", ""),
                "system": {"title": source["data"].get("title"), "mime": source["data"].get("mime")},
            }
            existing = await (
                await conn.execute(
                    """SELECT * FROM project_artifacts WHERE project_id=%s AND source_version_id=%s""",
                    (project_id, data["source_version_id"]),
                )
            ).fetchone()
            if existing:
                if existing["status"] == "removed":
                    return await (
                        await conn.execute(
                            """UPDATE project_artifacts SET status='active',removed_at=NULL,metadata=%s,updated_at=now()
                            WHERE id=%s RETURNING *""",
                            (Jsonb(metadata), existing["id"]),
                        )
                    ).fetchone()
                return existing
            row = await (
                await conn.execute(
                    """INSERT INTO project_artifacts
                    (id,tenant_id,project_id,kind,logical_id,source_version_id,metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        project_id,
                        data.get("kind", "file"),
                        uid(),
                        data["source_version_id"],
                        Jsonb(metadata),
                    ),
                )
            ).fetchone()
            await self._audit(
                conn,
                tenant,
                project_id,
                "artifact.added",
                "project_artifact",
                str(row["id"]),
                after=dict(row),
            )
            return row

    async def project_artifacts(
        self, tenant: str, project_id: str, include_removed: bool = False
    ) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """SELECT a.*,r.data AS source,s.status AS index_status,s.error AS index_error
                    FROM project_artifacts a
                    LEFT JOIN records r ON r.id=a.source_version_id AND r.kind='source'
                    LEFT JOIN LATERAL (
                      SELECT status,error FROM source_index_jobs j WHERE j.source_id=a.source_version_id
                      ORDER BY created_at DESC LIMIT 1
                    ) s ON true
                    WHERE a.project_id=%s AND (%s OR a.status='active') ORDER BY a.updated_at DESC""",
                    (project_id, include_removed),
                )
            ).fetchall()

    async def remove_project_artifact(self, tenant: str, project_id: str, artifact_id: str) -> None:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            before = await (
                await conn.execute(
                    "SELECT * FROM project_artifacts WHERE id=%s AND project_id=%s FOR UPDATE",
                    (artifact_id, project_id),
                )
            ).fetchone()
            if not before:
                raise NotFound(artifact_id)
            after = await (
                await conn.execute(
                    """UPDATE project_artifacts SET status='removed',removed_at=now(),updated_at=now()
                    WHERE id=%s RETURNING *""",
                    (artifact_id,),
                )
            ).fetchone()
            await self._audit(
                conn,
                tenant,
                project_id,
                "artifact.removed",
                "project_artifact",
                artifact_id,
                dict(before),
                dict(after),
            )

    async def update_project_artifact(
        self, tenant: str, project_id: str, artifact_id: str, changes: dict
    ) -> dict:
        allowed = {k: v for k, v in changes.items() if v is not None}
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            before = await (
                await conn.execute(
                    """SELECT * FROM project_artifacts WHERE id=%s AND project_id=%s
                    AND status='active' FOR UPDATE""",
                    (artifact_id, project_id),
                )
            ).fetchone()
            if not before:
                raise NotFound(artifact_id)
            metadata = dict(before["metadata"])
            metadata.update(allowed)
            after = await (
                await conn.execute(
                    """UPDATE project_artifacts SET metadata=%s,updated_at=now()
                    WHERE id=%s RETURNING *""",
                    (Jsonb(metadata), artifact_id),
                )
            ).fetchone()
            await self._audit(
                conn, tenant, project_id, "artifact.updated", "project_artifact", artifact_id,
                dict(before), dict(after),
            )
            return after

    async def memories(self, tenant: str, project_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """SELECT m.*,(m.memory_version=2) AS active_for_context,
                    (m.memory_version<2) AS legacy,
                    coalesce(jsonb_agg(jsonb_build_object('type',e.object_type,'id',e.object_id))
                    FILTER(WHERE e.object_id IS NOT NULL),'[]'::jsonb) AS evidence
                    FROM project_memories m LEFT JOIN memory_evidence e ON e.memory_id=m.id
                    WHERE m.project_id=%s AND m.status!='deleted' GROUP BY m.id ORDER BY m.updated_at DESC""",
                    (project_id,),
                )
            ).fetchall()

    async def create_memory(self, tenant: str, project_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            project = await (
                await conn.execute("SELECT id,status FROM projects WHERE id=%s", (project_id,))
            ).fetchone()
            if not project:
                raise NotFound(project_id)
            if project["status"] != "active":
                raise Conflict("project_not_active")
            row = await (
                await conn.execute(
                    """INSERT INTO project_memories
                    (id,tenant_id,project_id,type,content,status,confidence,created_by,memory_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,2) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        project_id,
                        data["type"],
                        data["content"],
                        data.get("status", "candidate"),
                        data.get("confidence", 1),
                        tenant,
                    ),
                )
            ).fetchone()
            await conn.execute(
                """INSERT INTO memory_evidence(tenant_id,memory_id,object_type,object_id)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    tenant, row["id"], data.get("source_type", "user"),
                    data.get("source_id") or tenant,
                ),
            )
            await self._audit(
                conn, tenant, project_id, "memory.created", "memory", str(row["id"]), after=dict(row)
            )
            return row

    async def update_memory(self, tenant: str, project_id: str, memory_id: str, changes: dict) -> dict:
        allowed = {
            key: value
            for key, value in changes.items()
            if key in {"content", "status", "confidence", "supersedes_id", "conflicts_with_id"}
            and value is not None
        }
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            before = await (
                await conn.execute(
                    "SELECT * FROM project_memories WHERE id=%s AND project_id=%s FOR UPDATE",
                    (memory_id, project_id),
                )
            ).fetchone()
            if not before:
                raise NotFound(memory_id)
            if not allowed:
                return before
            for relation in ("supersedes_id", "conflicts_with_id"):
                target_id = allowed.get(relation)
                if target_id:
                    target = await (
                        await conn.execute(
                            "SELECT id FROM project_memories WHERE id=%s AND project_id=%s",
                            (target_id, project_id),
                        )
                    ).fetchone()
                    if not target:
                        raise NotFound(f"{relation} target not in project")
            if "content" in allowed and allowed["content"] != before["content"]:
                replacement = await (
                    await conn.execute(
                        """INSERT INTO project_memories
                        (id,tenant_id,project_id,type,content,status,confidence,supersedes_id,created_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            uid(), tenant, project_id, before["type"], allowed.pop("content"),
                            allowed.pop("status", before["status"]), allowed.pop("confidence", before["confidence"]),
                            memory_id, tenant,
                        ),
                    )
                ).fetchone()
                await conn.execute(
                    """UPDATE project_memories SET status='superseded',valid_to=now(),updated_at=now()
                    WHERE id=%s""",
                    (memory_id,),
                )
                await conn.execute(
                    """INSERT INTO memory_evidence(tenant_id,memory_id,object_type,object_id)
                    SELECT tenant_id,%s,object_type,object_id FROM memory_evidence WHERE memory_id=%s
                    ON CONFLICT DO NOTHING""",
                    (replacement["id"], memory_id),
                )
                await self._audit(
                    conn, tenant, project_id, "memory.superseded", "memory", memory_id,
                    dict(before), dict(replacement),
                )
                return replacement
            fields, values = [], []
            for key, value in allowed.items():
                fields.append(f"{key}=%s")
                values.append(value)
            values.append(memory_id)
            after = await (
                await conn.execute(
                    f"UPDATE project_memories SET {','.join(fields)},updated_at=now() WHERE id=%s RETURNING *",
                    values,
                )
            ).fetchone()
            await self._audit(
                conn, tenant, project_id, "memory.updated", "memory", memory_id, dict(before), dict(after)
            )
            return after

    async def set_memory_embedding(
        self, tenant: str, project_id: str, memory_id: str, embedding: list[float]
    ) -> None:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            row = await (
                await conn.execute(
                    """UPDATE project_memories SET embedding=%s::vector,updated_at=now()
                    WHERE id=%s AND project_id=%s RETURNING id""",
                    (vector_literal(embedding), memory_id, project_id),
                )
            ).fetchone()
            if not row:
                raise NotFound(memory_id)

    async def research_profile(self, tenant: str) -> dict:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            profile = await (
                await conn.execute("SELECT * FROM research_profiles WHERE user_id=%s", (tenant,))
            ).fetchone()
            signals = await (
                await conn.execute(
                    """SELECT *,(memory_version=2 AND state IN ('active','frozen')) AS active_for_context,
                    (memory_version<2) AS legacy FROM profile_signals
                    WHERE profile_user_id=%s ORDER BY updated_at DESC""",
                    (tenant,),
                )
            ).fetchall()
            return {**profile, "signals": signals}

    async def update_research_profile(self, tenant: str, changes: dict) -> dict:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            before = await (
                await conn.execute("SELECT * FROM research_profiles WHERE user_id=%s FOR UPDATE", (tenant,))
            ).fetchone()
            learning = changes.get("learning_enabled", before["learning_enabled"])
            preferences = changes.get("preferences", before["preferences"])
            after = await (
                await conn.execute(
                    """UPDATE research_profiles SET learning_enabled=%s,preferences=%s,
                    version=version+1,updated_at=now() WHERE user_id=%s RETURNING *""",
                    (learning, Jsonb(preferences), tenant),
                )
            ).fetchone()
            return after

    async def add_profile_signal(self, tenant: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            field = data["field"].strip().lower()
            if field in SENSITIVE_PROFILE_FIELDS:
                raise Conflict("sensitive_profile_field_not_allowed")
            if data.get("scope") == "project":
                await self._project_row(conn, data.get("project_id"), writable=True)
            row = await (
                await conn.execute(
                    """INSERT INTO profile_signals
                    (id,tenant_id,profile_user_id,field,value,source,scope,project_id,confidence,state,
                     provenance,rejected_until,category,memory_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      CASE WHEN %s='rejected' THEN now()+interval '180 days' ELSE NULL END,%s,2) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        tenant,
                        data["field"],
                        data["value"],
                        "explicit",
                        data.get("scope", "user"),
                        data.get("project_id"),
                        data.get("confidence", 1),
                        data.get("state", "active"),
                        Jsonb({"actor": "user"}),
                        data.get("state", "active"),
                        data.get("category", "user_profile"),
                    ),
                )
            ).fetchone()
            return row

    async def add_inferred_profile_signal(
        self, tenant: str, project_id: str, data: dict, source_job: dict
    ) -> dict | None:
        async with self.tx(tenant) as conn:
            await self._ensure_workspace(conn, tenant)
            field = str(data.get("field", "")).strip().lower()
            value = str(data.get("value", "")).strip()
            category = data.get("category", "user_profile")
            scope = data.get("scope", "user")
            if not field or not value or field in SENSITIVE_PROFILE_FIELDS:
                return None
            if category not in {"assistant_style", "user_profile", "research_taste", "project_profile"}:
                return None
            scoped_project = project_id if scope == "project" or category == "project_profile" else None
            fingerprint = digest({"field": field, "value": value.casefold(), "scope": scope, "project": scoped_project})
            rejected = await (
                await conn.execute(
                    """SELECT 1 FROM profile_signals WHERE profile_user_id=%s AND state='rejected'
                    AND rejected_until>now() AND provenance->>'fingerprint'=%s LIMIT 1""",
                    (tenant, fingerprint),
                )
            ).fetchone()
            if rejected:
                return None
            existing = await (
                await conn.execute(
                    """SELECT * FROM profile_signals WHERE profile_user_id=%s AND memory_version=2
                    AND provenance->>'fingerprint'=%s AND state!='rejected' LIMIT 1""",
                    (tenant, fingerprint),
                )
            ).fetchone()
            if existing:
                return existing
            return await (
                await conn.execute(
                    """INSERT INTO profile_signals
                    (id,tenant_id,profile_user_id,field,value,source,scope,project_id,confidence,state,
                     provenance,category,memory_version)
                    VALUES (%s,%s,%s,%s,%s,'inferred',%s,%s,%s,'candidate',%s,%s,2) RETURNING *""",
                    (
                        uid(), tenant, tenant, field, value, scope, scoped_project,
                        min(float(data.get("confidence", 0.8)), 0.95),
                        Jsonb({"fingerprint": fingerprint, "memory_job_id": str(source_job["id"]),
                               "source_id": source_job["source_id"]}),
                        category,
                    ),
                )
            ).fetchone()

    async def update_profile_signal(self, tenant: str, signal_id: str, changes: dict) -> dict:
        async with self.tx(tenant) as conn:
            before = await (
                await conn.execute(
                    "SELECT * FROM profile_signals WHERE id=%s FOR UPDATE", (signal_id,)
                )
            ).fetchone()
            if not before:
                raise NotFound(signal_id)
            value = changes.get("value", before["value"])
            state = changes.get("state", before["state"])
            row = await (
                await conn.execute(
                    """UPDATE profile_signals SET value=%s,state=%s,
                    rejected_until=CASE WHEN %s='rejected' THEN now()+interval '180 days' ELSE NULL END,
                    updated_at=now() WHERE id=%s RETURNING *""",
                    (value, state, state, signal_id),
                )
            ).fetchone()
            return row

    async def delete_profile_signal(self, tenant: str, signal_id: str) -> None:
        await self.update_profile_signal(tenant, signal_id, {"state": "rejected"})

    async def clear_inferred_profile(self, tenant: str) -> int:
        async with self.tx(tenant) as conn:
            rows = await (
                await conn.execute(
                    """UPDATE profile_signals SET state='rejected',rejected_until=now()+interval '180 days',
                    updated_at=now() WHERE profile_user_id=%s AND source='inferred' AND state!='rejected'
                    RETURNING id""",
                    (tenant,),
                )
            ).fetchall()
            return len(rows)

    async def observations(self, tenant: str, project_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """SELECT o.*,(o.memory_version=2 AND o.status='confirmed') AS active_for_context,
                    (o.memory_version<2) AS legacy,
                    (o.memory_version=2 AND o.memory_type='procedural'
                      AND o.status='confirmed') AS skill_proposal_eligible,
                    coalesce(jsonb_agg(DISTINCT jsonb_build_object('type',e.object_type,'id',e.object_id))
                      FILTER(WHERE e.object_id IS NOT NULL),'[]'::jsonb) AS evidence,
                    coalesce(jsonb_agg(DISTINCT jsonb_build_object(
                      'id',r.id,'target_id',r.target_observation_id,'relation',r.relation,'reason',r.reason))
                      FILTER(WHERE r.id IS NOT NULL),'[]'::jsonb) AS relations
                    FROM observations o
                    LEFT JOIN observation_evidence e ON e.observation_id=o.id
                    LEFT JOIN observation_relations r ON r.source_observation_id=o.id
                    WHERE o.owner_user_id=%s AND (o.project_id=%s OR o.scope='user_global')
                      AND o.status!='deleted'
                    GROUP BY o.id ORDER BY o.updated_at DESC""",
                    (tenant, project_id),
                )
            ).fetchall()

    async def create_observation(self, tenant: str, project_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            scope = data.get("scope", "project")
            scoped_project = project_id if scope == "project" else None
            normalized = " ".join(
                f"{data['memory_type']} {scope} {data['summary']} {data['body']}".casefold().split()
            )
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()
            source_id = data.get("source_id") or tenant
            source_type = data.get("source_type", "user")
            row = await (
                await conn.execute(
                    """INSERT INTO observations
                    (id,tenant_id,owner_user_id,project_id,scope,memory_type,summary,body,
                     why_it_matters,status,confidence,fingerprint,source_type,source_id,source_digest,
                     created_by,memory_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,2)
                    ON CONFLICT(tenant_id,owner_user_id,fingerprint,memory_version)
                    DO UPDATE SET updated_at=now() RETURNING *""",
                    (
                        uid(), tenant, tenant, scoped_project, scope, data["memory_type"],
                        data["summary"], data["body"], data.get("why_it_matters", ""),
                        data.get("status", "candidate"), data.get("confidence", 1), fingerprint,
                        source_type, source_id, digest({"type": source_type, "id": source_id}), tenant,
                    ),
                )
            ).fetchone()
            await conn.execute(
                """INSERT INTO observation_evidence(tenant_id,observation_id,object_type,object_id)
                VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (tenant, row["id"], source_type, source_id),
            )
            await self._audit(
                conn, tenant, project_id, "observation.created", "observation", str(row["id"]),
                after={"memory_type": row["memory_type"], "status": row["status"]},
            )
            job_digest = digest({"observation_id": str(row["id"]), "fingerprint": fingerprint})
            await self._enqueue_memory_job(
                conn, tenant, project_id, None, "embedding_backfill", "observation",
                str(row["id"]), job_digest,
                {"object_type": "observation", "object_id": str(row["id"]),
                 "text": f"{row['summary']}\n{row['body']}"},
            )
            await self._enqueue_memory_job(
                conn, tenant, project_id, None, "observation_linking", "observation",
                str(row["id"]), job_digest, {"observation_id": str(row["id"])},
            )
            return row

    async def update_observation(
        self, tenant: str, project_id: str, observation_id: str, changes: dict
    ) -> dict:
        allowed = {
            key: value for key, value in changes.items()
            if key in {"summary", "body", "why_it_matters", "status", "confidence"}
            and value is not None
        }
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            before = await (
                await conn.execute(
                    """SELECT * FROM observations WHERE id=%s AND owner_user_id=%s
                    AND (project_id=%s OR scope='user_global') FOR UPDATE""",
                    (observation_id, tenant, project_id),
                )
            ).fetchone()
            if not before:
                raise NotFound(observation_id)
            content_changed = any(
                key in allowed and allowed[key] != before[key]
                for key in {"summary", "body", "why_it_matters"}
            )
            if content_changed:
                replacement_data = dict(before)
                replacement_data.update(allowed)
                normalized = " ".join(
                    f"{before['memory_type']} {before['scope']} {replacement_data['summary']} "
                    f"{replacement_data['body']}".casefold().split()
                )
                replacement_id = uid()
                replacement = await (
                    await conn.execute(
                        """INSERT INTO observations
                        (id,tenant_id,owner_user_id,project_id,scope,memory_type,summary,body,
                         why_it_matters,status,confidence,fingerprint,source_type,source_id,source_digest,
                         supersedes_id,created_by,memory_version)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,2)
                        RETURNING *""",
                        (
                            replacement_id, tenant, tenant, before["project_id"], before["scope"],
                            before["memory_type"], replacement_data["summary"], replacement_data["body"],
                            replacement_data["why_it_matters"], replacement_data["status"],
                            replacement_data["confidence"], hashlib.sha256(normalized.encode()).hexdigest(),
                            before["source_type"], before["source_id"], before["source_digest"],
                            observation_id, tenant,
                        ),
                    )
                ).fetchone()
                await conn.execute(
                    "UPDATE observations SET status='superseded',updated_at=now() WHERE id=%s",
                    (observation_id,),
                )
                await conn.execute(
                    """INSERT INTO observation_relations
                    (id,tenant_id,source_observation_id,target_observation_id,relation,reason,created_by)
                    VALUES (%s,%s,%s,%s,'supersedes','Edited observation version','user')
                    ON CONFLICT DO NOTHING""",
                    (uid(), tenant, replacement_id, observation_id),
                )
                await conn.execute(
                    """INSERT INTO observation_evidence(tenant_id,observation_id,object_type,object_id)
                    SELECT tenant_id,%s,object_type,object_id FROM observation_evidence
                    WHERE observation_id=%s ON CONFLICT DO NOTHING""",
                    (replacement_id, observation_id),
                )
                job_digest = digest(
                    {"observation_id": replacement_id, "fingerprint": replacement["fingerprint"]}
                )
                await self._enqueue_memory_job(
                    conn, tenant, project_id, None, "embedding_backfill", "observation",
                    replacement_id, job_digest,
                    {"object_type": "observation", "object_id": replacement_id,
                     "text": f"{replacement['summary']}\n{replacement['body']}"},
                )
                await self._enqueue_memory_job(
                    conn, tenant, project_id, None, "observation_linking", "observation",
                    replacement_id, job_digest, {"observation_id": replacement_id},
                )
                await self._audit(
                    conn, tenant, project_id, "observation.superseded", "observation",
                    observation_id, before=dict(before), after=dict(replacement),
                )
                return replacement
            if not allowed:
                return before
            fields = [f"{key}=%s" for key in allowed]
            row = await (
                await conn.execute(
                    f"UPDATE observations SET {','.join(fields)},updated_at=now() WHERE id=%s RETURNING *",
                    [*allowed.values(), observation_id],
                )
            ).fetchone()
            await self._audit(
                conn, tenant, project_id, "observation.updated", "observation", observation_id,
                before=dict(before), after=dict(row),
            )
            return row

    async def set_observation_embedding(
        self, tenant: str, observation_id: str, embedding: list[float]
    ) -> None:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    "UPDATE observations SET embedding=%s::vector,updated_at=now() WHERE id=%s RETURNING id",
                    (vector_literal(embedding), observation_id),
                )
            ).fetchone()
            if not row:
                raise NotFound(observation_id)

    async def observation_link_material(
        self, tenant: str, project_id: str, observation_id: str, limit: int = 12
    ) -> dict:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            source = await (
                await conn.execute(
                    """SELECT * FROM observations WHERE id=%s AND owner_user_id=%s
                    AND (project_id=%s OR scope='user_global') AND memory_version=2""",
                    (observation_id, tenant, project_id),
                )
            ).fetchone()
            if not source:
                raise NotFound(observation_id)
            candidates = await (
                await conn.execute(
                    """SELECT id,memory_type,scope,project_id,summary,body,status,confidence,
                    ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                    CASE WHEN %s::vector IS NULL OR embedding IS NULL THEN 0
                         ELSE 1-(embedding <=> %s::vector) END AS semantic_score
                    FROM observations
                    WHERE owner_user_id=%s AND id<>%s AND memory_version=2
                      AND status IN ('candidate','confirmed')
                      AND ((%s='project' AND (project_id=%s OR scope='user_global'))
                           OR (%s='user_global' AND scope='user_global'))
                    ORDER BY semantic_score DESC,lexical_score DESC,confidence DESC,updated_at DESC
                    LIMIT %s""",
                    (
                        f"{source['summary']} {source['body']}",
                        vector_literal(source["embedding"]) if source.get("embedding") else None,
                        vector_literal(source["embedding"]) if source.get("embedding") else None,
                        tenant, observation_id, source["scope"], project_id, source["scope"], limit,
                    ),
                )
            ).fetchall()
            return {"source": source, "candidates": candidates}

    async def create_observation_relation(
        self, tenant: str, project_id: str, source_id: str, target_id: str,
        relation: str, reason: str,
    ) -> dict:
        if relation not in {"complements", "contradicts", "supersedes"}:
            raise ValueError("invalid_observation_relation")
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id, writable=True)
            rows = await (
                await conn.execute(
                    """SELECT * FROM observations WHERE owner_user_id=%s AND id=ANY(%s::uuid[])
                    AND memory_version=2""",
                    (tenant, [source_id, target_id]),
                )
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            if source_id not in by_id or target_id not in by_id or source_id == target_id:
                raise NotFound("observation relation target")
            source, target = by_id[source_id], by_id[target_id]
            allowed = (
                source["scope"] == "project"
                and str(source["project_id"]) == project_id
                and (target["scope"] == "user_global" or str(target["project_id"]) == project_id)
            ) or (source["scope"] == "user_global" and target["scope"] == "user_global")
            if not allowed:
                raise Conflict("observation_relation_scope_violation")
            return await (
                await conn.execute(
                    """INSERT INTO observation_relations
                    (id,tenant_id,source_observation_id,target_observation_id,relation,reason,created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,'worker')
                    ON CONFLICT(tenant_id,source_observation_id,target_observation_id,relation)
                    DO UPDATE SET reason=excluded.reason RETURNING *""",
                    (uid(), tenant, source_id, target_id, relation, reason[:500]),
                )
            ).fetchone()

    async def search_memory(
        self, tenant: str, project_id: str, query: str, include_candidates: bool = True,
        limit: int = 8,
    ) -> list[dict]:
        """Return bounded memory leads; callers must use read_memory for full evidence."""
        states = ["confirmed", "candidate"] if include_candidates else ["confirmed"]
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """WITH candidates AS (
                      SELECT 'project_memory' AS object_type,id,status,content AS title,
                        left(content,500) AS snippet,confidence,updated_at,
                        ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                        CASE WHEN position(lower(%s) in lower(content))>0 THEN 1 ELSE 0 END AS exact_score
                      FROM project_memories WHERE project_id=%s AND memory_version=2
                        AND status=ANY(%s) AND (valid_to IS NULL OR valid_to>now())
                      UNION ALL
                      SELECT 'observation',id,status,summary,left(body,500),confidence,updated_at,
                        ts_rank_cd(search_vector,plainto_tsquery('simple',%s)),
                        CASE WHEN position(lower(%s) in lower(summary||' '||body))>0 THEN 1 ELSE 0 END
                      FROM observations WHERE owner_user_id=%s AND memory_version=2
                        AND status=ANY(%s) AND (project_id=%s OR scope='user_global')
                    ) SELECT object_type,id,status,title,snippet,confidence,
                      exact_score,lexical_score FROM candidates
                    ORDER BY exact_score DESC,lexical_score DESC,confidence DESC,updated_at DESC
                    LIMIT %s""",
                    (query, query, project_id, states, query, query, tenant, states, project_id, limit),
                )
            ).fetchall()

    async def read_memory(
        self, tenant: str, project_id: str, object_type: str, object_id: str
    ) -> dict:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            if object_type == "project_memory":
                row = await (
                    await conn.execute(
                        """SELECT * FROM project_memories WHERE id=%s AND project_id=%s
                        AND memory_version=2 AND status IN ('confirmed','candidate')""",
                        (object_id, project_id),
                    )
                ).fetchone()
                if row:
                    evidence = await (
                        await conn.execute(
                            "SELECT object_type,object_id FROM memory_evidence WHERE memory_id=%s",
                            (object_id,),
                        )
                    ).fetchall()
            elif object_type == "observation":
                row = await (
                    await conn.execute(
                        """SELECT * FROM observations WHERE id=%s AND owner_user_id=%s
                        AND memory_version=2 AND status IN ('confirmed','candidate')
                        AND (project_id=%s OR scope='user_global')""",
                        (object_id, tenant, project_id),
                    )
                ).fetchone()
                if row:
                    evidence = await (
                        await conn.execute(
                            """SELECT object_type,object_id FROM observation_evidence
                            WHERE observation_id=%s""",
                            (object_id,),
                        )
                    ).fetchall()
            else:
                raise ValueError("invalid_memory_object_type")
            if not row:
                raise NotFound(object_id)
            return {**row, "evidence": evidence}

    async def enqueue_memory_job(
        self, tenant: str, project_id: str, conversation_id: str | None,
        kind: str, source_type: str, source_id: str, source_digest: str, payload: dict | None = None,
    ) -> dict:
        async with self.tx(tenant) as conn:
            return await self._enqueue_memory_job(
                conn, tenant, project_id, conversation_id, kind, source_type, source_id,
                source_digest, payload or {},
            )

    async def _enqueue_memory_job(
        self, conn, tenant: str, project_id: str, conversation_id: str | None,
        kind: str, source_type: str, source_id: str, source_digest: str, payload: dict,
    ) -> dict:
        row = await (
            await conn.execute(
                """INSERT INTO memory_jobs
                (id,tenant_id,project_id,conversation_id,kind,source_type,source_id,source_digest,payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,kind,source_type,source_id,source_digest)
                DO UPDATE SET updated_at=memory_jobs.updated_at RETURNING *""",
                (uid(), tenant, project_id, conversation_id, kind, source_type, source_id,
                 source_digest, Jsonb(payload)),
            )
        ).fetchone()
        if conversation_id:
            announced = await (
                await conn.execute(
                    """SELECT 1 FROM conversation_events WHERE conversation_id=%s
                    AND event_type='memory.job.queued' AND payload->>'job_id'=%s LIMIT 1""",
                    (conversation_id, str(row["id"])),
                )
            ).fetchone()
            if not announced:
                await self._conversation_event(
                    conn, tenant, conversation_id, "memory.job.queued",
                    {"job_id": str(row["id"]), "kind": kind, "status": row["status"]},
                )
        return row

    async def memory_job(self, tenant: str, job_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (await conn.execute("SELECT * FROM memory_jobs WHERE id=%s", (job_id,))).fetchone()
            if not row:
                raise NotFound(job_id)
            return row

    async def claim_memory_job(self) -> dict | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM claim_next_memory_job(%s,%s)",
                    (self.settings.worker_id, self.settings.lease_seconds),
                )
            ).fetchone()
            return {"job_id": str(row["job_id"]), "tenant": str(row["tenant"])} if row else None

    async def finish_memory_job(
        self, tenant: str, job_id: str, *, result: dict | None = None,
        usage: dict | None = None, error: str | None = None,
    ) -> None:
        async with self.tx(tenant) as conn:
            job = await (
                await conn.execute("SELECT * FROM memory_jobs WHERE id=%s FOR UPDATE", (job_id,))
            ).fetchone()
            if not job:
                raise NotFound(job_id)
            if error and job["attempts"] < 3:
                await conn.execute(
                    """UPDATE memory_jobs SET status='queued',error=%s,lease_until=NULL,
                    available_at=now()+make_interval(secs=>attempts*attempts*5),updated_at=now()
                    WHERE id=%s""",
                    (error[:1000], job_id),
                )
                status = "queued"
            else:
                status = "dead_letter" if error else "completed"
                await conn.execute(
                    """UPDATE memory_jobs SET status=%s,result=%s,usage=%s,error=%s,
                    lease_until=NULL,updated_at=now() WHERE id=%s""",
                    (status, Jsonb(result or {}), Jsonb(usage or {}), error[:1000] if error else None, job_id),
                )
            if job["conversation_id"]:
                await self._conversation_event(
                    conn, tenant, str(job["conversation_id"]), f"memory.job.{status}",
                    {"job_id": job_id, "kind": job["kind"], "status": status},
                )
                if status == "completed" and job["kind"] == "conversation_compaction":
                    await self._conversation_event(
                        conn, tenant, str(job["conversation_id"]), "conversation.compacted",
                        {"job_id": job_id, **(result or {})},
                    )
                created = sum(
                    int((result or {}).get(key, 0))
                    for key in ("memories", "observations", "profiles")
                )
                if status == "completed" and created:
                    await self._conversation_event(
                        conn, tenant, str(job["conversation_id"]), "memory.candidate.created",
                        {"job_id": job_id, "count": created, "counts": result or {}},
                    )
            await self._audit(
                conn, tenant, str(job["project_id"]), f"memory.job.{status}", "memory_job", job_id,
                after={"kind": job["kind"], "status": status, "usage": usage or {},
                       "result": result or {}, "error": error[:1000] if error else None},
            )

    async def create_subscription(self, tenant: str, project_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (project_id,))
            ).fetchone()
            if not project:
                raise NotFound(project_id)
            if project["status"] != "active":
                raise Conflict("project_not_active")
            query_config = {
                "topic": data["topic"],
                "query_terms": data.get("query_terms", []),
                "exclusions": data.get("exclusions", []),
                "language": data.get("language", "zh"),
                "paper_count": data.get("paper_count", 5),
                "lookback_days": data.get("lookback_days", 14),
                "allow_needs_review_delivery": data.get("allow_needs_review_delivery", False),
            }
            schedule = {
                "weekday": data.get("weekday", 0),
                "delivery_time": data.get("delivery_time", "09:00"),
                "timezone": data.get("timezone", "UTC"),
            }
            row = await (
                await conn.execute(
                    """INSERT INTO research_subscriptions
                    (id,tenant_id,project_id,owner_user_id,name,query_config,schedule)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        project_id,
                        tenant,
                        data.get("name", "论文周报"),
                        Jsonb(query_config),
                        Jsonb(schedule),
                    ),
                )
            ).fetchone()
            await self._audit(
                conn,
                tenant,
                project_id,
                "subscription.created",
                "subscription",
                str(row["id"]),
                after=dict(row),
            )
            return row

    async def subscriptions(self, tenant: str, project_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    """SELECT s.*,(SELECT count(*) FROM weekly_digests d WHERE d.subscription_id=s.id) AS digest_count
                    FROM research_subscriptions s WHERE project_id=%s AND status!='deleted' ORDER BY updated_at DESC""",
                    (project_id,),
                )
            ).fetchall()

    async def subscription(self, tenant: str, subscription_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """SELECT s.* FROM research_subscriptions s JOIN projects p ON p.id=s.project_id
                    WHERE s.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (subscription_id,),
                )
            ).fetchone()
            if not row or row["status"] == "deleted":
                raise NotFound(subscription_id)
            return row

    async def update_subscription(self, tenant: str, subscription_id: str, changes: dict) -> dict:
        async with self.tx(tenant) as conn:
            before = await (
                await conn.execute(
                    """SELECT s.* FROM research_subscriptions s JOIN projects p ON p.id=s.project_id
                    WHERE s.id=%s AND p.status NOT IN ('deleted_pending','purged') FOR UPDATE OF s""",
                    (subscription_id,),
                )
            ).fetchone()
            if not before or before["status"] == "deleted":
                raise NotFound(subscription_id)
            query_config, schedule = dict(before["query_config"]), dict(before["schedule"])
            for key in (
                "topic", "query_terms", "exclusions", "language", "paper_count", "lookback_days",
                "allow_needs_review_delivery",
            ):
                if changes.get(key) is not None:
                    query_config[key] = changes[key]
            for key in ("weekday", "delivery_time", "timezone"):
                if changes.get(key) is not None:
                    schedule[key] = changes[key]
            after = await (
                await conn.execute(
                    """UPDATE research_subscriptions SET name=%s,query_config=%s,schedule=%s,status=%s,updated_at=now()
                    WHERE id=%s RETURNING *""",
                    (
                        changes.get("name") or before["name"],
                        Jsonb(query_config),
                        Jsonb(schedule),
                        changes.get("status") or before["status"],
                        subscription_id,
                    ),
                )
            ).fetchone()
            if after["status"] == "paused":
                await conn.execute(
                    """UPDATE notifications n SET state='cancelled',updated_at=now()
                    FROM weekly_digests d WHERE n.digest_id=d.id AND d.subscription_id=%s AND n.state='pending'""",
                    (subscription_id,),
                )
            await self._audit(
                conn,
                tenant,
                str(before["project_id"]),
                "subscription.updated",
                "subscription",
                subscription_id,
                dict(before),
                dict(after),
            )
            return after

    async def create_digest_preview(self, tenant: str, subscription_id: str) -> dict:
        async with self.tx(tenant) as conn:
            subscription = await (
                await conn.execute(
                    """SELECT s.* FROM research_subscriptions s JOIN projects p ON p.id=s.project_id
                    WHERE s.id=%s AND s.status!='deleted' AND p.status='active'""",
                    (subscription_id,),
                )
            ).fetchone()
            if not subscription:
                raise NotFound(subscription_id)
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (subscription["project_id"],))
            ).fetchone()
            memories = await (
                await conn.execute(
                    """SELECT id,type,content FROM project_memories WHERE project_id=%s
                    AND status='confirmed' AND memory_version=2
                    ORDER BY updated_at DESC LIMIT 20""",
                    (subscription["project_id"],),
                )
            ).fetchall()
            signals = await (
                await conn.execute(
                    """SELECT id,field,value FROM profile_signals WHERE profile_user_id=%s
                    AND state IN ('active','frozen') AND memory_version=2
                    AND confidence>=0.7 ORDER BY updated_at DESC LIMIT 20""",
                    (tenant,),
                )
            ).fetchall()
            snapshot = {
                "topic": subscription["query_config"]["topic"],
                "query_terms": subscription["query_config"].get("query_terms", []),
                "exclusions": list(
                    dict.fromkeys(project["exclusions"] + subscription["query_config"].get("exclusions", []))
                ),
                "lookback_days": subscription["query_config"].get("lookback_days", 14),
                "paper_count": subscription["query_config"].get("paper_count", 5),
                "connectors": ["openalex", "crossref", "arxiv", "pubmed"],
                "memory_ids": [str(item["id"]) for item in memories],
                "profile_signal_ids": [str(item["id"]) for item in signals],
                "memory_terms": [item["content"] for item in memories],
                "profile_terms": [item["value"] for item in signals],
            }
            row = await (
                await conn.execute(
                    """INSERT INTO weekly_digests
                    (id,tenant_id,subscription_id,status,revision,quality,query_snapshot)
                    VALUES (%s,%s,%s,'draft',1,%s,%s) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        subscription_id,
                        Jsonb({"state": "preview", "disclosure": "候选将通过论文连接器获取；预览不进入正式去重窗口"}),
                        Jsonb(snapshot),
                    ),
                )
            ).fetchone()
            return row

    async def prepare_digest_run(
        self, tenant: str, subscription_id: str, period_start, period_end
    ) -> tuple[dict, bool]:
        async with self.tx(tenant) as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"digest:{subscription_id}:{period_start}",)
            )
            subscription = await (
                await conn.execute(
                    "SELECT * FROM research_subscriptions WHERE id=%s FOR UPDATE", (subscription_id,)
                )
            ).fetchone()
            if not subscription or subscription["status"] == "deleted":
                raise NotFound(subscription_id)
            if subscription["status"] != "active":
                raise Conflict("subscription_not_active")
            existing = await (
                await conn.execute(
                    """SELECT * FROM weekly_digests WHERE subscription_id=%s AND period_start=%s
                    AND revision=1 ORDER BY created_at DESC LIMIT 1""",
                    (subscription_id, period_start),
                )
            ).fetchone()
            if existing:
                return existing, False
            project = await (
                await conn.execute("SELECT * FROM projects WHERE id=%s", (subscription["project_id"],))
            ).fetchone()
            if not project or project["status"] != "active":
                raise Conflict("project_not_active")
            memories = await (
                await conn.execute(
                    """SELECT id,content FROM project_memories WHERE project_id=%s AND status='confirmed'
                    ORDER BY updated_at DESC LIMIT 20""",
                    (subscription["project_id"],),
                )
            ).fetchall()
            snapshot = {
                **subscription["query_config"],
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "active_connectors": ["openalex", "crossref", "arxiv", "pubmed"],
                "project_exclusions": project["exclusions"],
                "memory_ids": [str(item["id"]) for item in memories],
                "evidence_policy": "full_text_or_explicit_abstract_metadata_disclosure",
            }
            row = await (
                await conn.execute(
                    """INSERT INTO weekly_digests
                    (id,tenant_id,subscription_id,period_start,period_end,status,revision,quality,query_snapshot)
                    VALUES (%s,%s,%s,%s,%s,'scheduled',1,%s,%s) RETURNING *""",
                    (uid(), tenant, subscription_id, period_start, period_end, Jsonb({}), Jsonb(snapshot)),
                )
            ).fetchone()
            await self._audit(
                conn,
                tenant,
                str(subscription["project_id"]),
                "digest.scheduled",
                "digest",
                str(row["id"]),
                after={"period_start": period_start.isoformat(), "period_end": period_end.isoformat()},
            )
            return row, True

    async def link_digest_run(self, tenant: str, digest_id: str, run_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """UPDATE weekly_digests SET run_id=%s,status='collecting'
                    WHERE id=%s AND run_id IS NULL RETURNING *""",
                    (run_id, digest_id),
                )
            ).fetchone()
            if not row:
                row = await (
                    await conn.execute("SELECT * FROM weekly_digests WHERE id=%s", (digest_id,))
                ).fetchone()
            if not row:
                raise NotFound(digest_id)
            return row

    async def fail_digest(self, tenant: str, digest_id: str, error: str) -> None:
        async with self.tx(tenant) as conn:
            await conn.execute(
                """UPDATE weekly_digests SET status='failed',quality=%s WHERE id=%s""",
                (Jsonb({"state": "failed", "error": error[:500]}), digest_id),
            )

    async def digests(self, tenant: str, subscription_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            subscription = await (
                await conn.execute(
                    """SELECT s.id FROM research_subscriptions s JOIN projects p ON p.id=s.project_id
                    WHERE s.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (subscription_id,),
                )
            ).fetchone()
            if not subscription:
                raise NotFound(subscription_id)
            return await (
                await conn.execute(
                    "SELECT * FROM weekly_digests WHERE subscription_id=%s ORDER BY created_at DESC",
                    (subscription_id,),
                )
            ).fetchall()

    async def save_digest_collection(
        self, tenant: str, digest_id: str, candidates: list[dict], attempts: list[dict], selected: int
    ) -> dict:
        async with self.tx(tenant) as conn:
            digest_row = await (
                await conn.execute(
                    """SELECT d.*,s.project_id FROM weekly_digests d
                    JOIN research_subscriptions s ON s.id=d.subscription_id WHERE d.id=%s FOR UPDATE OF d""",
                    (digest_id,),
                )
            ).fetchone()
            if not digest_row:
                raise NotFound(digest_id)
            await self._project_row(conn, str(digest_row["project_id"]), writable=True)
            await conn.execute("DELETE FROM connector_attempts WHERE digest_id=%s", (digest_id,))
            for attempt in attempts:
                await conn.execute(
                    """INSERT INTO connector_attempts
                    (id,tenant_id,digest_id,connector,query,status,attempt,result_count,latency_ms,
                     provider_cost_usd,error,finished_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())""",
                    (
                        uid(), tenant, digest_id, attempt["connector"], Jsonb(attempt["query"]),
                        attempt["status"], attempt["attempt"], attempt["result_count"],
                        attempt.get("latency_ms"), attempt.get("provider_cost_usd"), attempt.get("error"),
                    ),
                )
            await conn.execute("DELETE FROM paper_candidates WHERE digest_id=%s", (digest_id,))
            for index, item in enumerate(candidates):
                await conn.execute(
                    """INSERT INTO paper_candidates
                    (id,tenant_id,digest_id,canonical_id,metadata,scores,evidence_scope,state,selection_rationale)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        uid(), tenant, digest_id, item["canonical_id"],
                        Jsonb({k: v for k, v in item.items() if k not in {"scores", "evidence_scope", "selection_rationale"}}),
                        Jsonb(item["scores"]), item["evidence_scope"],
                        "selected" if index < selected else "candidate", item["selection_rationale"],
                    ),
                )
            states = {item["connector"]: item["status"] for item in attempts}
            row = await (
                await conn.execute(
                    """UPDATE weekly_digests SET selected_count=%s,status=CASE WHEN period_start IS NULL
                    THEN 'draft' ELSE 'screening' END,
                    quality=quality || %s WHERE id=%s RETURNING *""",
                    (
                        min(selected, len(candidates)),
                        Jsonb({"connector_states": states, "candidate_count": len(candidates)}),
                        digest_id,
                    ),
                )
            ).fetchone()
            await self._audit(
                conn, tenant, str(digest_row["project_id"]), "digest.candidates.collected", "digest",
                digest_id, after={"count": len(candidates), "selected": min(selected, len(candidates)), "connectors": states},
            )
            return row

    async def digest_detail(self, tenant: str, digest_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """SELECT d.*,s.project_id,s.name AS subscription_name FROM weekly_digests d
                    JOIN research_subscriptions s ON s.id=d.subscription_id
                    JOIN projects p ON p.id=s.project_id
                    WHERE d.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (digest_id,),
                )
            ).fetchone()
            if not row:
                raise NotFound(digest_id)
            candidates = await (
                await conn.execute(
                    "SELECT * FROM paper_candidates WHERE digest_id=%s ORDER BY (state='selected') DESC,(scores->>'total')::numeric DESC",
                    (digest_id,),
                )
            ).fetchall()
            attempts = await (
                await conn.execute(
                    "SELECT * FROM connector_attempts WHERE digest_id=%s ORDER BY connector,attempt",
                    (digest_id,),
                )
            ).fetchall()
            return {**row, "candidates": candidates, "connector_attempts": attempts}

    async def feedback_for_subscription(self, tenant: str, subscription_id: str) -> dict[str, list[str]]:
        async with self.tx(tenant) as conn:
            rows = await (
                await conn.execute(
                    """SELECT f.canonical_id,f.feedback FROM paper_feedback f JOIN weekly_digests d ON d.id=f.digest_id
                    WHERE d.subscription_id=%s AND f.canonical_id IS NOT NULL""",
                    (subscription_id,),
                )
            ).fetchall()
            result: dict[str, list[str]] = {}
            for row in rows:
                result.setdefault(row["canonical_id"], []).append(row["feedback"])
            recent = await (
                await conn.execute(
                    """SELECT DISTINCT c.canonical_id FROM paper_candidates c JOIN weekly_digests d ON d.id=c.digest_id
                    WHERE d.subscription_id=%s AND d.period_start>=current_date-interval '8 weeks'
                      AND c.state='selected'""",
                    (subscription_id,),
                )
            ).fetchall()
            for row in recent:
                result.setdefault(row["canonical_id"], []).append("recently_recommended")
            return result

    async def publish_digest(self, tenant: str, digest_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """SELECT d.*,s.owner_user_id,s.project_id FROM weekly_digests d
                    JOIN research_subscriptions s ON s.id=d.subscription_id WHERE d.id=%s FOR UPDATE OF d""",
                    (digest_id,),
                )
            ).fetchone()
            if not row:
                raise NotFound(digest_id)
            await self._project_row(conn, str(row["project_id"]), writable=True)
            if row["status"] not in {"needs_review", "published"}:
                raise Conflict("digest_not_publishable")
            published = await (
                await conn.execute(
                    """UPDATE weekly_digests SET status='published',published_at=coalesce(published_at,now())
                    WHERE id=%s RETURNING *""",
                    (digest_id,),
                )
            ).fetchone()
            await conn.execute(
                """INSERT INTO notifications(id,tenant_id,user_id,digest_id,channel,state,attempts,payload)
                VALUES (%s,%s,%s,%s,'in_app','sent',1,%s)
                ON CONFLICT(tenant_id,user_id,digest_id,channel) DO NOTHING""",
                (uid(), tenant, row["owner_user_id"], digest_id, Jsonb({"quality": row["quality"]})),
            )
            return published

    async def add_digest_feedback(self, tenant: str, digest_id: str, data: dict) -> dict:
        async with self.tx(tenant) as conn:
            digest = await (
                await conn.execute(
                    """SELECT d.* FROM weekly_digests d JOIN research_subscriptions s ON s.id=d.subscription_id
                    JOIN projects p ON p.id=s.project_id WHERE d.id=%s
                    AND p.status NOT IN ('deleted_pending','purged')""",
                    (digest_id,),
                )
            ).fetchone()
            if not digest:
                raise NotFound(digest_id)
            row = await (
                await conn.execute(
                    """INSERT INTO paper_feedback(id,tenant_id,user_id,canonical_id,digest_id,feedback)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (uid(), tenant, tenant, data.get("canonical_paper_id"), digest_id, data["feedback"]),
                )
            ).fetchone()
            return row

    async def notifications(self, tenant: str, unread_only: bool = False) -> list[dict]:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT n.*,d.status AS digest_status,s.name AS subscription_name,s.project_id
                    FROM notifications n JOIN weekly_digests d ON d.id=n.digest_id
                    JOIN research_subscriptions s ON s.id=d.subscription_id
                    JOIN projects p ON p.id=s.project_id
                    WHERE n.user_id=%s AND p.status NOT IN ('deleted_pending','purged')
                      AND (%s=false OR n.read_at IS NULL)
                    ORDER BY n.created_at DESC LIMIT 200""",
                    (tenant, unread_only),
                )
            ).fetchall()

    async def read_notification(self, tenant: str, notification_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """UPDATE notifications SET read_at=coalesce(read_at,now()),updated_at=now()
                    WHERE id=%s AND user_id=%s RETURNING *""",
                    (notification_id, tenant),
                )
            ).fetchone()
            if not row:
                raise NotFound(notification_id)
            return row

    async def conversation_memory_material(self, tenant: str, conversation_id: str) -> dict:
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    """SELECT c.*,p.objective,p.exclusions,p.status AS project_status
                    FROM conversations c JOIN projects p ON p.id=c.project_id WHERE c.id=%s""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation or conversation["project_status"] in {"deleted_pending", "purged"}:
                raise NotFound(conversation_id)
            summary = None
            covered_sequence = conversation["memory_cutover_sequence"]
            if conversation["summary_id"]:
                summary = await (
                    await conn.execute(
                        """SELECT s.*,m.sequence AS covered_sequence FROM conversation_summaries s
                        JOIN messages m ON m.id=s.covered_until_message_id
                        WHERE s.id=%s AND s.memory_version=2""",
                        (conversation["summary_id"],),
                    )
                ).fetchone()
                if summary:
                    covered_sequence = summary["covered_sequence"]
            rows = await (
                await conn.execute(
                    """SELECT id,role,content,status,sequence FROM messages
                    WHERE conversation_id=%s AND sequence>%s ORDER BY sequence""",
                    (conversation_id, covered_sequence),
                )
            ).fetchall()
            closed = []
            for row in rows:
                if row["status"] not in {"completed", "failed", "cancelled"}:
                    break
                closed.append(row)
            while closed and closed[-1]["role"] != "assistant":
                closed.pop()
            return {"conversation": conversation, "summary": summary, "messages": closed}

    async def save_semantic_summary(
        self, tenant: str, conversation_id: str, content: dict, quality: dict,
        covered_message_id: str, model: str,
    ) -> dict:
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    "SELECT * FROM conversations WHERE id=%s FOR UPDATE", (conversation_id,)
                )
            ).fetchone()
            if not conversation:
                raise NotFound(conversation_id)
            covered = await (
                await conn.execute(
                    """SELECT id,sequence FROM messages WHERE id=%s AND conversation_id=%s
                    AND role='assistant' AND status IN ('completed','failed','cancelled')""",
                    (covered_message_id, conversation_id),
                )
            ).fetchone()
            if not covered or covered["sequence"] <= conversation["memory_cutover_sequence"]:
                raise Conflict("invalid_summary_boundary")
            previous = await (
                await conn.execute(
                    """SELECT * FROM conversation_summaries WHERE id=%s AND memory_version=2""",
                    (conversation["summary_id"],),
                )
            ).fetchone() if conversation["summary_id"] else None
            version = await (
                await conn.execute(
                    """SELECT coalesce(max(version),0)+1 AS version FROM conversation_summaries
                    WHERE conversation_id=%s AND memory_version=2""",
                    (conversation_id,),
                )
            ).fetchone()
            row = await (
                await conn.execute(
                    """INSERT INTO conversation_summaries
                    (id,tenant_id,conversation_id,covered_until_message_id,version,content,quality,
                     source_summary_id,model,prompt_version,estimated_tokens,memory_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'semantic-v1',%s,2) RETURNING *""",
                    (
                        uid(), tenant, conversation_id, covered_message_id, version["version"],
                        Jsonb(content), Jsonb(quality), previous["id"] if previous else None, model,
                        max(1, len(json.dumps(content, ensure_ascii=False)) // 3),
                    ),
                )
            ).fetchone()
            await conn.execute(
                """UPDATE conversations SET summary_id=%s,memory_revision=memory_revision+1,
                updated_at=now() WHERE id=%s""",
                (row["id"], conversation_id),
            )
            await self._conversation_event(
                conn, tenant, conversation_id, "conversation.compacted",
                {"summary_id": str(row["id"]), "version": row["version"], "quality": quality},
            )
            return row

    async def conversation_memory_state(self, tenant: str, conversation_id: str) -> dict:
        material = await self.conversation_memory_material(tenant, conversation_id)
        conversation = material["conversation"]
        async with self.tx(tenant) as conn:
            jobs = await (
                await conn.execute(
                    """SELECT id,kind,status,attempts,error,created_at,updated_at FROM memory_jobs
                    WHERE conversation_id=%s ORDER BY created_at DESC LIMIT 20""",
                    (conversation_id,),
                )
            ).fetchall()
        return {
            "memory_system_version": conversation["memory_system_version"],
            "memory_cutover_sequence": conversation["memory_cutover_sequence"],
            "memory_revision": conversation["memory_revision"],
            "summary": material["summary"],
            "uncompacted_message_count": len(material["messages"]),
            "jobs": jobs,
        }

    async def summarize_conversation(self, tenant: str, conversation_id: str) -> dict | None:
        """Create a legacy v1 extractive checkpoint for export compatibility only."""
        async with self.tx(tenant) as conn:
            conversation = await (
                await conn.execute(
                    """SELECT c.* FROM conversations c JOIN projects p ON p.id=c.project_id
                    WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                    (conversation_id,),
                )
            ).fetchone()
            if not conversation:
                raise NotFound(conversation_id)
            count = await (
                await conn.execute(
                    """SELECT count(*) AS n,coalesce(sum(char_length(content)),0) AS chars
                    FROM messages WHERE conversation_id=%s AND role='user' AND status='completed'""",
                    (conversation_id,),
                )
            ).fetchone()
            capacity = count["chars"] / 3 / 64000
            if count["n"] == 0 or (count["n"] % 8 and capacity < 0.70):
                return None
            previous = await (
                await conn.execute(
                    """SELECT s.*,m.sequence AS covered_sequence FROM conversation_summaries s
                    JOIN messages m ON m.id=s.covered_until_message_id
                    WHERE s.conversation_id=%s ORDER BY s.version DESC LIMIT 1""",
                    (conversation_id,),
                )
            ).fetchone()
            rows = await (
                await conn.execute(
                    """SELECT id,role,content,sequence,status FROM messages
                    WHERE conversation_id=%s AND sequence>%s ORDER BY sequence""",
                    (conversation_id, previous["covered_sequence"] if previous else 0),
                )
            ).fetchall()
            closed = []
            for item in rows:
                if item["status"] not in {"completed", "failed", "cancelled"}:
                    break
                closed.append(item)
            # A user turn whose assistant placeholder is still pending is not a closed protocol.
            while closed and closed[-1]["role"] != "assistant":
                closed.pop()
            if not closed:
                return None
            covered = closed[-1]
            version = await (
                await conn.execute(
                    "SELECT coalesce(max(version),0)+1 AS version FROM conversation_summaries WHERE conversation_id=%s",
                    (conversation_id,),
                )
            ).fetchone()
            content = {
                "strategy": "incremental_extractive_v2",
                "closed_messages": [
                    {
                        "id": str(item["id"]),
                        "role": item["role"],
                        "sequence": item["sequence"],
                        "content": item["content"][:1200],
                    }
                    for item in closed[-64:]
                ],
                "capacity_ratio": round(capacity, 4),
            }
            row = await (
                await conn.execute(
                    """INSERT INTO conversation_summaries
                    (id,tenant_id,conversation_id,covered_until_message_id,version,content,quality,
                     source_summary_id,prompt_version,estimated_tokens,memory_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'extractive-v2',%s,1) RETURNING *""",
                    (
                        uid(),
                        tenant,
                        conversation_id,
                        covered["id"],
                        version["version"],
                        Jsonb(content),
                        Jsonb({"facts_inferred": False, "protocol_closed": True, "passed": True}),
                        previous["id"] if previous else None,
                        sum(len(item["content"]) for item in content["closed_messages"]) // 3,
                    ),
                )
            ).fetchone()
            await conn.execute(
                "UPDATE conversations SET summary_id=%s,updated_at=now() WHERE id=%s",
                (row["id"], conversation_id),
            )
            return row

    async def project_context(
        self,
        tenant: str,
        project_id: str,
        conversation_id: str | None = None,
        query: str = "",
        query_vector: list[float] | None = None,
    ) -> dict:
        async with self.tx(tenant) as conn:
            project = await self._project_row(conn, project_id)
            if query_vector:
                memories = await (
                    await conn.execute(
                        """WITH base AS (
                          SELECT id,type,content,status,confidence,updated_at,
                          ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                          coalesce(1-(embedding <=> %s::vector),0) AS semantic_score,
                          CASE WHEN position(lower(%s) in lower(content))>0 THEN 1 ELSE 0 END AS exact_score
                          FROM project_memories WHERE project_id=%s AND status='confirmed'
                            AND memory_version=2 AND (valid_to IS NULL OR valid_to>now())
                            AND EXISTS (SELECT 1 FROM memory_evidence me
                              WHERE me.memory_id=project_memories.id)
                        ), ranked AS (
                          SELECT *,row_number() OVER(ORDER BY lexical_score DESC,updated_at DESC) AS lr,
                          row_number() OVER(ORDER BY semantic_score DESC,updated_at DESC) AS sr FROM base
                        ) SELECT *,exact_score+(1.0/(60+lr))+(1.0/(60+sr)) AS rrf_score
                        FROM ranked ORDER BY rrf_score DESC,confidence DESC,updated_at DESC LIMIT 100""",
                        (query, vector_literal(query_vector), query, project_id),
                    )
                ).fetchall()
            else:
                memories = await (
                    await conn.execute(
                        """SELECT id,type,content,status,confidence,
                        ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                        0::real AS semantic_score,
                        CASE WHEN position(lower(%s) in lower(content))>0 THEN 1 ELSE 0 END AS exact_score
                        FROM project_memories WHERE project_id=%s AND status='confirmed'
                          AND memory_version=2 AND (valid_to IS NULL OR valid_to>now())
                          AND EXISTS (SELECT 1 FROM memory_evidence me
                            WHERE me.memory_id=project_memories.id)
                        ORDER BY exact_score DESC,lexical_score DESC,confidence DESC,updated_at DESC LIMIT 100""",
                        (query, query, project_id),
                    )
                ).fetchall()
            omitted_memory_ids = [str(item["id"]) for item in memories[20:]]
            memories = memories[:20]
            if query_vector:
                observations = await (
                    await conn.execute(
                        """WITH base AS (
                          SELECT id,memory_type,summary,body,why_it_matters,scope,confidence,updated_at,
                          ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                          coalesce(1-(embedding <=> %s::vector),0) AS semantic_score,
                          CASE WHEN position(lower(%s) in lower(summary||' '||body))>0
                               THEN 1 ELSE 0 END AS exact_score
                          FROM observations WHERE owner_user_id=%s AND status='confirmed'
                            AND memory_version=2 AND (project_id=%s OR scope='user_global')
                            AND EXISTS (SELECT 1 FROM observation_evidence oe
                              WHERE oe.observation_id=observations.id)
                        ), ranked AS (
                          SELECT *,row_number() OVER(ORDER BY lexical_score DESC,updated_at DESC) AS lr,
                          row_number() OVER(ORDER BY semantic_score DESC,updated_at DESC) AS sr FROM base
                        ) SELECT *,exact_score+(1.0/(60+lr))+(1.0/(60+sr)) AS rrf_score
                        FROM ranked ORDER BY rrf_score DESC,confidence DESC,updated_at DESC LIMIT 20""",
                        (query, vector_literal(query_vector), query, tenant, project_id),
                    )
                ).fetchall()
            else:
                observations = await (
                    await conn.execute(
                        """SELECT id,memory_type,summary,body,why_it_matters,scope,confidence,
                        ts_rank_cd(search_vector,plainto_tsquery('simple',%s)) AS lexical_score,
                        0::real AS semantic_score,
                        CASE WHEN position(lower(%s) in lower(summary||' '||body))>0 THEN 1 ELSE 0 END AS exact_score
                        FROM observations WHERE owner_user_id=%s AND status='confirmed' AND memory_version=2
                          AND (project_id=%s OR scope='user_global')
                          AND EXISTS (SELECT 1 FROM observation_evidence oe
                            WHERE oe.observation_id=observations.id)
                        ORDER BY exact_score DESC,lexical_score DESC,confidence DESC,updated_at DESC LIMIT 20""",
                        (query, query, tenant, project_id),
                    )
                ).fetchall()
            signals = await (
                await conn.execute(
                    """SELECT id,field,value,source,confidence,category FROM profile_signals
                    WHERE profile_user_id=%s AND state IN ('active','frozen') AND confidence>=0.7
                      AND memory_version=2
                      AND (scope='user' OR project_id=%s)
                    ORDER BY (source='explicit') DESC,updated_at DESC LIMIT 12""",
                    (tenant, project_id),
                )
            ).fetchall()
            artifacts = await (
                await conn.execute(
                    """SELECT source_version_id FROM project_artifacts
                    WHERE project_id=%s AND status='active' AND source_version_id IS NOT NULL
                    ORDER BY updated_at DESC LIMIT 20""",
                    (project_id,),
                )
            ).fetchall()
            recent = []
            summary = None
            conversation = None
            recent_runs = []
            if conversation_id:
                conversation = await (
                    await conn.execute(
                        "SELECT * FROM conversations WHERE id=%s AND project_id=%s",
                        (conversation_id, project_id),
                    )
                ).fetchone()
                if not conversation:
                    raise NotFound("conversation not accessible")
                recent = await (
                    await conn.execute(
                        """SELECT id,role,content,sequence FROM messages WHERE conversation_id=%s
                        AND sequence>%s AND status IN ('completed','failed','cancelled')
                        ORDER BY sequence DESC LIMIT 200""",
                        (conversation_id, conversation["memory_cutover_sequence"]),
                    )
                ).fetchall()
                recent.reverse()
                while recent and recent[-1]["role"] != "assistant":
                    recent.pop()
                recent_tail = recent[-12:]
                if conversation["summary_id"]:
                    summary = await (
                        await conn.execute(
                            "SELECT * FROM conversation_summaries WHERE id=%s AND memory_version=2",
                            (conversation["summary_id"],)
                        )
                    ).fetchone()
                    if summary:
                        covered = await (
                            await conn.execute(
                                "SELECT sequence FROM messages WHERE id=%s",
                                (summary["covered_until_message_id"],),
                            )
                        ).fetchone()
                        recent = [item for item in recent if item["sequence"] > covered["sequence"]]
                        recent = sorted(
                            {str(item["id"]): item for item in [*recent, *recent_tail]}.values(),
                            key=lambda item: item["sequence"],
                        )
                recent_runs = await (
                    await conn.execute(
                        """SELECT r.id,r.status,r.stop_reason,r.quality_status,r.finished_at,
                        report.id AS report_id
                        FROM research_runs r LEFT JOIN LATERAL (
                          SELECT rec.id FROM records rec WHERE rec.run_id=r.id AND rec.kind='report'
                          ORDER BY rec.created_at DESC LIMIT 1
                        ) report ON true
                        WHERE r.conversation_id=%s AND r.status IN ('completed','failed','interrupted')
                        ORDER BY r.finished_at DESC NULLS LAST LIMIT 5""",
                        (conversation_id,),
                    )
                ).fetchall()
            usage = await (
                await conn.execute(
                    """SELECT coalesce(sum(char_length(m.content)),0) AS chars FROM messages m
                    JOIN conversations c ON c.id=m.conversation_id
                    WHERE m.conversation_id=%s AND m.sequence>c.memory_cutover_sequence""",
                    (conversation_id,),
                )
            ).fetchone() if conversation_id else {"chars": 0}
            return {
                "project": project,
                "memories": memories,
                "observations": observations,
                "profile_signals": signals,
                "source_ids": [item["source_version_id"] for item in artifacts],
                "recent_messages": recent,
                "summary": summary,
                "recent_runs": recent_runs,
                "memory_revision": conversation["memory_revision"] if conversation else 0,
                "memory_cutover_sequence": (
                    conversation["memory_cutover_sequence"] if conversation else 0
                ),
                "omitted_memory_ids": omitted_memory_ids,
                "capacity_ratio": float(usage["chars"]) / 3 / 64000,
            }

    async def search_project_objects(
        self,
        tenant: str,
        project_id: str,
        query: str,
        types: list[str],
        conversation_id: str | None,
        limit: int,
    ) -> list[dict]:
        requested = set(types or ["artifact", "message", "memory", "claim", "report", "digest"])
        pattern = f"%{query}%"
        results: list[dict] = []
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            if "message" in requested:
                rows = await (
                    await conn.execute(
                        """SELECT m.id,m.conversation_id,m.sequence,m.content,m.created_at
                        FROM messages m JOIN conversations c ON c.id=m.conversation_id
                        WHERE c.project_id=%s AND (%s::uuid IS NULL OR c.id=%s) AND m.content ILIKE %s
                        ORDER BY m.created_at DESC LIMIT %s""",
                        (project_id, conversation_id, conversation_id, pattern, limit),
                    )
                ).fetchall()
                results.extend(
                    {
                        "type": "message",
                        "id": str(row["id"]),
                        "title": f"对话消息 #{row['sequence']}",
                        "snippet": row["content"][:500],
                        "conversation_id": str(row["conversation_id"]),
                        "locator": {"message_id": str(row["id"]), "sequence": row["sequence"]},
                        "created_at": row["created_at"],
                        "match_reason": "message_text",
                    }
                    for row in rows
                )
            if "memory" in requested:
                rows = await (
                    await conn.execute(
                        """SELECT * FROM project_memories WHERE project_id=%s AND status!='deleted'
                        AND content ILIKE %s ORDER BY updated_at DESC LIMIT %s""",
                        (project_id, pattern, limit),
                    )
                ).fetchall()
                results.extend(
                    {
                        "type": "memory",
                        "id": str(row["id"]),
                        "title": row["type"],
                        "snippet": row["content"][:500],
                        "locator": {"memory_id": str(row["id"])},
                        "match_reason": "memory_text",
                        "created_at": row["created_at"],
                    }
                    for row in rows
                )
            if "artifact" in requested:
                rows = await (
                    await conn.execute(
                        """SELECT * FROM project_artifacts WHERE project_id=%s AND status='active'
                        AND metadata::text ILIKE %s ORDER BY updated_at DESC LIMIT %s""",
                        (project_id, pattern, limit),
                    )
                ).fetchall()
                results.extend(
                    {
                        "type": "artifact",
                        "id": str(row["id"]),
                        "title": row["metadata"].get("title") or "项目资产",
                        "snippet": row["metadata"].get("notes", "")[:500],
                        "source_id": row["source_version_id"],
                        "locator": {"artifact_id": str(row["id"])},
                        "match_reason": "artifact_metadata",
                        "created_at": row["created_at"],
                        "tags": row["metadata"].get("tags", []),
                    }
                    for row in rows
                )
            for kind in ("claim", "report"):
                if kind not in requested:
                    continue
                rows = await (
                    await conn.execute(
                        """SELECT rec.id,rec.data,rec.run_id FROM records rec JOIN research_runs r ON r.id=rec.run_id
                        WHERE r.project_id=%s AND rec.kind=%s AND rec.data::text ILIKE %s
                        ORDER BY rec.created_at DESC LIMIT %s""",
                        (project_id, kind, pattern, limit),
                    )
                ).fetchall()
                results.extend(
                    {
                        "type": kind,
                        "id": row["id"],
                        "title": row["data"].get("title", kind),
                        "snippet": (
                            row["data"].get("text") or row["data"].get("markdown") or str(row["data"])
                        )[:500],
                        "run_id": str(row["run_id"]),
                        "locator": {"run_id": str(row["run_id"]), "record_id": row["id"]},
                        "match_reason": f"{kind}_text",
                        "created_at": row["data"].get("created_at"),
                    }
                    for row in rows
                )
            if "digest" in requested:
                rows = await (
                    await conn.execute(
                        """SELECT d.* FROM weekly_digests d JOIN research_subscriptions s ON s.id=d.subscription_id
                        WHERE s.project_id=%s AND (d.query_snapshot::text ILIKE %s OR coalesce(d.report_id,'') ILIKE %s)
                        ORDER BY d.created_at DESC LIMIT %s""",
                        (project_id, pattern, pattern, limit),
                    )
                ).fetchall()
                results.extend(
                    {
                        "type": "digest",
                        "id": str(row["id"]),
                        "title": "论文周报",
                        "snippet": str(row["query_snapshot"])[:500],
                        "locator": {"digest_id": str(row["id"])},
                        "match_reason": "digest_query",
                        "created_at": row["created_at"],
                    }
                    for row in rows
                )
        # Keep the per-type quotas intact; the service performs cross-type RRF interleaving.
        return results

    async def audit_events(self, tenant: str, project_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            await self._project_row(conn, project_id)
            return await (
                await conn.execute(
                    "SELECT * FROM audit_events WHERE project_id=%s ORDER BY created_at DESC LIMIT 200",
                    (project_id,),
                )
            ).fetchall()

    async def claim(self, expected: str | None = None) -> dict | None:
        async with self.pool.connection() as conn:
            r = await (
                await conn.execute(
                    "SELECT * FROM claim_next_run(%s,%s,%s)",
                    (self.settings.worker_id, self.settings.lease_seconds, expected),
                )
            ).fetchone()
            return {k: str(v) if k != "token" else v for k, v in r.items()} if r else None

    async def heartbeat(self, tenant: str, run_id: str, fence: int):
        async with self.tx(tenant) as conn:
            await self.guard(conn, run_id, fence)
            await conn.execute(
                "UPDATE research_runs SET lease_until=now()+make_interval(secs=>%s) WHERE id=%s",
                (self.settings.lease_seconds, run_id),
            )

    async def phase(self, tenant: str, run_id: str, fence: int, phase: str, payload: dict | None = None):
        async with self.tx(tenant) as conn:
            await self.guard(conn, run_id, fence)
            await conn.execute("UPDATE research_runs SET phase=%s WHERE id=%s", (phase, run_id))
            await self.event(conn, tenant, run_id, "run.phase", {"phase": phase, **(payload or {})})

    async def finish(
        self, tenant: str, run_id: str, fence: int, status: str, reason: str, quality="unchecked"
    ):
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError(status)
        async with self.tx(tenant) as conn:
            run = await self.guard(conn, run_id, fence)
            await conn.execute(
                """UPDATE research_runs SET status=%s,stop_reason=%s,quality_status=%s,
                finished_at=now(),lease_until=NULL WHERE id=%s""",
                (status, reason, quality, run_id),
            )
            if run.get("reply_message_id"):
                message_status = "completed" if status == "completed" else "failed"
                message = (
                    "研究已完成，可查看关联报告。"
                    if status == "completed"
                    else f"研究执行未完成：{reason[:180]}"
                )
                await conn.execute(
                    """UPDATE messages SET status=%s,content=%s,updated_at=now()
                    WHERE id=%s""",
                    (message_status, message, run["reply_message_id"]),
                )
                await conn.execute(
                    "UPDATE conversations SET updated_at=now() WHERE id=%s",
                    (run["conversation_id"],),
                )
                await conn.execute("UPDATE projects SET updated_at=now() WHERE id=%s", (run["project_id"],))
            if status == "completed" and run.get("project_id"):
                report = await (
                    await conn.execute(
                        """SELECT data FROM records WHERE run_id=%s AND kind='report'
                        ORDER BY created_at DESC LIMIT 1""",
                        (run_id,),
                    )
                ).fetchone()
                if report:
                    draft = report["data"].get("report", {})
                    nodes = draft.get("nodes", [])
                    summary = "\n".join(
                        filter(
                            None,
                            [
                                draft.get("title", "研究结论"),
                                *[
                                    f"{item.get('title', '')}: {item.get('text', '')}".strip(": ")
                                    for item in nodes[:3]
                                ],
                            ],
                        )
                    )[:6000]
                    if summary:
                        if run.get("reply_message_id"):
                            await conn.execute(
                                """UPDATE messages SET content=%s,updated_at=now() WHERE id=%s""",
                                (summary[:4000], run["reply_message_id"]),
                            )
                        memory_id = uid()
                        await conn.execute(
                            """INSERT INTO project_memories
                            (id,tenant_id,project_id,type,content,status,confidence,created_by)
                            VALUES (%s,%s,%s,'conclusion',%s,'candidate',0.8,NULL)""",
                            (memory_id, tenant, run["project_id"], summary),
                        )
                        await conn.execute(
                            """INSERT INTO memory_evidence(tenant_id,memory_id,object_type,object_id)
                            VALUES (%s,%s,'message',%s)""",
                            (tenant, memory_id, str(run["trigger_message_id"])),
                        )
                        await self._audit(
                            conn,
                            tenant,
                            str(run["project_id"]),
                            "memory.candidate.created",
                            "memory",
                            memory_id,
                            after={
                                "type": "conclusion",
                                "status": "candidate",
                                "run_id": run_id,
                            },
                            source={
                                "message_id": str(run["trigger_message_id"]),
                                "run_id": run_id,
                            },
                        )
                        if run.get("conversation_id"):
                            await self._conversation_event(
                                conn, tenant, str(run["conversation_id"]),
                                "memory.candidate.created",
                                {"memory_id": memory_id, "type": "conclusion", "run_id": run_id},
                            )
            digest = await (
                await conn.execute(
                    """SELECT d.*,s.owner_user_id,s.project_id,s.query_config FROM weekly_digests d
                    JOIN research_subscriptions s ON s.id=d.subscription_id WHERE d.run_id=%s""",
                    (run_id,),
                )
            ).fetchone()
            if digest:
                latest_report = await (
                    await conn.execute(
                        """SELECT id FROM records WHERE run_id=%s AND kind='report'
                        ORDER BY created_at DESC LIMIT 1""",
                        (run_id,),
                    )
                ).fetchone()
                allow_needs_review = bool(digest["query_config"].get("allow_needs_review_delivery", False))
                digest_status = (
                    "published"
                    if status == "completed" and quality == "passed"
                    else "published"
                    if status == "completed" and allow_needs_review
                    else "needs_review"
                    if status == "completed"
                    else "failed"
                )
                await conn.execute(
                    """UPDATE weekly_digests SET status=%s,quality=%s,report_id=%s,
                    published_at=CASE WHEN %s='published' THEN now() ELSE NULL END WHERE id=%s""",
                    (
                        digest_status,
                        Jsonb({"state": quality, "run_status": status, "stop_reason": reason}),
                        latest_report["id"] if latest_report else None,
                        digest_status,
                        digest["id"],
                    ),
                )
                if digest_status == "published":
                    await conn.execute(
                        """INSERT INTO notifications
                        (id,tenant_id,user_id,digest_id,channel,state,attempts,payload)
                        VALUES (%s,%s,%s,%s,'in_app','sent',1,%s)
                        ON CONFLICT(tenant_id,user_id,digest_id,channel) DO NOTHING""",
                        (
                            uid(), tenant, digest["owner_user_id"], digest["id"],
                            Jsonb({"quality": quality, "selected_count": digest["selected_count"]}),
                        ),
                    )
                await self._audit(
                    conn,
                    tenant,
                    str(digest["project_id"]),
                    f"digest.{digest_status}",
                    "digest",
                    str(digest["id"]),
                    after={"run_id": run_id, "quality": quality},
                )
            if run.get("conversation_id") and run.get("project_id"):
                turn_rows = await (
                    await conn.execute(
                        """SELECT id,role,content,status,sequence FROM messages
                        WHERE id IN (%s,%s) ORDER BY sequence""",
                        (run.get("trigger_message_id"), run.get("reply_message_id")),
                    )
                ).fetchall()
                current_summary = await (
                    await conn.execute(
                        """SELECT s.id,s.content FROM conversations c
                        JOIN conversation_summaries s ON s.id=c.summary_id
                        WHERE c.id=%s AND s.memory_version=2""",
                        (run["conversation_id"],),
                    )
                ).fetchone()
                turn_payload = {
                    "conversation_summary": (
                        {"id": str(current_summary["id"]), "content": current_summary["content"]}
                        if current_summary else None
                    ),
                    "messages": [
                        {"id": str(row["id"]), "role": row["role"], "content": row["content"][:4000]}
                        for row in turn_rows
                    ],
                }
                turn_digest = hashlib.sha256(
                    json.dumps(turn_payload, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                profile = await (
                    await conn.execute(
                        "SELECT learning_enabled FROM research_profiles WHERE user_id=%s", (tenant,)
                    )
                ).fetchone()
                if profile and profile["learning_enabled"]:
                    await self._enqueue_memory_job(
                        conn, tenant, str(run["project_id"]), str(run["conversation_id"]),
                        "turn_distillation", "message", str(run["reply_message_id"]),
                        turn_digest, turn_payload,
                    )
                    records = await (
                        await conn.execute(
                            """SELECT id,kind,data FROM records WHERE run_id=%s
                            AND kind IN ('task','finding','claim','report') ORDER BY created_at LIMIT 100""",
                            (run_id,),
                        )
                    ).fetchall()
                    tool_rows = await (
                        await conn.execute(
                            """SELECT id,kind,status,error,result FROM actions WHERE run_id=%s
                            AND kind!='model' ORDER BY started_at LIMIT 100""",
                            (run_id,),
                        )
                    ).fetchall()
                    run_payload = {
                        "run": {"id": run_id, "status": status, "reason": reason, "quality": quality},
                        "records": [
                            {"id": row["id"], "kind": row["kind"], "data": row["data"]}
                            for row in records
                        ],
                        "tool_result_summaries": [
                            {
                                "id": row["id"], "kind": row["kind"], "status": row["status"],
                                "error": row["error"],
                                "result_keys": (
                                    sorted(row["result"].keys())
                                    if isinstance(row["result"], dict) else []
                                ),
                            }
                            for row in tool_rows
                        ],
                    }
                    run_digest = hashlib.sha256(
                        json.dumps(run_payload, sort_keys=True, ensure_ascii=False, default=str).encode()
                    ).hexdigest()
                    await self._enqueue_memory_job(
                        conn, tenant, str(run["project_id"]), str(run["conversation_id"]),
                        "run_distillation", "run", run_id, run_digest, run_payload,
                    )
                count = await (
                    await conn.execute(
                        """SELECT count(*) AS n FROM messages m JOIN conversations c
                        ON c.id=m.conversation_id WHERE m.conversation_id=%s
                        AND m.sequence>c.memory_cutover_sequence AND m.role='user'
                        AND m.status='completed'""",
                        (run["conversation_id"],),
                    )
                ).fetchone()
                if count["n"] and count["n"] % 8 == 0:
                    await self._enqueue_memory_job(
                        conn, tenant, str(run["project_id"]), str(run["conversation_id"]),
                        "conversation_compaction", "conversation", str(run["conversation_id"]),
                        turn_digest, {"trigger": "eight_closed_turns"},
                    )
                await conn.execute(
                    "UPDATE conversations SET memory_revision=memory_revision+1 WHERE id=%s",
                    (run["conversation_id"],),
                )
            await self.event(
                conn, tenant, run_id, "run." + status, {"reason": reason, "quality_status": quality}
            )
            await self._conversation_event(
                conn,
                tenant,
                str(run["conversation_id"]),
                "run.terminal",
                {
                    "run_id": run_id,
                    "message_id": str(run["reply_message_id"]) if run.get("reply_message_id") else None,
                    "status": status,
                    "quality_status": quality,
                },
            )

    async def cancel(self, tenant: str, run_id: str):
        async with self.tx(tenant) as conn:
            r = await (
                await conn.execute("SELECT * FROM research_runs WHERE id=%s FOR UPDATE", (run_id,))
            ).fetchone()
            if not r:
                raise NotFound(run_id)
            if r["status"] in {"completed", "failed", "cancelled"}:
                return
            await conn.execute(
                """UPDATE research_runs SET status='cancelled',stop_reason='user_cancelled',
                fence=fence+1,finished_at=now(),lease_until=NULL WHERE id=%s""",
                (run_id,),
            )
            if r.get("reply_message_id"):
                await conn.execute(
                    """UPDATE messages SET status='cancelled',content='研究任务已取消。',updated_at=now()
                    WHERE id=%s""",
                    (r["reply_message_id"],),
                )
            await self.event(conn, tenant, run_id, "run.cancelled", {"reason": "user_cancelled"})

    async def resume(self, tenant: str, run_id: str):
        async with self.tx(tenant) as conn:
            room = await (await conn.execute("SELECT queue_has_room() AS room")).fetchone()
            r = await (
                await conn.execute("SELECT * FROM research_runs WHERE id=%s FOR UPDATE", (run_id,))
            ).fetchone()
            if not r:
                raise NotFound(run_id)
            if r["status"] in {"queued", "running"}:
                return
            if r["status"] != "interrupted":
                raise Conflict("only interrupted runs can resume; create a new run for terminal results")
            if not room["room"]:
                raise Conflict("run queue is full")
            await conn.execute(
                "UPDATE research_runs SET status='queued',finished_at=NULL WHERE id=%s", (run_id,)
            )
            await self.event(conn, tenant, run_id, "run.resumed", {})

    async def put(
        self,
        tenant: str,
        kind: str,
        record_id: str,
        data: dict,
        run_id: str | None = None,
        fence: int | None = None,
        immutable: bool = True,
    ):
        async with self.tx(tenant) as conn:
            if run_id:
                if fence is None:
                    raise ValueError("run writes require fence")
                await self.guard(conn, run_id, fence)
            old = await (
                await conn.execute("SELECT data,kind,run_id FROM records WHERE id=%s", (record_id,))
            ).fetchone()
            if old:
                if old["kind"] != kind or str(old["run_id"] or "") != str(run_id or ""):
                    raise Conflict("record identity collision")
                if immutable:
                    if old["data"] != data:
                        raise Conflict("immutable record cannot change")
                    return
                await conn.execute("UPDATE records SET data=%s WHERE id=%s", (Jsonb(data), record_id))
            else:
                await conn.execute(
                    "INSERT INTO records(tenant_id,id,run_id,kind,data) VALUES (%s,%s,%s,%s,%s)",
                    (tenant, record_id, run_id, kind, Jsonb(data)),
                )
            if run_id:
                await self.event(conn, tenant, run_id, kind + ".saved", {"id": record_id})

    async def get(self, tenant: str, record_id: str, kind: str | None = None) -> dict:
        async with self.tx(tenant) as conn:
            r = await (
                await conn.execute("SELECT data,kind FROM records WHERE id=%s", (record_id,))
            ).fetchone()
            if not r or (kind and r["kind"] != kind):
                raise NotFound(record_id)
            return r["data"]

    async def assert_source_visible(self, tenant: str, source_id: str) -> None:
        """Hide a source once every project reference to it belongs to a deleted project."""
        async with self.tx(tenant) as conn:
            source = await (
                await conn.execute(
                    "SELECT 1 FROM records WHERE id=%s AND kind='source'", (source_id,)
                )
            ).fetchone()
            if not source:
                raise NotFound(source_id)
            links = await (
                await conn.execute(
                    """SELECT count(*) AS total,
                    count(*) FILTER (WHERE p.status NOT IN ('deleted_pending','purged')) AS visible
                    FROM project_artifacts a JOIN projects p ON p.id=a.project_id
                    WHERE a.source_version_id=%s""",
                    (source_id,),
                )
            ).fetchone()
            if links["total"] and not links["visible"]:
                raise NotFound(source_id)

    async def records(self, tenant: str, run_id: str, kind: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            return [
                r["data"]
                for r in await (
                    await conn.execute(
                        "SELECT data FROM records WHERE run_id=%s AND kind=%s ORDER BY created_at,id",
                        (run_id, kind),
                    )
                ).fetchall()
            ]

    async def source_artifacts(self, tenant: str, source_id: str) -> list[dict]:
        await self.assert_source_visible(tenant, source_id)
        async with self.tx(tenant) as conn:
            return [
                row["data"]
                for row in await (
                    await conn.execute(
                        """SELECT data FROM records
                        WHERE kind='derived_artifact' AND data->>'source_id'=%s
                        ORDER BY created_at,id""",
                        (source_id,),
                    )
                ).fetchall()
            ]

    async def enqueue_upload(
        self,
        tenant: str,
        upload_id: str,
        raw_hash: str,
        raw_key: str,
        mime: str,
        title: str,
        parser_mode: str,
    ):
        async with self.tx(tenant) as conn:
            await conn.execute(
                """INSERT INTO source_uploads
                (tenant_id,upload_id,raw_hash,raw_key,mime,title,parser_mode)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,upload_id) DO NOTHING""",
                (tenant, upload_id, raw_hash, raw_key, mime, title, parser_mode),
            )

    async def upload_status(self, tenant: str, upload_id: str) -> dict:
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute("SELECT * FROM source_uploads WHERE upload_id=%s", (upload_id,))
            ).fetchone()
            if not row:
                raise NotFound(upload_id)
            return row

    async def claim_upload(self) -> dict | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM claim_next_source_upload(%s,%s)",
                    (self.settings.worker_id + "-indexer", self.settings.index_lease_seconds),
                )
            ).fetchone()
            return dict(row) if row else None

    async def complete_upload(self, tenant: str, upload_id: str, source_id: str):
        async with self.tx(tenant) as conn:
            await conn.execute(
                """UPDATE source_uploads SET status='ready',source_id=%s,progress=1,
                error=NULL,lease_until=NULL,updated_at=now() WHERE upload_id=%s""",
                (source_id, upload_id),
            )

    async def heartbeat_upload(self, tenant: str, upload_id: str):
        async with self.tx(tenant) as conn:
            await conn.execute(
                """UPDATE source_uploads
                SET lease_until=now()+make_interval(secs=>%s),updated_at=now()
                WHERE upload_id=%s AND status='processing'""",
                (self.settings.index_lease_seconds, upload_id),
            )

    async def fail_upload(self, tenant: str, upload_id: str, error: str):
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute("SELECT attempts FROM source_uploads WHERE upload_id=%s", (upload_id,))
            ).fetchone()
            retry = bool(row and row["attempts"] < 3)
            await conn.execute(
                """UPDATE source_uploads SET status=%s,error=%s,
                lease_until=CASE WHEN %s THEN now()+interval '10 seconds' ELSE NULL END,
                updated_at=now() WHERE upload_id=%s""",
                ("processing" if retry else "failed", error[:300], retry, upload_id),
            )

    async def enqueue_index(
        self,
        tenant: str,
        source_id: str,
        parsed_hash: str,
        index_version: str,
        chunker_version: str,
        embedding_model: str,
        embedding_revision: str,
    ):
        async with self.tx(tenant) as conn:
            await conn.execute(
                """INSERT INTO source_index_jobs
                (tenant_id,source_id,parsed_hash,index_version,chunker_version,
                 embedding_model,embedding_revision)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,source_id,index_version) DO NOTHING""",
                (
                    tenant,
                    source_id,
                    parsed_hash,
                    index_version,
                    chunker_version,
                    embedding_model,
                    embedding_revision,
                ),
            )

    async def requeue_index(
        self,
        tenant: str,
        source_id: str,
        parsed_hash: str,
        index_version: str,
        chunker_version: str,
        embedding_model: str,
        embedding_revision: str,
    ):
        """Create a new build while the last usable index remains searchable."""
        await self.enqueue_index(
            tenant,
            source_id,
            parsed_hash,
            index_version,
            chunker_version,
            embedding_model,
            embedding_revision,
        )

    async def index_status(self, tenant: str, source_id: str) -> dict | None:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT * FROM source_index_jobs WHERE source_id=%s
                    ORDER BY created_at DESC LIMIT 1""",
                    (source_id,),
                )
            ).fetchone()

    async def usable_index_status(self, tenant: str, source_id: str) -> dict | None:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT * FROM source_index_jobs
                    WHERE source_id=%s AND status IN ('lexical_ready','ready')
                    ORDER BY created_at DESC LIMIT 1""",
                    (source_id,),
                )
            ).fetchone()

    async def index_job(self, tenant: str, source_id: str, index_version: str) -> dict | None:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT * FROM source_index_jobs
                    WHERE source_id=%s AND index_version=%s""",
                    (source_id, index_version),
                )
            ).fetchone()

    async def claim_index(self) -> dict | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM claim_next_index_job(%s,%s)",
                    (self.settings.worker_id + "-indexer", self.settings.index_lease_seconds),
                )
            ).fetchone()
            return dict(row) if row else None

    async def heartbeat_index(self, tenant: str, source_id: str, index_version: str):
        async with self.tx(tenant) as conn:
            await conn.execute(
                """UPDATE source_index_jobs
                SET lease_until=now()+make_interval(secs=>%s),updated_at=now()
                WHERE source_id=%s AND index_version=%s
                  AND status IN ('processing','lexical_ready')""",
                (self.settings.index_lease_seconds, source_id, index_version),
            )

    async def save_lexical_chunks(
        self, tenant: str, source_id: str, parsed_hash: str, index_version: str, chunks: list[dict]
    ):
        async with self.tx(tenant) as conn:
            await conn.execute(
                "DELETE FROM document_chunks WHERE source_id=%s AND index_version=%s",
                (source_id, index_version),
            )
            for chunk in chunks:
                await conn.execute(
                    """INSERT INTO document_chunks
                    (tenant_id,source_id,parsed_hash,index_version,chunk_id,ordinal,start_char,end_char,
                     page,bbox,kind,section_path,block_id,text,lexical_text,textsearch)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            to_tsvector('simple',%s))""",
                    (
                        tenant,
                        source_id,
                        parsed_hash,
                        index_version,
                        chunk["chunk_id"],
                        chunk["ordinal"],
                        chunk["start"],
                        chunk["end"],
                        chunk["page"],
                        Jsonb(chunk["bbox"]),
                        chunk["kind"],
                        Jsonb(chunk["section_path"]),
                        chunk["block_id"],
                        chunk["text"],
                        chunk["lexical_text"],
                        chunk["lexical_text"],
                    ),
                )
            await conn.execute(
                """UPDATE source_index_jobs SET status='lexical_ready',total_chunks=%s,
                embedded_chunks=0,error=NULL,updated_at=now()
                WHERE source_id=%s AND index_version=%s""",
                (len(chunks), source_id, index_version),
            )

    async def save_embeddings(
        self,
        tenant: str,
        source_id: str,
        index_version: str,
        rows: list[tuple[str, list[float]]],
        model: str,
        revision: str,
    ):
        async with self.tx(tenant) as conn:
            for chunk_id, vector in rows:
                await conn.execute(
                    """UPDATE document_chunks SET embedding=%s::vector,embedding_model=%s,
                    embedding_revision=%s WHERE source_id=%s AND index_version=%s AND chunk_id=%s""",
                    (vector_literal(vector), model, revision, source_id, index_version, chunk_id),
                )
            total = await (
                await conn.execute(
                    """SELECT count(*) AS total,count(embedding) AS embedded FROM document_chunks
                    WHERE source_id=%s AND index_version=%s""",
                    (source_id, index_version),
                )
            ).fetchone()
            status = "ready" if total["total"] == total["embedded"] else "lexical_ready"
            await conn.execute(
                """UPDATE source_index_jobs SET status=%s,total_chunks=%s,embedded_chunks=%s,
                error=NULL,lease_until=CASE WHEN %s='ready' THEN NULL ELSE lease_until END,
                updated_at=now()
                WHERE source_id=%s AND index_version=%s""",
                (
                    status,
                    total["total"],
                    total["embedded"],
                    status,
                    source_id,
                    index_version,
                ),
            )

    async def fail_index(
        self,
        tenant: str,
        source_id: str,
        index_version: str,
        error: str,
        retry: bool = True,
    ):
        async with self.tx(tenant) as conn:
            row = await (
                await conn.execute(
                    """SELECT total_chunks,attempts FROM source_index_jobs
                    WHERE source_id=%s AND index_version=%s""",
                    (source_id, index_version),
                )
            ).fetchone()
            has_lexical = bool(row and row["total_chunks"])
            should_retry = bool(retry and row and row["attempts"] < 3)
            status = "lexical_ready" if has_lexical else ("processing" if should_retry else "failed")
            retry_delay = min(60, 10 * (2 ** max(0, int(row["attempts"]) - 1))) if row else 10
            await conn.execute(
                """UPDATE source_index_jobs SET status=%s,error=%s,
                lease_until=CASE WHEN %s THEN now()+make_interval(secs=>%s) ELSE NULL END,
                updated_at=now()
                WHERE source_id=%s AND index_version=%s""",
                (status, error[:300], should_retry, retry_delay, source_id, index_version),
            )

    async def chunk_rows(self, tenant: str, source_id: str, index_version: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT chunk_id,text FROM document_chunks
                    WHERE source_id=%s AND index_version=%s ORDER BY ordinal""",
                    (source_id, index_version),
                )
            ).fetchall()

    async def retrieval_candidates(
        self,
        tenant: str,
        source_ids: list[str],
        query_terms: str,
        query_vector: list[float] | None,
        strategy: str,
    ) -> tuple[list[dict], list[dict]]:
        if not source_ids:
            return [], []
        async with self.tx(tenant) as conn:
            versions = await (
                await conn.execute(
                    """SELECT DISTINCT ON(source_id) source_id,index_version,status
                    FROM source_index_jobs WHERE source_id=ANY(%s) AND status IN ('lexical_ready','ready')
                    ORDER BY source_id,created_at DESC""",
                    (source_ids,),
                )
            ).fetchall()
            version_ids = [row["index_version"] for row in versions]
            if not version_ids:
                return [], []
            lexical = []
            if strategy in {"lexical", "hybrid"} and query_terms:
                lexical = await (
                    await conn.execute(
                        """SELECT c.*,coalesce(r.data->>'title','') AS title,
                        ts_rank_cd(c.textsearch,to_tsquery('simple',%s)) AS score
                        FROM document_chunks c
                        LEFT JOIN records r ON r.tenant_id=c.tenant_id AND r.id=c.source_id AND r.kind='source'
                        WHERE c.index_version=ANY(%s)
                          AND c.textsearch @@ to_tsquery('simple',%s)
                        ORDER BY score DESC,c.source_id,c.start_char LIMIT 30""",
                        (query_terms, version_ids, query_terms),
                    )
                ).fetchall()
            dense = []
            ready_versions = [row["index_version"] for row in versions if row["status"] == "ready"]
            if strategy in {"hybrid", "dense"} and query_vector and ready_versions:
                literal = vector_literal(query_vector)
                dense = await (
                    await conn.execute(
                        """SELECT c.*,coalesce(r.data->>'title','') AS title,
                        1-(c.embedding <=> %s::vector) AS score
                        FROM document_chunks c
                        LEFT JOIN records r ON r.tenant_id=c.tenant_id AND r.id=c.source_id AND r.kind='source'
                        WHERE c.index_version=ANY(%s) AND c.embedding IS NOT NULL
                        ORDER BY c.embedding <=> %s::vector,c.source_id,c.start_char LIMIT 30""",
                        (literal, ready_versions, literal),
                    )
                ).fetchall()

            def clean(row):
                result = dict(row)
                result["start"], result["end"] = result.pop("start_char"), result.pop("end_char")
                result.pop("embedding", None)
                result.pop("textsearch", None)
                return result

            return [clean(row) for row in lexical], [clean(row) for row in dense]

    async def events(self, tenant: str, run_id: str, after: int) -> list[dict]:
        await self.run(tenant, run_id)
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT * FROM run_events WHERE run_id=%s AND seq>%s
                ORDER BY seq LIMIT 200""",
                    (run_id, after),
                )
            ).fetchall()

    async def action(self, tenant: str, run_id: str, action_id: str) -> dict | None:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute("SELECT * FROM actions WHERE run_id=%s AND id=%s", (run_id, action_id))
            ).fetchone()

    async def reserve(
        self,
        tenant: str,
        run_id: str,
        fence: int,
        action_id: str,
        kind: str,
        usd: float,
        tokens: int = 0,
        task_id: str | None = None,
        closing: bool = False,
        role: str = "legacy",
        budget_group: str = "legacy",
        serialized_bytes: int = 0,
        estimated_input_tokens: int = 0,
        output_token_ceiling: int = 0,
        request: dict | None = None,
    ):
        async with self.tx(tenant) as conn:
            r = await self.guard(conn, run_id, fence)
            p = RunProfile.model_validate(r["profile"])
            old = await (
                await conn.execute(
                    "SELECT status FROM actions WHERE run_id=%s AND id=%s", (run_id, action_id)
                )
            ).fetchone()
            if old:
                raise Conflict("action already reserved")
            elapsed = await (
                await conn.execute(
                    "SELECT extract(epoch FROM now()-%s::timestamptz) AS s", (r["started_at"],)
                )
            ).fetchone()
            cap = p.max_usd * (1 if closing else 0.8)
            if float(r["spent_usd"] + r["reserved_usd"]) + usd > cap or elapsed["s"] > p.max_seconds:
                raise BudgetExceeded("money_or_deadline")
            # Reserve the estimated input plus the largest permitted output. Actual
            # provider usage is settled separately and remains the source of truth.
            token_cap = p.max_tokens
            model_cap = p.max_model_calls if closing else max(1, int(p.max_model_calls * 0.8))
            if r["tokens"] + r["reserved_tokens"] + tokens > token_cap:
                raise BudgetExceeded("tokens")
            soft_order = ["scope_plan", "research_extraction", "review_gap", "writer_patch"]
            if kind == "model" and budget_group in soft_order:
                position = soft_order.index(budget_group)
                allowed_groups = soft_order[: position + 1]
                cumulative_caps = [BUDGET_CUMULATIVE_CAPS[group] for group in soft_order]
                used = await (
                    await conn.execute(
                        """SELECT coalesce(sum(CASE WHEN status IN ('reserved','unknown') THEN
                        reserved_tokens ELSE coalesce((usage->>'input_tokens')::bigint,0)
                        +coalesce((usage->>'output_tokens')::bigint,0) END),0) AS tokens
                        FROM actions WHERE run_id=%s AND budget_group=ANY(%s)""",
                        (run_id, allowed_groups),
                    )
                ).fetchone()
                if int(used["tokens"]) + tokens > cumulative_caps[position]:
                    raise BudgetExceeded("soft_target:" + budget_group)
            if kind == "model" and r["model_calls"] >= model_cap:
                raise BudgetExceeded("model_calls")
            if kind != "model" and r["tool_calls"] >= p.max_tool_calls:
                raise BudgetExceeded("tool_calls")
            if kind == "search" and r["search_calls"] >= p.max_search_calls:
                raise BudgetExceeded("search_calls")
            if kind == "retrieve" and r["retrieval_calls"] >= p.max_retrieval_calls:
                raise BudgetExceeded("retrieval_calls")
            if task_id and kind != "model":
                if kind == "search" and p.variant != "B0":
                    searches = await (
                        await conn.execute(
                            "SELECT count(*) AS n FROM actions WHERE run_id=%s AND task_id=%s AND kind='search'",
                            (run_id, task_id),
                        )
                    ).fetchone()
                    if searches["n"] >= p.max_task_search_calls:
                        raise BudgetExceeded("task_search_calls")
                count = await (
                    await conn.execute(
                        """SELECT count(*) AS n FROM actions
                    WHERE run_id=%s AND task_id=%s AND kind!='model'""",
                        (run_id, task_id),
                    )
                ).fetchone()
                if count["n"] >= (p.max_tool_calls if p.variant == "B0" else p.max_task_tools):
                    raise BudgetExceeded("task_tools")
            if r["mode"] == "live":
                c = await (
                    await conn.execute("SELECT * FROM campaigns WHERE id='initial-live' FOR UPDATE")
                ).fetchone()
                if float(c["spent_usd"] + c["reserved_usd"]) + usd > min(
                    float(c["limit_usd"]), self.settings.live_campaign_usd
                ):
                    raise BudgetExceeded("live_campaign")
                await conn.execute(
                    "UPDATE campaigns SET reserved_usd=reserved_usd+%s WHERE id='initial-live'", (usd,)
                )
            await conn.execute(
                """UPDATE research_runs SET reserved_usd=reserved_usd+%s,
                reserved_tokens=reserved_tokens+%s,model_calls=model_calls+%s,tool_calls=tool_calls+%s,
                search_calls=search_calls+%s,retrieval_calls=retrieval_calls+%s WHERE id=%s""",
                (
                    usd,
                    tokens,
                    int(kind == "model"),
                    int(kind != "model"),
                    int(kind == "search"),
                    int(kind == "retrieve"),
                    run_id,
                ),
            )
            await conn.execute(
                """INSERT INTO actions
                (tenant_id,run_id,id,task_id,kind,status,reserved_usd,reserved_tokens,role,budget_group,
                 serialized_bytes,estimated_input_tokens,output_token_ceiling,request)
                VALUES (%s,%s,%s,%s,%s,'reserved',%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    tenant,
                    run_id,
                    action_id,
                    task_id,
                    kind,
                    usd,
                    tokens,
                    role,
                    budget_group,
                    serialized_bytes,
                    estimated_input_tokens,
                    output_token_ceiling,
                    Jsonb(request),
                ),
            )
            await self.event(
                conn, tenant, run_id, "action.reserved", {"id": action_id, "kind": kind, "task_id": task_id}
            )

    async def settle(
        self,
        tenant: str,
        run_id: str,
        fence: int,
        action_id: str,
        result: dict | None,
        usage: dict,
        error: str | None = None,
        unknown: bool = False,
    ):
        async with self.tx(tenant) as conn:
            r = await self.guard(conn, run_id, fence)
            a = await (
                await conn.execute(
                    "SELECT * FROM actions WHERE run_id=%s AND id=%s FOR UPDATE", (run_id, action_id)
                )
            ).fetchone()
            if not a or a["status"] != "reserved":
                return
            status = "unknown" if unknown else "failed" if error else "completed"
            if not unknown:
                usd = usage.get("usd", 0)
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                await conn.execute(
                    """UPDATE research_runs SET reserved_usd=reserved_usd-%s,spent_usd=spent_usd+%s,
                    reserved_tokens=reserved_tokens-%s,tokens=tokens+%s WHERE id=%s""",
                    (a["reserved_usd"], usd, a["reserved_tokens"], tokens, run_id),
                )
                if r["mode"] == "live":
                    await conn.execute(
                        """UPDATE campaigns SET reserved_usd=reserved_usd-%s,spent_usd=spent_usd+%s
                        WHERE id='initial-live'""",
                        (a["reserved_usd"], usd),
                    )
            await conn.execute(
                """UPDATE actions SET status=%s,result=%s,usage=%s,error=%s,finished_at=now()
                WHERE run_id=%s AND id=%s""",
                (status, Jsonb(result), Jsonb(usage), error, run_id, action_id),
            )
            await self.event(
                conn,
                tenant,
                run_id,
                "action." + status,
                {"id": action_id, "kind": a["kind"], "usage": usage, "error": error},
            )

    async def usage(self, tenant: str, run_id: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute(
                    """SELECT id,task_id,kind,status,usage,error,started_at,finished_at,
                reserved_usd FROM actions WHERE run_id=%s ORDER BY started_at""",
                    (run_id,),
                )
            ).fetchall()

    async def usage_summary(self, tenant: str, run_id: str) -> dict:
        async with self.tx(tenant) as conn:
            run = await (
                await conn.execute(
                    """SELECT tokens,reserved_tokens,spent_usd,reserved_usd,profile
                    FROM research_runs WHERE id=%s""",
                    (run_id,),
                )
            ).fetchone()
            if not run:
                raise NotFound(run_id)
            rows = await (
                await conn.execute(
                    """SELECT budget_group,
                    coalesce(sum((usage->>'input_tokens')::bigint),0) AS input_tokens,
                    coalesce(sum((usage->>'output_tokens')::bigint),0) AS output_tokens,
                    coalesce(sum((usage->>'cache_hit_tokens')::bigint),0) AS cache_hit_tokens,
                    count(*) FILTER (WHERE kind='model') AS model_calls,
                    count(*) FILTER (WHERE kind!='model') AS tool_calls,
                    coalesce(sum((usage->>'usd')::numeric),0) AS usd,
                    coalesce(sum(estimated_input_tokens),0) AS estimated_input_tokens,
                    coalesce(sum(serialized_bytes),0) AS serialized_bytes
                    FROM actions WHERE run_id=%s GROUP BY budget_group ORDER BY budget_group""",
                    (run_id,),
                )
            ).fetchall()
            events = await (
                await conn.execute(
                    """SELECT seq,payload,occurred_at FROM run_events
                    WHERE run_id=%s AND event_type='budget.degraded' ORDER BY seq""",
                    (run_id,),
                )
            ).fetchall()
            groups = {
                name: {
                    "target": target,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_hit_tokens": 0,
                    "model_calls": 0,
                    "tool_calls": 0,
                    "usd": 0.0,
                    "estimated_input_tokens": 0,
                    "serialized_bytes": 0,
                }
                for name, target in BUDGET_GROUPS.items()
            }
            for row in rows:
                group = row["budget_group"] or "legacy"
                groups.setdefault(group, {"target": 0})
                groups[group].update(
                    {
                        key: float(value) if key == "usd" else int(value)
                        for key, value in row.items()
                        if key != "budget_group"
                    }
                )
            actual = int(run["tokens"])
            hard = int(RunProfile.model_validate(run["profile"]).max_tokens)
            return {
                "totals": {
                    "tokens": actual,
                    "reserved_tokens": int(run["reserved_tokens"]),
                    "usd": float(run["spent_usd"]),
                    "reserved_usd": float(run["reserved_usd"]),
                },
                "soft_target": SOFT_TOKEN_TARGET,
                "hard_cap": hard,
                "soft_remaining": max(0, SOFT_TOKEN_TARGET - actual),
                "hard_remaining": max(0, hard - actual - int(run["reserved_tokens"])),
                "degradation_level": degradation_level(actual),
                "budget_groups": groups,
                "degradation_events": [dict(row) for row in events],
            }

    async def attach_source(self, tenant: str, run_id: str, fence: int, source: dict, related=False):
        async with self.tx(tenant) as conn:
            r = await self.guard(conn, run_id, fence)
            p = RunProfile.model_validate(r["profile"])
            rid = run_id + ":source:" + source["id"]
            if await (await conn.execute("SELECT id FROM records WHERE id=%s", (rid,))).fetchone():
                return
            counts = await (
                await conn.execute(
                    """SELECT count(*) AS total,
                count(*) FILTER(WHERE data->>'related'='true') AS related
                FROM records WHERE run_id=%s AND kind='source_ref'""",
                    (run_id,),
                )
            ).fetchone()
            if counts["total"] >= p.max_sources:
                raise BudgetExceeded("source_limit")
            if (
                related
                and r["brief"]["template"] == "paper_review"
                and counts["related"] >= p.paper_related_sources
            ):
                raise BudgetExceeded("paper_related_source_limit")
            data = {"source": source, "related": related}
            await conn.execute(
                "INSERT INTO records(tenant_id,id,run_id,kind,data) VALUES (%s,%s,%s,'source_ref',%s)",
                (tenant, rid, run_id, Jsonb(data)),
            )
            await self.event(
                conn, tenant, run_id, "source.attached", {"source_id": source["id"], "related": related}
            )
