import json

from research_agent.context import (
    BUDGET_CUMULATIVE_CAPS,
    BUDGET_GROUPS,
    ResearchProgress,
    degradation_level,
    estimated_tokens,
    merge_read_ranges,
    serialized_bytes,
    tool_protocol_closed,
)


def test_token_measurements_and_degradation_thresholds_are_distinct():
    payload = {"中文": "证据" * 100, "ascii": "evidence " * 100}
    assert serialized_bytes(payload) > estimated_tokens(payload)
    assert sum(BUDGET_GROUPS.values()) == 180_000
    assert BUDGET_CUMULATIVE_CAPS == {
        "scope_plan": 27_000,
        "research_extraction": 117_000,
        "review_gap": 141_000,
        "writer_patch": 180_000,
    }
    assert degradation_level(125_999) == 0
    assert degradation_level(126_000) == 70
    assert degradation_level(153_000) == 85
    assert degradation_level(171_000) == 95


def test_progress_capsule_excludes_history_and_merges_read_ranges():
    progress = ResearchProgress(
        authorized_source_ids=["source-a"],
        candidate_urls=["https://example.test/paper"],
        read_ranges={"source-a": merge_read_ranges([(0, 100), (80, 150), (300, 350)])},
        retrieval_hits=[{"chunk_id": "chunk-1", "source_id": "source-a", "start": 0, "end": 150}],
        acceptance_coverage=["criterion-1"],
        unresolved=["missing ablation"],
    ).prompt_view()
    assert "messages" not in progress
    assert progress["read_ranges"]["source-a"] == [[0, 150], [300, 350]]


def test_tool_protocol_must_close_before_capsule_boundary():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_source", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
    ]
    assert tool_protocol_closed(messages)
    assert not tool_protocol_closed(messages[:1])
    assert not tool_protocol_closed(
        [{"role": "tool", "tool_call_id": "unknown", "content": "{}"}]
    )


def test_fixed_tool_sequence_capsule_reduces_serialized_request_by_35_percent():
    task = {"id": "task-1", "objective": "Compare evidence", "acceptance_criteria": ["c1"]}
    source_text = "source evidence " * 800
    old_messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": json.dumps(task)}]
    for index in range(3):
        old_messages.extend(
            [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "function": {"name": "read_source", "arguments": '{"source_id":"source-a"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": f"call-{index}", "content": source_text},
            ]
        )
    capsule_request = {
        "task": task,
        "progress": ResearchProgress(
            authorized_source_ids=["source-a"],
            read_ranges={"source-a": [(0, len(source_text))]},
            retrieval_hits=[{"chunk_id": "chunk-1", "source_id": "source-a"}],
            acceptance_coverage=["c1"],
        ).prompt_view(),
        "dependency_context": {"claims": [{"id": "claim-1", "span_ids": ["span-1"]}]},
    }
    assert serialized_bytes(capsule_request) <= serialized_bytes(old_messages) * 0.65
    assert capsule_request["task"] == task
    assert capsule_request["progress"]["authorized_source_ids"] == ["source-a"]
    assert capsule_request["dependency_context"]["claims"][0]["span_ids"] == ["span-1"]
