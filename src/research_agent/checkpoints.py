"""Fence and tenant checks share a transaction with every checkpoint write."""

from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver, empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from research_agent.db import Database


class FencedSaver(BaseCheckpointSaver):
    def __init__(self, db: Database, tenant: str, run_id: str, fence: int):
        super().__init__()
        self.db, self.tenant, self.run_id, self.fence = db, tenant, run_id, fence

    def _check(self, config):
        thread = config["configurable"]["thread_id"]
        if thread != self.run_id and not thread.startswith(self.run_id + ":researcher:"):
            raise PermissionError("checkpoint thread outside run namespace")

    async def aget_tuple(self, config):
        self._check(config)
        async with self.db.tx(self.tenant) as conn:
            return await AsyncPostgresSaver(conn).aget_tuple(config)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        self._check(config)
        async with self.db.tx(self.tenant) as conn:
            async for value in AsyncPostgresSaver(conn).alist(
                config, filter=filter, before=before, limit=limit
            ):
                yield value

    async def aput(self, config, checkpoint, metadata, new_versions):
        self._check(config)
        async with self.db.tx(self.tenant) as conn:
            await self.db.guard(conn, self.run_id, self.fence)
            return await AsyncPostgresSaver(conn).aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        self._check(config)
        async with self.db.tx(self.tenant) as conn:
            await self.db.guard(conn, self.run_id, self.fence)
            await AsyncPostgresSaver(conn).aput_writes(config, writes, task_id, task_path)

    def get_next_version(self, current, channel):
        return AsyncPostgresSaver.get_next_version(self, current, channel)


class ConversationSaver:
    """Tenant-guarded, rebuildable conversation state stored in LangGraph checkpoints."""

    def __init__(self, db: Database, tenant: str, conversation_id: str):
        self.db = db
        self.tenant = tenant
        self.conversation_id = conversation_id
        self.thread_id = f"conversation:{conversation_id}:v2"

    @property
    def config(self):
        return {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": "memory"}}

    async def _guard(self, conn):
        row = await (
            await conn.execute(
                """SELECT c.memory_revision FROM conversations c JOIN projects p ON p.id=c.project_id
                WHERE c.id=%s AND p.status NOT IN ('deleted_pending','purged')""",
                (self.conversation_id,),
            )
        ).fetchone()
        if not row:
            raise PermissionError("conversation checkpoint outside tenant scope")
        return row

    async def load(self) -> dict | None:
        async with self.db.tx(self.tenant) as conn:
            await self._guard(conn)
            value = await AsyncPostgresSaver(conn).aget_tuple(self.config)
            if not value:
                return None
            return value.checkpoint.get("channel_values", {}).get("conversation_state")

    async def save(self, state: dict, revision: int) -> None:
        async with self.db.tx(self.tenant) as conn:
            row = await self._guard(conn)
            if int(row["memory_revision"]) != int(revision):
                raise RuntimeError("conversation_memory_revision_changed")
            saver = AsyncPostgresSaver(conn)
            checkpoint = empty_checkpoint()
            channel_version = f"{revision:032}.{uuid4().hex}"
            checkpoint["channel_values"] = {"conversation_state": state}
            checkpoint["channel_versions"] = {"conversation_state": channel_version}
            await saver.aput(
                self.config,
                checkpoint,
                {"source": "update", "step": revision, "parents": {}},
                {"conversation_state": channel_version},
            )
            keep = max(1, int(self.db.settings.memory_checkpoint_keep))
            await conn.execute(
                """DELETE FROM checkpoint_writes WHERE thread_id=%s AND checkpoint_ns='memory'
                AND checkpoint_id NOT IN (
                  SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='memory'
                  ORDER BY checkpoint_id DESC LIMIT %s
                )""",
                (self.thread_id, self.thread_id, keep),
            )
            await conn.execute(
                """DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='memory'
                AND checkpoint_id NOT IN (
                  SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='memory'
                  ORDER BY checkpoint_id DESC LIMIT %s
                )""",
                (self.thread_id, self.thread_id, keep),
            )
            await conn.execute(
                """DELETE FROM checkpoint_blobs b WHERE b.thread_id=%s AND b.checkpoint_ns='memory'
                AND NOT EXISTS (
                  SELECT 1 FROM checkpoints c,
                  LATERAL jsonb_each_text(c.checkpoint->'channel_versions') versions(channel,version)
                  WHERE c.thread_id=b.thread_id AND c.checkpoint_ns=b.checkpoint_ns
                    AND versions.channel=b.channel AND versions.version=b.version
                )""",
                (self.thread_id,),
            )
