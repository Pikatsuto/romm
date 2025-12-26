"""RetroArch session management handler.

This module manages RetroArch streaming sessions using Redis for state persistence.
Each session represents an active RetroArch instance with WebRTC streaming.
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from handler.redis_handler import async_cache
from logger.logger import log

REDIS_RETROARCH_SESSIONS_KEY = "retroarch:sessions"
SESSION_TIMEOUT_MINUTES = 30
MAX_SESSIONS_DEFAULT = 3


class SessionState(str, Enum):
    """RetroArch session lifecycle states."""

    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class RetroArchSession:
    """RetroArch streaming session data model."""

    session_id: str
    user_id: int
    rom_id: int
    platform_slug: str
    core: str
    pid: Optional[int] = None
    xvfb_display: Optional[int] = None
    state: SessionState = SessionState.STARTING
    created_at: Optional[str] = None
    last_activity: Optional[str] = None
    webrtc_offer: Optional[str] = None
    webrtc_answer: Optional[str] = None
    save_id: Optional[int] = None
    state_id: Optional[int] = None

    def __post_init__(self):
        """Initialize timestamps if not provided."""
        now = datetime.utcnow().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_activity:
            self.last_activity = now

    def to_dict(self) -> dict:
        """Convert to dictionary for Redis storage."""
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RetroArchSession":
        """Create session from dictionary."""
        data["state"] = SessionState(data["state"])
        return cls(**data)


class RetroArchSessionHandler:
    """Handler for RetroArch session lifecycle management."""

    async def get_session(self, session_id: str) -> Optional[RetroArchSession]:
        """Retrieve a session by ID.

        Args:
            session_id: Unique session identifier

        Returns:
            RetroArchSession if found, None otherwise
        """
        session_data = await async_cache.hget(REDIS_RETROARCH_SESSIONS_KEY, session_id)
        if not session_data:
            return None

        try:
            data = json.loads(session_data)
            return RetroArchSession.from_dict(data)
        except (json.JSONDecodeError, TypeError, KeyError) as e:
            log.error(f"Failed to deserialize session {session_id}: {e}")
            return None

    async def set_session(self, session: RetroArchSession) -> None:
        """Store or update a session.

        Args:
            session: Session to store
        """
        session_data = json.dumps(session.to_dict())
        await async_cache.hset(
            REDIS_RETROARCH_SESSIONS_KEY, session.session_id, session_data
        )
        log.debug(
            f"Session {session.session_id} stored (state={session.state.value})"
        )

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        Args:
            session_id: Session to delete
        """
        await async_cache.hdel(REDIS_RETROARCH_SESSIONS_KEY, session_id)
        log.debug(f"Session {session_id} deleted")

    async def get_user_sessions(self, user_id: int) -> list[RetroArchSession]:
        """Get all active sessions for a user.

        Args:
            user_id: User ID

        Returns:
            List of user's sessions
        """
        all_sessions = await self._get_all_sessions()
        return [s for s in all_sessions if s.user_id == user_id]

    async def get_active_sessions_count(self) -> int:
        """Get total number of active sessions (not stopped or error).

        Returns:
            Count of active sessions
        """
        all_sessions = await self._get_all_sessions()
        return len(
            [
                s
                for s in all_sessions
                if s.state not in (SessionState.STOPPED, SessionState.ERROR)
            ]
        )

    async def cleanup_stale_sessions(self) -> int:
        """Remove sessions that have been inactive for too long.

        Returns:
            Number of sessions cleaned up
        """
        all_sessions = await self._get_all_sessions()
        now = datetime.utcnow()
        cutoff = now - timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        cleaned_count = 0

        for session in all_sessions:
            try:
                last_activity = datetime.fromisoformat(session.last_activity)
                if last_activity < cutoff:
                    log.info(
                        f"Cleaning up stale session {session.session_id} "
                        f"(last activity: {session.last_activity})"
                    )
                    await self.delete_session(session.session_id)
                    cleaned_count += 1
            except (ValueError, TypeError) as e:
                log.warning(
                    f"Invalid timestamp in session {session.session_id}: {e}"
                )

        if cleaned_count > 0:
            log.info(
                f"Cleaned up {cleaned_count} stale sessions"
            )

        return cleaned_count

    async def update_activity(self, session_id: str) -> None:
        """Update the last activity timestamp for a session.

        Args:
            session_id: Session to update
        """
        session = await self.get_session(session_id)
        if session:
            session.last_activity = datetime.utcnow().isoformat()
            await self.set_session(session)

    async def _get_all_sessions(self) -> list[RetroArchSession]:
        """Get all sessions from Redis.

        Returns:
            List of all sessions
        """
        sessions_data = await async_cache.hgetall(REDIS_RETROARCH_SESSIONS_KEY)
        if not sessions_data:
            return []

        sessions = []
        for session_data in sessions_data.values():
            try:
                data = json.loads(session_data)
                sessions.append(RetroArchSession.from_dict(data))
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                log.error(f"Failed to deserialize session: {e}")

        return sessions


# Global handler instance
retroarch_handler = RetroArchSessionHandler()