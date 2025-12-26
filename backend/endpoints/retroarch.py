"""RetroArch streaming endpoints.

This module provides REST API endpoints for managing RetroArch streaming sessions.
"""

import json
import logging
import uuid
from typing import Annotated

from fastapi import Body, HTTPException, Request, status
from pydantic import BaseModel

from config.config_manager import config_manager
from decorators.auth import protected_route
from handler.auth.constants import Scope
from handler.database import db_rom_handler
from handler.redis_handler import async_cache
from handler.retroarch_handler import (
    MAX_SESSIONS_DEFAULT,
    SessionState,
    retroarch_handler,
)
from logger.logger import log
from utils.router import APIRouter

router = APIRouter(
    prefix="/retroarch",
    tags=["retroarch"],
)


# Request/Response Models
class StartSessionRequest(BaseModel):
    """Request to start a new RetroArch streaming session."""

    rom_id: int
    core: str
    save_id: int | None = None
    state_id: int | None = None


class StartSessionResponse(BaseModel):
    """Response containing session ID and WebRTC offer."""

    session_id: str
    webrtc_offer: str


class AnswerSessionRequest(BaseModel):
    """Request to provide WebRTC answer for a session."""

    session_id: str
    webrtc_answer: str


class StopSessionRequest(BaseModel):
    """Request to stop a streaming session."""

    session_id: str


class SessionInfoResponse(BaseModel):
    """Session information response."""

    session_id: str
    rom_id: int
    platform_slug: str
    core: str
    state: str
    created_at: str
    last_activity: str


@protected_route(router.post, "/stream/start", [Scope.ASSETS_READ])
async def start_stream(
    request: Request,
    data: StartSessionRequest,
) -> StartSessionResponse:
    """Start a new RetroArch streaming session.

    This endpoint:
    1. Validates the ROM exists and user has access
    2. Checks session limits
    3. Creates a session in Redis
    4. Signals the daemon to start RetroArch (via Redis pubsub)
    5. Returns session ID and WebRTC offer (placeholder for now)

    Args:
        request: FastAPI request with user context
        data: Session start parameters

    Returns:
        StartSessionResponse with session_id and webrtc_offer

    Raises:
        HTTPException 404: ROM not found
        HTTPException 429: Too many active sessions
    """
    # Check if RetroArch is enabled
    config = config_manager.get_config()
    if not getattr(config, "RETROARCH_ENABLED", False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RetroArch streaming is not enabled",
        )

    # Validate ROM exists and user has access
    rom = db_rom_handler.get_rom(data.rom_id)
    if not rom:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"ROM with ID {data.rom_id} not found",
        )

    # Check session limits
    max_sessions = getattr(config, "RETROARCH_MAX_SESSIONS", MAX_SESSIONS_DEFAULT)
    active_count = await retroarch_handler.get_active_sessions_count()

    if active_count >= max_sessions:
        log.warning(
            f"Session limit reached: {active_count}/{max_sessions}"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Maximum number of sessions ({max_sessions}) reached. Please try again later.",
        )

    # Generate session ID
    session_id = str(uuid.uuid4())

    log.info(
        f"Starting RetroArch session {session_id} for ROM {rom.name} (user: {request.user.username})"
    )

    # Create session in Redis
    from handler.retroarch_handler import RetroArchSession

    session = RetroArchSession(
        session_id=session_id,
        user_id=request.user.id,
        rom_id=rom.id,
        platform_slug=rom.platform_slug,
        core=data.core,
        save_id=data.save_id,
        state_id=data.state_id,
        state=SessionState.STARTING,
    )

    await retroarch_handler.set_session(session)

    # Wait for daemon to start RetroArch and generate WebRTC offer
    # Poll the session for up to 30 seconds
    import asyncio
    max_wait = 30  # seconds
    poll_interval = 0.5  # seconds
    elapsed = 0

    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        # Refresh session from Redis
        updated_session = await retroarch_handler.get_session(session_id)
        if not updated_session:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Session was deleted unexpectedly",
            )

        # Check if daemon has generated the offer
        if updated_session.webrtc_offer and updated_session.state == SessionState.RUNNING:
            log.info(f"Session {session_id} is ready with WebRTC offer")
            return StartSessionResponse(
                session_id=session_id,
                webrtc_offer=updated_session.webrtc_offer,
            )

        # Check for error state
        if updated_session.state == SessionState.ERROR:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="RetroArch daemon failed to start the session. Check daemon logs for details.",
            )

    # Timeout - daemon didn't respond in time
    await retroarch_handler.delete_session(session_id)
    raise HTTPException(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        detail="RetroArch daemon did not respond in time. Please ensure the daemon is running.",
    )


