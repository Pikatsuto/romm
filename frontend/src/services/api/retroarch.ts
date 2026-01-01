/**
 * RetroArch Streaming API Service
 *
 * Provides API methods for managing RetroArch cloud gaming sessions,
 * including WebRTC streaming, input forwarding, and session lifecycle.
 * @module services/api/retroarch
 */

import api from "@/services/api";

/**
 * Request payload for starting a RetroArch streaming session.
 */
interface StartSessionRequest {
  /** ROM identifier to load */
  rom_id: number;
  /** Libretro core name (e.g., "snes9x", "mgba") */
  core: string;
  /** Optional save file ID to load */
  save_id?: number;
  /** Optional save state ID to load on startup */
  state_id?: number;
  /** Client screen width for optimal resolution selection */
  screen_width?: number;
  /** Client screen height for optimal resolution selection */
  screen_height?: number;
  /** Optional firmware/BIOS file ID to use */
  firmware_id?: number;
  /** User interface language (e.g., "en_US", "fr_FR") */
  language?: string;
}

/**
 * ICE server configuration for WebRTC NAT traversal.
 */
interface IceServer {
  /** STUN/TURN server URLs */
  urls: string | string[];
  /** Username for TURN authentication */
  username?: string;
  /** Credential for TURN authentication */
  credential?: string;
}

/**
 * Response from starting a RetroArch streaming session.
 */
interface StartSessionResponse {
  /** Unique session identifier */
  session_id: string;
  /** WebRTC SDP offer for establishing the video stream */
  webrtc_offer: string;
  /** Touchscreen region for DS/3DS cores (normalized coordinates) */
  touchscreen_region?: {
    x_offset: number;
    y_offset: number;
    width: number;
    height: number;
  };
  /** Core-specific options loaded from backend config */
  core_options?: Record<string, string>;
  /** ICE servers for WebRTC (includes TURN if configured) */
  ice_servers?: IceServer[];
}

/**
 * Session information returned by the sessions list endpoint.
 */
interface SessionInfoResponse {
  /** Unique session identifier */
  session_id: string;
  /** ROM identifier being played */
  rom_id: number;
  /** Platform slug (e.g., "snes", "gba") */
  platform_slug: string;
  /** Libretro core name */
  core: string;
  /** Session state (starting, running, stopped, error) */
  state: string;
  /** ISO timestamp when session was created */
  created_at: string;
  /** ISO timestamp of last activity */
  last_activity: string;
}

/**
 * Start a new RetroArch streaming session.
 *
 * Initializes a RetroArch instance on the server with the specified ROM
 * and core, and returns a WebRTC offer for establishing the video stream.
 *
 * @param params - Session parameters
 * @param params.romId - ROM identifier to load
 * @param params.core - Libretro core name
 * @param params.saveId - Optional save file ID to load
 * @param params.stateId - Optional save state ID to load on startup
 * @param params.screenWidth - Client screen width for resolution optimization
 * @param params.screenHeight - Client screen height for resolution optimization
 * @returns Promise resolving to session data including WebRTC offer
 */
async function startSession({
  romId,
  core,
  saveId,
  stateId,
  screenWidth,
  screenHeight,
  firmwareId,
  language,
}: {
  romId: number;
  core: string;
  saveId?: number;
  stateId?: number;
  screenWidth?: number;
  screenHeight?: number;
  firmwareId?: number;
  language?: string;
}): Promise<{ data: StartSessionResponse }> {
  const requestData: StartSessionRequest = {
    rom_id: romId,
    core,
  };

  if (saveId) requestData.save_id = saveId;
  if (stateId) requestData.state_id = stateId;
  if (screenWidth) requestData.screen_width = screenWidth;
  if (screenHeight) requestData.screen_height = screenHeight;
  if (firmwareId) requestData.firmware_id = firmwareId;
  if (language) requestData.language = language;

  return api.post("/retroarch/stream/start", requestData);
}

/**
 * Send WebRTC answer SDP to complete the connection.
 *
 * After receiving the offer SDP from startSession, the client creates
 * an answer SDP which must be sent back to establish the WebRTC connection.
 *
 * @param params - Answer parameters
 * @param params.sessionId - Session identifier from startSession
 * @param params.webrtcAnswer - SDP answer string from RTCPeerConnection
 * @returns Promise resolving to status confirmation
 */
async function answerSession({
  sessionId,
  webrtcAnswer,
}: {
  sessionId: string;
  webrtcAnswer: string;
}): Promise<{ data: { status: string; message: string } }> {
  return api.post("/retroarch/stream/answer", {
    session_id: sessionId,
    webrtc_answer: webrtcAnswer,
  });
}

/**
 * Stop a RetroArch streaming session.
 *
 * Terminates the RetroArch process and releases server resources.
 * Should be called when the user exits the player.
 *
 * @param params - Stop parameters
 * @param params.sessionId - Session identifier to stop
 * @returns Promise resolving to status confirmation
 */
async function stopSession({
  sessionId,
}: {
  sessionId: string;
}): Promise<{ data: { status: string; message: string } }> {
  return api.post("/retroarch/stream/stop", {
    session_id: sessionId,
  });
}

/**
 * Send input event to RetroArch via HTTP.
 *
 * Note: For real-time input, prefer using SocketIO for lower latency.
 * This endpoint is provided as a fallback.
 *
 * @param params - Input parameters
 * @param params.sessionId - Session identifier
 * @param params.inputEvent - Input event object (keydown, keyup, mouse, etc.)
 * @returns Promise resolving to status confirmation
 */
async function sendInput({
  sessionId,
  inputEvent,
}: {
  sessionId: string;
  inputEvent: object;
}): Promise<{ data: { status: string } }> {
  return api.post("/retroarch/input", {
    session_id: sessionId,
    input_event: inputEvent,
  });
}

/**
 * Get all active RetroArch streaming sessions.
 *
 * Returns a list of all currently running sessions on the server.
 * Useful for monitoring and administration.
 *
 * @returns Promise resolving to array of session info objects
 */
async function getSessions(): Promise<{ data: SessionInfoResponse[] }> {
  return api.get("/retroarch/sessions");
}

export default {
  startSession,
  answerSession,
  stopSession,
  sendInput,
  getSessions,
};