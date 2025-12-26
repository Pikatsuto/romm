<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import type { SaveSchema, StateSchema } from "@/__generated__";
import { ROUTES } from "@/plugins/router";
import retroarchApi from "@/services/api/retroarch";
import storeConfig from "@/stores/config";
import type { DetailedRom } from "@/stores/roms";
import { io, Socket } from "socket.io-client";
import { storeToRefs } from "pinia";

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
const sessionId = ref<string | null>(null);
const peerConnection = ref<RTCPeerConnection | null>(null);
const socket = ref<Socket | null>(null);
const statusMessage = ref<string>("Initializing RetroArch...");
const isLoading = ref(true);
const error = ref<string | null>(null);

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

async function startSession() {
  try {
    statusMessage.value = "Starting RetroArch session...";

    // 1. Start session
    const { data } = await retroarchApi.startSession({
      romId: props.rom.id,
      core: props.core,
      saveId: props.save?.id,
      stateId: props.state?.id,
    });

    sessionId.value = data.session_id;
    console.log("[RetroArch] Session created:", sessionId.value);

    // TODO: Implement full RetroArch daemon integration
    // For now, show a development message
    statusMessage.value = "RetroArch Streaming - Coming Soon!";
    error.value = "RetroArch streaming is still under development. The backend daemon to launch RetroArch processes and stream video via WebRTC is not yet implemented.";
    isLoading.value = false;

    /*
    // This will be enabled once the RetroArch daemon is implemented:

    statusMessage.value = "Setting up WebRTC connection...";

    // 2. Setup WebRTC
    await setupWebRTC(data.webrtc_offer);

    // 3. Connect SocketIO
    connectSocket();

    statusMessage.value = "Connected! Starting stream...";
    isLoading.value = false;
    */
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
      videoRef.value.play().catch((err) => {
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
  socket.value = io("/netplay/socket.io", {
    transports: ["websocket"],
  });

  socket.value.on("connect", () => {
    console.log("[RetroArch] SocketIO connected");
  });

  socket.value.on("disconnect", () => {
    console.log("[RetroArch] SocketIO disconnected");
  });

  socket.value.on("connect_error", (err) => {
    console.error("[RetroArch] SocketIO connection error:", err);
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

function exitToGameDetails() {
  router.push({ name: ROUTES.ROM, params: { rom: props.rom.id } });
}
</script>

<template>
  <div
    id="retroarch-player"
    @keydown="handleKeyDown"
    @keyup="handleKeyUp"
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
    />

    <!-- Controls Overlay -->
    <div class="controls-overlay" v-if="!isLoading && !error">
      <v-btn icon="mdi-close" color="error" @click="exitToGameDetails" />
    </div>

    <!-- Info Overlay -->
    <div class="info-overlay" v-if="!isLoading && !error">
      <div class="info-chip">
        <v-chip size="small" color="primary">
          <v-icon start>mdi-gamepad-variant</v-icon>
          {{ rom.name }}
        </v-chip>
        <v-chip size="small" color="secondary" class="ml-2">
          <v-icon start>mdi-cpu-64-bit</v-icon>
          {{ core }}
        </v-chip>
      </div>
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
  width: 100%;
  height: 100%;
  object-fit: contain;
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

.controls-overlay {
  position: absolute;
  top: 1rem;
  right: 1rem;
  z-index: 20;
}

.info-overlay {
  position: absolute;
  top: 1rem;
  left: 1rem;
  z-index: 20;
}

.info-chip {
  display: flex;
  align-items: center;
}
</style>