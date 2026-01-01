/**
 * Game Controls Composable
 *
 * Vue composable for managing game input controls including physical gamepads,
 * virtual on-screen controls for mobile devices, and fullscreen state.
 * Inspired by EmulatorJS control handling.
 *
 * @module views/Player/RetroArch/useGameControls
 */

import { ref, onMounted, onUnmounted } from "vue";
import type { Socket } from "socket.io-client";

/**
 * Options for initializing game controls.
 */
export interface GameControlsOptions {
  /** Active session identifier for input routing */
  sessionId: string;
  /** SocketIO connection for sending input events */
  socket: Socket;
  /** Whether running on a mobile device (enables virtual gamepad) */
  isMobile?: boolean;
}

/**
 * Composable for managing game controls.
 *
 * Handles physical gamepad input polling, virtual gamepad state,
 * and fullscreen mode toggling. Gamepad inputs are sent via SocketIO
 * for low-latency transmission to the server.
 *
 * @param options - Factory function returning control options or null if not ready
 * @returns Reactive refs and methods for game control state
 *
 * @example
 * ```ts
 * const gameControls = useGameControls(() =>
 *   sessionId.value && socket.value
 *     ? { sessionId: sessionId.value, socket: socket.value }
 *     : null
 * );
 *
 * // Access state
 * if (gameControls.gamepadConnected.value) {
 *   console.log("Gamepad ready!");
 * }
 *
 * // Toggle fullscreen
 * gameControls.toggleFullscreen(containerElement);
 * ```
 */
