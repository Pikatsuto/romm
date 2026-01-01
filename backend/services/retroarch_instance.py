"""
RetroArch Instance Manager

Manages individual RetroArch emulator instances with WebRTC streaming,
input forwarding, save states, and core option configuration.
"""

import asyncio
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from services.gstreamer_webrtc import GStreamerWebRTC

logger = logging.getLogger(__name__)


# Touchscreen region configuration per core
TOUCHSCREEN_REGIONS = {
    "desmume": {
        "vertical": (0.3125, 0.5, 0.375, 0.5, 256, 192, 192),
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 256, 192, 192),
        "native_total_height": 384,
    },
    "melonds": {
        "vertical": (0.3125, 0.5, 0.375, 0.5, 256, 192, 192),
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 256, 192, 192),
        "native_total_height": 384,
    },
    "citra": {
        "vertical": (0.3125, 0.5, 0.375, 0.5, 320, 240, 240),
        "horizontal": (0.5, 0.3125, 0.5, 0.375, 320, 240, 240),
        "native_total_height": 480,
    },
    "cemu": {
        "vertical": (0.0, 0.0, 1.0, 1.0, 854, 480, 0),
        "horizontal": (0.0, 0.0, 1.0, 1.0, 854, 480, 0),
        "native_total_height": 480,
    },
}

# Native aspect ratios for cores (width, height)
CORE_ASPECT_RATIOS = {
    "desmume": (256, 384),  # DS: two 256x192 screens stacked
    "melonds": (256, 384),
    "mgba": (240, 160),     # GBA
    "gambatte": (160, 144),  # GB/GBC
    "snes9x": (256, 224),   # SNES
    "fceumm": (256, 240),   # NES
    "genesis_plus_gx": (320, 224),  # Genesis/Mega Drive
    "nestopia": (256, 240),  # NES
    "mesen": (256, 240),    # NES
}


