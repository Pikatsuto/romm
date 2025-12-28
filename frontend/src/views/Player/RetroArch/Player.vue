<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import type { SaveSchema, StateSchema } from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import retroarchApi from "@/services/api/retroarch";
import storeConfig from "@/stores/config";
import type { DetailedRom } from "@/stores/roms";
import { io, Socket } from "socket.io-client";
import { storeToRefs } from "pinia";
import { useGameControls } from "./useGameControls";
import PlayerMenu from "./PlayerMenu.vue";

const props = defineProps<{
  rom: DetailedRom;
  save: SaveSchema | null;
  state: StateSchema | null;
  core: string;
}>();

const router = useRouter();
const configStore = storeConfig();
const { config } = storeToRefs(configStore);
const videoRef = ref<HTMLVideoElement>();
const containerRef = ref<HTMLDivElement>();
const playerMenuRef = ref<InstanceType<typeof PlayerMenu>>();
const sessionId = ref<string | null>(null);
const peerConnection = ref<RTCPeerConnection | null>(null);
const socket = ref<Socket | null>(null);
const statusMessage = ref<string>("Initializing RetroArch...");
const isLoading = ref(true);
const error = ref<string | null>(null);

// Touchscreen region configuration from backend (DS, 3DS, etc.)
const touchscreenRegion = ref<{
  x_offset: number;
  y_offset: number;
  width: number;
  height: number;
} | null>(null);

// Core options loaded from backend
const coreOptions = ref<Record<string, string>>({});

// Pointer lock state for cursor capture
const isPointerLocked = ref(false);
// Virtual mouse position when pointer is locked (pixels within touchscreen zone)
const virtualMouseX = ref(0);
const virtualMouseY = ref(0);

// Throttle mousemove events to prevent flooding
let lastMouseMoveTime = 0;
const MOUSE_MOVE_THROTTLE_MS = 8; // ~120 FPS for better responsiveness

// Game controls (gamepad, virtual gamepad, fullscreen)
const gameControls = useGameControls(() =>
  sessionId.value && socket.value
    ? { sessionId: sessionId.value, socket: socket.value }
    : null
)

onMounted(async () => {
  document.addEventListener("pointerlockchange", handlePointerLockChange);
  try {
    await startSession();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "Failed to start session";
    statusMessage.value = error.value;
    isLoading.value = false;
  }
});

onBeforeUnmount(async () => {
  document.removeEventListener("pointerlockchange", handlePointerLockChange);
  if (document.pointerLockElement) {
    document.exitPointerLock();
  }
  await stopSession();
});

function handlePointerLockChange() {
  isPointerLocked.value = document.pointerLockElement === videoRef.value;
}

async function startSession() {
  try {
    statusMessage.value = "Starting RetroArch session...";

    // Detect screen dimensions and orientation
    const screenWidth = window.screen.width;
    const screenHeight = window.screen.height;
    const isPortrait = screenHeight > screenWidth;

    // 1. Start session with screen dimensions
    const { data } = await retroarchApi.startSession({
      romId: props.rom.id,
      core: props.core,
      saveId: props.save?.id,
      stateId: props.state?.id,
      screenWidth,
      screenHeight,
    });

    sessionId.value = data.session_id;
    touchscreenRegion.value = data.touchscreen_region || null;
    coreOptions.value = data.core_options || {};
    console.log("[RetroArch] Session created:", sessionId.value);
    if (touchscreenRegion.value) {
      console.log("[RetroArch] Touchscreen region:", touchscreenRegion.value);
    }
    if (Object.keys(coreOptions.value).length > 0) {
      console.log(`[RetroArch] Core options loaded: ${Object.keys(coreOptions.value).length} options`);
    }

    statusMessage.value = "Setting up WebRTC connection...";

    // 2. Setup WebRTC
    await setupWebRTC(data.webrtc_offer);

    // 3. Connect SocketIO for signaling
    connectSocket();

    statusMessage.value = "Connected! Starting stream...";
    isLoading.value = false;
  } catch (err) {
    // Provide user-friendly error messages
    const errorMessage = err instanceof Error ? err.message : String(err);

    // Handle specific HTTP status codes
    if (errorMessage.includes("429")) {
      throw new Error(
        "Too many RetroArch sessions are currently active. Please wait for an existing session to end or try again later."
      );
    } else if (errorMessage.includes("503")) {
      throw new Error(
        "RetroArch streaming is not enabled on this server. Please contact your administrator."
      );
    } else if (errorMessage.includes("404")) {
      throw new Error(
        "The selected game could not be found. Please try refreshing the page."
      );
    } else {
      throw new Error(`Failed to start RetroArch session: ${errorMessage}`);
    }
  }
}

