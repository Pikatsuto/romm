"""
GStreamer WebRTC Streaming

Provides WebRTC streaming capabilities for RetroArch using GStreamer.
Captures video from Xvfb and audio from PulseAudio.
"""

import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional
from gi.repository import Gst, GstWebRTC, GstSdp, GLib

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")

logger = logging.getLogger(__name__)

# Initialize GStreamer
Gst.init(None)


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

    def _get_pulse_env(
        self
    ) -> dict:
        """Get PulseAudio environment variables.

        Returns:
            dict: Environment dictionary with PULSE_SERVER configured
                for the session's PulseAudio connection.
        """
        env = os.environ.copy()
        env["PULSE_SERVER"] = self.pulse_server
        return env

    def _ensure_pulseaudio_running(
        self
    ):
        """Start PulseAudio daemon if not already running.

        Checks if PulseAudio is active and starts it if not,
        waiting 1 second for initialization.
        """
        result = subprocess.run(["pulseaudio", "--check"], capture_output=True)
        if result.returncode != 0:
            logger.info("Starting PulseAudio daemon...")
            subprocess.Popen(
                ["pulseaudio", "--start", "--exit-idle-time=-1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1)

    def _create_null_sink(
        self
    ) -> Optional[int]:
        """Create a PulseAudio null-sink for audio capture.

        Creates a virtual audio sink that RetroArch outputs to,
        allowing GStreamer to capture the audio stream.

        Returns:
            Module ID of the created null-sink, or None if creation failed.

        Raises:
            RuntimeError: If pactl fails to load the null-sink module.
        """
        cmd = [
            "pactl",
            "load-module",
            "module-null-sink",
            f"sink_name={self.sink_name}",
            "rate=48000",
            "channels=2",
            "format=s16le",
        ]
        env = self._get_pulse_env()
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create null-sink: {result.stderr}")
        module_str = result.stdout.strip()
        return int(module_str) if module_str.isdigit() else None

    def setup_pulseaudio(
        self
    ):
        """Setup PulseAudio null-sink for audio capture.

        Ensures PulseAudio is running and creates a null-sink
        for capturing RetroArch audio output.

        Raises:
            RuntimeError: If PulseAudio setup fails.
        """
        try:
            self._ensure_pulseaudio_running()
            self.sink_module_id = self._create_null_sink()
        except Exception as e:
            logger.error(f"Failed to setup PulseAudio: {e}")
            raise

    def get_pulseaudio_env(
        self
    ) -> dict:
        """Get environment variables for RetroArch to use our sink.

        Returns:
            dict: Environment variables with PULSE_SINK and PULSE_SERVER
                configured for this session's audio sink.
        """
        return {
            "PULSE_SINK": self.sink_name,
            "PULSE_SERVER": self.pulse_server,
        }

    def _build_pipeline(
        self
    ) -> str:
        """Build low-latency GStreamer pipeline string.

        Constructs a GStreamer pipeline for WebRTC streaming with:
        - VP8 video encoding from Xvfb display capture
        - Opus audio encoding from PulseAudio monitor
        - STUN/TURN server configuration for NAT traversal

        Returns:
            str: GStreamer pipeline description string.
        """
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

    def _on_negotiation_needed(
        self,
        webrtcbin
    ):
        """Called when WebRTC negotiation is needed.

        Creates a WebRTC offer SDP when the webrtcbin element
        signals that negotiation is required.

        Args:
            webrtcbin: The GStreamer webrtcbin element.
        """
        cb = self._on_offer_created
        promise = Gst.Promise.new_with_change_func(cb, webrtcbin, None)
        webrtcbin.emit("create-offer", None, promise)

    def _on_offer_created(
        self,
        promise,
        webrtcbin,
        _
    ):
        """Called when WebRTC offer creation completes.

        Sets the created offer as the local description on
        the webrtcbin element.

        Args:
            promise: GStreamer promise containing the offer.
            webrtcbin: The webrtcbin element to set description on.
            _: Unused user data (required by promise callback).
        """
        reply = promise.get_reply()
        offer = reply.get_value("offer")

        if offer is None:
            logger.error("Failed to create offer")
            return

        # Set local description
        promise = Gst.Promise.new()
        webrtcbin.emit("set-local-description", offer, promise)
        promise.interrupt()

    def _on_ice_candidate(
        self,
        _webrtcbin,
        mline_index,
        candidate
    ):
        """Called when ICE candidate is generated.

        Args:
            _webrtcbin: The webrtcbin element (required by GStreamer signal,
                not used as we access self.webrtcbin directly).
            mline_index: The media line index for this candidate.
            candidate: The ICE candidate string.
        """
        if candidate:
            self._ice_candidates.append(
                {
                    "sdpMLineIndex": mline_index,
                    "candidate": candidate,
                }
            )
            logger.debug(f"ICE candidate gathered: {candidate[:50]}...")

    def _on_ice_gathering_state_changed(
        self,
        webrtcbin,
        _pspec
    ):
        """Called when ICE gathering state changes.

        Args:
            webrtcbin: The webrtcbin element to query state from.
            _pspec: GObject property spec (required by 'notify').
        """
        state = webrtcbin.get_property("ice-gathering-state")
        if state == GstWebRTC.WebRTCICEGatheringState.COMPLETE:
            self._finalize_offer()

    def _on_bus_error(
        self,
        _bus,
        message
    ):
        """Handle GStreamer bus errors.

        Args:
            _bus: The GStreamer bus (required by bus signal, unused).
            message: The GStreamer error message to parse.
        """
        err, debug = message.parse_error()
        logger.error(f"GStreamer error: {err.message}")
        logger.debug(f"GStreamer debug: {debug}")

    def _on_bus_warning(
        self,
        _bus,
        message
    ):
        """Handle GStreamer bus warnings.

        Args:
            _bus: The GStreamer bus (required by bus signal, unused).
            message: The GStreamer warning message to parse.
        """
        warn, debug = message.parse_warning()
        logger.warning(f"GStreamer warning: {warn.message}")
        logger.debug(f"GStreamer warning debug: {debug}")

    def _replace_ips_with_localhost(
        self,
        sdp_text: str
    ) -> str:
        """Replace all IPs with localhost for Docker port forwarding.

        Rewrites IP addresses in SDP to 127.0.0.1 so that WebRTC
        connections route through Docker's port forwarding.

        Args:
            sdp_text: Original SDP text with container IPs.

        Returns:
            str: SDP text with IPs replaced by 127.0.0.1.
        """

        def replace_ip(match):
            ip = match.group(1)
            return ip if ip == "127.0.0.1" else "127.0.0.1"

        return re.sub(r"(\d+\.\d+\.\d+\.\d+)", replace_ip, sdp_text)

    def _finalize_offer(
        self
    ):
        """Finalize the WebRTC offer with all ICE candidates.

        Retrieves the local description from webrtcbin, processes
        it for Docker networking, and signals that the offer is ready.
        """
        if not self.webrtcbin:
            return

        local_desc = self.webrtcbin.get_property("local-description")
        if not local_desc:
            logger.error("Failed to get local description")
            return

        sdp_text = local_desc.sdp.as_text()
        self._offer_sdp = self._replace_ips_with_localhost(sdp_text)
        self._offer_ready.set()

    def start(
        self
    ):
        """Start the GStreamer pipeline in a separate thread.

        Launches a daemon thread that runs the GLib main loop
        for GStreamer pipeline execution and WebRTC signaling.
        """

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
                wb = self.webrtcbin
                ice_cb = self._on_ice_gathering_state_changed
                neg_cb = self._on_negotiation_needed
                wb.connect("on-negotiation-needed", neg_cb)
                wb.connect("on-ice-candidate", self._on_ice_candidate)
                wb.connect("notify::ice-gathering-state", ice_cb)

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

    def get_offer_sdp(
        self,
        timeout: float = 10.0
    ) -> Optional[str]:
        """Wait for and return the WebRTC offer SDP.

        Blocks until the offer is ready or timeout is reached.

        Args:
            timeout: Maximum seconds to wait for offer (default: 10.0).

        Returns:
            The offer SDP string, or None if timeout occurred.
        """
        if self._offer_ready.wait(timeout=timeout):
            return self._offer_sdp
        logger.error("Timeout waiting for offer SDP")
        return None

    def set_remote_description_in_glib(
        self,
        answer_sdp: str
    ):
        """Called in GLib mainloop thread.

        Args:
            answer_sdp: The remote answer SDP.
        """
        def on_answer_set(promise):
            state = promise.wait()
            if state != Gst.PromiseResult.REPLIED:
                logger.error(f"Failed to set answer SDP, state: {state}")

        try:
            _, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(answer_sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(
                GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg
            )

            promise = Gst.Promise.new_with_change_func(on_answer_set)
            self.webrtcbin.emit("set-remote-description", answer, promise)

        except Exception as e:
            logger.error(f"Failed to set answer SDP in GLib: {e}")

        return False

    def set_answer_sdp(
        self,
        answer_sdp: str
    ):
        """Set the remote answer SDP from the browser.

        Schedules the answer to be set in the GLib main loop thread.

        Args:
            answer_sdp: The SDP answer string from the browser.

        Returns:
            bool: True if scheduling succeeded, False if webrtcbin
                is not initialized.
        """
        if not self.webrtcbin:
            logger.error("webrtcbin not initialized")
            return False

        # Pass the function and argument separately
        # idle_add expects a callable
        GLib.idle_add(lambda: self.set_remote_description_in_glib(answer_sdp))
        return True

    def add_ice_candidate(
        self,
        candidate: dict
    ):
        """Add a remote ICE candidate from the browser.

        Schedules the ICE candidate to be added in the GLib thread.

        Args:
            candidate: ICE candidate dict with 'sdpMLineIndex' and
                'candidate' keys from the browser's WebRTC API.
        """
        if not self.webrtcbin:
            logger.warning("Cannot add ICE candidate: webrtcbin not init")
            return

        def add_candidate_in_glib():
            try:
                candidate_str = candidate.get("candidate", "")
                webrtcbin_args = (
                    "add-ice-candidate",
                    candidate.get("sdpMLineIndex", 0),
                    candidate_str,
                )

                if candidate_str:
                    self.webrtcbin.emit(*webrtcbin_args)
            except Exception as e:
                logger.error(f"Failed to add ICE candidate: {e}")
            return False

        GLib.idle_add(add_candidate_in_glib)

    def stop(
        self
    ):
        """Stop the GStreamer pipeline and cleanup resources.

        Quits the GLib main loop, sets pipeline to NULL state,
        and unloads the PulseAudio null-sink module.
        """
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