class RetroArchInstance:
    """Manages a single RetroArch instance with WebRTC streaming.

    Handles the complete lifecycle of a RetroArch emulator instance,
    including process management, GStreamer WebRTC streaming, input
    forwarding, save states, and core option configuration.

    Attributes:
        session_id: Unique identifier for this streaming session.
        rom_path: Absolute path to the ROM file being emulated.
        core: Name of the libretro core (without _libretro.so suffix).
        save_path: Optional path to the save file for persistent saves.
        state_path: Optional path to a save state to load on startup.
        display_num: X11 display number for the Xvfb virtual display.
        width: Display width in pixels.
        height: Display height in pixels.
        retroarch_process: Handle to the RetroArch subprocess.
        gstreamer: GStreamer WebRTC streaming instance.
        last_activity: Timestamp of last user activity for timeout detection.
        touchscreen_region: Calculated touchscreen region for DS/3DS cores.
        session_dir: Per-session temporary directory for saves/states.
    """

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
        """Initialize a RetroArch instance.

        Args:
            session_id: Unique session identifier.
            rom_path: Absolute path to the ROM file to run.
            core: Libretro core name (without _libretro.so suffix).
            save_path: Optional path to save file for the ROM.
            state_path: Optional path to save state to load on startup.
            display_num: X11 display number for Xvfb.
            width: Display width in pixels.
            height: Display height in pixels.
        """
        self.session_id = session_id
        self.rom_path = rom_path
        self.core = core
        self.save_path = save_path
        self.state_path = state_path
        self.display_num = display_num
        self.width = width
        self.height = height

        self.retroarch_process: Optional[subprocess.Popen] = None
        self.gstreamer: Optional[GStreamerWebRTC] = None
        self.last_activity = datetime.now()

        self.touchscreen_region = self._calculate_touchscreen_region()

        # Per-session directories
        self.session_dir = Path(f"/tmp/retroarch/{session_id}")
        self.saves_dir = self.session_dir / "saves"
        self.states_dir = self.session_dir / "states"
        self.screenshots_dir = self.session_dir / "screenshots"
        self.config_dir = self.session_dir / "config"

    def _setup_session_directories(self):
        """Create per-session users directories."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self.states_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created session directories at {self.session_dir}")

    def cleanup_session_dir(self):
        """Remove the per-session tempor ary directory and all its contents."""
        try:
            if self.session_dir.exists():
                shutil.rmtree(self.session_dir)
                msg = f"Cleaned up session directory: {self.session_dir}"
                logger.info(msg)
        except Exception as e:
            logger.error(f"Failed to cleanup session directory: {e}")

    async def start(
        self
    ):
        """Start RetroArch and GStreamer streaming.

        Initializes PulseAudio, creates RetroArch configuration,
        launches the RetroArch process, and starts GStreamer streaming.

        Returns:
            bool: True if RetroArch started successfully, False otherwise.
        """
        try:
            # Note: session directories are created by daemon before start()
            # to allow restoring saves/states before RetroArch starts

            # Create GStreamer source and setup PulseAudio
            self.gstreamer = GStreamerWebRTC(
                session_id=self.session_id,
                display_num=self.display_num,
                width=self.width,
                height=self.height,
            )
            self.gstreamer.setup_pulseaudio()

            # Setup environment for RetroArch
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"
            env.update(self.gstreamer.get_pulseaudio_env())

            # Create core options in session config directory
            core_options_path = self.config_dir / "retroarch-core-options.cfg"

            # Copy pre-generated core options if available
            core_cfg = f"{self.core.lower()}-core-options.cfg"
            pre_generated = Path("/app/romm/config/retroarch") / core_cfg
            if pre_generated.exists():
                shutil.copy2(pre_generated, core_options_path)

            config_path = f"/tmp/retroarch_{self.session_id}.cfg"
            with open(config_path, "w") as f:
                f.write('#include "/etc/retroarch.cfg"\n')
                f.write('input_auto_mouse_grab = "false"\n')
                f.write('input_overlay_show_mouse_cursor = "false"\n')

                # Network commands for remote control
                f.write('network_cmd_enable = "true"\n')
                f.write('network_cmd_port = "55355"\n')
                f.write('stdin_cmd_enable = "true"\n')

                # Per-session directories for saves, states, screenshots
                f.write(f'savefile_directory = "{self.saves_dir}"\n')
                f.write(f'savestate_directory = "{self.states_dir}"\n')
                f.write(f'screenshot_directory = "{self.screenshots_dir}"\n')
                f.write('notification_show_screenshot = "true"\n')
                f.write('input_screenshot = "f8"\n')

                # State slot, auto-save and auto-load
                f.write('state_slot = "0"\n')
                f.write('savestate_auto_save = "true"\n')
                if self.state_path:
                    f.write('savestate_auto_load = "true"\n')
                else:
                    f.write('savestate_auto_load = "false"\n')

                # Video settings
                f.write('video_driver = "gl"\n')
                f.write('video_threaded = "true"\n')
                f.write('video_vsync = "false"\n')
                f.write('video_black_frame_insertion = "0"\n')
                f.write('video_shader_enable = "false"\n')
                f.write('video_smooth = "false"\n')
                f.write('video_max_swapchain_images = "2"\n')
                f.write('video_font_enable = "true"\n')

                # Audio settings - direct PulseAudio (PULSE_SINK sets sink)
                f.write('audio_driver = "pulse"\n')
                f.write('audio_enable = "true"\n')
                f.write('audio_out_rate = "48000"\n')
                f.write('audio_sync = "true"\n')
                f.write('audio_rate_control = "true"\n')
                f.write('audio_latency = "32"\n')

                # Core options
                f.write(f'core_options_path = "{core_options_path}"\n')
                f.write('game_specific_options = "false"\n')
                f.write('auto_overrides_enable = "false"\n')
                f.write('auto_remaps_enable = "false"\n')

            # Start RetroArch
            cmd = [
                "retroarch",
                "-v",
                "--config",
                config_path,
                "-L",
                f"/usr/lib/libretro/{self.core}_libretro.so",
                "--fullscreen",
                self.rom_path,
            ]

            self.retroarch_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            await asyncio.sleep(0.5)

            if self.retroarch_process.poll() is not None:
                _, stderr = self.retroarch_process.communicate()
                logger.error(f"RetroArch failed to start: {stderr.decode()}")
                return False

            pid = self.retroarch_process.pid
            logger.info(f"Started RetroArch (PID: {pid})")

            # Start GStreamer streaming
            self.gstreamer.start()

            return True

        except Exception as e:
            logger.error(f"Failed to start RetroArch: {e}")
            return False

    def get_offer_sdp(
        self,
        timeout: float = 10.0
    ) -> Optional[str]:
        """Get WebRTC offer SDP from the GStreamer pipeline.

        Args:
            timeout: Maximum seconds to wait for the offer (default: 10.0).

        Returns:
            The WebRTC offer SDP string, or None if not available.
        """
        if self.gstreamer:
            return self.gstreamer.get_offer_sdp(timeout)
        return None

    def set_answer_sdp(
        self,
        answer_sdp: str
    ) -> bool:
        """Set WebRTC answer SDP from the browser.

        Args:
            answer_sdp: The SDP answer string from the browser.

        Returns:
            True if the answer was set successfully, False otherwise.
        """
        if self.gstreamer:
            return self.gstreamer.set_answer_sdp(answer_sdp)
        return False

    async def _send_retroarch_command(
        self,
        command: str
    ) -> Optional[str]:
        """Send command to RetroArch via UDP.

        Args:
            command: RetroArch network command (e.g., SAVE_STATE, QUIT).

        Returns:
            None on success, None on failure (errors are logged).
        """
        try:
            loop = asyncio.get_event_loop()
            addr = ("127.0.0.1", 55355)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: asyncio.DatagramProtocol(), remote_addr=addr
            )
            transport.sendto(f"{command}\n".encode())
            transport.close()
            return None
        except Exception as e:
            logger.error(f"Failed to send RetroArch command '{command}': {e}")
            return None

    def _find_core_options_file(
        self
    ) -> Optional[Path]:
        """Find the core options config file.

        Returns:
            Path to the core options file if found, None otherwise.
        """
        # First check session-specific config
        session_cfg = self.config_dir / "retroarch-core-options.cfg"
        if session_cfg.exists():
            return session_cfg

        home_cfg = Path.home() / ".config/retroarch/retroarch-core-options.cfg"
        paths = [
            Path("/tmp/retroarch_config/retroarch-core-options.cfg"),
            home_cfg,
        ]
        for path in paths:
            if path.exists():
                return path
        return None

    def _parse_core_option_line(
        self,
        line: str,
        core_name: str
    ) -> Optional[tuple]:
        """Parse a core option line from config file.

        Parses a line from the core options config file and extracts
        the key-value pair if it matches the specified core prefix.

        Args:
            line: Raw line from the config file.
            core_name: Core name prefix to filter options (e.g., "desmume").

        Returns:
            Tuple of (key, value) if line is a valid option for the core,
            None if line is empty, a comment, or doesn't match the core.
        """
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            return None
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if core_name and key.lower().startswith(core_name + "_"):
            return (key, value)
        return None

    async def get_core_options(
        self
    ) -> dict:
        """Retrieve core options from config file.

        Reads the RetroArch core options config file and returns
        all options that match the current core's prefix.

        Returns:
            dict: Mapping of option names to their current values.
                Empty dict if no config file found or on error.
        """
        try:
            config_path = self._find_core_options_file()
            if not config_path:
                return {}

            cn = self.core.lower().replace("ra-", "") if self.core else ""
            core_name = cn
            core_options = {}

            with open(config_path, "r", encoding="utf-8") as f:
                for line in f:
                    parsed = self._parse_core_option_line(line, core_name)
                    if parsed:
                        core_options[parsed[0]] = parsed[1]

            return core_options
        except Exception as e:
            logger.error(f"Failed to get core options: {e}")
            return {}

    def _get_xdotool_env(
        self
    ) -> dict:
        """Get environment for xdotool commands.

        Returns:
            dict: Environment dictionary with DISPLAY set to this
                instance's Xvfb display number.
        """
        env = os.environ.copy()
        env["DISPLAY"] = f":{self.display_num}"
        return env

    async def _xdotool(
        self,
        *args
    ):
        """Run xdotool command and wait for completion.

        Args:
            *args: Command arguments to pass to xdotool
                (e.g., "keydown", "a" or "mousedown", "1").
        """
        env = self._get_xdotool_env()
        proc = await asyncio.create_subprocess_exec(
            "xdotool",
            *args,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    def _xdotool_fire_and_forget(
        self,
        *args
    ):
        """Run xdotool without waiting for completion.

        Used for high-frequency events like mouse movement where
        waiting for each command would introduce latency.

        Args:
            *args: Command arguments to pass to xdotool.
        """
        env = self._get_xdotool_env()
        asyncio.create_task(
            asyncio.create_subprocess_exec(
                "xdotool",
                *args,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )

    def _calculate_mouse_position(
        self,
        x: float,
        y: float
    ) -> tuple[int, int]:
        """Calculate xvfb mouse position from normalized coordinates.

        Converts normalized (0-1) coordinates from the browser to
        absolute Xvfb pixel coordinates, accounting for touchscreen
        regions on DS/3DS cores.

        Args:
            x: Normalized X coordinate (0.0 to 1.0).
            y: Normalized Y coordinate (0.0 to 1.0).

        Returns:
            Tuple of (xvfb_x, xvfb_y) pixel coordinates clamped to
            display bounds.
        """
        if self.touchscreen_region:
            x_off, y_off, w_ratio, h_ratio = self.touchscreen_region[:4]
            xvfb_x = int((x_off + x * w_ratio) * self.width)
            xvfb_y = int((y_off + y * h_ratio) * self.height)
        else:
            xvfb_x = int(x * self.width)
            xvfb_y = int(y * self.height)
        xvfb_x = max(0, min(self.width - 1, xvfb_x))
        xvfb_y = max(0, min(self.height - 1, xvfb_y))
        return xvfb_x, xvfb_y

    async def send_input(
        self,
        event_data: dict
    ):
        """Send input to RetroArch via xdotool.

        Processes input events from the browser and forwards them
        to the Xvfb display using xdotool commands.

        Args:
            event_data: Input event dict containing 'type' and event-specific
                keys ('key' for keyboard, 'x'/'y'/'button' for mouse).
        """
        try:
            event_type = event_data.get("type", "")

            if event_type == "keydown":
                x11_key = self._map_key_to_x11(event_data.get("key", ""))
                if x11_key:
                    await self._xdotool("keydown", x11_key)

            elif event_type == "keyup":
                x11_key = self._map_key_to_x11(event_data.get("key", ""))
                if x11_key:
                    await self._xdotool("keyup", x11_key)

            elif event_type == "mousemove":
                x, y = event_data.get("x", 0), event_data.get("y", 0)
                xvfb_x, xvfb_y = self._calculate_mouse_position(x, y)
                args = ("mousemove", str(xvfb_x), str(xvfb_y))
                self._xdotool_fire_and_forget(*args)

            elif event_type == "mousedown":
                button = str(event_data.get("button", 0) + 1)
                await self._xdotool("mousedown", button)

            elif event_type == "mouseup":
                button = str(event_data.get("button", 0) + 1)
                await self._xdotool("mouseup", button)

            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    async def execute_command(
        self,
        command: str
    ) -> Optional[bytes]:
        """Execute a RetroArch command.

        Sends a control command to RetroArch via UDP network commands.
        Special handling for RESET (restarts process) and SAVE_AND_QUIT
        (saves then quits).

        Args:
            command: Command name (SAVESTATE, LOADSTATE, SCREENSHOT,
                PAUSE_TOGGLE, RESET, or SAVE_AND_QUIT).

        Returns:
            For SCREENSHOT command, returns the screenshot bytes if successful.
            For other commands, returns None.
        """
        try:
            if command == "RESET":
                await self.restart()
                return None

            command_map = {
                "SAVESTATE": "SAVE_STATE",
                "LOADSTATE": "LOAD_STATE",
                "SCREENSHOT": "SCREENSHOT",
                "PAUSE_TOGGLE": "PAUSE_TOGGLE",
                "SAVE_AND_QUIT": "SAVE_STATE",
            }

            retroarch_cmd = command_map.get(command)
            if not retroarch_cmd:
                logger.warning(f"Unknown command: {command}")
                return None

            # For SCREENSHOT, capture the file created by RetroArch
            if command == "SCREENSHOT":
                screenshot_data = await self._take_screenshot()
                self.last_activity = datetime.now()
                return screenshot_data

            await self._send_retroarch_command(retroarch_cmd)

            # For SAVESTATE or SAVE_AND_QUIT, capture screenshot
            # State file stays as .state0 for LOADSTATE to work
            # Timestamping happens during sync to RomM
            if command in ("SAVESTATE", "SAVE_AND_QUIT"):
                await asyncio.sleep(1.0)  # Wait for state file to be written
                await self._capture_manual_save_screenshot()

            if command == "SAVE_AND_QUIT":
                await asyncio.sleep(0.2)
                await self._send_retroarch_command("QUIT")

            self.last_activity = datetime.now()
            return None

        except Exception as e:
            logger.error(f"Failed to execute command {command}: {e}")
            return None

    def _calculate_game_crop(self) -> tuple[int, int, int, int]:
        """Calculate crop region to remove black bars around the game.

        RetroArch scales the game to fit the screen while maintaining
        aspect ratio, centering it with black bars on sides or top/bottom.

        Returns:
            Tuple of (width, height, x_offset, y_offset) for the game area.
        """
        # Get native aspect ratio for this core
        native = CORE_ASPECT_RATIOS.get(self.core, (4, 3))
        native_ratio = native[0] / native[1]
        screen_ratio = self.width / self.height

        if native_ratio > screen_ratio:
            # Game is wider than screen - black bars on top/bottom
            game_width = self.width
            game_height = int(self.width / native_ratio)
        else:
            # Game is taller than screen - black bars on sides
            game_height = self.height
            game_width = int(self.height * native_ratio)

        # Center position
        x_offset = (self.width - game_width) // 2
        y_offset = (self.height - game_height) // 2

        return game_width, game_height, x_offset, y_offset

    def _get_latest_state_file(self) -> Optional[Path]:
        """Find the recently modified state in the session states directory.

        Returns:
            Path to the latest state file, or None if no states exist.
        """
        try:
            state_files = list(self.states_dir.glob("*.state*"))
            if not state_files:
                return None
            # Sort by modification time, newest first
            state_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            return state_files[0]
        except Exception as e:
            logger.error(f"Failed to find latest state file: {e}")
            return None

    async def _rename_state_with_timestamp(self):
        """Rename state file with timestamp and capture associated screenshot.

        After a manual SAVESTATE, rename the state file to include a timestamp
        (e.g., ROMName_20260101_044850.state) to preserve multiple saves.
        Also captures a screenshot with matching name.
        Does NOT rename .state.auto files (auto-saves stay unique).
        """
        try:
            # Find the most recent state file
            state_file = self._get_latest_state_file()
            if not state_file:
                logger.warning("No state file found to rename")
                return

            # Don't rename auto-save files
            if state_file.name.endswith(".state.auto"):
                logger.debug("Skipping rename for auto-save file")
                return

            rom_name = Path(self.rom_path).stem
            original_ext = state_file.suffix  # e.g., ".state", ".state0", etc.

            # Generate timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            new_state_name = f"{rom_name}_{timestamp}{original_ext}"
            new_state_path = self.states_dir / new_state_name

            # Rename state file
            state_file.rename(new_state_path)
            logger.info(f"Renamed state to: {new_state_name}")

            # Capture screenshot with matching name
            screenshot_name = f"{rom_name}_{timestamp}.png"
            screenshot_path = self.screenshots_dir / screenshot_name

            env = self._get_xdotool_env()
            game_w, game_h, x_off, y_off = self._calculate_game_crop()
            crop_geometry = f"{game_w}x{game_h}+{x_off}+{y_off}"

            proc = await asyncio.create_subprocess_exec(
                "import",
                "-window", "root",
                "-crop", crop_geometry,
                "+repage",
                str(screenshot_path),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"State screenshot failed: {stderr.decode()}")
                return

            logger.info(f"State screenshot saved: {screenshot_name}")

        except Exception as e:
            logger.error(f"Failed to rename state with timestamp: {e}")

    async def _capture_manual_save_screenshot(self):
        """Capture screenshot and copy state file with matching timestamp.

        Finds the most recently modified state file and copies it with timestamp.
        Both screenshot and state copy get the same timestamp so they match in RomM.
        """
        try:
            rom_name = Path(self.rom_path).stem
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            # Find the most recently modified state file (any extension)
            state_file = self._get_latest_state_file()

            if state_file:
                # Get original extension (e.g., .state0, .state.auto, .state1)
                # For .state.auto, suffix is just ".auto", so handle specially
                if state_file.name.endswith(".state.auto"):
                    ext = ".state.auto"
                else:
                    ext = state_file.suffix  # .state0, .state1, etc.

                timestamped_state = self.states_dir / f"{rom_name}_{timestamp}{ext}"
                shutil.copy2(state_file, timestamped_state)
                logger.info(f"Copied state: {timestamped_state.name}")
            else:
                logger.warning("No state file found in states directory")

            # Take screenshot with same timestamp
            screenshot_name = f"{rom_name}_{timestamp}.png"
            screenshot_path = self.screenshots_dir / screenshot_name

            env = self._get_xdotool_env()
            game_w, game_h, x_off, y_off = self._calculate_game_crop()
            crop_geometry = f"{game_w}x{game_h}+{x_off}+{y_off}"

            proc = await asyncio.create_subprocess_exec(
                "import",
                "-window", "root",
                "-crop", crop_geometry,
                "+repage",
                str(screenshot_path),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"Manual save screenshot failed: {stderr.decode()}")
                return

            logger.info(f"Manual save screenshot: {screenshot_name}")

        except Exception as e:
            logger.error(f"Failed to capture manual save screenshot: {e}")

    async def _capture_state_screenshot(self):
        """Capture a screenshot for auto-save.

        Screenshot is named to match the auto-save's file_name_no_ext.
        For .state.auto files, file_name_no_ext is ROMName.state,
        so screenshot is ROMName.state.png.
        """
        try:
            rom_name = Path(self.rom_path).stem
            # Screenshot matches auto-save's file_name_no_ext (ROMName.state)
            screenshot_name = f"{rom_name}.state.png"
            screenshot_path = self.screenshots_dir / screenshot_name

            # Take screenshot
            env = self._get_xdotool_env()
            game_w, game_h, x_off, y_off = self._calculate_game_crop()
            crop_geometry = f"{game_w}x{game_h}+{x_off}+{y_off}"

            proc = await asyncio.create_subprocess_exec(
                "import",
                "-window", "root",
                "-crop", crop_geometry,
                "+repage",
                str(screenshot_path),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"State screenshot failed: {stderr.decode()}")
                return

            logger.info(f"State screenshot saved: {screenshot_path.name}")

        except Exception as e:
            logger.error(f"Failed to capture state screenshot: {e}")

    async def _take_screenshot(self) -> Optional[bytes]:
        """Take a screenshot of the Xvfb display.

        Uses ImageMagick's 'import' command to capture the Xvfb display
        directly, then crops to the game area to remove black bars.

        Returns:
            Screenshot image bytes (PNG format), or None on failure.
        """
        try:
            env = self._get_xdotool_env()
            screenshot_path = f"/tmp/screenshot_{self.session_id}.png"

            # Calculate crop region to remove black bars
            game_w, game_h, x_off, y_off = self._calculate_game_crop()
            crop_geometry = f"{game_w}x{game_h}+{x_off}+{y_off}"

            # Capture and crop in one command
            proc = await asyncio.create_subprocess_exec(
                "import",
                "-window", "root",
                "-crop", crop_geometry,
                "+repage",
                screenshot_path,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error(f"Screenshot failed: {stderr.decode()}")
                return None

            # Read and return the screenshot data
            screenshot_file = Path(screenshot_path)
            if not screenshot_file.exists():
                logger.warning("Screenshot file not created")
                return None

            screenshot_data = screenshot_file.read_bytes()
            screenshot_file.unlink()  # Clean up
            logger.info(f"Screenshot captured: {len(screenshot_data)} bytes")
            return screenshot_data

        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return None

    def _update_config_line(
        self,
        line: str,
        opt_name: str,
        opt_value: str
    ) -> tuple[str, bool]:
        """Update config line if it matches opt_name, else unchanged.

        Args:
            line: Raw line from the config file.
            opt_name: Option name to match against.
            opt_value: New value to set if option matches.

        Returns:
            Tuple of (line, found) where line is the potentially updated
            config line and found is True if the option was matched.
        """
        if "=" not in line:
            return line, False
        key = line.split("=", 1)[0].strip()
        if key == opt_name:
            return f'{opt_name} = "{opt_value}"\n', True
        return line, False

    async def set_core_option(
        self,
        option_name: str,
        option_value: str
    ):
        """Set a core option value in the config file.

        Updates the core options config file with the new value
        and triggers RetroArch to reload options.

        Args:
            option_name: Full option name (e.g., "desmume_screens_layout").
            option_value: New value to set for the option.
        """
        config_file = self._find_core_options_file()
        if not config_file:
            return

        try:
            lines = []
            option_found = False

            with open(config_file, "r", encoding="utf-8") as f:
                for line in f:
                    new_line, found = self._update_config_line(
                        line, option_name, option_value
                    )
                    lines.append(new_line)
                    option_found = option_found or found

            if not option_found:
                lines.append(f'{option_name} = "{option_value}"\n')

            with open(config_file, "w", encoding="utf-8") as f:
                f.writelines(lines)

            await self._send_retroarch_command("CORE_OPTION_RELOAD")
            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to set core option {option_name}: {e}")

    def _calculate_touchscreen_region(
        self
    ) -> Optional[tuple]:
        """Calculate touchscreen region for DS/3DS cores.

        Returns:
            Tuple of (x_offset, y_offset, width_ratio, height_ratio,
            native_width, native_height, offset_pixels) for touchscreen
            cores, or None for non-touchscreen cores.
        """
        if self.core not in TOUCHSCREEN_REGIONS:
            return None
        return TOUCHSCREEN_REGIONS[self.core].get("vertical")

    def _map_key_to_x11(
        self,
        js_key: str
    ) -> str:
        """Map JavaScript key names to X11 key names.

        Args:
            js_key: JavaScript key name from KeyboardEvent.key
                (e.g., "ArrowUp", "Enter", "a").

        Returns:
            X11 key name for use with xdotool (e.g., "Up", "Return", "a").
        """
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
        return key_map.get(js_key, js_key.lower())

    async def restart(
        self
    ) -> bool:
        """Restart RetroArch cleanly.

        Terminates the current RetroArch process and starts a new one
        with the same configuration. GStreamer streaming continues.

        Returns:
            True if restart succeeded, False on error.
        """
        try:
            proc = self.retroarch_process
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

            await asyncio.sleep(0.5)

            # Restart without recreating GStreamer (it keeps streaming)
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"
            if self.gstreamer:
                env.update(self.gstreamer.get_pulseaudio_env())

            config_path = f"/tmp/retroarch_{self.session_id}.cfg"
            cmd = [
                "retroarch",
                "-v",
                "--config",
                config_path,
                "-L",
                f"/usr/lib/libretro/{self.core}_libretro.so",
                "--fullscreen",
                self.rom_path,
            ]

            self.retroarch_process = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.last_activity = datetime.now()
            return True

        except Exception as e:
            logger.error(f"Failed to restart RetroArch: {e}")
            return False

    async def stop(
        self
    ):
        """Stop RetroArch and cleanup."""
        logger.info(f"Stopping session {self.session_id}")

        if self.gstreamer:
            self.gstreamer.stop()

        if self.retroarch_process and self.retroarch_process.poll() is None:
            self.retroarch_process.terminate()
            try:
                self.retroarch_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.retroarch_process.kill()

        config_path = f"/tmp/retroarch_{self.session_id}.cfg"
        try:
            if os.path.exists(config_path):
                os.remove(config_path)
        except OSError:
            pass