async function setupWebRTC(offerSdp: string) {
  const iceServers = config.value.EJS_NETPLAY_ICE_SERVERS || [
    { urls: "stun:stun.l.google.com:19302" },
  ];

  const pc = new RTCPeerConnection({ iceServers });
  peerConnection.value = pc;

  // Receive video/audio stream
  pc.ontrack = (event) => {
    console.log("[RetroArch] Received media track:", event.track.kind);
    if (videoRef.value && event.streams[0]) {
      videoRef.value.srcObject = event.streams[0];
      videoRef.value.play().catch((err: Error) => {
        console.error("[RetroArch] Failed to play video:", err);
      });
    }
  };

  // Handle ICE candidates
  pc.onicecandidate = (event) => {
    if (event.candidate && socket.value && sessionId.value) {
      console.log("[RetroArch] Sending ICE candidate");
      socket.value.emit("retroarch-ice-candidate", {
        session_id: sessionId.value,
        candidate: event.candidate.toJSON(),
      });
    }
  };

  // Connection state changes
  pc.onconnectionstatechange = () => {
    console.log("[RetroArch] Connection state:", pc.connectionState);
    if (pc.connectionState === "connected") {
      statusMessage.value = "Playing...";
    } else if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      error.value = "WebRTC connection failed";
      statusMessage.value = error.value;
    }
  };

  // Set remote description (offer)
  await pc.setRemoteDescription({ type: "offer", sdp: offerSdp });

  // Create answer
  const answer = await pc.createAnswer();
  await pc.setLocalDescription(answer);

  // Send answer to server
  if (sessionId.value && answer.sdp) {
    await retroarchApi.answerSession({
      sessionId: sessionId.value,
      webrtcAnswer: answer.sdp,
    });
  }
}

function connectSocket() {
  socket.value = io({
    path: "/netplay/socket.io",
    transports: ["websocket"],
  });

  socket.value.on("connect", () => {
    console.log("[RetroArch] SocketIO connected");

    // Join the session room to receive targeted events
    if (sessionId.value) {
      socket.value?.emit("join", sessionId.value);
      console.log(`[RetroArch] Joined room: ${sessionId.value}`);
    }
  });

  socket.value.on("disconnect", () => {
    console.log("[RetroArch] SocketIO disconnected");
  });

  socket.value.on("connect_error", (err: Error) => {
    console.error("[RetroArch] SocketIO connection error:", err);
  });

  // Listen for core options updates from backend
  socket.value.on("retroarch-core-options-ready", (data: { session_id: string; core_options: Record<string, string> }) => {
    console.log(`[RetroArch] Core options ready:`, data.core_options);

    if (data.session_id === sessionId.value) {
      // Update the core options ref
      coreOptions.value = data.core_options;
      console.log(`[RetroArch] Updated ${Object.keys(data.core_options).length} core options dynamically`);
    }
  });
}

async function stopSession() {
  console.log("[RetroArch] Stopping session");

  if (sessionId.value) {
    try {
      await retroarchApi.stopSession({ sessionId: sessionId.value });
    } catch (err) {
      console.error("[RetroArch] Failed to stop session:", err);
    }
  }

  if (peerConnection.value) {
    peerConnection.value.close();
    peerConnection.value = null;
  }

  if (socket.value) {
    socket.value.disconnect();
    socket.value = null;
  }
}

function handleKeyDown(event: KeyboardEvent) {
  if (!socket.value || !sessionId.value) return;

  // Prevent default browser behavior for game controls
  if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", " "].includes(event.key)) {
    event.preventDefault();
  }

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "keydown",
      key: event.key,
      code: event.code,
      timestamp: Date.now(),
    },
  });
}

function handleKeyUp(event: KeyboardEvent) {
  if (!socket.value || !sessionId.value) return;

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "keyup",
      key: event.key,
      code: event.code,
      timestamp: Date.now(),
    },
  });
}

