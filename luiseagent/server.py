from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import queue
import sys
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING, Union, cast

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential
from azure.ai.voicelive.aio import AgentSessionConfig, connect
from azure.ai.voicelive.models import (
    AvatarConfig,
    AvatarConfigTypes,
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureStandardVoice,
    AzureSemanticVadMultilingual,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import pyaudio
import uvicorn

if TYPE_CHECKING:
    from azure.ai.voicelive.aio import VoiceLiveConnection

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)
load_dotenv(os.path.join(SCRIPT_DIR, ".env"), override=True)

os.makedirs(os.path.join(SCRIPT_DIR, "logs"), exist_ok=True)
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

logging.basicConfig(
    handlers=[
        logging.FileHandler(os.path.join(SCRIPT_DIR, "logs", f"{timestamp}_server.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ENDPOINT = os.environ.get("AZURE_VOICELIVE_ENDPOINT", "https://agenttalks2026.services.ai.azure.com/")
VOICE = os.environ.get("AZURE_VOICELIVE_VOICE", "en-US-AvaNeural")
TRANSCRIPTION_MODEL = os.environ.get("AZURE_VOICELIVE_TRANSCRIPTION_MODEL", "azure-speech")
AVATAR_CHARACTER = os.environ.get("AZURE_AVATAR_CHARACTER", "layla")
AVATAR_MODEL = os.environ.get("AZURE_AVATAR_MODEL", "vasa-1")
AGENT_NAME = os.environ.get("AZURE_VOICELIVE_AGENT_ID", "AgentTalks2026")
PROJECT_NAME = os.environ.get("AZURE_VOICELIVE_PROJECT_NAME", "AgentTalks2026")
CONVERSATION_ID = os.environ.get("AZURE_VOICELIVE_CONVERSATION_ID")
FOUNDRY_RESOURCE_OVERRIDE = os.environ.get("AZURE_VOICELIVE_FOUNDRY_RESOURCE_OVERRIDE")
AUTH_IDENTITY_CLIENT_ID = os.environ.get("AZURE_VOICELIVE_AUTH_IDENTITY_CLIENT_ID")

VAD_THRESHOLD = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.74"))
VAD_PREFIX_PADDING_MS = int(os.environ.get("VOICE_VAD_PREFIX_PADDING_MS", "220"))
VAD_SILENCE_MS = int(os.environ.get("VOICE_VAD_SILENCE_MS", "900"))
MIC_DUCK_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_SECONDS", "0.45"))
MIC_DUCK_POST_RESPONSE = float(os.environ.get("VOICE_MIC_DUCK_POST_RESPONSE", "1.5"))
MIC_DUCK_GRACE_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_GRACE_SECONDS", "0.3"))
MIC_DUCK_MIN_TAIL_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_MIN_TAIL_SECONDS", "1.8"))
MIC_DUCK_MAX_TAIL_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_MAX_TAIL_SECONDS", "4.0"))
ECHO_SUPPRESS_WINDOW_SECONDS = float(os.environ.get("VOICE_ECHO_SUPPRESS_WINDOW_SECONDS", "8.0"))
ECHO_SUPPRESS_SIMILARITY = float(os.environ.get("VOICE_ECHO_SUPPRESS_SIMILARITY", "0.8"))

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
}

VOICE_SHORT_NAME_SUFFIX = "Neural"


def build_agent_config() -> AgentSessionConfig:
    cfg: AgentSessionConfig = {
        "agent_name": AGENT_NAME,
        "project_name": PROJECT_NAME,
        "conversation_id": CONVERSATION_ID if CONVERSATION_ID else None,
        "foundry_resource_override": FOUNDRY_RESOURCE_OVERRIDE if FOUNDRY_RESOURCE_OVERRIDE else None,
        "authentication_identity_client_id": (
            AUTH_IDENTITY_CLIENT_ID if AUTH_IDENTITY_CLIENT_ID and FOUNDRY_RESOURCE_OVERRIDE else None
        ),
    }
    # Do not pin agent version so Voice Live resolves the latest published one.
    cfg.pop("agent_version", None)
    return cfg


def build_voice_config(voice_name: str) -> Union[AzureStandardVoice, str]:
    normalized_voice = voice_name.strip()
    if not normalized_voice:
        raise ValueError("AZURE_VOICELIVE_VOICE cannot be empty")

    if normalized_voice.count("-") >= 2 and not normalized_voice.endswith(VOICE_SHORT_NAME_SUFFIX):
        normalized_voice = f"{normalized_voice}{VOICE_SHORT_NAME_SUFFIX}"

    if "-" in normalized_voice:
        return AzureStandardVoice(name=normalized_voice)

    return normalized_voice


def _build_session_request(include_avatar: bool, include_voice: bool = True) -> RequestSession:
    modalities = [Modality.TEXT, Modality.AUDIO]
    if include_avatar:
        modalities.append(Modality.AVATAR)

    kwargs = {
        "modalities": modalities,
        "input_audio_format": InputAudioFormat.PCM16,
        "output_audio_format": OutputAudioFormat.PCM16,
        "turn_detection": ServerVad(
            threshold=VAD_THRESHOLD,
            prefix_padding_ms=VAD_PREFIX_PADDING_MS,
            silence_duration_ms=VAD_SILENCE_MS,
        ),
        "input_audio_echo_cancellation": AudioEchoCancellation(),
        "input_audio_noise_reduction": AudioNoiseReduction(type="azure_deep_noise_suppression"),
        "input_audio_transcription": AudioInputTranscriptionOptions(model=TRANSCRIPTION_MODEL),
    }

    if include_voice:
        kwargs["voice"] = build_voice_config(VOICE)

    if include_avatar:
        kwargs["avatar"] = AvatarConfig(
            type=AvatarConfigTypes.PHOTO_AVATAR,
            character=AVATAR_CHARACTER,
            model=AVATAR_MODEL,
        )

    return RequestSession(**kwargs)


app = FastAPI(title="Foundry Voice Agent Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/ws")
async def voice_endpoint(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin", "")
    if origin and origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    await websocket.accept()
    logger.info("Frontend connected from '%s'", origin)

    session = VoiceSession(websocket)
    try:
        await session.run()
    except WebSocketDisconnect:
        logger.info("Frontend disconnected")
    except Exception as exc:
        logger.exception("Session error: %s", exc)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(exc)}))
        except Exception:
            pass
    finally:
        await session.cleanup()


class VoiceSession:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.connection: Optional[VoiceLiveConnection] = None
        self.ap: Optional[AudioProc] = None
        self._running = True
        self._active_response = False
        self._response_api_done = False
        self._audio_transcript_acc = ""
        self._last_spoken_chars = 0
        self._last_assistant_text = ""
        self._last_assistant_spoke_at = 0.0
        self._last_assistant_sent_norm = ""
        self._last_assistant_sent_at = 0.0
        self._pending_barge_in = False
        self._avatar_answer_event = asyncio.Event()
        self._avatar_server_sdp: Optional[str] = None
        self._avatar_supported = False
        self._avatar_connect_attempted = False

    @staticmethod
    def _encode_audio_chunk(audio_delta: object) -> Optional[str]:
        if audio_delta is None:
            return None
        if isinstance(audio_delta, bytes):
            return base64.b64encode(audio_delta).decode("utf-8")
        if isinstance(audio_delta, bytearray):
            return base64.b64encode(bytes(audio_delta)).decode("utf-8")
        if isinstance(audio_delta, str):
            return audio_delta
        return None

    @staticmethod
    def _norm_text(text: str) -> str:
        compact = " ".join(text.lower().split())
        return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in compact).strip()

    def _is_probable_echo(self, transcript: str) -> bool:
        if not transcript or not self._last_assistant_text:
            return False
        if (time.monotonic() - self._last_assistant_spoke_at) > ECHO_SUPPRESS_WINDOW_SECONDS:
            return False

        user_norm = self._norm_text(transcript)
        ai_norm = self._norm_text(self._last_assistant_text)
        if not user_norm or not ai_norm:
            return False
        if len(user_norm) >= 8 and user_norm in ai_norm:
            return True

        ratio = difflib.SequenceMatcher(None, user_norm, ai_norm).ratio()
        return ratio >= ECHO_SUPPRESS_SIMILARITY

    async def _send(self, msg: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:
            logger.debug("Ignored websocket send failure for message type=%s", msg.get("type"))

    async def run(self) -> None:
        await self._send({"type": "status", "value": "connecting"})

        if not ENDPOINT or not AGENT_NAME or not PROJECT_NAME:
            raise RuntimeError(
                "Set AZURE_VOICELIVE_ENDPOINT, AZURE_VOICELIVE_AGENT_ID, and AZURE_VOICELIVE_PROJECT_NAME in luiseagent/.env"
            )

        credential: Union[AzureKeyCredential, AsyncTokenCredential] = AzureCliCredential()

        async with connect(
            endpoint=ENDPOINT,
            credential=credential,
            api_version="2026-01-01-preview",
            agent_config=build_agent_config(),
        ) as conn:
            self.connection = conn
            self.ap = AudioProc(conn)

            await self._configure_session()
            await self._send({"type": "connected"})

            frontend_task = asyncio.create_task(self._listen_frontend())
            try:
                async for event in conn:
                    if not self._running:
                        break
                    await self._handle(event)
            finally:
                frontend_task.cancel()
                await credential.close()

    async def _listen_frontend(self) -> None:
        try:
            while self._running:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "stop":
                    self._running = False
                    break

                if msg_type == "input_audio":
                    audio = msg.get("audio", "")
                    if audio and self.connection is not None:
                        await self.connection.input_audio_buffer.append(audio=audio)
                    continue

                if msg_type == "avatar_offer":
                    if not self._avatar_supported:
                        await self._send(
                            {
                                "type": "avatar_error",
                                "message": "Avatar is not available for this agent session.",
                            }
                        )
                        continue

                    await self._send({"type": "avatar_connecting"})
                    client_sdp = msg.get("sdp", "")
                    server_sdp = await self._send_avatar_connect(client_sdp)
                    if server_sdp:
                        await self._send({"type": "avatar_answer", "sdp": server_sdp})
                    else:
                        await self._send(
                            {
                                "type": "avatar_error",
                                "message": "Could not establish avatar connection for this agent session.",
                            }
                        )
        except WebSocketDisconnect:
            self._running = False
        except Exception:
            logger.exception("Frontend listener failed")

    async def _send_avatar_connect(self, client_sdp: str) -> Optional[str]:
        conn = self.connection
        if conn is None:
            return None

        self._avatar_server_sdp = None
        self._avatar_answer_event.clear()
        self._avatar_connect_attempted = True

        try:
            await conn.send({"type": "session.avatar.connect", "client_sdp": client_sdp})
            await asyncio.wait_for(self._avatar_answer_event.wait(), timeout=30.0)
            return self._avatar_server_sdp
        except Exception as exc:
            logger.warning("Avatar connect failed: %s", exc)
            return None

    async def _configure_session(self) -> None:
        assert self.connection is not None

        attempts = (
            (True, True),
            (False, True),
            (False, False),
        )
        last_exc: Optional[Exception] = None

        for include_avatar, include_voice in attempts:
            try:
                await self.connection.session.update(
                    session=_build_session_request(include_avatar=include_avatar, include_voice=include_voice)
                )
                self._avatar_supported = include_avatar
                logger.info(
                    "Session configured in agent mode (avatar=%s, voice=%s)",
                    include_avatar,
                    include_voice,
                )
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Session configuration attempt failed (avatar=%s, voice=%s): %s",
                    include_avatar,
                    include_voice,
                    exc,
                )

        raise RuntimeError(f"Could not configure agent session for web use: {last_exc}")

    async def _handle(self, event) -> None:
        ap = self.ap
        conn = self.connection
        assert ap is not None and conn is not None

        etype = event.type
        logger.info("EVENT: %s", etype)

        if etype == ServerEventType.SESSION_UPDATED:
            session_avatar = getattr(getattr(event, "session", None), "avatar", None)
            logger.info("Session avatar payload: %s", session_avatar)
            ice_servers = []
            if session_avatar and getattr(session_avatar, "ice_servers", None):
                for ice in session_avatar.ice_servers:
                    ice_servers.append(
                        {
                            "urls": getattr(ice, "urls", []),
                            "username": getattr(ice, "username", ""),
                            "credential": getattr(ice, "credential", ""),
                        }
                    )

            if ice_servers:
                self._avatar_supported = True
                await self._send({"type": "avatar_ice_servers", "servers": ice_servers})
                logger.info("Avatar ICE servers sent to frontend: %d", len(ice_servers))
            else:
                self._avatar_supported = False
                await self._send(
                    {
                        "type": "avatar_error",
                        "message": "Avatar is not available for this agent session.",
                    }
                )

            await self._send({"type": "status", "value": "ready"})

        elif "avatar" in str(etype).lower():
            server_sdp = None
            if hasattr(event, "as_dict"):
                event_dict = event.as_dict()
                server_sdp = event_dict.get("server_sdp")
            if not server_sdp:
                server_sdp = getattr(event, "server_sdp", None)
            if server_sdp:
                self._avatar_server_sdp = server_sdp
                self._avatar_answer_event.set()
                await self._send({"type": "avatar_ready"})

        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            if ap.is_capture_ducked(grace_seconds=MIC_DUCK_GRACE_SECONDS) and not (
                self._active_response and not self._response_api_done
            ):
                logger.info("Ignoring speech_started during mic duck window")
                return

            ap.skip_pending_audio()
            await self._send({"type": "status", "value": "listening"})
            if self._active_response and not self._response_api_done:
                self._pending_barge_in = True

        elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            await self._send({"type": "status", "value": "processing"})

        elif etype == ServerEventType.RESPONSE_CREATED:
            self._active_response = True
            self._response_api_done = False
            self._pending_barge_in = False
            self._audio_transcript_acc = ""
            ap.duck_capture(max(MIC_DUCK_SECONDS, 1.0))
            await self._send({"type": "status", "value": "speaking"})

        elif etype == ServerEventType.RESPONSE_AUDIO_DELTA:
            ap.duck_capture(0.6)
            encoded_audio = self._encode_audio_chunk(getattr(event, "delta", None))
            if encoded_audio:
                await self._send({"type": "audio_chunk", "audio": encoded_audio, "format": "pcm16", "sampleRate": 24000})

        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            ap.duck_capture(1.0)
            self._audio_transcript_acc += getattr(event, "delta", "") or ""

        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            text = getattr(event, "transcript", None) or self._audio_transcript_acc
            self._last_spoken_chars = len(text)
            self._last_assistant_text = text or ""
            self._last_assistant_spoke_at = time.monotonic()
            self._audio_transcript_acc = ""
            ap.duck_capture(MIC_DUCK_POST_RESPONSE + 0.8)
            if text:
                norm = self._norm_text(text)
                now = time.monotonic()
                if norm and norm == self._last_assistant_sent_norm and (now - self._last_assistant_sent_at) < 2.0:
                    logger.info("Suppressed duplicate assistant transcript: %.120s", text)
                else:
                    logger.info("Agent spoke: %.120s", text)
                    await self._send({"type": "transcript", "role": "assistant", "text": text})
                    self._last_assistant_sent_norm = norm
                    self._last_assistant_sent_at = now

        elif etype == ServerEventType.CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED:
            transcript = getattr(event, "transcript", "")
            if transcript:
                if self._is_probable_echo(transcript):
                    logger.info("Suppressed probable echo transcript: %.120s", transcript)
                    self._pending_barge_in = False
                    return
                if self._pending_barge_in and self._active_response and not self._response_api_done:
                    try:
                        await conn.response.cancel()
                        logger.info("Cancelled active response on confirmed barge-in transcript")
                    except Exception:
                        logger.debug("Response cancel failed during barge-in", exc_info=True)
                    self._pending_barge_in = False
                logger.info("User said: %.120s", transcript)
                await self._send({"type": "transcript", "role": "user", "text": transcript})

        elif etype == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_api_done = True
            self._pending_barge_in = False
            tail_guard = min(
                max(MIC_DUCK_POST_RESPONSE + 0.8, MIC_DUCK_MIN_TAIL_SECONDS),
                MIC_DUCK_MAX_TAIL_SECONDS,
            )
            ap.duck_capture(tail_guard)
            logger.info("Post-response mic duck: %.1fs (transcript ~%d chars)", tail_guard, self._last_spoken_chars)
            await self._send({"type": "audio_done"})
            await self._send({"type": "status", "value": "ready"})

        elif etype == ServerEventType.ERROR:
            msg = getattr(event.error, "message", str(event.error))
            if "no active response" not in msg.lower():
                logger.error("VoiceLive error: %s", msg)
                await self._send({"type": "error", "message": msg})

    async def cleanup(self) -> None:
        self._running = False
        if self.ap:
            self.ap.shutdown()


