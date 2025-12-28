/**
 * Composable for game controls (gamepad, virtual gamepad, fullscreen)
 * Inspired by EmulatorJS controls
 */

import { ref, onMounted, onUnmounted } from "vue";
import type { Socket } from "socket.io-client";

export interface GameControlsOptions {
  sessionId: string;
  socket: Socket;
  isMobile?: boolean;
}

export function useGameControls(options: () => GameControlsOptions | null) {
  // Gamepad state
  let gamepadLoopId: number | null = null;
  const gamepadButtonStates = new Map<number, boolean[]>();
  const gamepadConnected = ref(false);

  // Virtual gamepad for mobile
  const showVirtualGamepad = ref(false);

  // Fullscreen state
  const isFullscreen = ref(false);

  // Gamepad button mapping (RetroArch standard)
  const GAMEPAD_MAPPING = {
    // Face buttons
    B: 0,      // A/Cross
    A: 1,      // B/Circle
    Y: 2,      // X/Square
    X: 3,      // Y/Triangle
    // Shoulder buttons
    L1: 4,
    R1: 5,
    L2: 6,
    R2: 7,
    // System buttons
    SELECT: 8,
    START: 9,
    // Analog stick buttons
    L3: 10,
    R3: 11,
    // D-pad
    UP: 12,
    DOWN: 13,
    LEFT: 14,
    RIGHT: 15,
  };

  /**
   * Poll gamepad state and send changes via socket
   */
  function pollGamepads() {
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

      // Check each button
      for (let btnIndex = 0; btnIndex < gamepad.buttons.length; btnIndex++) {
        const button = gamepad.buttons[btnIndex];
        const pressed = button.pressed;
        const wasPressed = previousStates[btnIndex];

        // Button state changed
        if (pressed !== wasPressed) {
          previousStates[btnIndex] = pressed;

          // Send button event
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

      // Check axes (analog sticks) - send if value changed significantly
      for (let axisIndex = 0; axisIndex < gamepad.axes.length; axisIndex++) {
        const value = gamepad.axes[axisIndex];

        // Only send if axis value is significant (dead zone)
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
   * Start gamepad polling
   */
  function startGamepadPolling() {
    if (gamepadLoopId !== null) return; // Already polling

    console.log("[GameControls] Starting gamepad polling");
    gamepadLoopId = requestAnimationFrame(pollGamepads);
  }

  /**
   * Stop gamepad polling
   */
  function stopGamepadPolling() {
    if (gamepadLoopId !== null) {
      cancelAnimationFrame(gamepadLoopId);
      gamepadLoopId = null;
      console.log("[GameControls] Stopped gamepad polling");
    }
  }

  /**
   * Handle gamepad connected event
   */
  function handleGamepadConnected(event: GamepadEvent) {
    console.log("[GameControls] Gamepad connected:", event.gamepad.id);
    gamepadConnected.value = true;
    startGamepadPolling();
  }

  /**
   * Handle gamepad disconnected event
   */
  function handleGamepadDisconnected(event: GamepadEvent) {
    console.log("[GameControls] Gamepad disconnected:", event.gamepad.id);
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
   * Toggle fullscreen mode
   */
  async function toggleFullscreen(container: HTMLElement | null) {
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
   * Handle fullscreen change event
   */
  function handleFullscreenChange() {
    isFullscreen.value = !!document.fullscreenElement;
  }

  /**
   * Toggle virtual gamepad visibility
   */
  function toggleVirtualGamepad() {
    showVirtualGamepad.value = !showVirtualGamepad.value;
  }

  // Lifecycle hooks
  onMounted(() => {
    // Check for existing gamepads
    const gamepads = navigator.getGamepads();
    const hasGamepads = Array.from(gamepads).some(gp => gp !== null);
    if (hasGamepads) {
      gamepadConnected.value = true;
      startGamepadPolling();
    }

    // Listen for gamepad events
    window.addEventListener("gamepadconnected", handleGamepadConnected);
    window.addEventListener("gamepaddisconnected", handleGamepadDisconnected);

    // Listen for fullscreen changes
    document.addEventListener("fullscreenchange", handleFullscreenChange);
  });

  onUnmounted(() => {
    stopGamepadPolling();
    gamepadButtonStates.clear();

    window.removeEventListener("gamepadconnected", handleGamepadConnected);
    window.removeEventListener("gamepaddisconnected", handleGamepadDisconnected);
    document.removeEventListener("fullscreenchange", handleFullscreenChange);
  });

  return {
    // Gamepad
    gamepadConnected,
    startGamepadPolling,
    stopGamepadPolling,

    // Virtual gamepad
    showVirtualGamepad,
    toggleVirtualGamepad,

    // Fullscreen
    isFullscreen,
    toggleFullscreen,
  };
}