function handleMouseMove(event: MouseEvent) {
  if (!socket.value || !sessionId.value || !videoRef.value || !touchscreenRegion.value) return;

  // Throttle mousemove events to prevent flooding
  const now = Date.now();
  if (now - lastMouseMoveTime < MOUSE_MOVE_THROTTLE_MS) {
    return;
  }
  lastMouseMoveTime = now;

  const rect = videoRef.value.getBoundingClientRect();
  const { x_offset, y_offset, width, height } = touchscreenRegion.value;

  // Calculate touchscreen zone size in pixels
  const touchscreenWidthPx = rect.width * width;
  const touchscreenHeightPx = rect.height * height;

  let touchPixelX: number;
  let touchPixelY: number;

  if (isPointerLocked.value) {
    // When pointer is locked, accumulate relative movements
    virtualMouseX.value += event.movementX;
    virtualMouseY.value += event.movementY;

    // Clamp to touchscreen zone bounds
    virtualMouseX.value = Math.max(0, Math.min(touchscreenWidthPx, virtualMouseX.value));
    virtualMouseY.value = Math.max(0, Math.min(touchscreenHeightPx, virtualMouseY.value));

    touchPixelX = virtualMouseX.value;
    touchPixelY = virtualMouseY.value;
  } else {
    // When pointer is not locked, use absolute position
    const relX = (event.clientX - rect.left) / rect.width;
    const relY = (event.clientY - rect.top) / rect.height;

    // Check if in touchscreen region
    if (
      relX < x_offset ||
      relX > x_offset + width ||
      relY < y_offset ||
      relY > y_offset + height
    ) {
      return; // Outside touchscreen region
    }

    // Calculate mouse position in pixels within touchscreen zone
    touchPixelX = (relX - x_offset) * rect.width;
    touchPixelY = (relY - y_offset) * rect.height;
  }

  // Normalize coordinates to 0-1 range for backend
  const normalizedX = touchPixelX / touchscreenWidthPx;
  const normalizedY = touchPixelY / touchscreenHeightPx;

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "mousemove",
      x: normalizedX,
      y: normalizedY,
      timestamp: Date.now(),
    },
  });
}

function handleMouseDown(event: MouseEvent) {
  if (!socket.value || !sessionId.value || !videoRef.value || !touchscreenRegion.value) return;

  event.preventDefault();

  // Get mouse position relative to video element
  const rect = videoRef.value.getBoundingClientRect();
  const relX = (event.clientX - rect.left) / rect.width;
  const relY = (event.clientY - rect.top) / rect.height;

  // Check if in touchscreen region
  const { x_offset, y_offset, width, height } = touchscreenRegion.value;
  if (
    relX < x_offset ||
    relX > x_offset + width ||
    relY < y_offset ||
    relY > y_offset + height
  ) {
    return; // Outside touchscreen region
  }

  function curve(v: number): number {
    const sign = Math.sign(v);
    const abs = Math.abs(v);
    return sign * Math.pow(abs, 1.4); // 1.2–1.5 à tester
  }

  // If not locked yet, request pointer lock (hide cursor)
  if (!isPointerLocked.value) {
    // Initialize virtual position to current mouse position within touchscreen zone
    const touchscreenWidthPx = rect.width * width;
    const touchscreenHeightPx = rect.height * height;
    virtualMouseX.value = (relX - x_offset) * rect.width;
    virtualMouseY.value = (relY - y_offset) * rect.height;
    virtualMouseX.value = curve(virtualMouseX.value);
    virtualMouseY.value = curve(virtualMouseY.value);

    videoRef.value.requestPointerLock();
    return; // Don't send click when requesting lock
  }

  // Send click only if pointer is already locked
  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "mousedown",
      button: event.button,
      timestamp: Date.now(),
    },
  });
}

function handleMouseUp(event: MouseEvent) {
  if (!socket.value || !sessionId.value || !videoRef.value || !touchscreenRegion.value) return;

  event.preventDefault();

  // Only send mouseup if pointer is locked
  if (!isPointerLocked.value) return;

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "mouseup",
      button: event.button,
      timestamp: Date.now(),
    },
  });
}

function exitToGameDetails() {
  router.push({ name: ROUTES.ROM, params: { rom: props.rom.id } });
}

// Container mouse move handler to show/hide menu
function handleContainerMouseMove() {
  if (playerMenuRef.value) {
    playerMenuRef.value.handleMouseMove();
  }
}

// Menu handlers
function sendCommand(command: string) {
  if (!socket.value || !sessionId.value) {
    console.error("[RetroArch] Cannot send command: socket or sessionId not available");
    return;
  }

  socket.value.emit("retroarch-command", {
    session_id: sessionId.value,
    command: command,
  });

  console.log(`[RetroArch] Sent command: ${command}`);
}