class AudioProc:
    loop: asyncio.AbstractEventLoop

    class _Pkt:
        __slots__ = ("seq_num", "data")

        def __init__(self, seq: int, data: Optional[bytes]) -> None:
            self.seq_num = seq
            self.data = data

    def __init__(self, conn: VoiceLiveConnection) -> None:
        self.conn = conn
        self.pa = pyaudio.PyAudio()
        self.fmt = pyaudio.paInt16
        self.ch = 1
        self.rate = 24000
        self.chunk = 1200
        self.input_stream = None
        self.output_stream = None
        self.pb_queue: queue.Queue[AudioProc._Pkt] = queue.Queue()
        self.pb_base = 0
        self._seq = 0
        self._duck_capture_until = 0.0

    def duck_capture(self, seconds: float) -> None:
        self._duck_capture_until = max(self._duck_capture_until, time.monotonic() + max(seconds, 0.0))

    def is_capture_ducked(self, grace_seconds: float = 0.0) -> bool:
        return time.monotonic() < (self._duck_capture_until + max(grace_seconds, 0.0))

    def start_capture(self) -> None:
        if self.input_stream:
            return
        self.loop = asyncio.get_event_loop()

        def _cb(in_data, _fc, _ti, _sf):
            if time.monotonic() < self._duck_capture_until:
                return (None, pyaudio.paContinue)
            b64 = base64.b64encode(in_data).decode("utf-8")
            asyncio.run_coroutine_threadsafe(self.conn.input_audio_buffer.append(audio=b64), self.loop)
            return (None, pyaudio.paContinue)

        self.input_stream = self.pa.open(
            format=self.fmt,
            channels=self.ch,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk,
            stream_callback=_cb,
        )

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def queue_audio(self, data: Optional[bytes]) -> None:
        self.pb_queue.put(self._Pkt(self._next_seq(), data))

    def skip_pending_audio(self) -> None:
        self.pb_base = self._next_seq()

    def shutdown(self) -> None:
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None
        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None
        self.pa.terminate()


def _check_audio_devices() -> None:
    p = pyaudio.PyAudio()
    try:
        def _has_channels(key: str) -> bool:
            return any(
                cast(Union[int, float], p.get_device_info_by_index(i).get(key, 0) or 0) > 0
                for i in range(p.get_device_count())
            )

        if not _has_channels("maxInputChannels"):
            logger.warning("No audio input device found on backend host")
        if not _has_channels("maxOutputChannels"):
            logger.warning("No audio output device found on backend host")
    finally:
        p.terminate()


if __name__ == "__main__":
    _check_audio_devices()
    print("Foundry Voice Agent Server")
    print("=" * 40)
    print("WebSocket : ws://127.0.0.1:8765/ws")
    print("Health    : http://127.0.0.1:8765/health")
    print("Logs      : luiseagent/logs/")
    print("Press Ctrl+C to stop")
    print()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")