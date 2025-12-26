"""
RetroArch Streaming Daemon

This daemon manages RetroArch instances for cloud gaming streaming.
It handles:
- Xvfb virtual display allocation
- RetroArch process lifecycle
- WebRTC streaming via aiortc
- FFmpeg audio/video capture
- Input forwarding via network commands
- Session cleanup and auto-save
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiortc
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
    AudioStreamTrack,
    MediaStreamTrack,
)
from aiortc.contrib.media import MediaPlayer
from av import AudioFrame, VideoFrame

from config import config_manager
from handler import retroarch_handler
from handler.redis_handler import async_cache

logger = logging.getLogger(__name__)


@dataclass
class XvfbDisplay:
    """Represents an Xvfb virtual display"""
    display_number: int
    process: subprocess.Popen
    in_use: bool = False


class XvfbManager:
    """Manages allocation and cleanup of Xvfb virtual displays"""

    def __init__(self, start_display: int = 99, max_displays: int = 10):
        self.start_display = start_display
        self.max_displays = max_displays
        self.displays: dict[int, XvfbDisplay] = {}
        self.lock = asyncio.Lock()

    async def allocate_display(self) -> Optional[int]:
        """Allocate an available Xvfb display"""
        async with self.lock:
            # Try to reuse existing unused display
            for display_num, display in self.displays.items():
                if not display.in_use and display.process.poll() is None:
                    display.in_use = True
                    logger.info(f"Reusing Xvfb display :{display_num}")
                    return display_num

            # Create new display if under limit
            if len(self.displays) < self.max_displays:
                display_num = self.start_display + len(self.displays)

                try:
                    # Start Xvfb with 720p resolution
                    process = subprocess.Popen(
                        [
                            "Xvfb",
                            f":{display_num}",
                            "-screen", "0", "1280x720x24",
                            "-ac",  # Disable access control
                            "-nolisten", "tcp",
                            "+extension", "GLX",
                            "+render",
                            "-noreset",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

                    # Wait a bit for Xvfb to start
                    await asyncio.sleep(0.5)

                    if process.poll() is not None:
                        logger.error(f"Xvfb display :{display_num} failed to start")
                        return None

                    display = XvfbDisplay(
                        display_number=display_num,
                        process=process,
                        in_use=True,
                    )
                    self.displays[display_num] = display

                    logger.info(f"Created new Xvfb display :{display_num}")
                    return display_num

                except Exception as e:
                    logger.error(f"Failed to create Xvfb display: {e}")
                    return None

            logger.warning("No available Xvfb displays")
            return None

    async def release_display(self, display_num: int):
        """Mark display as available for reuse"""
        async with self.lock:
            if display_num in self.displays:
                self.displays[display_num].in_use = False
                logger.info(f"Released Xvfb display :{display_num}")

    async def cleanup_all(self):
        """Terminate all Xvfb processes"""
        async with self.lock:
            for display in self.displays.values():
                if display.process.poll() is None:
                    display.process.terminate()
                    try:
                        display.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        display.process.kill()
            self.displays.clear()
            logger.info("Cleaned up all Xvfb displays")


class RetroArchMediaSource:
    """Captures RetroArch video/audio using FFmpeg"""

    def __init__(self, display_num: int, session_id: str):
        self.display_num = display_num
        self.session_id = session_id
        self.player: Optional[MediaPlayer] = None

    async def start(self):
        """Start FFmpeg capture"""
        try:
            # FFmpeg command to capture X11 display and PulseAudio
            # Note: We use MediaPlayer from aiortc which wraps FFmpeg
            options = {
                "framerate": "30",
                "video_size": "1280x720",
                "thread_queue_size": "512",
            }

            # Create media player with X11grab (video) and pulse (audio)
            # Format: display:DISPLAY+x_offset,y_offset
            video_source = f":{self.display_num}.0+0,0"

            self.player = MediaPlayer(
                video_source,
                format="x11grab",
                options=options,
            )

            logger.info(f"Started FFmpeg capture for session {self.session_id} on display :{self.display_num}")

        except Exception as e:
            logger.error(f"Failed to start FFmpeg capture: {e}")
            raise

    def get_video_track(self) -> Optional[MediaStreamTrack]:
        """Get video track for WebRTC"""
        return self.player.video if self.player else None

    def get_audio_track(self) -> Optional[MediaStreamTrack]:
        """Get audio track for WebRTC"""
        return self.player.audio if self.player else None

    async def stop(self):
        """Stop FFmpeg capture"""
        if self.player:
            # MediaPlayer doesn't have a direct stop method
            # The underlying process will be cleaned up on deletion
            self.player = None
            logger.info(f"Stopped FFmpeg capture for session {self.session_id}")


class RetroArchInstance:
    """Manages a single RetroArch instance with WebRTC streaming"""

    def __init__(
        self,
        session_id: str,
        rom_path: str,
        core: str,
        save_path: Optional[str] = None,
        state_path: Optional[str] = None,
        display_num: int = 99,
    ):
        self.session_id = session_id
        self.rom_path = rom_path
        self.core = core
        self.save_path = save_path
        self.state_path = state_path
        self.display_num = display_num

        self.retroarch_process: Optional[subprocess.Popen] = None
        self.media_source: Optional[RetroArchMediaSource] = None
        self.peer_connection: Optional[RTCPeerConnection] = None
        self.last_activity = datetime.now()

    async def start_retroarch(self):
        """Launch RetroArch process"""
        try:
            # Build RetroArch command
            cmd = [
                "retroarch",
                "-v",  # Verbose
                "--config", "/etc/retroarch.cfg",
                "-L", f"/usr/lib/libretro/{self.core}_libretro.so",
                self.rom_path,
            ]

            # Load save state if provided
            if self.state_path:
                cmd.extend(["-e", "1", "-s", self.state_path])

            # Set environment for Xvfb display
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"

            # Start RetroArch
            self.retroarch_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Wait a bit for RetroArch to initialize
            await asyncio.sleep(2)

            if self.retroarch_process.poll() is not None:
                stdout, stderr = self.retroarch_process.communicate()
                logger.error(f"RetroArch failed to start: {stderr.decode()}")
                return False

            logger.info(f"Started RetroArch for session {self.session_id} (PID: {self.retroarch_process.pid})")
            return True

        except Exception as e:
            logger.error(f"Failed to start RetroArch: {e}")
            return False

    async def start_streaming(self):
        """Start FFmpeg capture and prepare for WebRTC"""
        try:
            self.media_source = RetroArchMediaSource(self.display_num, self.session_id)
            await self.media_source.start()
            logger.info(f"Started streaming for session {self.session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to start streaming: {e}")
            return False

    async def create_webrtc_offer(self) -> Optional[str]:
        """Create WebRTC offer SDP"""
        try:
            self.peer_connection = RTCPeerConnection()

            # Add video track
            video_track = self.media_source.get_video_track()
            if video_track:
                self.peer_connection.addTrack(video_track)

            # Add audio track
            audio_track = self.media_source.get_audio_track()
            if audio_track:
                self.peer_connection.addTrack(audio_track)

            # Create offer
            offer = await self.peer_connection.createOffer()
            await self.peer_connection.setLocalDescription(offer)

            logger.info(f"Created WebRTC offer for session {self.session_id}")
            return self.peer_connection.localDescription.sdp

        except Exception as e:
            logger.error(f"Failed to create WebRTC offer: {e}")
            return None

    async def set_webrtc_answer(self, answer_sdp: str):
        """Set WebRTC answer from client"""
        try:
            if not self.peer_connection:
                logger.error("No peer connection exists")
                return False

            answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
            await self.peer_connection.setRemoteDescription(answer)

            logger.info(f"Set WebRTC answer for session {self.session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to set WebRTC answer: {e}")
            return False

    async def send_input(self, key_code: str, event_type: str):
        """Send input to RetroArch via network commands"""
        try:
            # RetroArch network command port (from config)
            port = 55355

            # Build RetroArch network command
            # Format: COMMAND arg1 arg2...
            if event_type == "keydown":
                command = f"KEYBOARD_PRESS {key_code}\n"
            elif event_type == "keyup":
                command = f"KEYBOARD_RELEASE {key_code}\n"
            else:
                logger.warning(f"Unknown input event type: {event_type}")
                return

            # Send to RetroArch via UDP
            proc = await asyncio.create_subprocess_exec(
                "sh", "-c",
                f"echo '{command}' | nc -u -w 0 localhost {port}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    async def stop(self):
        """Stop RetroArch instance and cleanup"""
        logger.info(f"Stopping RetroArch instance for session {self.session_id}")

        # Stop media capture
        if self.media_source:
            await self.media_source.stop()

        # Close peer connection
        if self.peer_connection:
            await self.peer_connection.close()

        # Terminate RetroArch
        if self.retroarch_process and self.retroarch_process.poll() is None:
            self.retroarch_process.terminate()
            try:
                self.retroarch_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.retroarch_process.kill()
            logger.info(f"Terminated RetroArch process for session {self.session_id}")


class RetroArchDaemon:
    """Main daemon managing all RetroArch streaming sessions"""

    def __init__(self):
        self.config = config_manager.get_config()
        self.xvfb_manager = XvfbManager()
        self.instances: dict[str, RetroArchInstance] = {}
        self.running = False
        self.cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the daemon"""
        self.running = True
        logger.info("RetroArch streaming daemon started")

        # Subscribe to Redis channels for events
        await self._subscribe_to_events()

        # Start cleanup task
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """Stop the daemon"""
        self.running = False
        logger.info("Stopping RetroArch streaming daemon...")

        # Stop all instances
        for session_id in list(self.instances.keys()):
            await self._stop_session(session_id)

        # Cleanup Xvfb
        await self.xvfb_manager.cleanup_all()

        # Cancel cleanup task
        if self.cleanup_task:
            self.cleanup_task.cancel()

        logger.info("RetroArch streaming daemon stopped")

    async def _subscribe_to_events(self):
        """Subscribe to Redis pubsub channels for session events"""
        try:
            # Subscribe to session start/stop/input events
            asyncio.create_task(self._handle_session_events())
        except Exception as e:
            logger.error(f"Failed to subscribe to events: {e}")

    async def _handle_session_events(self):
        """Handle incoming session events from Redis"""
        while self.running:
            try:
                # Check for new sessions in Redis
                sessions = await retroarch_handler.get_all_sessions()

                for session in sessions:
                    if (
                        session.session_id not in self.instances
                        and session.state == retroarch_handler.SessionState.STARTING
                    ):
                        # Start new session
                        await self._start_session(session)

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error handling session events: {e}")
                await asyncio.sleep(5)

    async def _start_session(self, session_id: str, session_data: dict):
        """Start a new RetroArch streaming session"""
        try:
            logger.info(f"Starting session {session_id}")

            # Allocate Xvfb display
            display_num = await self.xvfb_manager.allocate_display()
            if display_num is None:
                logger.error(f"Failed to allocate display for session {session_id}")
                await retroarch_handler.update_session_state(session_id, "ERROR")
                return

            # Create instance
            instance = RetroArchInstance(
                session_id=session_id,
                rom_path=session_data["rom_path"],
                core=session_data["core"],
                save_path=session_data.get("save_path"),
                state_path=session_data.get("state_path"),
                display_num=display_num,
            )

            # Start RetroArch
            if not await instance.start_retroarch():
                logger.error(f"Failed to start RetroArch for session {session_id}")
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(session_id, "ERROR")
                return

            # Start streaming
            if not await instance.start_streaming():
                logger.error(f"Failed to start streaming for session {session_id}")
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(session_id, "ERROR")
                return

            # Create WebRTC offer
            offer_sdp = await instance.create_webrtc_offer()
            if not offer_sdp:
                logger.error(f"Failed to create WebRTC offer for session {session_id}")
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(session_id, "ERROR")
                return

            # Store instance
            self.instances[session_id] = instance

            # Update session in Redis
            await retroarch_handler.update_session_webrtc_offer(session_id, offer_sdp)
            await retroarch_handler.update_session_state(session_id, "RUNNING")

            logger.info(f"Session {session_id} started successfully")

        except Exception as e:
            logger.error(f"Failed to start session {session_id}: {e}")
            await retroarch_handler.update_session_state(session_id, "ERROR")

    async def _stop_session(self, session_id: str):
        """Stop a RetroArch streaming session"""
        try:
            if session_id not in self.instances:
                return

            instance = self.instances[session_id]
            display_num = instance.display_num

            # Stop instance
            await instance.stop()

            # Release display
            await self.xvfb_manager.release_display(display_num)

            # Remove from instances
            del self.instances[session_id]

            # Update session in Redis
            await retroarch_handler.update_session_state(session_id, "STOPPED")

            logger.info(f"Session {session_id} stopped")

        except Exception as e:
            logger.error(f"Failed to stop session {session_id}: {e}")

    async def _cleanup_loop(self):
        """Periodic cleanup of stale sessions"""
        while self.running:
            try:
                await asyncio.sleep(60)  # Check every minute

                # Clean up sessions inactive for 30 minutes
                timeout = timedelta(minutes=30)
                now = datetime.now()

                for session_id, instance in list(self.instances.items()):
                    if now - instance.last_activity > timeout:
                        logger.info(f"Cleaning up inactive session {session_id}")
                        await self._stop_session(session_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")


async def main():
    """Main entry point"""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: [RomM][retroarch_daemon][%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Create daemon
    daemon = RetroArchDaemon()

    # Setup signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        # Start daemon
        await daemon.start()

        # Keep running
        while daemon.running:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")

    finally:
        await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())