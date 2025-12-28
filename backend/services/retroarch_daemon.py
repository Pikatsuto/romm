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

from config.config_manager import config_manager
from handler.retroarch_handler import retroarch_handler, RetroArchSession, SessionState
from handler.database import db_rom_handler
from handler.redis_handler import async_cache
from models.rom import Rom

logger = logging.getLogger(__name__)


# Touchscreen region configuration per core
# Format: (x_offset, y_offset, width_ratio, height_ratio, native_width, native_height, y_offset_native)
# Values are ratios of total screen size (0.0 to 1.0) + native resolution
TOUCHSCREEN_REGIONS = {
    # Nintendo DS cores - dual screen 256x192 each (4:3 aspect ratio)
    # Native touchscreen: 256x192, positioned below top screen (y_offset = 192)
    "desmume": {
        # Vertical: bottom half, centered 4:3 screen
        "vertical": (0.3125, 0.5, 0.375, 0.5, 256, 192, 192),
        # Horizontal: right half, centered 4:3 screen
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 256, 192, 192),
        "native_total_height": 384,  # Both screens: 192 + 192
    },
    "melonds": {
        "vertical": (0.3125, 0.5, 0.375, 0.5, 256, 192, 192),
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 256, 192, 192),
        "native_total_height": 384,
    },
    # Nintendo 3DS - different screen sizes
    # Top: 400x240, Bottom: 320x240 (touchscreen)
    "citra": {
        "vertical": (0.3125, 0.5, 0.375, 0.5, 320, 240, 240),
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 320, 240, 240),
        "native_total_height": 480,  # 240 + 240
    },
    # Wii U gamepad - 854x480 touchscreen
    "cemu": {
        "vertical": (0.0, 0.0, 1.0, 1.0, 854, 480, 0),
        "horizontal": (0.0, 0.0, 1.0, 1.0, 854, 480, 0),
        "native_total_height": 480,
    },
}


# Standard resolutions (landscape format)
# Will be automatically rotated for portrait orientation
STANDARD_RESOLUTIONS = [
    # 4K / UHD (PC, TV)
    (3840, 2160),
    (3440, 1440),  # Ultrawide 21:9
    # 2K / QHD (PC, TV)
    (2560, 1440),
    (2560, 1080),  # Ultrawide 21:9
    # Full HD+ (PC, TV, Phone landscape - modern aspect ratios)
    (2400, 1080),  # 20:9
    (2340, 1080),  # 19.5:9 (notch)
    (2280, 1080),  # 19:9
    (2160, 1080),  # 18:9
    # Full HD (PC, TV, Phone landscape - standard)
    (1920, 1200),  # 16:10
    (1920, 1080),  # 16:9
    # QHD+ Phone landscape
    (3200, 1440),  # 20:9
    (3040, 1440),  # 19:9
    (2960, 1440),  # 18.5:9
    # HD+ / HD (PC, Phone - modern aspect ratios)
    (1600, 900),   # 16:9
    (1600, 720),   # 20:9
    (1560, 720),   # 19.5:9
    (1520, 720),   # 19:9
    (1480, 720),   # 18.5:9
    # HD (PC, Phone)
    (1366, 768),   # 16:9
    (1280, 800),   # 16:10
    (1280, 720),   # 16:9
    # Lower resolutions
    (1024, 768),   # 4:3
    (960, 540),    # 16:9
    (854, 480),    # 16:9
    (800, 600),    # 4:3
]


