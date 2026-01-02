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
from services.retroarch_instance import RetroArchInstance, CORE_POINTER_ZONES
from services.retroarch_sync import (
    restore_save_to_session,
    restore_state_to_session,
    restore_firmware_to_session,
    sync_session_to_romm,
)

logger = logging.getLogger(__name__)

# Sync interval in seconds (5 minutes)
SYNC_INTERVAL_SECONDS = 300


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
        self.sync_task: Optional[asyncio.Task] = None
        # Store session metadata for sync
        # (user_id, rom_id, platform_slug, core)
        self.session_metadata: dict[str, dict] = {}

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
        self.sync_task = asyncio.create_task(self._sync_loop())

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

        if self.sync_task:
            self.sync_task.cancel()

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
                for session_id, instance in list(self.instances.items()):
                    options = (session_id, instance)
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
            state_id = data.get("state_id")  # Optional: specific state to load
            if command:
                result = await instance.execute_command(
                    command,
                    state_id=state_id
                )
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

            # Apply max screen dimension limits from environment variables
            max_width = os.getenv("MAX_SCREEN_WIDTH")
            max_height = os.getenv("MAX_SCREEN_HEIGHT")
            if max_width:
                screen_width = min(screen_width, int(max_width))
            if max_height:
                screen_height = min(screen_height, int(max_height))

            xvfb_width, xvfb_height = calculate_optimal_resolution(
                screen_width,
                screen_height,
                os.getenv("RETROARCH_MAX_RESOLUTION")
            )

            # Check if we need to rotate for horizontal cores in portrait mode
            # Portrait player + horizontal core = create horizontal + rotate
            is_portrait_player = xvfb_height > xvfb_width
            zone = CORE_POINTER_ZONES.get(session.core, {})
            native = zone.get("native", (4, 3))
            is_horizontal_core = native[0] > native[1]
            needs_rotation = is_portrait_player and is_horizontal_core

            if needs_rotation:
                # Swap dimensions to create horizontal xvfb
                xvfb_width, xvfb_height = xvfb_height, xvfb_width

            # Calculate dimensions based on core aspect ratio
            # This ensures xvfb matches the game area exactly (no black bars)
            # Frontend will center the video via CSS
            #
            # The limiting dimension depends on screen orientation:
            # - Landscape screen: height is smaller, so calculate width from height
            # - Portrait screen: width is smaller, so calculate height from width
            is_landscape_screen = xvfb_width > xvfb_height
            if is_landscape_screen:
                xvfb_width = native[0] * xvfb_height // native[1]
            else:
                xvfb_height = native[1] * xvfb_width // native[0]
            logger.info(
                f"Adjusted xvfb to {xvfb_width}x{xvfb_height} "
                f"for core ratio {native[0]}:{native[1]}"
                + (" (rotated)" if needs_rotation else "")
            )

            display_num = await self.xvfb_manager.allocate_display(
                xvfb_width, xvfb_height
            )
            retroarch_args = (session.session_id, SessionState.ERROR)
            if display_num is None:
                await retroarch_handler.update_session_state(*retroarch_args)
                return

            # Determine state_path if state_id is provided
            state_path = None

            instance = RetroArchInstance(
                session_id=session.session_id,
                rom_path=rom_path,
                core=session.core,
                display_num=display_num,
                width=xvfb_width,
                height=xvfb_height,
                language=session.language,
            )

            # Create session directories BEFORE restoring saves/states
            instance._setup_session_directories()

            # Restore save from RomM if save_id is provided
            if session.save_id:
                restore_save_to_session(
                    save_id=session.save_id,
                    user_id=session.user_id,
                    session_saves_dir=instance.saves_dir,
                    rom_path=rom_path,
                )

            # Restore state from RomM if state_id is provided
            if session.state_id:
                state_path = restore_state_to_session(
                    state_id=session.state_id,
                    user_id=session.user_id,
                    session_states_dir=instance.states_dir,
                    rom_path=rom_path,
                )
                if state_path:
                    instance.state_path = state_path

            # Restore firmware to session's system directory
            if session.firmware_id:
                restore_firmware_to_session(
                    firmware_id=session.firmware_id,
                    session_system_dir=instance.system_dir,
                )

            # Store session metadata for periodic sync
            self.session_metadata[session.session_id] = {
                "user_id": session.user_id,
                "rom_id": session.rom_id,
                "platform_slug": session.platform_slug,
                "core": session.core,
            }

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

            # Store rotation flag for frontend
            # (horizontal core in portrait mode)
            if needs_rotation:
                rotation_key = f"retroarch:needs_rotation:{session.session_id}"
                await async_cache.set(rotation_key, "true", ex=300)

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

    def _sync_session(self, session_id: str):
        """Sync saves, states, and screenshots from a session to RomM.

        Args:
            session_id: Unique session identifier to sync.
        """
        if session_id not in self.instances:
            return

        instance = self.instances[session_id]
        metadata = self.session_metadata.get(session_id)
        if not metadata:
            return

        saves_synced, states_synced, screenshots_synced = sync_session_to_romm(
            session_saves_dir=instance.saves_dir,
            session_states_dir=instance.states_dir,
            session_screenshots_dir=instance.screenshots_dir,
            user_id=metadata["user_id"],
            rom_id=metadata["rom_id"],
            platform_slug=metadata["platform_slug"],
            emulator=metadata["core"],
        )

        if saves_synced > 0 or states_synced > 0 or screenshots_synced > 0:
            logger.info(
                f"Synced {saves_synced} saves, {states_synced} states, "
                f"{screenshots_synced} screenshots for session {session_id}"
            )

    async def _stop_session(
        self,
        session_id: str
    ):
        """Stop a RetroArch streaming session.

        Syncs saves/states to RomM, stops the RetroArch instance,
        releases the Xvfb display, cleans up the session directory,
        and updates the session state to STOPPED.

        Args:
            session_id: Unique session identifier to stop.
        """
        try:
            if session_id not in self.instances:
                return

            instance = self.instances[session_id]
            display_num = instance.display_num

            # Capture screenshot for auto-save before syncing
            if (
                instance.retroarch_process and
                instance.retroarch_process.poll() is None
            ):
                await instance._capture_state_screenshot()

            # Sync saves/states to RomM before stopping
            self._sync_session(session_id)

            await instance.stop()

            # Cleanup session directory
            instance.cleanup_session_dir()

            await self.xvfb_manager.release_display(display_num)

            # Cleanup metadata
            if session_id in self.session_metadata:
                del self.session_metadata[session_id]

            del self.instances[session_id]

            # Delete session from Redis
            await retroarch_handler.delete_session(session_id)

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

    async def _sync_loop(
        self
    ):
        """Periodic sync of saves/states to RomM.

        Runs every 5 minutes (SYNC_INTERVAL_SECONDS) to sync saves and states
        from all active sessions to RomM storage.
        """
        while self.running:
            try:
                await asyncio.sleep(SYNC_INTERVAL_SECONDS)

                for session_id in list(self.instances.keys()):
                    self._sync_session(session_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in sync loop: {e}")


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
