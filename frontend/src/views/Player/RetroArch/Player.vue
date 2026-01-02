/**
 * RetroArch WebRTC Player Component
 *
 * Main player component for RetroArch cloud gaming streaming.
 * Handles WebRTC connection, input forwarding, and session lifecycle.
 *
 * Features:
 * - WebRTC video/audio streaming from server-side RetroArch
 * - Keyboard and mouse input forwarding via SocketIO
 * - Gamepad support via useGameControls composable
 * - Touchscreen support for DS/3DS games with absolute coordinates
 * - EmulatorJS-style menu overlay for settings and commands
 *
 * @component
 */
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch, nextTick, computed } from "vue";
import { useRouter } from "vue-router";
import type { FirmwareSchema, SaveSchema, StateSchema } from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import retroarchApi from "@/services/api/retroarch";
import screenshotApi from "@/services/api/screenshot";
import storeConfig from "@/stores/config";
import storeLanguage from "@/stores/language";
import type { DetailedRom } from "@/stores/roms";
import { io, Socket } from "socket.io-client";
import { storeToRefs } from "pinia";
import { useGameControls } from "./useGameControls";
import PlayerMenu from "./PlayerMenu.vue";

/** Component props */
const props = defineProps<{
  /** ROM data including id, name, and metadata */
  rom: DetailedRom;
  /** Optional save file to load on startup */
  save: SaveSchema | null;
  /** Optional save state to load on startup */
  state: StateSchema | null;
  /** Libretro core name to use for emulation */
  core: string;
  /** Optional firmware/BIOS file to use */
  firmware: FirmwareSchema | null;
  /** Whether to enter fullscreen when the player loads */
  fullscreenOnPlay: boolean;
}>();

// Vue Router and store references
const router = useRouter();
const configStore = storeConfig();
const { config } = storeToRefs(configStore);
const languageStore = storeLanguage();
const { selectedLanguage } = storeToRefs(languageStore);

// DOM element refs
/** Video element for WebRTC stream display */
const videoRef = ref<HTMLVideoElement>();
/** Container element for fullscreen and event handling */
const containerRef = ref<HTMLDivElement>();
/** Player menu component ref for programmatic control */
const playerMenuRef = ref<InstanceType<typeof PlayerMenu>>();

// Session state
/** Current session ID from backend */
const sessionId = ref<string | null>(null);
/** WebRTC peer connection for video/audio streaming */
const peerConnection = ref<RTCPeerConnection | null>(null);
/** SocketIO connection for real-time input and commands */
const socket = ref<Socket | null>(null);
/** Status message displayed during loading/errors */
const statusMessage = ref<string>("Initializing RetroArch...");
/** Whether session is currently loading */
const isLoading = ref(true);
/** Error message if session failed to start */
const error = ref<string | null>(null);

/**
 * Touchscreen region for DS/3DS cores.
 * Coordinates are normalized (0-1) relative to video element.
 */
const touchscreenRegion = ref<{
  x_offset: number;
  y_offset: number;
  width: number;
  height: number;
} | null>(null);

/** Core-specific options loaded from backend config */
const coreOptions = ref<Record<string, string>>({});

/** Whether to show the screenshot flash animation */
const showFlash = ref(false);

/** Whether to rotate the video (horizontal core in portrait mode) */
const needsRotation = ref(false);
/** User toggle to override rotation (XOR with needsRotation) */
const rotationToggled = ref(false);
/** Effective rotation state (needsRotation XOR rotationToggled - toggle inverts default) */
const effectiveRotation = computed(() => needsRotation.value !== rotationToggled.value);

/** Timestamp of last mouse move event (for throttling) */
let lastMouseMoveTime = 0;
/** Minimum ms between mouse move events (~120 FPS) */
const MOUSE_MOVE_THROTTLE_MS = 8;

/** Timestamp of last touch move event (for throttling) */
let lastTouchMoveTime = 0;
/** Minimum ms between touch move events (~120 FPS) */
const TOUCH_MOVE_THROTTLE_MS = 8;

/** Whether touch input is currently active (finger down) */
const isTouching = ref(false);

/** Game controls composable (gamepad, fullscreen) */
const gameControls = useGameControls(() =>
  sessionId.value && socket.value
    ? { sessionId: sessionId.value, socket: socket.value }
    : null
)