function handleQuickSave() {
  sendCommand("SAVESTATE");
}

function handleQuickLoad() {
  sendCommand("LOADSTATE");
}

function handleRestart() {
  sendCommand("RESET");
}

function handleScreenshot() {
  sendCommand("SCREENSHOT");
}

function handleTogglePause() {
  sendCommand("PAUSE_TOGGLE");
}

function handleSaveAndQuit() {
  sendCommand("SAVE_AND_QUIT");
  // Wait a bit for save to complete, then exit
  setTimeout(() => {
    exitToGameDetails();
  }, 1000);
}

function handleSettingsChanged(newSettings: any) {
  if (!socket.value || !sessionId.value) {
    console.error("[RetroArch] Cannot send settings: socket or sessionId not available");
    return;
  }

  // Send each changed setting to RetroArch
  for (const [optionName, optionValue] of Object.entries(newSettings)) {
    socket.value.emit("retroarch-set-core-option", {
      session_id: sessionId.value,
      option_name: optionName,
      option_value: optionValue,
    });
  }

  console.log("[RetroArch] Settings changed:", newSettings);
}
</script>

<template>
  <div
    ref="containerRef"
    id="retroarch-player"
    @keydown="handleKeyDown"
    @keyup="handleKeyUp"
    @mousemove="handleContainerMouseMove"
    tabindex="0"
    class="retroarch-container"
  >
    <!-- Loading/Error Overlay -->
    <div v-if="isLoading || error" class="status-overlay">
      <v-progress-circular
        v-if="isLoading && !error"
        indeterminate
        size="64"
        color="primary"
      />
      <v-icon v-if="error" size="64" color="error">mdi-alert-circle</v-icon>
      <p class="text-h6 mt-4">{{ statusMessage }}</p>
      <v-btn v-if="error" color="primary" @click="exitToGameDetails" class="mt-4">
        Return to Game Details
      </v-btn>
    </div>

    <!-- Video Stream -->
    <video
      ref="videoRef"
      autoplay
      playsinline
      class="game-video"
      :class="{ hidden: isLoading || error }"
      @mousemove="handleMouseMove"
      @mousedown="handleMouseDown"
      @mouseup="handleMouseUp"
    />

    <!-- Player Menu (EmulatorJS-like UI) -->
    <PlayerMenu
      ref="playerMenuRef"
      v-if="!isLoading && !error && sessionId && socket"
      :rom="rom"
      :core="core"
      :session-id="sessionId"
      :socket="socket"
      :core-options-from-backend="coreOptions"
      :is-fullscreen="gameControls.isFullscreen.value"
      @fullscreen="gameControls.toggleFullscreen(containerRef)"
      @quick-save="handleQuickSave"
      @quick-load="handleQuickLoad"
      @restart="handleRestart"
      @screenshot="handleScreenshot"
      @toggle-pause="handleTogglePause"
      @save-and-quit="handleSaveAndQuit"
      @settings-changed="handleSettingsChanged"
      @exit="exitToGameDetails"
    />

    <!-- Gamepad Connected Indicator (bottom left) -->
    <div v-if="!isLoading && !error && gameControls.gamepadConnected.value" class="gamepad-indicator">
      <v-chip size="small" color="success">
        <v-icon start size="small">mdi-controller</v-icon>
        Gamepad Connected
      </v-chip>
    </div>
  </div>
</template>

<style scoped>
.retroarch-container {
  position: relative;
  width: 100%;
  height: 100vh;
  background: #000;
  overflow: hidden;
  outline: none;
  display: flex;
  align-items: center;
  justify-content: center;
}

.game-video {
  object-fit: contain;
}

/* Portrait orientation: fill width, auto height with max constraint */
@media (orientation: portrait) {
  .game-video {
    width: 100vw;
    max-height: 100vh;
  }
}

/* Landscape orientation: fill height, auto width with max constraint */
@media (orientation: landscape) {
  .game-video {
    height: 100vh;
    max-width: 100vw;
  }
}

.game-video.hidden {
  display: none;
}

.status-overlay {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  text-align: center;
  color: white;
  z-index: 10;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.gamepad-indicator {
  position: absolute;
  bottom: 1rem;
  left: 1rem;
  z-index: 90;
  opacity: 0.9;
}

.mouse-hint {
  position: absolute;
  bottom: 2rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 15;
  animation: pulse 2s ease-in-out infinite;
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.6;
  }
}
</style>