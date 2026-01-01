"""RetroArch streaming endpoints.

This module provides REST API endpoints for managing RetroArch streaming sessions.
"""

import json
import os
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
    screen_width: int | None = None
    screen_height: int | None = None
    firmware_id: int | None = None


class TouchscreenRegion(BaseModel):
    """Touchscreen region configuration for dual-screen systems."""

    x_offset: float  # Ratio 0.0-1.0
    y_offset: float  # Ratio 0.0-1.0
    width: float     # Ratio 0.0-1.0
    height: float    # Ratio 0.0-1.0


class IceServer(BaseModel):
    """ICE server configuration for WebRTC."""

    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class StartSessionResponse(BaseModel):
    """Response containing session ID and WebRTC offer."""

    session_id: str
    webrtc_offer: str
    touchscreen_region: TouchscreenRegion | None = None  # Only for cores with touchscreen
    core_options: dict[str, str] = {}  # Core-specific options loaded from RetroArch
    ice_servers: list[IceServer] = []  # ICE servers (STUN/TURN) for WebRTC


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
        firmware_id=data.firmware_id,
        state=SessionState.STARTING,
    )

    await retroarch_handler.set_session(session)

    # Store screen dimensions in Redis for daemon
    if data.screen_width and data.screen_height:
        dims_key = f"retroarch:screen_dims:{session_id}"
        await async_cache.set(
            dims_key,
            json.dumps({"width": data.screen_width, "height": data.screen_height}),
            ex=300  # Expire after 5 minutes
        )

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

            # Check if there's a touchscreen region config in Redis
            region_key = f"retroarch:touchscreen_region:{session_id}"
            region_data = await async_cache.get(region_key)
            touchscreen_region = None

            if region_data:
                try:
                    region = json.loads(region_data)
                    touchscreen_region = TouchscreenRegion(
                        x_offset=region["x_offset"],
                        y_offset=region["y_offset"],
                        width=region["width"],
                        height=region["height"],
                    )
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning(f"Failed to parse touchscreen region: {e}")

            # Retrieve core options from Redis (populated by daemon when RetroArch starts)
            options_key = f"retroarch:core_options:{session_id}"
            options_data = await async_cache.get(options_key)
            core_options = {}

            if options_data:
                try:
                    core_options = json.loads(options_data)
                except json.JSONDecodeError as e:
                    log.warning(f"Failed to parse core options: {e}")

            # Build ICE servers list from environment variables
            ice_servers = []

            # Always add STUN server
            stun_server = os.getenv("RETROARCH_STUN_SERVER", "stun:stun.l.google.com:19302")
            ice_servers.append(IceServer(urls=stun_server))

            # Add integrated coturn TURN server if available
            # These env vars are set by entrypoint.sh when coturn starts
            turn_host = os.getenv("RETROARCH_TURN_EXTERNAL_HOST")
            turn_user = os.getenv("RETROARCH_TURN_USER")
            turn_password = os.getenv("RETROARCH_TURN_PASSWORD")
            turn_port = os.getenv("RETROARCH_TURN_PORT", "3478")

            if turn_host and turn_user and turn_password:
                # Add TURN server with both UDP and TCP transports in a single entry
                # Using array of URLs is more compatible with browsers
                ice_servers.append(IceServer(
                    urls=[
                        f"turn:{turn_host}:{turn_port}",
                        f"turn:{turn_host}:{turn_port}?transport=tcp",
                    ],
                    username=turn_user,
                    credential=turn_password,
                ))

            return StartSessionResponse(
                session_id=session_id,
                webrtc_offer=updated_session.webrtc_offer,
                touchscreen_region=touchscreen_region,
                core_options=core_options,
                ice_servers=ice_servers,
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
    # The daemon will sync saves/states to RomM and delete the session
    stop_key = f"retroarch:stop:{data.session_id}"
    await async_cache.set(stop_key, "1", ex=10)  # Expire after 10s

    return {"status": "ok", "message": "Session stop signal sent"}


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