// Watch for loading completion to trigger fullscreen if enabled
watch(isLoading, async (loading: boolean) => {
  if (!loading && !error.value && props.fullscreenOnPlay) {
    // Wait for next tick to ensure containerRef is available
    await nextTick();
    if (containerRef.value) {
      gameControls.toggleFullscreen(containerRef.value);
    }
  }
});

onMounted(async () => {
  try {
    await startSession();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "Failed to start session";
    statusMessage.value = error.value;
    isLoading.value = false;
  }
});

onBeforeUnmount(async () => {
  await stopSession();
});

// Store ICE servers from backend for WebRTC
const iceServersFromBackend = ref<RTCIceServer[]>([]);

async function startSession() {
  try {
    statusMessage.value = "Starting RetroArch session...";

    // Detect screen dimensions (use physical screen size for real devices)
    const screenWidth = window.screen.width;
    const screenHeight = window.screen.height;

    // Start session with screen dimensions and user language
    const { data } = await retroarchApi.startSession({
      romId: props.rom.id,
      core: props.core,
      saveId: props.save?.id,
      stateId: props.state?.id,
      screenWidth,
      screenHeight,
      firmwareId: props.firmware?.id,
      language: selectedLanguage.value.value,
    });

    sessionId.value = data.session_id;
    touchscreenRegion.value = data.touchscreen_region || null;
    coreOptions.value = data.core_options || {};
    needsRotation.value = data.needs_rotation || false;

    // Store ICE servers from backend (includes TURN if configured)
    if (data.ice_servers && data.ice_servers.length > 0) {
      iceServersFromBackend.value = data.ice_servers.map((server: any) => ({
        urls: server.urls,
        username: server.username || undefined,
        credential: server.credential || undefined,
      }));
    }

    statusMessage.value = "Connecting to signaling server...";

    // 2. Connect SocketIO first and wait for connection
    await connectSocket();

    statusMessage.value = "Setting up WebRTC connection...";

    // 3. Setup WebRTC after socket is connected
    await setupWebRTC(data.webrtc_offer);

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
  // Use ICE servers from backend (includes TURN if configured), fallback to config or STUN
  const iceServers: RTCIceServer[] = iceServersFromBackend.value.length > 0
    ? iceServersFromBackend.value
    : config.value.EJS_NETPLAY_ICE_SERVERS || [{ urls: "stun:stun.l.google.com:19302" }];

  const pc = new RTCPeerConnection({
    iceServers,
    iceCandidatePoolSize: 10,
  });
  peerConnection.value = pc;

  // Receive video/audio stream
  pc.ontrack = (event) => {
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
      socket.value.emit("retroarch-ice-candidate", {
        session_id: sessionId.value,
        candidate: event.candidate.toJSON(),
      });
    }
  };

  // Connection state changes
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === "connected") {
      statusMessage.value = "Playing...";
    } else if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      console.error("[RetroArch] WebRTC connection failed:", pc.connectionState);
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

function connectSocket(): Promise<void> {
  return new Promise((resolve, reject) => {
    socket.value = io({
      path: "/netplay/socket.io",
      transports: ["websocket"],
    });

    const timeout = setTimeout(() => {
      reject(new Error("Socket connection timeout"));
    }, 10000);

    socket.value.on("connect", () => {
      clearTimeout(timeout);
      if (sessionId.value) {
        socket.value?.emit("join", sessionId.value);
      }
      resolve();
    });

    socket.value.on("connect_error", (err: Error) => {
      clearTimeout(timeout);
      console.error("[RetroArch] SocketIO connection error:", err);
      reject(err);
    });

    // Listen for core options updates from backend
    socket.value.on("retroarch-core-options-ready", (data: { session_id: string; core_options: Record<string, string> }) => {
      if (data.session_id === sessionId.value) {
        coreOptions.value = data.core_options;
      }
    });

    // Listen for screenshot data from backend
    socket.value.on("retroarch-screenshot", async (data: { session_id: string; screenshot: string }) => {
      if (data.session_id === sessionId.value && data.screenshot) {
        await handleScreenshotReceived(data.screenshot);
      }
    });
  });
}

