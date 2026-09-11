from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from opentelemetry import trace
from pydantic import BaseModel, ValidationError

from research_agent.context import POLICY, token_upper_bound
from research_agent.contracts import RunProfile
from research_agent.db import BudgetExceeded, Database, StaleLease
from research_agent.providers import ModelProvider, ProviderError


class Gateway:
    def __init__(
        self,
        db: Database,
        tenant: str,
        run_id: str,
        fence: int,
        profile: RunProfile,
        provider: ModelProvider,
        mode: str,
        execution_settings=None,
    ):
        self.db, self.tenant, self.run_id, self.fence = db, tenant, run_id, fence
        self.profile, self.provider, self.mode = profile, provider, mode
        self.settings = execution_settings or db.settings
        from research_agent.observability import configure

        configure()
        self.tracer = trace.get_tracer("research-agent", "0.1.0")

    async def _invoke(
        self,
        kind: str,
        key: str,
        operation: Callable[[], Awaitable[tuple[dict, dict]]],
        usd: float,
        tokens=0,
        task_id=None,
        closing=False,
        estimate=None,
    ) -> dict:
        for attempt in range(self.profile.max_retries + 1):
            action_id = f"{key}:attempt:{attempt}"
            old = await self.db.action(self.tenant, self.run_id, action_id)
            if old:
                if old["status"] == "completed":
                    return old["result"]
                # An interrupted reserved attempt has an unknown external outcome. Keep its reservation.
                continue
            attempt_usd, attempt_tokens = estimate() if estimate else (usd, tokens)
            await self.db.reserve(
                self.tenant,
                self.run_id,
                self.fence,
                action_id,
                kind,
                attempt_usd,
                attempt_tokens,
                task_id,
                closing,
            )
            started = time.monotonic()
            parent = trace.NonRecordingSpan(
                trace.SpanContext(
                    trace_id=int(self.run_id.replace("-", ""), 16),
                    span_id=1,
                    is_remote=False,
                    trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
                )
            )
            with self.tracer.start_as_current_span(
                kind, context=trace.set_span_in_context(parent), record_exception=False
            ) as span:
                span.set_attribute("research.run_id", self.run_id)
                span.set_attribute("research.action_id", action_id)
                try:
                    result, usage = await operation()
                    usage["latency_ms"] = round((time.monotonic() - started) * 1000)
                    usage["trace_id"] = format(span.get_span_context().trace_id, "032x")
                    usage["span_id"] = format(span.get_span_context().span_id, "016x")
                    await self.db.settle(self.tenant, self.run_id, self.fence, action_id, result, usage)
                    return result
                except (BudgetExceeded, StaleLease, asyncio.CancelledError):
                    raise
                except ProviderError as exc:
                    usage = getattr(exc, "usage", {})
                    diagnostic = getattr(exc, "diagnostic", None)
                    if diagnostic:
                        usage["diagnostic"] = diagnostic
                    await self.db.settle(
                        self.tenant,
                        self.run_id,
                        self.fence,
                        action_id,
                        getattr(exc, "response", None),
                        usage,
                        error=str(exc),
                        unknown=exc.unknown,
                    )
                    if not exc.retryable:
                        raise
                except (ValueError, KeyError) as exc:
                    await self.db.settle(
                        self.tenant, self.run_id, self.fence, action_id, None, {}, error=type(exc).__name__
                    )
                    raise
                except Exception as exc:
                    # Non-provider tools can raise transport/parser errors. Preserve uncertain external outcomes.
                    await self.db.settle(
                        self.tenant,
                        self.run_id,
                        self.fence,
                        action_id,
                        None,
                        {},
                        error=type(exc).__name__,
                        unknown=usd > 0,
                    )
                    raise ProviderError("tool_execution:" + type(exc).__name__) from exc
            await asyncio.sleep(min(2**attempt, 4))
        raise ProviderError("attempts_exhausted", retryable=False)

    async def model(
        self,
        key: str,
        messages: list[dict],
        *,
        tools=None,
        schema: type[BaseModel] | None = None,
        task_id=None,
        closing=False,
    ) -> dict:
        input_bound = token_upper_bound({"messages": messages, "tools": tools})
        if input_bound > self.profile.prompt_token_limit:
            raise BudgetExceeded("prompt_context_limit")
        # Thinking and visible JSON share the output allowance. Structured tasks need
        # enough room for both; a truncated attempt may grow within the frozen profile.
        role_cap = 16384 if schema else 4096
        output_bound = min(self.profile.max_output_tokens, role_cap)
        s = self.settings
        reserve = (
            0
            if self.mode == "fixture"
            else (input_bound * s.model_input_usd_per_million + output_bound * s.model_output_usd_per_million)
            / 1_000_000
        )

        request_messages = list(messages)

        def estimate():
            current = token_upper_bound({"messages": request_messages, "tools": tools})
            if current > self.profile.prompt_token_limit:
                raise BudgetExceeded("prompt_context_limit")
            usd = (
                0
                if self.mode == "fixture"
                else (current * s.model_input_usd_per_million + output_bound * s.model_output_usd_per_million)
                / 1_000_000
            )
            return usd, current + output_bound

        async def call():
            nonlocal output_bound
            completion = await self.provider.complete(
                request_messages, tools=tools, structured=schema is not None, max_tokens=output_bound
            )
            usage = completion.usage.model_dump()
            usage["provider_model"] = completion.provider_model
            try:
                if completion.finish_reason == "length":
                    raise ValueError("model_output_truncated")
                value = None
                if schema:
                    value = schema.model_validate_json(completion.message.get("content") or "").model_dump(
                        mode="json"
                    )
                elif not completion.message.get("content") and not completion.message.get("tool_calls"):
                    raise ValueError("empty_model_response")
                for tool_call in completion.message.get("tool_calls", []):
                    if not isinstance(tool_call.get("id"), str) or not isinstance(
                        tool_call.get("function", {}).get("arguments"), str
                    ):
                        raise ValueError("invalid_tool_envelope")
                return {"message": completion.message, "value": value}, usage
            except (ValueError, ValidationError) as cause:
                diagnostic = {"finish_reason": completion.finish_reason, "error_type": type(cause).__name__}
                if isinstance(cause, ValidationError):
                    diagnostic["schema_errors"] = [
                        {"location": list(e["loc"]), "type": e["type"]}
                        for e in cause.errors(include_input=False, include_context=False)
                    ]
                else:
                    diagnostic["reason"] = str(cause)
                error = ProviderError(
                    "invalid_structured_response:"
                    + ("truncated" if completion.finish_reason == "length" else "schema_or_empty")
                )
                error.usage = usage
                error.diagnostic = diagnostic
                # Authorized diagnostic/replay data, never returned through the ordinary usage endpoint.
                error.response = {"message": completion.message}
                if not tools and completion.message.get("content"):
                    # Non-tool requests ignore prior reasoning_content; retain it only in the
                    # protected action record. Never append unmatched/partial tool calls.
                    request_messages.append({"role": "assistant", "content": completion.message["content"]})
                request_messages.append(
                    {
                        "role": "user",
                        "content": "Repair the response using the original JSON schema. "
                        "Keep it concise, remove redundant prose, preserve required fields and valid IDs. "
                        "Do not include markdown fences. Diagnostic: " + json.dumps(diagnostic),
                    }
                )
                if completion.finish_reason == "length":
                    output_bound = min(self.profile.max_output_tokens, output_bound * 2)
                raise error from cause

        return await self._invoke(
            "model", key, call, reserve, input_bound + output_bound, task_id, closing, estimate
        )

    async def structured(self, role: str, key: str, payload: dict, schema: type[BaseModel], closing=False):
        messages = [
            {
                "role": "system",
                "content": POLICY
                + "\nRole: "
                + role
                + "\nJSON schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        return (await self.model(key, messages, schema=schema, closing=closing))["value"]

    async def tool(self, kind: str, key: str, operation, task_id=None):
        usd = self.settings.search_usd_per_call if kind == "search" and self.mode == "live" else 0

        async def call():
            result = await operation()
            return result, {"usd": usd}

        return await self._invoke(kind, key, call, usd, task_id=task_id)
