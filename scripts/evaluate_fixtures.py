"""Reproduce baseline/ablation plumbing without paid APIs. Requires an idle queue."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from research_agent.evaluation import evaluate_suite
from research_agent.settings import Settings


async def main():
    entries = Path(".local/test-tenants.jsonl").read_text().splitlines()
    key = json.loads(entries[1])["api_key"]  # keep evaluation history in the second test tenant
    output = Path("artifacts/fixture-evaluation") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    arms = [
        ("B2-dev20", "B2", 20, 3, "evidence"),
        ("B0", "B0", 1, 1, "evidence"),
        ("B1", "B1", 1, 3, "evidence"),
        ("B2-serial", "B2", 1, 1, "evidence"),
        ("B2-full-context", "B2", 1, 3, "full"),
    ]
    for name, variant, limit, concurrency, context in arms:
        await evaluate_suite(
            Settings(), key, "dev", variant, limit, 1, False, output / f"{name}.jsonl", concurrency, context
        )
        print(json.dumps({"arm": name, "runs": limit, "mode": "fixture", "output": str(output)}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