export function useGameControls(options: () => GameControlsOptions | null) {
  /** Request animation frame ID for gamepad polling loop */
  let gamepadLoopId: number | null = null;

  /** Map of gamepad index to button pressed states for change detection */
  const gamepadButtonStates = new Map<number, boolean[]>();

  /** Whether any physical gamepad is currently connected */
  const gamepadConnected = ref(false);

  /** Whether the virtual on-screen gamepad is visible (mobile) */
  const showVirtualGamepad = ref(false);

  /** Whether the player is currently in fullscreen mode */
  const isFullscreen = ref(false);

  /**
   * Poll all connected gamepads and send state changes via socket.
   *
   * Uses requestAnimationFrame for smooth 60fps polling. Only sends
   * events when button states change to minimize network traffic.
   * Analog sticks use a dead zone to filter noise.
   */
  function pollGamepads(): void {
    const ctrl = options();
    if (!ctrl) return;

    const gamepads = navigator.getGamepads();

    for (let i = 0; i < gamepads.length; i++) {
      const gamepad = gamepads[i];
      if (!gamepad) continue;

      // Initialize button states for this gamepad if needed
      if (!gamepadButtonStates.has(i)) {
        gamepadButtonStates.set(i, new Array(gamepad.buttons.length).fill(false));
      }

      const previousStates = gamepadButtonStates.get(i)!;

      // Check each button for state changes
      for (let btnIndex = 0; btnIndex < gamepad.buttons.length; btnIndex++) {
        const button = gamepad.buttons[btnIndex];
        const pressed = button.pressed;
        const wasPressed = previousStates[btnIndex];

        if (pressed !== wasPressed) {
          previousStates[btnIndex] = pressed;

          ctrl.socket.emit("retroarch-input", {
            session_id: ctrl.sessionId,
            event: {
              type: pressed ? "gamepad-buttondown" : "gamepad-buttonup",
              gamepadIndex: i,
              buttonIndex: btnIndex,
              timestamp: Date.now(),
            },
          });
        }
      }

      // Check analog axes (apply dead zone filtering)
      for (let axisIndex = 0; axisIndex < gamepad.axes.length; axisIndex++) {
        const value = gamepad.axes[axisIndex];

        // Dead zone: only send if value is significant
        if (Math.abs(value) > 0.15) {
          ctrl.socket.emit("retroarch-input", {
            session_id: ctrl.sessionId,
            event: {
              type: "gamepad-axis",
              gamepadIndex: i,
              axisIndex,
              value,
              timestamp: Date.now(),
            },
          });
        }
      }
    }

    // Schedule next poll
    gamepadLoopId = requestAnimationFrame(pollGamepads);
  }

  /**
   * Start the gamepad polling loop.
   *
   * Called automatically when a gamepad is connected.
   * Safe to call multiple times (will not start duplicate loops).
   */
  function startGamepadPolling(): void {
    if (gamepadLoopId !== null) return;
    gamepadLoopId = requestAnimationFrame(pollGamepads);
  }

  /**
   * Stop the gamepad polling loop.
   *
   * Called automatically when all gamepads are disconnected
   * or when the component is unmounted.
   */
  function stopGamepadPolling(): void {
    if (gamepadLoopId !== null) {
      cancelAnimationFrame(gamepadLoopId);
      gamepadLoopId = null;
    }
  }

  /**
   * Handle gamepad connected browser event.
   *
   * @param _event - Gamepad event (unused, but required by event listener)
   */
  function handleGamepadConnected(_event: GamepadEvent): void {
    gamepadConnected.value = true;
    startGamepadPolling();
  }

  /**
   * Handle gamepad disconnected browser event.
   *
   * Cleans up state for the disconnected gamepad and stops polling
   * if no gamepads remain connected.
   *
   * @param event - Gamepad event containing the disconnected gamepad
   */
  function handleGamepadDisconnected(event: GamepadEvent): void {
    gamepadButtonStates.delete(event.gamepad.index);

    // Stop polling if no gamepads left
    const gamepads = navigator.getGamepads();
    const hasGamepads = Array.from(gamepads).some(gp => gp !== null);
    if (!hasGamepads) {
      gamepadConnected.value = false;
      stopGamepadPolling();
    }
  }

  /**
   * Toggle fullscreen mode for the player container.
   *
   * Uses the Fullscreen API to enter or exit fullscreen mode.
   * Updates the isFullscreen ref to reflect current state.
   *
   * @param container - HTML element to make fullscreen (usually the player container)
   */
  async function toggleFullscreen(container: HTMLElement | null): Promise<void> {
    if (!container) return;

    try {
      if (!document.fullscreenElement) {
        await container.requestFullscreen();
        isFullscreen.value = true;
      } else {
        await document.exitFullscreen();
        isFullscreen.value = false;
      }
    } catch (err) {
      console.error("[GameControls] Fullscreen toggle failed:", err);
    }
  }

  /**
   * Handle fullscreen change browser event.
   *
   * Syncs the isFullscreen ref with the actual fullscreen state,
   * which may change due to user pressing Escape or other browser actions.
   */
  function handleFullscreenChange(): void {
    isFullscreen.value = !!document.fullscreenElement;
  }

  /**
   * Toggle virtual gamepad visibility.
   *
   * Used on mobile devices to show/hide on-screen touch controls.
   */
  function toggleVirtualGamepad(): void {
    showVirtualGamepad.value = !showVirtualGamepad.value;
  }

  // Lifecycle: setup event listeners on mount
  onMounted(() => {
    // Check for existing gamepads (may already be connected)
    const gamepads = navigator.getGamepads();
    const hasGamepads = Array.from(gamepads).some(gp => gp !== null);
    if (hasGamepads) {
      gamepadConnected.value = true;
      startGamepadPolling();
    }

    // Listen for gamepad connect/disconnect events
    window.addEventListener("gamepadconnected", handleGamepadConnected);
    window.addEventListener("gamepaddisconnected", handleGamepadDisconnected);

    // Listen for fullscreen changes (user may press Escape)
    document.addEventListener("fullscreenchange", handleFullscreenChange);
  });

  // Lifecycle: cleanup on unmount
  onUnmounted(() => {
    stopGamepadPolling();
    gamepadButtonStates.clear();

    window.removeEventListener("gamepadconnected", handleGamepadConnected);
    window.removeEventListener("gamepaddisconnected", handleGamepadDisconnected);
    document.removeEventListener("fullscreenchange", handleFullscreenChange);
  });

  return {
    // Gamepad state
    gamepadConnected,
    startGamepadPolling,
    stopGamepadPolling,

    // Virtual gamepad state
    showVirtualGamepad,
    toggleVirtualGamepad,

    // Fullscreen state
    isFullscreen,
    toggleFullscreen,
  };
}