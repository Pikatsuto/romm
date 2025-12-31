import api from "@/services/api";

interface StartSessionRequest {
  rom_id: number;
  core: string;
  save_id?: number;
  state_id?: number;
}

interface StartSessionResponse {
  session_id: string;
  webrtc_offer: string;
  touchscreen_region?: {
    x_offset: number;
    y_offset: number;
    width: number;
    height: number;
  };
  core_options?: Record<string, string>;
}

interface SessionInfoResponse {
  session_id: string;
  rom_id: number;
  platform_slug: string;
  core: string;
  state: string;
  created_at: string;
  last_activity: string;
}

async function startSession({
  romId,
  core,
  saveId,
  stateId,
}: {
  romId: number;
  core: string;
  saveId?: number;
  stateId?: number;
}): Promise<{ data: StartSessionResponse }> {
  const requestData: StartSessionRequest = {
    rom_id: romId,
    core,
  };

  if (saveId) requestData.save_id = saveId;
  if (stateId) requestData.state_id = stateId;

  return api.post("/retroarch/stream/start", requestData);
}

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

async function stopSession({
  sessionId,
}: {
  sessionId: string;
}): Promise<{ data: { status: string; message: string } }> {
  return api.post("/retroarch/stream/stop", {
    session_id: sessionId,
  });
}

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