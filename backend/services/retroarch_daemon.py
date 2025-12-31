"""
RetroArch Streaming Daemon

This daemon manages RetroArch instances for cloud gaming streaming.
It handles:
- Xvfb virtual display allocation
- RetroArch process lifecycle
- GStreamer WebRTC streaming (video + audio)
- Input forwarding via network commands
- Session cleanup and auto-save
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

from config.config_manager import config_manager
from handler.retroarch_handler import retroarch_handler, RetroArchSession
from handler.retroarch_handler import SessionState
from handler.database import db_rom_handler
from handler.redis_handler import async_cache

# GStreamer imports
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstWebRTC', '1.0')
gi.require_version('GstSdp', '1.0')

logger = logging.getLogger(__name__)

# Initialize GStreamer
Gst.init(None)


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


STANDARD_RESOLUTIONS = [
    (3840, 2160), (3440, 1440), (2560, 1440), (2560, 1080),
    (2400, 1080), (2340, 1080), (2280, 1080), (2160, 1080),
    (1920, 1200), (1920, 1080), (3200, 1440), (3040, 1440), (2960, 1440),
    (1600, 900), (1600, 720), (1560, 720), (1520, 720), (1480, 720),
    (1366, 768), (1280, 800), (1280, 720),
    (1024, 768), (960, 540), (854, 480), (800, 600),
]


def calculate_optimal_resolution(
    screen_width: int, screen_height: int, max_resolution: str | None = None,
) -> tuple[int, int]:
    """Calculate optimal Xvfb resolution based on screen dimensions.

    Selects the largest standard resolution that fits within the client's
    screen dimensions, respecting an optional maximum resolution cap.

    Args:
        screen_width: Client screen width in pixels.
        screen_height: Client screen height in pixels.
        max_resolution: Optional maximum resolution in "WxH" format (e.g., "1920x1080").

    Returns:
        Tuple of (width, height) for the optimal Xvfb display resolution.
    """
    max_width = None
    max_height = None
    if max_resolution:
        try:
            parts = max_resolution.lower().split('x')
            if len(parts) == 2:
                max_width = int(parts[0])
                max_height = int(parts[1])
        except (ValueError, IndexError):
            msg = f"Invalid max resolution format: {max_resolution}, ignoring"
            logger.warning(msg)

    is_portrait = screen_height > screen_width

    available_resolutions = []
    for width, height in STANDARD_RESOLUTIONS:
        if is_portrait:
            res_width, res_height = height, width
        else:
            res_width, res_height = width, height

        if max_width and max_height:
            if res_width <= max_width and res_height <= max_height:
                available_resolutions.append((res_width, res_height))
        else:
            available_resolutions.append((res_width, res_height))

    if not available_resolutions:
        if max_width and max_height:
            return (max_width, max_height)
        else:
            return (1280, 720) if not is_portrait else (720, 1280)

    best_resolution = None
    best_score = -1

    for res_width, res_height in available_resolutions:
        if res_width <= screen_width and res_height <= screen_height:
            score = res_width * res_height
            if score > best_score:
                best_score = score
                best_resolution = (res_width, res_height)

    if best_resolution is None:
        best_resolution = min(available_resolutions, key=lambda r: r[0] * r[1])

    return best_resolution


@dataclass
class XvfbDisplay:
    """Represents an Xvfb virtual display.

    Attributes:
        display_number: X11 display number (e.g., 99 for :99).
        process: The Xvfb subprocess handle.
        in_use: Whether the display is currently allocated to a session.
    """

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

    def _find_reusable_display(self) -> Optional[int]:
        """Find an existing display that can be reused."""
        for display_num, display in self.displays.items():
            not_in_use = not display.in_use
            is_available = not_in_use and display.process.poll() is None
            if is_available:
                display.in_use = True
                logger.info(f"Reusing Xvfb display :{display_num}")
                return display_num
        return None

    def _create_xvfb_process(self, display_num: int, width: int, height: int):
        """Create a new Xvfb process."""
        cmd = [
            "Xvfb", f":{display_num}",
            "-screen", "0", f"{width}x{height}x24",
            "-ac", "-nolisten", "tcp",
            "+extension", "GLX", "+render", "-noreset",
        ]
        return subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    async def _create_new_display(
        self, width: int, height: int,
    ) -> Optional[int]:
        """Create a new Xvfb display."""
        if len(self.displays) >= self.max_displays:
            logger.warning("No available Xvfb displays")
            return None

        display_num = self.start_display + len(self.displays)
        try:
            process = self._create_xvfb_process(display_num, width, height)
            await asyncio.sleep(0.2)

            if process.poll() is not None:
                logger.error(f"Xvfb display :{display_num} failed to start")
                return None

            self.displays[display_num] = XvfbDisplay(
                display_number=display_num, process=process, in_use=True,
            )
            res = f"{width}x{height}"
            logger.info(f"Created Xvfb display :{display_num} ({res})")
            return display_num

        except Exception as e:
            logger.error(f"Failed to create Xvfb display: {e}")
            return None

    async def allocate_display(self, width: int = 1280, height: int = 720):
        """Allocate an available Xvfb display with specified resolution."""
        async with self.lock:
            reused = self._find_reusable_display()
            if reused is not None:
                return reused
            return await self._create_new_display(width, height)

    async def release_display(self, display_num: int):
        """Mark display as available for reuse."""
        async with self.lock:
            if display_num in self.displays:
                self.displays[display_num].in_use = False

    def _terminate_display(self, display: XvfbDisplay):
        """Terminate a single display process."""
        if display.process.poll() is not None:
            return
        display.process.terminate()
        try:
            display.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            display.process.kill()

    async def cleanup_all(self):
        """Terminate all Xvfb processes."""
        async with self.lock:
            for display in self.displays.values():
                self._terminate_display(display)
            self.displays.clear()
            logger.info("Cleaned up all Xvfb displays")


class GStreamerWebRTC:
    """
    GStreamer-based WebRTC streaming for RetroArch.

    Captures video from Xvfb and audio from PulseAudio,
    streams via WebRTC using GStreamer's webrtcbin.
    """

    # Fixed port range for Docker port mapping
    MIN_RTP_PORT = 10000
    MAX_RTP_PORT = 10020

    def __init__(
        self,
        session_id: str,
        display_num: int,
        width: int,
        height: int,
        fps: int = 60,
    ):
        """Initialize GStreamer WebRTC streaming.

        Args:
            session_id: Unique session identifier for this stream.
            display_num: X11 display number to capture from.
            width: Video capture width in pixels.
            height: Video capture height in pixels.
            fps: Target frame rate for video capture (default: 60).
        """
        self.session_id = session_id
        self.display_num = display_num
        self.width = width
        self.height = height
        self.fps = fps

        self.sink_name = f"retroarch_sink_{session_id[:8]}"
        self.sink_module_id: Optional[int] = None
        self.pulse_server = "unix:/var/run/pulse/native"

        # TURN server configuration from environment
        # Format: turn://username:password@host:port or turns://... for TLS
        self.turn_server = os.getenv("RETROARCH_TURN_SERVER")
        default_stun = "stun://stun.l.google.com:19302"
        self.stun_server = os.getenv("RETROARCH_STUN_SERVER", default_stun)

        self.pipeline: Optional[Gst.Pipeline] = None
        self.webrtcbin: Optional[Gst.Element] = None
        self.loop: Optional[GLib.MainLoop] = None
        self.thread: Optional[threading.Thread] = None

        self._offer_sdp: Optional[str] = None
        self._ice_candidates: list[dict] = []
        self._offer_ready = threading.Event()
        self._ice_gathering_complete = threading.Event()

    def _get_pulse_env(self) -> dict:
        """Get PulseAudio environment."""
        env = os.environ.copy()
        env["PULSE_SERVER"] = self.pulse_server
        return env

    def _ensure_pulseaudio_running(self):
        """Start PulseAudio if not running."""
        result = subprocess.run(["pulseaudio", "--check"], capture_output=True)
        if result.returncode != 0:
            logger.info("Starting PulseAudio daemon...")
            subprocess.Popen(
                ["pulseaudio", "--start", "--exit-idle-time=-1"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            import time
            time.sleep(1)

    def _create_null_sink(self) -> Optional[int]:
        """Create null-sink for audio capture."""
        cmd = [
            "pactl", "load-module", "module-null-sink",
            f"sink_name={self.sink_name}",
            "rate=48000", "channels=2", "format=s16le",
        ]
        env = self._get_pulse_env()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create null-sink: {result.stderr}")
        module_str = result.stdout.strip()
        return int(module_str) if module_str.isdigit() else None

    def setup_pulseaudio(self):
        """Setup PulseAudio null-sink for audio capture."""
        try:
            self._ensure_pulseaudio_running()
            self.sink_module_id = self._create_null_sink()
        except Exception as e:
            logger.error(f"Failed to setup PulseAudio: {e}")
            raise

    def get_pulseaudio_env(self) -> dict:
        """Get environment variables for RetroArch to use our sink."""
        return {
            "PULSE_SINK": self.sink_name,
            "PULSE_SERVER": self.pulse_server,
        }

    def _build_pipeline(self) -> str:
        """Build low-latency GStreamer pipeline string."""
        stun = self.stun_server
        webrtcbin_props = (
            f"webrtcbin name=webrtcbin bundle-policy=max-bundle "
            f"stun-server={stun}"
        )

        if self.turn_server:
            webrtcbin_props += f" turn-server={self.turn_server}"

        # Ultra low-latency pipeline
        pipeline = (
            f"{webrtcbin_props} "

            # Video: 60fps, VP8 realtime, no buffering
            f"ximagesrc display-name=:{self.display_num} "
            "use-damage=false show-pointer=false ! "
            f"video/x-raw,framerate={self.fps}/1 ! "
            "videoscale method=0 ! videoconvert ! "
            "video/x-raw,format=I420 ! "
            "vp8enc deadline=1 cpu-used=16 lag-in-frames=0 error-resilient=1 "
            "target-bitrate=6000000 keyframe-max-dist=60 static-threshold=0 ! "
            "rtpvp8pay pt=96 ! "
            "webrtcbin. "

            # Audio: balanced latency/stability
            f"pulsesrc device={self.sink_name}.monitor "
            f"server={self.pulse_server} "
            "buffer-time=40000 latency-time=20000 ! "
            "audio/x-raw,rate=48000,channels=2,format=S16LE ! "
            "opusenc bitrate=128000 frame-size=20 ! "
            "rtpopuspay pt=97 ! "
            "webrtcbin. "
        )

        return pipeline

    def _on_negotiation_needed(self, webrtcbin):
        """Called when negotiation is needed - create offer."""
        cb = self._on_offer_created
        promise = Gst.Promise.new_with_change_func(cb, webrtcbin, None)
        webrtcbin.emit("create-offer", None, promise)

    def _on_offer_created(self, promise, webrtcbin, _):
        """Called when offer is created."""
        reply = promise.get_reply()
        offer = reply.get_value("offer")

        if offer is None:
            logger.error("Failed to create offer")
            return

        # Set local description
        promise = Gst.Promise.new()
        webrtcbin.emit("set-local-description", offer, promise)
        promise.interrupt()

    def _on_ice_candidate(self, _webrtcbin, mline_index, candidate):
        """Called when ICE candidate is generated.

        Args:
            _webrtcbin: The webrtcbin element (required by GStreamer signal,
                not used as we access self.webrtcbin directly).
            mline_index: The media line index for this candidate.
            candidate: The ICE candidate string.
        """
        if candidate:
            self._ice_candidates.append({
                "sdpMLineIndex": mline_index,
                "candidate": candidate,
            })
            logger.debug(f"ICE candidate gathered: {candidate[:50]}...")

    def _on_ice_gathering_state_changed(self, webrtcbin, _pspec):
        """Called when ICE gathering state changes.

        Args:
            webrtcbin: The webrtcbin element to query the state from.
            _pspec: GObject property spec (required by 'notify' signal, unused).
        """
        state = webrtcbin.get_property("ice-gathering-state")
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._finalize_offer()

    def _on_bus_error(self, _bus, message):
        """Handle GStreamer bus errors.

        Args:
            _bus: The GStreamer bus (required by bus signal, unused).
            message: The GStreamer error message to parse.
        """
        err, debug = message.parse_error()
        logger.error(f"GStreamer error: {err.message}")
        logger.debug(f"GStreamer debug: {debug}")

    def _on_bus_warning(self, _bus, message):
        """Handle GStreamer bus warnings.

        Args:
            _bus: The GStreamer bus (required by bus signal, unused).
            message: The GStreamer warning message to parse.
        """
        warn, debug = message.parse_warning()
        logger.warning(f"GStreamer warning: {warn.message}")
        logger.debug(f"GStreamer warning debug: {debug}")

    def _replace_ips_with_localhost(self, sdp_text: str) -> str:
        """Replace all IPs with localhost for Docker port forwarding."""
        import re
        def replace_ip(match):
            ip = match.group(1)
            return ip if ip == '127.0.0.1' else '127.0.0.1'
        return re.sub(r'(\d+\.\d+\.\d+\.\d+)', replace_ip, sdp_text)

    def _finalize_offer(self):
        """Get final SDP with all ICE candidates."""
        if not self.webrtcbin:
            return

        local_desc = self.webrtcbin.get_property("local-description")
        if not local_desc:
            logger.error("Failed to get local description")
            return

        sdp_text = local_desc.sdp.as_text()
        self._offer_sdp = self._replace_ips_with_localhost(sdp_text)
        self._offer_ready.set()

    def start(self):
        """Start the GStreamer pipeline in a separate thread."""
        def run_pipeline():
            try:
                pipeline_str = self._build_pipeline()

                self.pipeline = Gst.parse_launch(pipeline_str)
                self.webrtcbin = self.pipeline.get_by_name("webrtcbin")

                if not self.webrtcbin:
                    logger.error("Failed to get webrtcbin element")
                    return

                # Configure ICE agent for Docker networking
                ice_agent = self.webrtcbin.get_property("ice-agent")
                if ice_agent:
                    ice_agent.set_property("min-rtp-port", self.MIN_RTP_PORT)
                    ice_agent.set_property("max-rtp-port", self.MAX_RTP_PORT)
                    try:
                        ice_agent.emit("add-local-ip-address", "0.0.0.0")
                    except Exception:
                        pass

                # Connect signals
                self.webrtcbin.connect("on-negotiation-needed", self._on_negotiation_needed)
                self.webrtcbin.connect("on-ice-candidate", self._on_ice_candidate)
                self.webrtcbin.connect("notify::ice-gathering-state", self._on_ice_gathering_state_changed)

                # Bus message handler for errors
                bus = self.pipeline.get_bus()
                bus.add_signal_watch()
                bus.connect("message::error", self._on_bus_error)
                bus.connect("message::warning", self._on_bus_warning)

                # Set pipeline to PLAYING
                ret = self.pipeline.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    logger.error("Failed to start pipeline")
                    return

                # Run GLib main loop
                self.loop = GLib.MainLoop()
                self.loop.run()

            except Exception as e:
                logger.error(f"GStreamer pipeline error: {e}")
            finally:
                if self.pipeline:
                    self.pipeline.set_state(Gst.State.NULL)

        self.thread = threading.Thread(target=run_pipeline, daemon=True)
        self.thread.start()

    def get_offer_sdp(self, timeout: float = 10.0) -> Optional[str]:
        """Wait for and return the WebRTC offer SDP."""
        if self._offer_ready.wait(timeout=timeout):
            return self._offer_sdp
        logger.error("Timeout waiting for offer SDP")
        return None

    def set_answer_sdp(self, answer_sdp: str):
        """Set the remote answer SDP."""
        if not self.webrtcbin:
            logger.error("webrtcbin not initialized")
            return False

        def set_remote_description_in_glib():
            """Called in GLib mainloop thread."""
            try:
                _, sdpmsg = GstSdp.SDPMessage.new()
                GstSdp.sdp_message_parse_buffer(bytes(answer_sdp.encode()), sdpmsg)
                answer = GstWebRTC.WebRTCSessionDescription.new(
                    GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
                )

                def on_answer_set(promise):
                    state = promise.wait()
                    if state != Gst.PromiseResult.REPLIED:
                        logger.error(f"Failed to set answer SDP, state: {state}")

                promise = Gst.Promise.new_with_change_func(on_answer_set)
                self.webrtcbin.emit("set-remote-description", answer, promise)

            except Exception as e:
                logger.error(f"Failed to set answer SDP in GLib: {e}")

            return False  # Don't repeat

        GLib.idle_add(set_remote_description_in_glib)
        return True

    def add_ice_candidate(self, candidate: dict):
        """Add a remote ICE candidate from the browser."""
        if not self.webrtcbin:
            logger.warning("Cannot add ICE candidate: webrtcbin not initialized")
            return

        def add_candidate_in_glib():
            try:
                sdp_mline_index = candidate.get("sdpMLineIndex", 0)
                candidate_str = candidate.get("candidate", "")

                if candidate_str:
                    self.webrtcbin.emit("add-ice-candidate", sdp_mline_index, candidate_str)
            except Exception as e:
                logger.error(f"Failed to add ICE candidate: {e}")
            return False

        GLib.idle_add(add_candidate_in_glib)

    def stop(self):
        """Stop the GStreamer pipeline."""
        if self.loop:
            self.loop.quit()

        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        # Cleanup PulseAudio sink
        if self.sink_module_id:
            try:
                pulse_env = os.environ.copy()
                pulse_env["PULSE_SERVER"] = self.pulse_server
                subprocess.run(
                    ["pactl", "unload-module", str(self.sink_module_id)],
                    capture_output=True,
                    env=pulse_env,
                )
            except Exception:
                pass


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
        """Initialize a RetroArch instance.

        Args:
            session_id: Unique session identifier.
            rom_path: Absolute path to the ROM file to run.
            core: Name of the libretro core to use (without _libretro.so suffix).
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

    async def start(self):
        """Start RetroArch and GStreamer streaming."""
        try:
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

            # Create RetroArch config
            config_dir = Path("/tmp/retroarch_config")
            config_dir.mkdir(exist_ok=True)
            core_options_path = config_dir / "retroarch-core-options.cfg"

            # Copy pre-generated core options if available
            pre_generated = Path("/app/romm/config/retroarch") / f"{self.core.lower()}-core-options.cfg"
            if pre_generated.exists() and not core_options_path.exists():
                import shutil
                shutil.copy2(pre_generated, core_options_path)

            config_path = f"/tmp/retroarch_{self.session_id}.cfg"
            with open(config_path, "w") as f:
                f.write('#include "/etc/retroarch.cfg"\n')
                f.write("input_auto_mouse_grab = \"false\"\n")
                f.write("input_overlay_show_mouse_cursor = \"false\"\n")

                # Network commands for remote control
                f.write("network_cmd_enable = \"true\"\n")
                f.write("network_cmd_port = \"55355\"\n")
                f.write("stdin_cmd_enable = \"true\"\n")

                # Video settings
                f.write("video_driver = \"gl\"\n")
                f.write("video_threaded = \"true\"\n")
                f.write("video_vsync = \"false\"\n")
                f.write("video_black_frame_insertion = \"0\"\n")
                f.write("video_shader_enable = \"false\"\n")
                f.write("video_smooth = \"false\"\n")
                f.write("video_max_swapchain_images = \"2\"\n")

                # Audio settings - direct PulseAudio (PULSE_SINK env var sets the sink)
                f.write("audio_driver = \"pulse\"\n")
                f.write("audio_enable = \"true\"\n")
                f.write("audio_out_rate = \"48000\"\n")
                f.write("audio_sync = \"true\"\n")
                f.write("audio_rate_control = \"true\"\n")
                f.write("audio_latency = \"32\"\n")

                # Core options
                f.write(f'core_options_path = "{core_options_path}"\n')
                f.write("game_specific_options = \"false\"\n")
                f.write("auto_overrides_enable = \"false\"\n")
                f.write("auto_remaps_enable = \"false\"\n")

            # Start RetroArch
            cmd = [
                "retroarch",
                "-v",
                "--config", config_path,
                "-L", f"/usr/lib/libretro/{self.core}_libretro.so",
                "--fullscreen",
                self.rom_path,
            ]

            if self.state_path:
                cmd.extend(["-e", "1", "-s", self.state_path])

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

            logger.info(f"Started RetroArch (PID: {self.retroarch_process.pid})")

            # Start GStreamer streaming
            self.gstreamer.start()

            return True

        except Exception as e:
            logger.error(f"Failed to start RetroArch: {e}")
            return False

    def get_offer_sdp(self, timeout: float = 10.0) -> Optional[str]:
        """Get WebRTC offer SDP."""
        if self.gstreamer:
            return self.gstreamer.get_offer_sdp(timeout)
        return None

    def set_answer_sdp(self, answer_sdp: str) -> bool:
        """Set WebRTC answer SDP."""
        if self.gstreamer:
            return self.gstreamer.set_answer_sdp(answer_sdp)
        return False

    async def _send_retroarch_command(self, command: str) -> Optional[str]:
        """Send command to RetroArch via UDP."""
        try:
            loop = asyncio.get_event_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: asyncio.DatagramProtocol(),
                remote_addr=('127.0.0.1', 55355)
            )
            transport.sendto(f"{command}\n".encode())
            transport.close()
            return None
        except Exception as e:
            logger.error(f"Failed to send RetroArch command '{command}': {e}")
            return None

    def _find_core_options_file(self) -> Optional[Path]:
        """Find the core options config file."""
        paths = [
            Path("/tmp/retroarch_config/retroarch-core-options.cfg"),
            Path.home() / ".config" / "retroarch" / "retroarch-core-options.cfg",
        ]
        for path in paths:
            if path.exists():
                return path
        return None

    def _parse_core_option_line(self, line: str, core_name: str) -> Optional[tuple]:
        """Parse a single core option line. Returns (key, value) or None."""
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            return None
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"')
        if core_name and key.lower().startswith(core_name + '_'):
            return (key, value)
        return None

    async def get_core_options(self) -> dict:
        """Retrieve core options from config file."""
        try:
            config_path = self._find_core_options_file()
            if not config_path:
                return {}

            core_name = self.core.lower().replace('ra-', '') if self.core else ""
            core_options = {}

            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    parsed = self._parse_core_option_line(line, core_name)
                    if parsed:
                        core_options[parsed[0]] = parsed[1]

            return core_options
        except Exception as e:
            logger.error(f"Failed to get core options: {e}")
            return {}

    def _get_xdotool_env(self) -> dict:
        """Get environment for xdotool commands."""
        env = os.environ.copy()
        env["DISPLAY"] = f":{self.display_num}"
        return env

    async def _xdotool(self, *args):
        """Run xdotool command."""
        env = self._get_xdotool_env()
        proc = await asyncio.create_subprocess_exec(
            "xdotool", *args, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    def _xdotool_fire_and_forget(self, *args):
        """Run xdotool without waiting (for mousemove)."""
        env = self._get_xdotool_env()
        asyncio.create_task(asyncio.create_subprocess_exec(
            "xdotool", *args, env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        ))

    def _calculate_mouse_position(self, x: float, y: float) -> tuple[int, int]:
        """Calculate xvfb mouse position from normalized coordinates."""
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

    async def send_input(self, event_data: dict):
        """Send input to RetroArch via xdotool."""
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
                self._xdotool_fire_and_forget("mousemove", str(xvfb_x), str(xvfb_y))

            elif event_type == "mousedown":
                button = str(event_data.get("button", 0) + 1)
                await self._xdotool("mousedown", button)

            elif event_type == "mouseup":
                button = str(event_data.get("button", 0) + 1)
                await self._xdotool("mouseup", button)

            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to send input: {e}")

    async def execute_command(self, command: str):
        """Execute a RetroArch command."""
        try:
            if command == "RESET":
                await self.restart()
                return

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
                return

            await self._send_retroarch_command(retroarch_cmd)

            if command == "SAVE_AND_QUIT":
                await asyncio.sleep(0.5)
                await self._send_retroarch_command("QUIT")

            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to execute command {command}: {e}")

    def _update_config_line(self, line: str, option_name: str, option_value: str):
        """Update a config line if it matches option_name, else return unchanged."""
        if '=' not in line:
            return line, False
        key = line.split('=', 1)[0].strip()
        if key == option_name:
            return f'{option_name} = "{option_value}"\n', True
        return line, False

    async def set_core_option(self, option_name: str, option_value: str):
        """Set a core option value."""
        config_file = Path("/tmp/retroarch_config/retroarch-core-options.cfg")
        if not config_file.exists():
            return

        try:
            lines = []
            option_found = False

            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    new_line, found = self._update_config_line(line, option_name, option_value)
                    lines.append(new_line)
                    option_found = option_found or found

            if not option_found:
                lines.append(f'{option_name} = "{option_value}"\n')

            with open(config_file, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            await self._send_retroarch_command("CORE_OPTION_RELOAD")
            self.last_activity = datetime.now()

        except Exception as e:
            logger.error(f"Failed to set core option {option_name}: {e}")

    def _calculate_touchscreen_region(self) -> Optional[tuple]:
        """Calculate touchscreen region for DS/3DS cores."""
        if self.core not in TOUCHSCREEN_REGIONS:
            return None
        return TOUCHSCREEN_REGIONS[self.core].get("vertical")

    def _map_key_to_x11(self, js_key: str) -> str:
        """Map JavaScript key names to X11 key names."""
        key_map = {
            "ArrowUp": "Up", "ArrowDown": "Down",
            "ArrowLeft": "Left", "ArrowRight": "Right",
            " ": "space", "Enter": "Return", "Escape": "Escape",
            "Backspace": "BackSpace", "Tab": "Tab",
            "Shift": "Shift_L", "Control": "Control_L",
            "Alt": "Alt_L", "Meta": "Super_L",
        }
        return key_map.get(js_key, js_key.lower())

    async def restart(self):
        """Restart RetroArch cleanly."""
        try:
            if self.retroarch_process and self.retroarch_process.poll() is None:
                self.retroarch_process.terminate()
                try:
                    self.retroarch_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.retroarch_process.kill()
                    self.retroarch_process.wait()

            await asyncio.sleep(0.5)

            # Restart without recreating GStreamer (it keeps streaming)
            env = os.environ.copy()
            env["DISPLAY"] = f":{self.display_num}"
            if self.gstreamer:
                env.update(self.gstreamer.get_pulseaudio_env())

            config_path = f"/tmp/retroarch_{self.session_id}.cfg"
            cmd = [
                "retroarch", "-v",
                "--config", config_path,
                "-L", f"/usr/lib/libretro/{self.core}_libretro.so",
                "--fullscreen",
                self.rom_path,
            ]

            self.retroarch_process = subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )

            self.last_activity = datetime.now()
            return True

        except Exception as e:
            logger.error(f"Failed to restart RetroArch: {e}")
            return False

    async def stop(self):
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


class RetroArchDaemon:
    """Main daemon managing all RetroArch streaming sessions"""

    def __init__(self):
        """Initialize the RetroArch streaming daemon.

        Sets up the Xvfb manager and prepares instance tracking
        for managing concurrent streaming sessions.
        """
        self.config = config_manager.get_config()
        self.xvfb_manager = XvfbManager()
        self.instances: dict[str, RetroArchInstance] = {}
        self.running = False
        self.cleanup_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the daemon"""
        self.running = True
        logger.info("RetroArch streaming daemon started (GStreamer)")

        await self._subscribe_to_events()
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        """Stop the daemon"""
        self.running = False
        logger.info("Stopping RetroArch streaming daemon...")

        for session_id in list(self.instances.keys()):
            await self._stop_session(session_id)

        await self.xvfb_manager.cleanup_all()

        if self.cleanup_task:
            self.cleanup_task.cancel()

        logger.info("RetroArch streaming daemon stopped")

    async def _subscribe_to_events(self):
        """Subscribe to Redis pubsub channels for session events"""
        try:
            asyncio.create_task(self._handle_session_events())
            asyncio.create_task(self._handle_pubsub_events())
        except Exception as e:
            logger.error(f"Failed to subscribe to events: {e}")

    async def _handle_session_events(self):
        """Handle incoming session events from Redis"""
        while self.running:
            try:
                sessions = await retroarch_handler.get_all_sessions()

                for session in sessions:
                    if (
                        session.session_id not in self.instances
                        and session.state == SessionState.STARTING
                    ):
                        await self._start_session(session)

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error handling session events: {e}")
                await asyncio.sleep(5)

    async def _check_webrtc_answer(self, session_id: str, instance: RetroArchInstance):
        """Check and apply WebRTC answer for a session."""
        key = f"retroarch:webrtc_answer:{session_id}"
        answer_sdp = await async_cache.get(key)
        if not answer_sdp:
            return
        instance.set_answer_sdp(answer_sdp)
        await async_cache.delete(key)

    async def _check_stop_signal(self, session_id: str):
        """Check for stop signal for a session."""
        key = f"retroarch:stop:{session_id}"
        if await async_cache.get(key):
            await self._stop_session(session_id)
            await async_cache.delete(key)

    async def _check_core_options_request(self, session_id: str, instance: RetroArchInstance):
        """Check for core options request for a session."""
        key = f"retroarch:get_core_options:{session_id}"
        if not await async_cache.get(key):
            return
        core_options = await instance.get_core_options()
        response_key = f"retroarch:core_options:{session_id}"
        await async_cache.set(response_key, json.dumps(core_options), ex=10)
        await async_cache.delete(key)

    async def _handle_pubsub_events(self):
        """Handle Redis pubsub events."""
        while self.running:
            try:
                for session_id, instance in list(self.instances.items()):
                    await self._check_webrtc_answer(session_id, instance)
                    await self._check_stop_signal(session_id)
                    await self._check_core_options_request(session_id, instance)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error handling pubsub events: {e}")
                await asyncio.sleep(1)

    async def _process_input_message(self, message: dict, instance: RetroArchInstance):
        """Process a single input message."""
        if message["type"] != "message":
            return
        try:
            event = json.loads(message["data"])
            await instance.send_input(event)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid input event: {e}")

    async def _listen_for_inputs(self, session_id: str, instance: RetroArchInstance):
        """Real-time pubsub listener for input events."""
        pubsub = async_cache.pubsub()
        channel = f"retroarch:input:{session_id}"

        try:
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                await self._process_input_message(message, instance)
                if session_id not in self.instances:
                    break
        except Exception as e:
            logger.error(f"Error in input listener for {session_id}: {e}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def _process_command_message(
        self, message: dict, instance: RetroArchInstance,
        command_channel: str, option_channel: str,
    ):
        """Process a single command/option message."""
        if message["type"] != "message":
            return

        try:
            channel = message["channel"]
            data = json.loads(message["data"])
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid command event: {e}")
            return

        if channel == command_channel:
            command = data.get("command")
            if command:
                await instance.execute_command(command)
        elif channel == option_channel:
            option_name = data.get("option_name")
            option_value = data.get("option_value")
            if option_name and option_value is not None:
                await instance.set_core_option(option_name, option_value)

    async def _listen_for_commands(self, session_id: str, instance: RetroArchInstance):
        """Real-time pubsub listener for commands."""
        pubsub = async_cache.pubsub()
        cmd_channel = f"retroarch:command:{session_id}"
        opt_channel = f"retroarch:set_option:{session_id}"

        try:
            await pubsub.subscribe(cmd_channel, opt_channel)
            async for message in pubsub.listen():
                await self._process_command_message(message, instance, cmd_channel, opt_channel)
                if session_id not in self.instances:
                    break
        except Exception as e:
            logger.error(f"Error in command listener for {session_id}: {e}")
        finally:
            await pubsub.unsubscribe(cmd_channel, opt_channel)
            await pubsub.close()

    def _process_ice_message(self, message: dict, instance: RetroArchInstance):
        """Process a single ICE candidate message."""
        if message["type"] != "message":
            return
        try:
            data = json.loads(message["data"])
            candidate = data.get("candidate")
            if candidate and instance.gstreamer:
                instance.gstreamer.add_ice_candidate(candidate)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid ICE candidate event: {e}")

    async def _listen_for_ice_candidates(self, session_id: str, instance: RetroArchInstance):
        """Real-time pubsub listener for ICE candidates from browser."""
        pubsub = async_cache.pubsub()
        channel = f"retroarch:ice:{session_id}"

        try:
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                self._process_ice_message(message, instance)
                if session_id not in self.instances:
                    break
        except Exception as e:
            logger.error(f"Error in ICE listener for {session_id}: {e}")
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()

    async def _start_session(self, session: RetroArchSession):
        """Start a new RetroArch streaming session"""
        try:
            logger.info(f"Starting session {session.session_id}")

            rom = db_rom_handler.get_rom(session.rom_id)
            if not rom:
                logger.error(f"ROM {session.rom_id} not found")
                await retroarch_handler.update_session_state(session.session_id, SessionState.ERROR)
                return

            from config import LIBRARY_BASE_PATH
            rom_path = os.path.join(str(LIBRARY_BASE_PATH), rom.full_path)

            # Get screen dimensions
            dims_key = f"retroarch:screen_dims:{session.session_id}"
            dims_data = await async_cache.get(dims_key)
            screen_width, screen_height = 1920, 1080

            if dims_data:
                try:
                    dims = json.loads(dims_data)
                    screen_width = dims.get("width", 1920)
                    screen_height = dims.get("height", 1080)
                except (json.JSONDecodeError, TypeError):
                    pass

            max_resolution = os.getenv("RETROARCH_MAX_RESOLUTION")
            xvfb_width, xvfb_height = calculate_optimal_resolution(screen_width, screen_height, max_resolution)

            display_num = await self.xvfb_manager.allocate_display(xvfb_width, xvfb_height)
            if display_num is None:
                await retroarch_handler.update_session_state(session.session_id, SessionState.ERROR)
                return

            instance = RetroArchInstance(
                session_id=session.session_id,
                rom_path=rom_path,
                core=session.core,
                display_num=display_num,
                width=xvfb_width,
                height=xvfb_height,
            )

            if not await instance.start():
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(session.session_id, SessionState.ERROR)
                return

            # Get WebRTC offer
            offer_sdp = instance.get_offer_sdp(timeout=15.0)
            if not offer_sdp:
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(session.session_id, SessionState.ERROR)
                return

            self.instances[session.session_id] = instance

            # Start input/command/ICE listeners
            asyncio.create_task(self._listen_for_inputs(session.session_id, instance))
            asyncio.create_task(self._listen_for_commands(session.session_id, instance))
            asyncio.create_task(self._listen_for_ice_candidates(session.session_id, instance))

            # Store touchscreen region if applicable
            if instance.touchscreen_region:
                region_key = f"retroarch:touchscreen_region:{session.session_id}"
                region_data = {
                    "x_offset": instance.touchscreen_region[0],
                    "y_offset": instance.touchscreen_region[1],
                    "width": instance.touchscreen_region[2],
                    "height": instance.touchscreen_region[3],
                }
                await async_cache.set(region_key, json.dumps(region_data), ex=300)

            # Update session
            session.webrtc_offer = offer_sdp
            session.state = SessionState.RUNNING
            session.pid = instance.retroarch_process.pid if instance.retroarch_process else None
            session.xvfb_display = display_num
            await retroarch_handler.set_session(session)

            logger.info(f"Session {session.session_id} started successfully")

        except Exception as e:
            logger.error(f"Failed to start session {session.session_id}: {e}")
            await retroarch_handler.update_session_state(session.session_id, SessionState.ERROR)

    async def _stop_session(self, session_id: str):
        """Stop a RetroArch streaming session"""
        try:
            if session_id not in self.instances:
                return

            instance = self.instances[session_id]
            display_num = instance.display_num

            await instance.stop()
            await self.xvfb_manager.release_display(display_num)

            del self.instances[session_id]
            await retroarch_handler.update_session_state(session_id, SessionState.STOPPED)

            logger.info(f"Session {session_id} stopped")

        except Exception as e:
            logger.error(f"Failed to stop session {session_id}: {e}")

    async def _cleanup_loop(self):
        """Periodic cleanup of stale sessions"""
        while self.running:
            try:
                await asyncio.sleep(60)
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: [RomM][retroarch_daemon][%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    daemon = RetroArchDaemon()
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await daemon.start()
        while daemon.running:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        await daemon.stop()


if __name__ == "__main__":
    asyncio.run(main())
