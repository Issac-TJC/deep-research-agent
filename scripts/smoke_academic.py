"""Free public-metadata smoke test; never invokes a model provider."""

import argparse
import asyncio
import json
from datetime import date, timedelta

from research_agent.academic import AcademicSearch
from research_agent.settings import Settings


async def run(query: str, days: int) -> None:
    end = date.today()
    settings = Settings(
        research_mode="live",
        academic_connector_attempts=1,
        academic_candidates_per_connector=3,
        academic_connector_timeout=15,
        _env_file=None,
    )
    candidates, attempts = await AcademicSearch(settings).collect(
        query, [], end - timedelta(days=days - 1), end
    )
    succeeded = {item["connector"] for item in attempts if item["status"] == "succeeded"}
    result = {
        "query": query,
        "period": [str(end - timedelta(days=days - 1)), str(end)],
        "candidate_count": len(candidates),
        "succeeded_connectors": sorted(succeeded),
        "attempts": attempts,
        "passed_partial_success": len(succeeded) >= 2,
        "paid_model_calls": 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if len(succeeded) < 2:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default="3D Gaussian Splatting")
    parser.add_argument("--days", type=int, default=30)
    arguments = parser.parse_args()
    asyncio.run(run(arguments.query, arguments.days))