def calculate_optimal_resolution(screen_width: int, screen_height: int, max_resolution: str | None = None) -> tuple[int, int]:
    """
    Calculate optimal Xvfb resolution based on screen dimensions.

    Args:
        screen_width: Client screen width in pixels
        screen_height: Client screen height in pixels
        max_resolution: Optional max resolution in format "WIDTHxHEIGHT" (e.g., "1920x1080")

    Returns:
        Tuple of (width, height) for Xvfb display
    """
    # Parse max resolution if provided
    max_width = None
    max_height = None
    if max_resolution:
        try:
            parts = max_resolution.lower().split('x')
            if len(parts) == 2:
                max_width = int(parts[0])
                max_height = int(parts[1])
        except (ValueError, IndexError):
            logger.warning(f"Invalid max resolution format: {max_resolution}, ignoring")

    # Determine orientation
    is_portrait = screen_height > screen_width

    # Build available resolutions (swap width/height for portrait)
    available_resolutions = []
    for width, height in STANDARD_RESOLUTIONS:
        if is_portrait:
            # Swap for portrait orientation
            res_width, res_height = height, width
        else:
            res_width, res_height = width, height

        # Filter by max resolution if specified
        if max_width and max_height:
            if res_width <= max_width and res_height <= max_height:
                available_resolutions.append((res_width, res_height))
        else:
            available_resolutions.append((res_width, res_height))

    # Fallback if no resolutions available after filtering
    if not available_resolutions:
        if max_width and max_height:
            logger.warning(f"No resolutions available below max {max_width}x{max_height}, using max")
            return (max_width, max_height)
        else:
            default = (1280, 720) if not is_portrait else (720, 1280)
            return default

    # Find best matching resolution
    # Strategy: Find the largest resolution that fits within screen dimensions
    best_resolution = None
    best_score = -1

    for res_width, res_height in available_resolutions:
        # Resolution must fit within screen dimensions
        if res_width <= screen_width and res_height <= screen_height:
            # Score based on area coverage (prefer larger resolutions)
            score = res_width * res_height

            if score > best_score:
                best_score = score
                best_resolution = (res_width, res_height)

    # Fallback if no resolution fits within screen
    if best_resolution is None:
        # Use smallest available resolution
        best_resolution = min(available_resolutions, key=lambda r: r[0] * r[1])
        logger.warning(
            f"No standard resolution fits screen {screen_width}x{screen_height}, "
            f"using {best_resolution[0]}x{best_resolution[1]}"
        )
    else:
        logger.info(
            f"Selected resolution {best_resolution[0]}x{best_resolution[1]} "
            f"for screen {screen_width}x{screen_height} "
            f"({'portrait' if is_portrait else 'landscape'})"
        )

    return best_resolution


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

    async def allocate_display(self, width: int = 1280, height: int = 720) -> Optional[int]:
        """Allocate an available Xvfb display with specified resolution"""
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
                    # Start Xvfb with specified resolution
                    process = subprocess.Popen(
                        [
                            "Xvfb",
                            f":{display_num}",
                            "-screen", "0", f"{width}x{height}x24",
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

                    logger.info(f"Created new Xvfb display :{display_num} with resolution {width}x{height}")
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

    def __init__(self, display_num: int, session_id: str, width: int = 1280, height: int = 720):
        self.display_num = display_num
        self.session_id = session_id
        self.width = width
        self.height = height
        self.player: Optional[MediaPlayer] = None

    async def start(self):
        """Start FFmpeg capture"""
        try:
            # FFmpeg command to capture X11 display
            # TODO: Add PipeWire/PulseAudio audio capture
            options = {
                "framerate": "30",
                "video_size": f"{self.width}x{self.height}",
                "thread_queue_size": "512",
            }

            # Video source: X11grab
            video_source = f":{self.display_num}.0+0,0"

            self.player = MediaPlayer(
                video_source,
                format="x11grab",
                options=options,
            )

            logger.info(
                f"Started FFmpeg capture for session {self.session_id} "
                f"on display :{self.display_num} ({self.width}x{self.height})"
            )

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
        width: int = 1280,
        height: int = 720,
    ):
        self.session_id = session_id
        self.rom_path = rom_path
        self.core = core
        self.save_path = save_path
        self.state_path = state_path
        self.display_num = display_num
        self.width = width
        self.height = height

        self.retroarch_process: Optional[subprocess.Popen] = None
        self.media_source: Optional[RetroArchMediaSource] = None
        self.peer_connection: Optional[RTCPeerConnection] = None
        self.last_activity = datetime.now()

        # Calculate touchscreen region if core supports it
        self.touchscreen_region = self._calculate_touchscreen_region()

    async def start_retroarch(self):
        """Launch RetroArch process"""
        try:
            # Set environment for Xvfb display
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"

            # Create persistent directory for RetroArch config
            config_dir = Path("/tmp/retroarch_config")
            config_dir.mkdir(exist_ok=True)

            # Path for core options file (persistent across sessions)
            core_options_path = config_dir / "retroarch-core-options.cfg"

            # Create temporary config file
            config_path = f"/tmp/retroarch_{self.session_id}.cfg"
            with open(config_path, "w") as f:
                # Include base config and enable network commands
                f.write('#include "/etc/retroarch.cfg"\n')
                f.write("input_auto_mouse_grab = \"false\"\n")
                f.write("input_overlay_show_mouse_cursor = \"false\"\n")

                # Force core options to be saved to persistent location
                f.write(f'core_options_path = "{core_options_path}"\n')
                f.write("game_specific_options = \"false\"\n")  # Use global core options file
                f.write("auto_overrides_enable = \"false\"\n")  # Disable per-game overrides
                f.write("auto_remaps_enable = \"false\"\n")  # Disable per-game remaps

            # Build RetroArch command
            cmd = [
                "retroarch",
                "-v",  # Verbose
                "--config", config_path,
                "-L", f"/usr/lib/libretro/{self.core}_libretro.so",
                "--fullscreen",
                self.rom_path,
            ]

            # Load save state if provided
            if self.state_path:
                cmd.extend(["-e", "1", "-s", self.state_path])

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
                _, stderr = self.retroarch_process.communicate()
                logger.error(f"RetroArch failed to start: {stderr.decode()}")
                # Cleanup temp config
                try:
                    os.remove(config_path)
                except:
                    pass
                return False

            logger.info(f"Started RetroArch for session {self.session_id} (PID: {self.retroarch_process.pid})")

            # Wait for core options file to be created by RetroArch
            # RetroArch generates this file when loading a core
            core_options_path = Path("/tmp/retroarch_config/retroarch-core-options.cfg")
            timeout = 10  # seconds
            elapsed = 0
            check_interval = 0.1  # Check every 100ms

            while not core_options_path.exists() and elapsed < timeout:
                await asyncio.sleep(check_interval)
                elapsed += check_interval

            if core_options_path.exists():
                logger.info(f"Core options file created after {elapsed:.1f}s")
            else:
                logger.warning(f"Core options file not created after {timeout}s timeout")

            return True

        except Exception as e:
            logger.error(f"Failed to start RetroArch: {e}")
            return False

    async def start_streaming(self):
        """Start FFmpeg capture and prepare for WebRTC"""
        try:
            self.media_source = RetroArchMediaSource(
                self.display_num,
                self.session_id,
                self.width,
                self.height
            )
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

    async def _send_retroarch_command(self, command: str, read_response: bool = False) -> Optional[str]:
        """Send command to RetroArch via network command interface"""
        try:
            # Send TCP command to RetroArch on localhost:55355
            reader, writer = await asyncio.open_connection('127.0.0.1', 55355)
            writer.write(f"{command}\n".encode())
            await writer.drain()

            response = None
            if read_response:
                # Read response with timeout
                try:
                    response = await asyncio.wait_for(reader.read(8192), timeout=1.0)
                    response = response.decode('utf-8').strip()
                except asyncio.TimeoutError:
                    logger.warning(f"Timeout reading response for command '{command}'")

            writer.close()
            await writer.wait_closed()
            return response
        except Exception as e:
            logger.error(f"Failed to send RetroArch command '{command}': {e}")
            return None

    async def get_core_options(self) -> dict:
        """Retrieve core options from RetroArch core options file"""
        try:
            # Try multiple possible locations for retroarch-core-options.cfg
            # Priority: our persistent file first, then standard locations
            possible_paths = [
                Path("/tmp/retroarch_config/retroarch-core-options.cfg"),  # Our persistent location
                Path.home() / ".config" / "retroarch" / "retroarch-core-options.cfg",
                Path("/root/.config/retroarch/retroarch-core-options.cfg"),
                Path("/storage/.config/retroarch/retroarch-core-options.cfg"),  # Batocera
                Path("/userdata/system/configs/retroarch/cores/retroarch-core-options.cfg"),  # Batocera alt
            ]

            config_path = None
            for path in possible_paths:
                if path.exists():
                    config_path = path
                    logger.info(f"Found core options file at: {config_path}")
                    break

            if not config_path:
                logger.warning(f"Core options file not found. Tried: {[str(p) for p in possible_paths]}")
                return {}

            core_options = {}

            # Extract core name without "ra-" prefix for matching
            core_name = self.core.lower().replace('ra-', '') if self.core else ""
            logger.info(f"Looking for core options prefixed with: {core_name}")

            # Read the core options file
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()

                    # Skip comments and empty lines
                    if not line or line.startswith('#'):
                        continue

                    # Format: option_name = "value"
                    if '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip()

                        # Remove quotes from value
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]

                        # Only include options for the current core
                        # Core options are prefixed with core name (e.g., "melonds_console_mode")
                        if core_name and key.lower().startswith(core_name + '_'):
                            core_options[key] = value
                            logger.debug(f"Found core option: {key} = {value}")

            logger.info(f"Retrieved {len(core_options)} core options for core {self.core} from {config_path}")
            return core_options

        except Exception as e:
            logger.error(f"Failed to get core options: {e}", exc_info=True)
            return {}

    async def send_input(self, event_data: dict):
        """Send input to RetroArch via network command API or xdotool"""
        try:
            event_type = event_data.get("type", "")

            # Set DISPLAY environment for xdotool
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"

            # Use xdotool for keyboard, network API for mouse
            if event_type == "keydown":
                key = event_data.get("key", "")
                # Map JavaScript key names to X11 key names
                x11_key = self._map_key_to_x11(key)
                if x11_key:
                    proc = await asyncio.create_subprocess_exec(
                        "xdotool", "keydown", x11_key,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()

            elif event_type == "keyup":
                key = event_data.get("key", "")
                x11_key = self._map_key_to_x11(key)
                if x11_key:
                    proc = await asyncio.create_subprocess_exec(
                        "xdotool", "keyup", x11_key,
                        env=env,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await proc.wait()

            elif event_type == "mousemove":
                try:
                    # Frontend sends normalized coordinates (0-1) relative to touchscreen zone
                    x = event_data.get("x", 0)
                    y = event_data.get("y", 0)

                    # If touchscreen region defined, map to that region
                    if self.touchscreen_region:
                        x_offset, y_offset, width_ratio, height_ratio = self.touchscreen_region[:4]

                        # Map normalized coords to touchscreen region in Xvfb
                        xvfb_x = int((x_offset + x * width_ratio) * self.width)
                        xvfb_y = int((y_offset + y * height_ratio) * self.height)
                    else:
                        # Full screen mapping
                        xvfb_x = int(x * self.width)
                        xvfb_y = int(y * self.height)

                    # Clamp to bounds
                    xvfb_x = max(0, min(self.width - 1, xvfb_x))
                    xvfb_y = max(0, min(self.height - 1, xvfb_y))

                    # Move mouse with xdotool (fire and forget - no wait)
                    asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            "xdotool", "mousemove", str(xvfb_x), str(xvfb_y),
                            env=env,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                    )
                except Exception as e:
                    # Silent fail - don't log to avoid spam
                    pass

            elif event_type == "mousedown":
                button = event_data.get("button", 0)
                # Mouse buttons: 1=left, 2=middle, 3=right
                xdotool_button = button + 1
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "mousedown", str(xdotool_button),
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()

            elif event_type == "mouseup":
                button = event_data.get("button", 0)
                xdotool_button = button + 1
                proc = await asyncio.create_subprocess_exec(
                    "xdotool", "mouseup", str(xdotool_button),
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            else:
                logger.warning(f"Unknown input event type: {event_type}")
                return

            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    def _calculate_touchscreen_region(self) -> Optional[tuple[float, float, float, float]]:
        """Calculate touchscreen region for cores with dual screens (DS, 3DS)

        Returns:
            Tuple of (x_offset_ratio, y_offset_ratio, width_ratio, height_ratio) or None
        """
        if self.core not in TOUCHSCREEN_REGIONS:
            return None

        # DS/3DS always display vertically (stacked screens)
        orientation = "vertical"

        region = TOUCHSCREEN_REGIONS[self.core].get(orientation)
        if region:
            logger.info(
                f"Touchscreen region for {self.core} ({orientation}): "
                f"x={region[0]:.1%}, y={region[1]:.1%}, w={region[2]:.1%}, h={region[3]:.1%}"
            )
        return region

    def _map_key_to_x11(self, js_key: str) -> str:
        """Map JavaScript key names to X11 key names for xdotool"""
        # Common mappings
        key_map = {
            "ArrowUp": "Up",
            "ArrowDown": "Down",
            "ArrowLeft": "Left",
            "ArrowRight": "Right",
            " ": "space",
            "Enter": "Return",
            "Escape": "Escape",
            "Backspace": "BackSpace",
            "Tab": "Tab",
            "Shift": "Shift_L",
            "Control": "Control_L",
            "Alt": "Alt_L",
            "Meta": "Super_L",
        }

        # Return mapped key or original if single character
        return key_map.get(js_key, js_key.lower())

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

        # Cleanup temporary config file
        config_path = f"/tmp/retroarch_{self.session_id}.cfg"
        try:
            if os.path.exists(config_path):
                os.remove(config_path)
        except Exception as e:
            logger.warning(f"Failed to remove temp config {config_path}: {e}")


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
            # Subscribe to session start events (polling-based)
            asyncio.create_task(self._handle_session_events())

            # Subscribe to Redis pubsub for real-time events
            asyncio.create_task(self._handle_pubsub_events())
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
                        and session.state == SessionState.STARTING
                    ):
                        # Start new session
                        await self._start_session(session)

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error handling session events: {e}")
                await asyncio.sleep(5)

    async def _handle_pubsub_events(self):
        """Handle Redis pubsub events for WebRTC signaling, stop, input, and core options"""
        # This loop handles WebRTC answers, stop signals, and core options requests
        # Input events are handled via dedicated pubsub listeners per session
        while self.running:
            try:
                # Check for WebRTC answers, stop signals, and core options requests
                for session_id, instance in list(self.instances.items()):
                    # Check for WebRTC answer
                    answer_key = f"retroarch:webrtc_answer:{session_id}"
                    answer_sdp = await async_cache.get(answer_key)
                    if answer_sdp and instance.peer_connection:
                        await instance.set_webrtc_answer(answer_sdp)
                        await async_cache.delete(answer_key)
                        logger.info(f"Processed WebRTC answer for session {session_id}")

                    # Check for stop signal
                    stop_key = f"retroarch:stop:{session_id}"
                    stop_signal = await async_cache.get(stop_key)
                    if stop_signal:
                        logger.info(f"Received stop signal for session {session_id}")
                        await self._stop_session(session_id)
                        await async_cache.delete(stop_key)

                    # Check for core options request
                    request_key = f"retroarch:get_core_options:{session_id}"
                    request = await async_cache.get(request_key)
                    if request:
                        logger.info(f"Received core options request for session {session_id}")
                        # Get core options from RetroArch
                        core_options = await instance.get_core_options()
                        # Store response in Redis
                        response_key = f"retroarch:core_options:{session_id}"
                        await async_cache.set(response_key, json.dumps(core_options), ex=10)
                        # Delete request
                        await async_cache.delete(request_key)
                        logger.info(f"Sent {len(core_options)} core options for session {session_id}")

                await asyncio.sleep(0.5)  # Slower polling for non-critical events

            except Exception as e:
                logger.error(f"Error handling pubsub events: {e}")
                await asyncio.sleep(1)

    async def _listen_for_inputs(self, session_id: str, instance):
        """Real-time pubsub listener for input events - NO POLLING DELAY"""
        # Create a dedicated async pubsub connection
        pubsub = async_cache.pubsub()
        channel = f"retroarch:input:{session_id}"

        try:
            await pubsub.subscribe(channel)
            logger.info(f"Listening for inputs on {channel}")

            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        event = json.loads(message["data"])
                        await instance.send_input(event)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.error(f"Invalid input event: {e}")

                # Check if session is still active
                if session_id not in self.instances:
                    logger.info(f"Session {session_id} ended, stopping input listener")
                    break

        except Exception as e:
            logger.error(f"Error in input listener for {session_id}: {e}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def _start_session(self, session: RetroArchSession):
        """Start a new RetroArch streaming session"""
        try:
            logger.info(f"Starting session {session.session_id}")

            # Get ROM from database
            rom = db_rom_handler.get_rom(session.rom_id)
            if not rom:
                logger.error(f"ROM {session.rom_id} not found for session {session.session_id}")
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Build ROM path
            from config import LIBRARY_BASE_PATH
            rom_path = os.path.join(str(LIBRARY_BASE_PATH), rom.full_path)

            logger.info(f"Using ROM path: {rom_path}")

            # Get screen dimensions from Redis
            dims_key = f"retroarch:screen_dims:{session.session_id}"
            dims_data = await async_cache.get(dims_key)
            screen_width = 1920
            screen_height = 1080

            if dims_data:
                try:
                    dims = json.loads(dims_data)
                    screen_width = dims.get("width", 1920)
                    screen_height = dims.get("height", 1080)
                    logger.info(f"Retrieved screen dimensions: {screen_width}x{screen_height}")
                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Failed to parse screen dimensions: {e}, using default")

            # Get max resolution from environment variable
            max_resolution = os.getenv("RETROARCH_MAX_RESOLUTION")

            # Calculate optimal resolution
            xvfb_width, xvfb_height = calculate_optimal_resolution(
                screen_width,
                screen_height,
                max_resolution
            )

            # Allocate Xvfb display with calculated resolution
            display_num = await self.xvfb_manager.allocate_display(xvfb_width, xvfb_height)
            if display_num is None:
                logger.error(f"Failed to allocate display for session {session.session_id}")
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Create instance
            instance = RetroArchInstance(
                session_id=session.session_id,
                rom_path=rom_path,
                core=session.core,
                save_path=None,  # TODO: Build save path from session.save_id
                state_path=None,  # TODO: Build state path from session.state_id
                display_num=display_num,
                width=xvfb_width,
                height=xvfb_height,
            )

            # Start RetroArch
            if not await instance.start_retroarch():
                logger.error(f"Failed to start RetroArch for session {session.session_id}")
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Start streaming
            if not await instance.start_streaming():
                logger.error(f"Failed to start streaming for session {session.session_id}")
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Create WebRTC offer
            offer_sdp = await instance.create_webrtc_offer()
            if not offer_sdp:
                logger.error(f"Failed to create WebRTC offer for session {session.session_id}")
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Store instance
            self.instances[session.session_id] = instance

            # Start real-time input listener (pubsub - no polling delay)
            asyncio.create_task(self._listen_for_inputs(session.session_id, instance))

            # Store touchscreen region config in Redis for frontend
            if instance.touchscreen_region:
                region_key = f"retroarch:touchscreen_region:{session.session_id}"
                region_data = {
                    "x_offset": instance.touchscreen_region[0],
                    "y_offset": instance.touchscreen_region[1],
                    "width": instance.touchscreen_region[2],
                    "height": instance.touchscreen_region[3],
                }
                await async_cache.set(region_key, json.dumps(region_data), ex=300)

            # Load and store core options automatically for frontend
            core_options = await instance.get_core_options()
            if core_options:
                options_key = f"retroarch:core_options:{session.session_id}"
                await async_cache.set(options_key, json.dumps(core_options), ex=300)
                logger.info(f"Stored {len(core_options)} core options for session {session.session_id}")

            # Update session in Redis with WebRTC offer and running state
            session.webrtc_offer = offer_sdp
            session.state = SessionState.RUNNING
            session.pid = instance.retroarch_process.pid if instance.retroarch_process else None
            session.xvfb_display = display_num
            await retroarch_handler.set_session(session)

            logger.info(f"Session {session.session_id} started successfully")

        except Exception as e:
            logger.error(f"Failed to start session {session.session_id}: {e}")
            await retroarch_handler.update_session_state(
                session.session_id, SessionState.ERROR
            )

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
            await retroarch_handler.update_session_state(
                session_id, SessionState.STOPPED
            )

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