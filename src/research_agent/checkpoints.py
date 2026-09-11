"""Fence and tenant checks share a transaction with every checkpoint write."""

from langgraph.checkpoint.base import BaseCheckpointSaver
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
