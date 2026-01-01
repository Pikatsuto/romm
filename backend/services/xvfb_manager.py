"""
Xvfb Virtual Display Manager

Manages allocation and cleanup of Xvfb virtual X11 displays
for running graphical applications headlessly.
"""

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


STANDARD_RESOLUTIONS = [
    (3840, 2160),
    (3440, 1440),
    (2560, 1440),
    (2560, 1080),
    (2400, 1080),
    (2340, 1080),
    (2280, 1080),
    (2160, 1080),
    (1920, 1200),
    (1920, 1080),
    (3200, 1440),
    (3040, 1440),
    (2960, 1440),
    (1600, 900),
    (1600, 720),
    (1560, 720),
    (1520, 720),
    (1480, 720),
    (1366, 768),
    (1280, 800),
    (1280, 720),
    (1024, 768),
    (960, 540),
    (854, 480),
    (800, 600),
]


def calculate_optimal_resolution(
    screen_width: int,
    screen_height: int,
    max_resolution: str | None = None,
) -> tuple[int, int]:
    """Calculate optimal Xvfb resolution based on screen dimensions.

    Selects the largest standard resolution that fits within the client's
    screen dimensions, respecting an optional maximum resolution cap.

    Args:
        screen_width: Client screen width in pixels.
        screen_height: Client screen height in pixels.
        max_resolution: Optional max resolution in "WxH" format.

    Returns:
        Tuple of (width, height) for the optimal Xvfb display resolution.
    """
    max_width = None
    max_height = None
    if max_resolution:
        try:
            parts = max_resolution.lower().split("x")
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
        in_use: Whether display is currently allocated.
        width: Display width in pixels.
        height: Display height in pixels.
    """

    display_number: int
    process: subprocess.Popen
    in_use: bool = False
    width: int = 1280
    height: int = 720


class XvfbManager:
    """Manages allocation and cleanup of Xvfb virtual displays.

    Provides a pool of virtual X11 displays for running graphical
    applications headlessly. Handles display creation, reuse, and cleanup.

    Attributes:
        start_display: First display number to allocate from (default: 99).
        max_displays: Maximum concurrent displays allowed.
        displays: Mapping of display numbers to XvfbDisplay instances.
        lock: Async lock for thread-safe display allocation.
    """

    def __init__(
        self,
        start_display: int = 99,
        max_displays: int = 10
    ):
        """Initialize the Xvfb display manager.

        Args:
            start_display: First X11 display number to use (default: 99).
            max_displays: Maximum number of concurrent displays (default: 10).
        """
        self.start_display = start_display
        self.max_displays = max_displays
        self.displays: dict[int, XvfbDisplay] = {}
        self.lock = asyncio.Lock()

    def _find_reusable_display(
        self,
        width: int,
        height: int
    ) -> Optional[int]:
        """Find an existing display that can be reused.

        Searches for a display that is not currently in use, whose
        Xvfb process is still running, and has matching resolution.

        Args:
            width: Required display width in pixels.
            height: Required display height in pixels.

        Returns:
            Display number if a reusable display is found, None otherwise.
        """
        for display_num, display in self.displays.items():
            not_in_use = not display.in_use
            process_running = display.process.poll() is None
            resolution_matches = display.width == width and display.height == height
            is_available = not_in_use and process_running and resolution_matches
            if is_available:
                display.in_use = True
                logger.info(f"Reusing Xvfb display :{display_num} ({width}x{height})")
                return display_num
        return None

    def _create_xvfb_process(
        self,
        display_num: int,
        width: int,
        height: int
    ):
        """Create a new Xvfb process.

        Spawns an Xvfb subprocess with the specified display number and
        resolution, configured with GLX extension and 24-bit color depth.

        Args:
            display_num: X11 display number to use (e.g., 99 for :99).
            width: Screen width in pixels.
            height: Screen height in pixels.

        Returns:
            subprocess.Popen: Handle to the spawned Xvfb process.
        """
        cmd = [
            "Xvfb",
            f":{display_num}",
            "-screen",
            "0",
            f"{width}x{height}x24",
            "-ac",
            "-nolisten",
            "tcp",
            "+extension",
            "GLX",
            "+render",
            "-noreset",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def _create_new_display(
        self,
        width: int,
        height: int,
    ) -> Optional[int]:
        """Create a new Xvfb display.

        Allocates a new display number and spawns an Xvfb process for it.
        Waits briefly to verify the process started successfully.

        Args:
            width: Screen width in pixels.
            height: Screen height in pixels.

        Returns:
            Display number if successful, None if max displays reached or
            if the Xvfb process failed to start.
        """
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
                display_number=display_num,
                process=process,
                in_use=True,
                width=width,
                height=height,
            )
            res = f"{width}x{height}"
            logger.info(f"Created Xvfb display :{display_num} ({res})")
            return display_num

        except Exception as e:
            logger.error(f"Failed to create Xvfb display: {e}")
            return None

    async def allocate_display(
        self,
        width: int = 1280,
        height: int = 720
    ) -> Optional[int]:
        """Allocate an available Xvfb display with specified resolution.

        Args:
            width: Display width in pixels (default: 1280).
            height: Display height in pixels (default: 720).

        Returns:
            Display number if allocation succeeded, None otherwise.
        """
        async with self.lock:
            reused = self._find_reusable_display(width, height)
            if reused is not None:
                return reused
            return await self._create_new_display(width, height)

    async def release_display(
        self,
        display_num: int
    ):
        """Mark display as available for reuse.

        Args:
            display_num: X11 display number to release.
        """
        async with self.lock:
            if display_num in self.displays:
                self.displays[display_num].in_use = False

    def _terminate_display(
        self,
        display: XvfbDisplay
    ):
        """Terminate a single display process.

        Args:
            display: XvfbDisplay instance to terminate.
        """
        if display.process.poll() is not None:
            return
        display.process.terminate()
        try:
            display.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            display.process.kill()

    async def cleanup_all(
        self
    ):
        """Terminate all Xvfb processes."""
        async with self.lock:
            for display in self.displays.values():
                self._terminate_display(display)
            self.displays.clear()
            logger.info("Cleaned up all Xvfb displays")
