"""Natural-language requests prepare validated previews without a robot gateway."""

import json
from types import SimpleNamespace

import pytest

from roborock_auto.adapters.cleaning_assistant import (
    AssistantUnavailable,
    CleaningAssistant,
)
from roborock_auto.domain.models import Room

ROOMS = [Room(segment_id=16, name="Kitchen"), Room(segment_id=17, name="Study")]


def reply(**changes):
    return {
        "kind": "plan", "message": "Vacuum the kitchen, including carpets, once.",
        "plan": {"segment_ids": [16], "mode": "vacuum", "carpet": "rise", "repeat": 1},
        "evidence_ids": [], **changes,
    }


def sdk_return(data, calls):
    async def query(**kwargs):
        calls.append(kwargs)
        yield SimpleNamespace(structured_output=data, is_error=False, subtype="success")
    return query


async def test_plan_and_tool_isolation_snapshot():
    calls = []
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(reply(), calls))
    result = await assistant.preview("Vacuum kitchen including carpets", rooms=ROOMS, history=[], timezone="Australia/Sydney")
    assert result.model_dump() == reply()
    options = calls[0]["options"]
    assert {
        "model": options.model, "tools": options.tools, "settings": options.setting_sources,
        "mcp": options.mcp_servers, "turns": options.max_turns,
        "permission": options.permission_mode, "persist": options.extra_args, "strict_mcp": options.strict_mcp_config,
    } == {
        "model": "sonnet", "tools": [], "settings": [], "mcp": {}, "turns": 3,
        "permission": "dontAsk", "persist": {"no-session-persistence": None}, "strict_mcp": True,
    }
    context = json.loads(calls[0]["prompt"])
    assert context == {"request": "Vacuum kitchen including carpets", "timezone": "Australia/Sydney",
                       "rooms": [{"segment_id": 16, "name": "Kitchen"}, {"segment_id": 17, "name": "Study"}],
                       "history": []}


async def test_disabled_does_not_invoke_sdk():
    calls = []
    assistant = CleaningAssistant(query_fn=sdk_return(reply(), calls))
    with pytest.raises(AssistantUnavailable, match="not enabled"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")
    assert calls == []


@pytest.mark.parametrize("plan", [
    {"segment_ids": None, "mode": "vacuum", "carpet": "rise", "repeat": 2},
    {"segment_ids": [999], "mode": "vacuum", "carpet": "rise", "repeat": 1},
    {"segment_ids": [], "mode": "vacuum", "carpet": "rise", "repeat": 1},
    {"segment_ids": [16, 16], "mode": "vacuum", "carpet": "rise", "repeat": 1},
    {"segment_ids": [16], "mode": "shell", "carpet": "rise", "repeat": 1},
    {"segment_ids": [16], "mode": "vacuum", "carpet": "ignore", "repeat": 1},
    {"segment_ids": [16], "mode": "vacuum", "carpet": "rise", "repeat": 4},
    {"segment_ids": [16], "mode": "vacuum", "carpet": "rise", "repeat": True},
    {"segment_ids": [16], "mode": "two-stage", "carpet": "avoid", "repeat": 2},
    {"segment_ids": [16], "mode": "two-stage", "carpet": "rise", "repeat": 1},
])
async def test_invalid_plan_never_becomes_actionable(plan):
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(reply(plan=plan), []))
    with pytest.raises(AssistantUnavailable, match="valid preview"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")


async def test_whole_house_two_stage_and_history_evidence():
    expected = reply(plan={"segment_ids": None, "mode": "two-stage", "carpet": "avoid", "repeat": 1})
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(expected, []))
    assert (await assistant.preview("Two-stage clean the whole house", rooms=ROOMS, history=[], timezone="UTC")).model_dump() == expected
    expected = reply(kind="answer", message="The app started a kitchen vacuum; completion is not recorded.", plan=None, evidence_ids=["app-1"])
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(expected, []))
    assert (await assistant.preview("What happened?", rooms=ROOMS, history=[{"id": "app-1", "status": "started"}], timezone="UTC")).model_dump() == expected


async def test_unknown_history_citation_is_rejected():
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(reply(kind="answer", plan=None, evidence_ids=["invented"]), []))
    with pytest.raises(AssistantUnavailable, match="valid preview"):
        await assistant.preview("What happened?", rooms=ROOMS, history=[], timezone="UTC")


async def test_sdk_failure_is_sanitized():
    async def fail(**kwargs):
        raise RuntimeError("secret credential path")
        yield
    assistant = CleaningAssistant(enabled=True, query_fn=fail)
    with pytest.raises(AssistantUnavailable, match="temporarily unavailable") as exc:
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("data", [
    None,
    reply(kind="answer"),
    reply(kind="plan", plan=None),
    reply(message=""),
    reply(unexpected="field"),
])
async def test_malformed_output_is_not_returned(data):
    assistant = CleaningAssistant(enabled=True, query_fn=sdk_return(data, []))
    with pytest.raises(AssistantUnavailable, match="valid preview"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")


async def test_sdk_error_result_is_not_accepted():
    async def fail(**kwargs):
        yield SimpleNamespace(structured_output=reply(), is_error=True, subtype="error_max_turns")
    assistant = CleaningAssistant(enabled=True, query_fn=fail)
    with pytest.raises(AssistantUnavailable, match="temporarily unavailable"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")


async def test_timeout_and_busy_request_are_user_safe():
    async def timeout(**kwargs):
        raise TimeoutError()
        yield
    assistant = CleaningAssistant(enabled=True, query_fn=timeout)
    with pytest.raises(AssistantUnavailable, match="took too long"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")
    async with assistant._lock:
        with pytest.raises(AssistantUnavailable, match="another request"):
            await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")


@pytest.mark.parametrize("model", ["opus", "claude-opus-4-6", ""])
async def test_non_sonnet_configuration_is_rejected(model):
    calls = []
    assistant = CleaningAssistant(enabled=True, model=model, query_fn=sdk_return(reply(), calls))
    with pytest.raises(AssistantUnavailable, match="Sonnet"):
        await assistant.preview("Clean", rooms=ROOMS, history=[], timezone="UTC")
    assert calls == []