async function handleScreenshotReceived(screenshotBase64: string) {
  try {
    // Convert base64 to blob
    const byteCharacters = atob(screenshotBase64);
    const byteNumbers = new Array(byteCharacters.length);
    for (let i = 0; i < byteCharacters.length; i++) {
      byteNumbers[i] = byteCharacters.charCodeAt(i);
    }
    const byteArray = new Uint8Array(byteNumbers);
    const blob = new Blob([byteArray], { type: "image/png" });

    // Create a File object with timestamp name
    const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
    const filename = `screenshot-${props.rom.name}-${timestamp}.png`;
    const file = new File([blob], filename, { type: "image/png" });

    // Download the screenshot
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);

    // Upload screenshot to RomM
    const results = await screenshotApi.uploadScreenshots({
      rom: props.rom,
      screenshotsToUpload: [{ screenshotFile: file }],
    });

    // Check if upload was successful
    const result = results[0];
    if (result.status === "fulfilled") {
      console.log("[RetroArch] Screenshot saved successfully:", result.value);
    } else {
      console.error("[RetroArch] Failed to save screenshot:", result.reason);
    }
  } catch (err) {
    console.error("[RetroArch] Error processing screenshot:", err);
  }
}

async function stopSession() {
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
  // Always show menu bar on mouse movement over video
  handleContainerMouseMove();

  if (!socket.value || !sessionId.value || !videoRef.value || !touchscreenRegion.value) return;

  // Throttle mousemove events to prevent flooding
  const now = Date.now();
  if (now - lastMouseMoveTime < MOUSE_MOVE_THROTTLE_MS) {
    return;
  }
  lastMouseMoveTime = now;

  const rect = videoRef.value.getBoundingClientRect();
  const { x_offset, y_offset, width, height } = touchscreenRegion.value;
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

  // Normalize coordinates within the touchscreen region (0-1 range)
  const normalizedX = (relX - x_offset) / width;
  const normalizedY = (relY - y_offset) / height;

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
  // Always show menu bar on click over video
  handleContainerMouseMove();

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

  // Normalize coordinates within the touchscreen region (0-1 range)
  const normalizedX = (relX - x_offset) / width;
  const normalizedY = (relY - y_offset) / height;

  // Send position first, then mousedown (like touch events)
  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "mousemove",
      x: normalizedX,
      y: normalizedY,
      timestamp: Date.now(),
    },
  });

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

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "mouseup",
      button: event.button,
      timestamp: Date.now(),
    },
  });
}

/**
 * Calculate normalized touch coordinates within the touchscreen region.
 * Returns null if the touch is outside the touchscreen region.
 */
function calculateTouchCoordinates(touch: Touch): { x: number; y: number } | null {
  if (!videoRef.value || !touchscreenRegion.value) return null;

  const rect = videoRef.value.getBoundingClientRect();
  const { x_offset, y_offset, width, height } = touchscreenRegion.value;

  // Get touch position relative to video element (0-1 range)
  const relX = (touch.clientX - rect.left) / rect.width;
  const relY = (touch.clientY - rect.top) / rect.height;

  // Check if touch is within the touchscreen region
  if (
    relX < x_offset ||
    relX > x_offset + width ||
    relY < y_offset ||
    relY > y_offset + height
  ) {
    return null; // Outside touchscreen region
  }

  // Normalize coordinates within the touchscreen region (0-1 range)
  const normalizedX = (relX - x_offset) / width;
  const normalizedY = (relY - y_offset) / height;

  return { x: normalizedX, y: normalizedY };
}

/**
 * Handle touch start event - direct touch input for pointer-based cores.
 * This bypasses the pointer lock mechanism for a more natural touch experience.
 */
function handleTouchStart(event: TouchEvent) {
  if (!socket.value || !sessionId.value || !touchscreenRegion.value) return;

  // Prevent default to avoid mouse emulation and scrolling
  event.preventDefault();

  const touch = event.touches[0];
  if (!touch) return;

  const coords = calculateTouchCoordinates(touch);
  if (!coords) return; // Touch outside touchscreen region

  isTouching.value = true;

  // Send touch position first (move to touch location)
  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "touchmove",
      x: coords.x,
      y: coords.y,
      timestamp: Date.now(),
    },
  });

  // Then send touch down (equivalent to mouse down)
  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "touchstart",
      x: coords.x,
      y: coords.y,
      button: 0,
      timestamp: Date.now(),
    },
  });
}

/**
 * Handle touch move event - track finger movement on touchscreen.
 */
