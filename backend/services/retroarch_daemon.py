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
from datetime import datetime, timedelta
from typing import Optional

from config.config_manager import config_manager
from handler.retroarch_handler import retroarch_handler, RetroArchSession
from handler.retroarch_handler import SessionState
from handler.database import db_rom_handler
from handler.redis_handler import async_cache

from services.xvfb_manager import XvfbManager, calculate_optimal_resolution
from services.retroarch_instance import RetroArchInstance

logger = logging.getLogger(__name__)


class RetroArchDaemon:
    """Main daemon managing all RetroArch streaming sessions.

    Orchestrates the lifecycle of multiple RetroArch instances,
    handles Redis pubsub events for session control, input forwarding,
    and WebRTC signaling. Runs periodic cleanup of inactive sessions.

    Attributes:
        config: Application configuration from config_manager.
        xvfb_manager: Manager for allocating virtual X11 displays.
        instances: Mapping of session IDs to RetroArchInstance objects.
        running: Whether the daemon is currently running.
        cleanup_task: Asyncio task for periodic session cleanup.
    """

    def __init__(
        self
    ):
        """Initialize the RetroArch streaming daemon.

        Sets up the Xvfb manager and prepares instance tracking
        for managing concurrent streaming sessions.
        """
        self.config = config_manager.get_config()
        self.xvfb_manager = XvfbManager()
        self.instances: dict[str, RetroArchInstance] = {}
        self.running = False
        self.cleanup_task: Optional[asyncio.Task] = None

    async def start(
        self
    ):
        """Start the daemon and begin listening for session events.

        Sets the running flag, subscribes to Redis events, and starts
        the periodic cleanup task for inactive sessions.
        """
        self.running = True
        logger.info("RetroArch streaming daemon started (GStreamer)")

        await self._subscribe_to_events()
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(
        self
    ):
        """Stop the daemon and cleanup all sessions.

        Stops all active RetroArch instances, releases Xvfb displays,
        and cancels the cleanup task.
        """
        self.running = False
        logger.info("Stopping RetroArch streaming daemon...")

        for session_id in list(self.instances.keys()):
            await self._stop_session(session_id)

        await self.xvfb_manager.cleanup_all()

        if self.cleanup_task:
            self.cleanup_task.cancel()

        logger.info("RetroArch streaming daemon stopped")

    async def _subscribe_to_events(
        self
    ):
        """Subscribe to Redis pubsub channels for session events"""
        try:
            asyncio.create_task(self._handle_session_events())
            asyncio.create_task(self._handle_pubsub_events())
        except Exception as e:
            logger.error(f"Failed to subscribe to events: {e}")

    async def _handle_session_events(
        self
    ):
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

    async def _check_webrtc_answer(
        self,
        session_id: str,
        instance: RetroArchInstance
    ):
        """Check and apply WebRTC answer for a session.

        Args:
            session_id: Unique session identifier to check.
            instance: RetroArchInstance to apply the answer to.
        """
        key = f"retroarch:webrtc_answer:{session_id}"
        answer_sdp = await async_cache.get(key)
        if not answer_sdp:
            return
        instance.set_answer_sdp(answer_sdp)
        await async_cache.delete(key)

    async def _check_stop_signal(
        self,
        session_id: str
    ):
        """Check for stop signal for a session.

        Args:
            session_id: Unique session identifier to check for stop signal.
        """
        key = f"retroarch:stop:{session_id}"
        if await async_cache.get(key):
            await self._stop_session(session_id)
            await async_cache.delete(key)

    async def _check_core_options_request(
        self,
        session_id: str,
        instance: RetroArchInstance
    ):
        """Check for core options request for a session.

        Args:
            session_id: Unique session identifier to check.
            instance: RetroArchInstance to get core options from.
        """
        key = f"retroarch:get_core_options:{session_id}"
        if not await async_cache.get(key):
            return
        core_options = await instance.get_core_options()
        response_key = f"retroarch:core_options:{session_id}"
        await async_cache.set(response_key, json.dumps(core_options), ex=10)
        await async_cache.delete(key)

    async def _handle_pubsub_events(
        self
    ):
        """Handle Redis pubsub events."""
        while self.running:
            try:
                for options in list(self.instances.items()):
                    session_id, instance = options
                    await self._check_webrtc_answer(*options)
                    await self._check_stop_signal(session_id)
                    await self._check_core_options_request(*options)
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error handling pubsub events: {e}")
                await asyncio.sleep(1)

    async def _process_input_message(
        self,
        message: dict,
        instance: RetroArchInstance
    ):
        """Process a single input message.

        Args:
            message: Redis pubsub message dict with 'type' and 'data' keys.
            instance: RetroArchInstance to forward the input to.
        """
        if message["type"] != "message":
            return
        try:
            event = json.loads(message["data"])
            await instance.send_input(event)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid input event: {e}")

    async def _listen_for_inputs(
        self,
        session_id: str,
        instance: RetroArchInstance
    ):
        """Real-time pubsub listener for input events.

        Args:
            session_id: Unique session identifier for the channel.
            instance: RetroArchInstance to forward inputs to.
        """
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
        self,
        message: dict,
        instance: RetroArchInstance,
        command_channel: str,
        option_channel: str,
    ):
        """Process a single command/option message.

        Args:
            message: Redis pubsub message dict with 'type' and 'data' keys.
            instance: RetroArchInstance to execute commands on.
            command_channel: Redis channel name for commands.
            option_channel: Redis channel name for core option changes.
        """
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
                result = await instance.execute_command(command)
                # For SCREENSHOT command, publish the screenshot data
                if command == "SCREENSHOT" and result:
                    import base64
                    screenshot_b64 = base64.b64encode(result).decode("utf-8")
                    await async_cache.publish(
                        f"retroarch:screenshot:{instance.session_id}",
                        json.dumps({"screenshot": screenshot_b64}),
                    )
        elif channel == option_channel:
            option_name = data.get("option_name")
            option_value = data.get("option_value")
            if option_name and option_value is not None:
                await instance.set_core_option(option_name, option_value)

    async def _listen_for_commands(
        self,
        session_id: str,
        instance: RetroArchInstance
    ):
        """Real-time pubsub listener for commands.

        Args:
            session_id: Unique session identifier for the channels.
            instance: RetroArchInstance to execute commands on.
        """
        pubsub = async_cache.pubsub()
        cmd_channel = f"retroarch:command:{session_id}"
        opt_channel = f"retroarch:set_option:{session_id}"

        try:
            await pubsub.subscribe(cmd_channel, opt_channel)
            async for message in pubsub.listen():
                await self._process_command_message(
                    message, instance, cmd_channel, opt_channel
                )
                if session_id not in self.instances:
                    break
        except Exception as e:
            logger.error(f"Error in command listener for {session_id}: {e}")
        finally:
            await pubsub.unsubscribe(cmd_channel, opt_channel)
            await pubsub.close()

    def _process_ice_message(
        self,
        message: dict,
        instance: RetroArchInstance
    ):
        """Process a single ICE candidate message.

        Args:
            message: Redis pubsub message dict with 'type' and 'data' keys.
            instance: RetroArchInstance to add ICE candidate to.
        """
        if message["type"] != "message":
            return
        try:
            data = json.loads(message["data"])
            candidate = data.get("candidate")
            if candidate and instance.gstreamer:
                instance.gstreamer.add_ice_candidate(candidate)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid ICE candidate event: {e}")

    async def _listen_for_ice_candidates(
        self,
        session_id: str,
        instance: RetroArchInstance
    ):
        """Real-time pubsub listener for ICE candidates from browser.

        Args:
            session_id: Unique session identifier for the channel.
            instance: RetroArchInstance to add ICE candidates to.
        """
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

    async def _start_session(
        self,
        session: RetroArchSession
    ):
        """Start a new RetroArch streaming session.

        Creates and initializes all resources needed for streaming:
        allocates Xvfb display, starts RetroArch process, initializes
        GStreamer WebRTC pipeline, and starts pubsub listeners.

        Args:
            session: RetroArchSession containing rom_id, core, and
                session_id for the new streaming session.
        """
        try:
            logger.info(f"Starting session {session.session_id}")

            rom = db_rom_handler.get_rom(session.rom_id)
            if not rom:
                logger.error(f"ROM {session.rom_id} not found")
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
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

            xvfb_width, xvfb_height = calculate_optimal_resolution(
                screen_width,
                screen_height,
                os.getenv("RETROARCH_MAX_RESOLUTION")
            )

            display_num = await self.xvfb_manager.allocate_display(
                xvfb_width, xvfb_height
            )
            retroarch_args = (session.session_id, SessionState.ERROR)
            if display_num is None:
                await retroarch_handler.update_session_state(*retroarch_args)
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
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            # Get WebRTC offer
            offer_sdp = instance.get_offer_sdp(timeout=15.0)
            if not offer_sdp:
                await instance.stop()
                await self.xvfb_manager.release_display(display_num)
                await retroarch_handler.update_session_state(
                    session.session_id, SessionState.ERROR
                )
                return

            self.instances[session.session_id] = instance

            # Start input/command/ICE listeners
            options = (session.session_id, instance)
            asyncio.create_task(self._listen_for_inputs(*options))
            asyncio.create_task(self._listen_for_commands(*options))
            asyncio.create_task(
                self._listen_for_ice_candidates(*options)
            )

            # Store touchscreen region if applicable
            if instance.touchscreen_region:
                session_id = session.session_id
                region_key = f"retroarch:touchscreen_region:{session_id}"
                region_data = {
                    "x_offset": instance.touchscreen_region[0],
                    "y_offset": instance.touchscreen_region[1],
                    "width": instance.touchscreen_region[2],
                    "height": instance.touchscreen_region[3],
                }
                await async_cache.set(
                    region_key,
                    json.dumps(region_data),
                    ex=300
                )

            # Update session
            session.webrtc_offer = offer_sdp
            session.state = SessionState.RUNNING
            session.pid = None
            if instance.retroarch_process:
                session.pid = instance.retroarch_process.pid

            session.xvfb_display = display_num
            await retroarch_handler.set_session(session)

            logger.info(f"Session {session.session_id} started successfully")

        except Exception as e:
            logger.error(f"Failed to start session {session.session_id}: {e}")
            await retroarch_handler.update_session_state(
                session.session_id, SessionState.ERROR
            )

    async def _stop_session(
        self,
        session_id: str
    ):
        """Stop a RetroArch streaming session.

        Stops the RetroArch instance, releases the Xvfb display,
        and updates the session state to STOPPED.

        Args:
            session_id: Unique session identifier to stop.
        """
        try:
            if session_id not in self.instances:
                return

            instance = self.instances[session_id]
            display_num = instance.display_num

            await instance.stop()
            await self.xvfb_manager.release_display(display_num)

            del self.instances[session_id]
            await retroarch_handler.update_session_state(
                session_id, SessionState.STOPPED
            )

            logger.info(f"Session {session_id} stopped")

        except Exception as e:
            logger.error(f"Failed to stop session {session_id}: {e}")

    async def _cleanup_loop(
        self
    ):
        """Periodic cleanup of stale sessions.

        Runs every 60 seconds checking for sessions inactive for more
        than 30 minutes and stops them to free resources.
        """
        while self.running:
            try:
                await asyncio.sleep(60)
                timeout = timedelta(minutes=30)
                now = datetime.now()

                for session_id, instance in list(self.instances.items()):
                    if now - instance.last_activity > timeout:
                        msg = f"Cleaning up inactive session {session_id}"
                        logger.info(msg)
                        await self._stop_session(session_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")


async def main():
    """Main entry point for the RetroArch streaming daemon.

    Configures logging, creates the daemon instance, registers signal
    handlers for graceful shutdown, and runs the main event loop.
    """
    prefix = "[RomM][retroarch_daemon]"
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(levelname)s: {prefix} [%(asctime)s] %(message)s",
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