@protected_route(router.post, "/stream/answer", [Scope.ASSETS_READ])
async def answer_stream(
    request: Request,
    data: AnswerSessionRequest,
) -> dict[str, str]:
    """Provide WebRTC answer for a session.

    Args:
        request: FastAPI request with user context
        data: WebRTC answer data

    Returns:
        Success message

    Raises:
        HTTPException 404: Session not found
        HTTPException 403: User doesn't own the session
    """
    session = await retroarch_handler.get_session(data.session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {data.session_id} not found",
        )

    # Verify ownership
    if session.user_id != request.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this session",
        )

    # Update session with answer
    session.webrtc_answer = data.webrtc_answer
    await retroarch_handler.set_session(session)

    # Store answer in Redis for daemon to pick up
    answer_key = f"retroarch:webrtc_answer:{data.session_id}"
    await async_cache.set(answer_key, data.webrtc_answer, ex=60)  # Expire after 60s

    log.debug(
        f"WebRTC answer received and stored in Redis for session {data.session_id}"
    )

    return {"status": "ok", "message": "WebRTC answer processed"}


@protected_route(router.post, "/stream/stop", [Scope.ASSETS_READ])
async def stop_stream(
    request: Request,
    data: StopSessionRequest,
) -> dict[str, str]:
    """Stop a streaming session.

    Args:
        request: FastAPI request with user context
        data: Session stop request

    Returns:
        Success message

    Raises:
        HTTPException 404: Session not found
        HTTPException 403: User doesn't own the session
    """
    session = await retroarch_handler.get_session(data.session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {data.session_id} not found",
        )

    # Verify ownership
    if session.user_id != request.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this session",
        )

    log.info(
        f"Stopping RetroArch session {data.session_id}"
    )

    # Signal daemon via Redis to stop the process
    stop_key = f"retroarch:stop:{data.session_id}"
    await async_cache.set(stop_key, "1", ex=10)  # Expire after 10s

    # The daemon will delete the session when it processes the stop signal
    # But we also delete it here to ensure cleanup
    await retroarch_handler.delete_session(data.session_id)

    return {"status": "ok", "message": "Session stopped"}


@protected_route(router.post, "/input", [Scope.ASSETS_READ])
async def send_input(
    request: Request,
    session_id: Annotated[str, Body(embed=True)],
    input_event: Annotated[dict, Body(embed=True)],
) -> dict[str, str]:
    """Send input event to a RetroArch session.

    Args:
        request: FastAPI request with user context
        session_id: Target session ID
        input_event: Input event data (type, key, code, etc.)

    Returns:
        Success message

    Raises:
        HTTPException 404: Session not found
        HTTPException 403: User doesn't own the session
    """
    session = await retroarch_handler.get_session(session_id)

    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session {session_id} not found",
        )

    # Verify ownership
    if session.user_id != request.user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this session",
        )

    # Update activity timestamp
    await retroarch_handler.update_activity(session_id)

    # Forward input to daemon via Redis list
    input_key = f"retroarch:input:{session_id}"
    await async_cache.rpush(input_key, json.dumps(input_event))
    await async_cache.expire(input_key, 60)  # Expire list after 60s

    return {"status": "ok"}


@protected_route(router.get, "/sessions", [Scope.ASSETS_READ])
async def get_sessions(
    request: Request,
) -> list[SessionInfoResponse]:
    """Get all sessions for the current user.

    Args:
        request: FastAPI request with user context

    Returns:
        List of user's sessions
    """
    sessions = await retroarch_handler.get_user_sessions(request.user.id)

    return [
        SessionInfoResponse(
            session_id=s.session_id,
            rom_id=s.rom_id,
            platform_slug=s.platform_slug,
            core=s.core,
            state=s.state.value,
            created_at=s.created_at,
            last_activity=s.last_activity,
        )
        for s in sessions
    ]