function handleTouchMove(event: TouchEvent) {
  if (!socket.value || !sessionId.value || !touchscreenRegion.value || !isTouching.value) return;

  // Prevent default to avoid scrolling
  event.preventDefault();

  // Throttle touchmove events to prevent flooding
  const now = Date.now();
  if (now - lastTouchMoveTime < TOUCH_MOVE_THROTTLE_MS) {
    return;
  }
  lastTouchMoveTime = now;

  const touch = event.touches[0];
  if (!touch) return;

  const coords = calculateTouchCoordinates(touch);
  if (!coords) return;

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "touchmove",
      x: coords.x,
      y: coords.y,
      timestamp: Date.now(),
    },
  });
}

/**
 * Handle touch end event - finger lifted from touchscreen.
 */
function handleTouchEnd(event: TouchEvent) {
  if (!socket.value || !sessionId.value || !isTouching.value) return;

  // Prevent default to avoid mouse emulation
  event.preventDefault();

  isTouching.value = false;

  socket.value.emit("retroarch-input", {
    session_id: sessionId.value,
    event: {
      type: "touchend",
      button: 0,
      timestamp: Date.now(),
    },
  });
}

/**
 * Handle touch cancel event - touch interrupted (e.g., by system gesture).
 */
function handleTouchCancel(event: TouchEvent) {
  handleTouchEnd(event);
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
}

function handleQuickSave() {
  sendCommand("SAVESTATE");
}

function handleQuickLoad() {
  sendCommand("LOADSTATE");
}

function handleLoadState(stateId: number) {
  // Send LOADSTATE command with state_id to load a specific state from RomM
  if (!socket.value || !sessionId.value) {
    console.error("[RetroArch] Cannot send command: socket or sessionId not available");
    return;
  }

  socket.value.emit("retroarch-command", {
    session_id: sessionId.value,
    command: "LOADSTATE",
    state_id: stateId,
  });
}

function handleRestart() {
  sendCommand("RESET");
}

function handleScreenshot() {
  // Trigger flash animation
  showFlash.value = true;
  setTimeout(() => {
    showFlash.value = false;
  }, 150);

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

    <div
      :class="{
        hidden: isLoading || error,
        rotated: effectiveRotation && gameControls.isFullscreen.value
      }"
    >
      <!-- Video Stream -->
      <video
        ref="videoRef"
        autoplay
        playsinline
        class="game-video"
        @mousemove="handleMouseMove"
        @mousedown="handleMouseDown"
        @mouseup="handleMouseUp"
        @touchstart="handleTouchStart"
        @touchmove="handleTouchMove"
        @touchend="handleTouchEnd"
        @touchcancel="handleTouchCancel"
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
        :container="containerRef"
        :needs-rotation="effectiveRotation && gameControls.isFullscreen.value"
        @fullscreen="gameControls.toggleFullscreen(containerRef)"
        @toggle-rotation="rotationToggled = !rotationToggled"
        @quick-save="handleQuickSave"
        @quick-load="handleQuickLoad"
        @load-state="handleLoadState"
        @restart="handleRestart"
        @screenshot="handleScreenshot"
        @toggle-pause="handleTogglePause"
        @save-and-quit="handleSaveAndQuit"
        @settings-changed="handleSettingsChanged"
        @exit="exitToGameDetails"
      />
    </div>

    <!-- Gamepad Connected Indicator (bottom left) -->
    <div v-if="!isLoading && !error && gameControls.gamepadConnected.value" class="gamepad-indicator">
      <v-chip size="small" color="success">
        <v-icon start size="small">mdi-controller</v-icon>
        Gamepad Connected
      </v-chip>
    </div>

    <!-- Screenshot Flash Overlay -->
    <div v-if="showFlash" class="screenshot-flash" />
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

/* Portrait mode: align video to top */
@media (orientation: portrait) {
  .retroarch-container {
    align-items: flex-start;
  }
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

.hidden {
  display: none;
}

/* Rotated video for horizontal cores in portrait fullscreen mode */
/* Like watching a video on a phone - rotate 90° to fill the screen */
.rotated {
  rotate: 90deg;

  position: absolute;
  top: 0;
  bottom: 0;
  right: 0;

  display: flex;
  justify-content: center;

  /* Swap dimensions: width becomes height, height becomes width */
  min-width: 100vh;
  min-height: 100vw;
}

.rotated>.game-video {
  height: 100vw;
  width: min-content;
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

.screenshot-flash {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  background: white;
  pointer-events: none;
  z-index: 100;
  animation: flash-fade 150ms ease-out forwards;
}

@keyframes flash-fade {
  0% {
    opacity: 0.8;
  }
  100% {
    opacity: 0;
  }
}
</style>