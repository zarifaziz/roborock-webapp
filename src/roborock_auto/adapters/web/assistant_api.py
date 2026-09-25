"""Read-only language previews and recorded cleaning results."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from roborock_auto.adapters.cleaning_assistant import AssistantUnavailable
from roborock_auto.adapters.web.cleaning_context import assistant_evidence

router = APIRouter()
_LOGGER = logging.getLogger(__name__)


class AssistantRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=2000)

    @field_validator('prompt')
    @classmethod
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError('Enter a cleaning request or question')
        return value.strip()


@router.get('/assistant')
async def assistant_status(request: Request):
    assistant = request.app.state.assistant
    return {'enabled': assistant.enabled, 'model': assistant.model}


@router.post('/assistant')
async def assistant_preview(request: Request, body: AssistantRequest):
    state = request.app.state
    if not state.assistant.enabled:
        raise HTTPException(503, 'The cleaning assistant is not enabled.')
    try:
        async with asyncio.timeout(15):
            rooms = await state.gateway.list_rooms()
            recent = await state.recent_cleaning.read()
        reply = await state.assistant.preview(
            body.prompt, rooms=rooms, history=assistant_evidence(recent),
            timezone=state.config.timezone,
        )
    except AssistantUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except Exception:
        _LOGGER.exception('Cleaning assistant context unavailable')
        raise HTTPException(503, 'Cleaning context is unavailable. Please try again.') from None
    return reply.model_dump()


@router.get('/cleaning/recent')
async def recent_cleaning(request: Request):
    return await request.app.state.recent_cleaning.read()
