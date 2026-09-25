from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from roborock_auto.adapters.cleaning_assistant import AssistantReply, CleaningAssistant
from roborock_auto.adapters.web.assistant_api import router
from roborock_auto.domain.models import Room


def client_for(assistant):
    app = FastAPI()
    app.include_router(router)
    app.state.assistant = assistant
    app.state.gateway = SimpleNamespace(list_rooms=AsyncMock(return_value=[Room(7, 'Kitchen')]))
    app.state.config = SimpleNamespace(timezone='Australia/Sydney')
    app.state.recent_cleaning = SimpleNamespace(read=AsyncMock(return_value={
        'last_record': None, 'record_available': True, 'record_stale': False,
        'recent_requests': [],
    }))
    return TestClient(app)


def test_assistant_preview_never_invokes_robot_commands():
    reply = AssistantReply(kind='plan', message='Vacuum Kitchen, including carpets, twice.',
                           plan={'segment_ids': [7], 'mode': 'vacuum', 'carpet': 'rise', 'repeat': 2},
                           evidence_ids=[])
    assistant = SimpleNamespace(enabled=True, model='sonnet', preview=AsyncMock(return_value=reply))
    with client_for(assistant) as client:
        response = client.post('/assistant', json={'prompt': 'Vacuum the kitchen twice'})
    assert (response.status_code, response.json()) == (200, reply.model_dump())
    assert assistant.preview.call_args.kwargs == {
        'rooms': [Room(7, 'Kitchen')], 'history': [], 'timezone': 'Australia/Sydney',
    }


def test_disabled_assistant_does_not_read_the_robot():
    with client_for(CleaningAssistant()) as client:
        response = client.post('/assistant', json={'prompt': 'Vacuum the kitchen'})
        assert client.app.state.gateway.list_rooms.await_count == 0
    assert (response.status_code, response.json()) == (503, {
        'detail': 'The cleaning assistant is not enabled.',
    })


def test_assistant_rejects_empty_input_before_reading_robot():
    with client_for(CleaningAssistant(enabled=True)) as client:
        response = client.post('/assistant', json={'prompt': '   '})
        assert client.app.state.gateway.list_rooms.await_count == 0
    assert response.status_code == 422


def test_context_failure_does_not_invoke_sonnet_or_expose_details():
    assistant = SimpleNamespace(enabled=True, model='sonnet', preview=AsyncMock())
    with client_for(assistant) as client:
        client.app.state.gateway.list_rooms.side_effect = TimeoutError('private connection detail')
        response = client.post('/assistant', json={'prompt': 'Vacuum the kitchen'})
    assert (response.status_code, response.json(), assistant.preview.await_count) == (503, {
        'detail': 'Cleaning context is unavailable. Please try again.',
    }, 0)
