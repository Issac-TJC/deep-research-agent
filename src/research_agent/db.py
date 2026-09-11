from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

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


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


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
            r = await (
                await conn.execute(
                    """INSERT INTO research_runs
                (id,tenant_id,idempotency_key,request_hash,brief,profile,mode,configuration)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                    (
                        run_id,
                        tenant,
                        key,
                        request_hash,
                        Jsonb(payload["brief"]),
                        Jsonb(payload["profile"]),
                        self.settings.research_mode,
                        Jsonb(self.settings.execution_snapshot()),
                    ),
                )
            ).fetchone()
            await self.event(conn, tenant, run_id, "run.queued", {"mode": self.settings.research_mode})
            return r, True

    async def run(self, tenant: str, run_id: str) -> dict:
        async with self.tx(tenant) as conn:
            r = await (await conn.execute("SELECT * FROM research_runs WHERE id=%s", (run_id,))).fetchone()
            if not r:
                raise NotFound(run_id)
            return r

    async def runs(self, tenant: str) -> list[dict]:
        async with self.tx(tenant) as conn:
            return await (
                await conn.execute("SELECT * FROM research_runs ORDER BY created_at DESC LIMIT 100")
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
            await self.guard(conn, run_id, fence)
            await conn.execute(
                """UPDATE research_runs SET status=%s,stop_reason=%s,quality_status=%s,
                finished_at=now(),lease_until=NULL WHERE id=%s""",
                (status, reason, quality, run_id),
            )
            await self.event(
                conn, tenant, run_id, "run." + status, {"reason": reason, "quality_status": quality}
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
            token_cap = p.max_tokens if closing else int(p.max_tokens * 0.8)
            model_cap = p.max_model_calls if closing else max(1, int(p.max_model_calls * 0.8))
            if r["tokens"] + r["reserved_tokens"] + tokens > token_cap:
                raise BudgetExceeded("tokens")
            if kind == "model" and r["model_calls"] >= model_cap:
                raise BudgetExceeded("model_calls")
            if kind != "model" and r["tool_calls"] >= p.max_tool_calls:
                raise BudgetExceeded("tool_calls")
            if kind == "search" and r["search_calls"] >= p.max_search_calls:
                raise BudgetExceeded("search_calls")
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
                search_calls=search_calls+%s WHERE id=%s""",
                (usd, tokens, int(kind == "model"), int(kind != "model"), int(kind == "search"), run_id),
            )
            await conn.execute(
                """INSERT INTO actions(tenant_id,run_id,id,task_id,kind,status,reserved_usd,reserved_tokens)
                VALUES (%s,%s,%s,%s,%s,'reserved',%s,%s)""",
                (tenant, run_id, action_id, task_id, kind, usd, tokens),
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
