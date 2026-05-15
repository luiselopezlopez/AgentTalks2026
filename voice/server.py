"""
General Voice Companion Server
WebSocket bridge between Azure VoiceLive and the React frontend.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import html
import json
import logging
import os
import queue
import re
import socket
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from ipaddress import ip_address, ip_network
from typing import Optional, Union
from urllib.parse import quote_plus, urlparse

import requests
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential
from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AvatarConfig,
    AvatarConfigTypes,
    AudioEchoCancellation,
    AudioInputTranscriptionOptions,
    AudioNoiseReduction,
    AzureStandardVoice,
    FunctionCallOutputItem,
    FunctionTool,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad,
)
from ddgs import DDGS
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import pyaudio
import uvicorn

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
load_dotenv("./.env", override=True)

# ── Logging ──────────────────────────────────────────────────────────────────

if not os.path.exists("logs"):
    os.makedirs("logs")
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

logging.basicConfig(
    handlers=[
        logging.FileHandler(f"logs/{timestamp}_server.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    format="%(asctime)s:%(name)s:%(levelname)s:%(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ENDPOINT = os.environ.get("AZURE_VOICELIVE_ENDPOINT", "")
MODEL = os.environ.get("AZURE_VOICELIVE_MODEL", "gpt-5-mini")
VOICE = os.environ.get("AZURE_VOICELIVE_VOICE", "en-US-AvaNeural")
TRANSCRIPTION_MODEL = os.environ.get("AZURE_VOICELIVE_TRANSCRIPTION_MODEL", "azure-speech")
CAPTURE_SOURCE = os.environ.get("VOICE_CAPTURE_SOURCE", "browser").strip().lower()
AVATAR_CHARACTER = os.environ.get("AZURE_AVATAR_CHARACTER", "layla")
AVATAR_MODEL = os.environ.get("AZURE_AVATAR_MODEL", "vasa-1")
VAD_THRESHOLD = float(os.environ.get("VOICE_VAD_THRESHOLD", "0.74"))
VAD_PREFIX_PADDING_MS = int(os.environ.get("VOICE_VAD_PREFIX_PADDING_MS", "220"))
VAD_SILENCE_MS = int(os.environ.get("VOICE_VAD_SILENCE_MS", "900"))
MIC_DUCK_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_SECONDS", "0.45"))
# Minimum seconds of mic silence to add after a response finishes, on top of the estimated speech duration
MIC_DUCK_POST_RESPONSE = float(os.environ.get("VOICE_MIC_DUCK_POST_RESPONSE", "1.5"))
MIC_DUCK_GRACE_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_GRACE_SECONDS", "0.3"))
MIC_DUCK_MIN_TAIL_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_MIN_TAIL_SECONDS", "1.8"))
MIC_DUCK_MAX_TAIL_SECONDS = float(os.environ.get("VOICE_MIC_DUCK_MAX_TAIL_SECONDS", "4.0"))
ECHO_SUPPRESS_WINDOW_SECONDS = float(os.environ.get("VOICE_ECHO_SUPPRESS_WINDOW_SECONDS", "8.0"))
ECHO_SUPPRESS_SIMILARITY = float(os.environ.get("VOICE_ECHO_SUPPRESS_SIMILARITY", "0.8"))
ENABLE_LOCAL_PLAYBACK = os.environ.get("VOICE_ENABLE_LOCAL_PLAYBACK", "false").lower() in {
    "1", "true", "yes", "on"
}
WEB_TOOL_TIMEOUT_SECONDS = float(os.environ.get("WEB_TOOL_TIMEOUT_SECONDS", "10"))
WEB_TOOL_MAX_RESULTS = int(os.environ.get("WEB_TOOL_MAX_RESULTS", "5"))
WEB_TOOL_MAX_CHARS = int(os.environ.get("WEB_TOOL_MAX_CHARS", "4000"))
WEB_TOOL_HTML_DEBUG_CHARS = int(os.environ.get("WEB_TOOL_HTML_DEBUG_CHARS", "700"))
WEB_TOOL_FOLLOW_RESULTS = int(os.environ.get("WEB_TOOL_FOLLOW_RESULTS", "5"))
WEB_SEARCH_ENGINE = os.environ.get("WEB_SEARCH_ENGINE", "duckduckgo").strip().lower()
WEB_TOOL_USER_AGENT = os.environ.get(
    "WEB_TOOL_USER_AGENT",
    "Mozilla/5.0 (compatible; VoiceCompanionBot/1.0; +https://localhost)",
)
GOOGLE_SEARCH_API_KEY = os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip()
GOOGLE_SEARCH_CX = os.environ.get("GOOGLE_SEARCH_CX", "").strip()
ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://localhost:4173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:4173",
}
PRIVATE_NETWORKS = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
    ip_network("fe80::/10"),
)

VOICE_SHORT_NAME_PATTERN = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z][A-Za-z0-9]+$")


def _markdown_to_safe_html(text: str) -> str:
    safe = html.escape(text or "", quote=True)
    safe = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>',
        safe,
    )
    safe = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", safe)
    safe = re.sub(r"`([^`]+)`", r"<code>\1</code>", safe)

    lines = [line.strip() for line in safe.splitlines()]
    blocks: list[str] = []
    in_list = False
    for line in lines:
        if not line:
            if in_list:
                blocks.append("</ul>")
                in_list = False
            continue

        if line.startswith("- ") or line.startswith("* "):
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            blocks.append(f"<li>{line[2:].strip()}</li>")
            continue

        if in_list:
            blocks.append("</ul>")
            in_list = False
        blocks.append(f"<p>{line}</p>")

    if in_list:
        blocks.append("</ul>")

    return "".join(blocks) if blocks else "<p></p>"


def build_voice_config(voice_name: str) -> Union[AzureStandardVoice, str]:
    normalized_voice = voice_name.strip()
    if not normalized_voice:
        raise ValueError("AZURE_VOICELIVE_VOICE cannot be empty")

    # Accept Azure Speech short names like es-ES-Abril and normalize them to the
    # standard voice identifier that Voice Live expects.
    if VOICE_SHORT_NAME_PATTERN.fullmatch(normalized_voice) and not normalized_voice.endswith("Neural"):
        normalized_voice = f"{normalized_voice}Neural"

    if "-" in normalized_voice:
        return AzureStandardVoice(name=normalized_voice)

    return normalized_voice

SYSTEM_INSTRUCTIONS = """You are a warm, thoughtful voice companion and a strong technical thinking partner.

Primary role:
- Have natural, light conversations about everyday topics.
- Go deep when the user wants serious discussion, especially about technology, emerging trends, product strategy, and project execution.
- Help the user think clearly, structure ideas, and move toward decisions and next steps.

Core capabilities:
- Casual conversation: talk comfortably about life, work, ideas, learning, culture, and current themes.
- Deep technology discussion: explain concepts, compare approaches, discuss tradeoffs, and help the user understand new technologies.
- Product and project thinking: help shape vague ideas into clear goals, constraints, options, decisions, and action plans.
- Technical execution: help with architecture, implementation strategy, sequencing, and practical engineering choices.
- Documentation support: help draft and refine specs, plans, technical notes, decision records, and project documentation.

Conversation style:
- Be natural, warm, sharp, and easy to talk to.
- Sound like a smart collaborator, not a scripted assistant.
- Respond in the same language as the user unless asked otherwise.
- Keep spoken responses clear and easy to follow by ear.
- Be concise by default: usually answer in 2 to 5 spoken sentences unless the user asks for depth.
- Do not repeat or paraphrase the user's request unless it adds real clarity.
- Ask at most one meaningful follow-up question at a time.
- Avoid long lists and dense monologues unless the user explicitly asks for a structured breakdown.

Reasoning behavior:
- When the user is exploring an idea, help them clarify the problem, constraints, options, tradeoffs, and next step.
- When the user is learning, teach progressively: start simple, then deepen.
- When the user is deciding, compare options practically and recommend a default when there is a sensible one.
- When the user is stuck, break the problem into the smallest useful next moves.
- Use a light Socratic style when helpful: ask a precise question that unlocks the next part of the thinking.

Product and project support:
- Help define audience, problem statement, value proposition, scope, risks, milestones, and execution strategy.
- Help turn fuzzy thinking into concrete plans, technical tasks, and documentation.
- If useful, use a compact frame like goals, constraints, options, recommendation, and next steps.

Documentation behavior:
- Think like both an engineer and a technical writer.
- Prefer documentation that is clear, concise, concrete, and ready to use.
- If the user wants help writing, offer structure first, then content.

Web browsing behavior:
- You can use the browse_web tool when the user asks for current information, external references, documentation, news, or specific web pages.
- Prefer search first when the user asks an open web question.
- Prefer open when the user gives a URL or when you already found a relevant result and need details.
- Summarize findings clearly instead of reading raw web text aloud.
- If browsing is not necessary, answer directly without using the tool.

Boundaries:
- If you are unsure about a fact, say so clearly and offer the best available interpretation or a way to verify it.
- Do not pretend to have completed actions in external systems.
- Do not force the conversation into a fixed workflow or narrow domain.

Session start behavior:
- Open with a brief, friendly introduction.
- Invite the user to talk about whatever matters to them.
- A good first turn is: "Hi, I can chat with you, think through ideas, and help with technology, products, or documentation. What do you want to explore today?"
"""


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth > 0:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            text = data.strip()
            if text:
                self._parts.append(text)

    def get_text(self) -> str:
        return "\n".join(self._parts)


def _truncate_text(value: str, max_chars: int = WEB_TOOL_MAX_CHARS) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _resolve_hostname_ips(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return [candidate for candidate in {str(info[4][0]) for info in infos}]


def _is_public_web_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return False

    ips_to_check: list[str] = []
    try:
        ips_to_check.append(str(ip_address(hostname)))
    except ValueError:
        ips_to_check.extend(_resolve_hostname_ips(hostname))

    if not ips_to_check:
        return False

    for candidate in ips_to_check:
        parsed_ip = ip_address(candidate)
        if any(parsed_ip in network for network in PRIVATE_NETWORKS):
            return False
    return True


def _extract_html_text(markup: str) -> str:
    parser = HTMLTextExtractor()
    parser.feed(markup)
    return _truncate_text(parser.get_text())


def _search_web_duckduckgo(query: str) -> list[dict]:
    logger.info("browse_web search started: engine=duckduckgo query=%r", query)
    results: list[dict] = []
    try:
        raw_results = DDGS().text(query, max_results=WEB_TOOL_MAX_RESULTS)
    except Exception as exc:
        logger.warning("browse_web duckduckgo client failed: query=%r error=%s", query, exc)
        raise RuntimeError(f"DuckDuckGo search failed: {exc}") from exc

    for item in raw_results:
        title = _truncate_text(str(item.get("title", "")), 180)
        snippet = _truncate_text(str(item.get("body", "")), 280)
        url = str(item.get("href", "")).strip()
        if not _is_public_web_url(url):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= WEB_TOOL_MAX_RESULTS:
            break

    logger.info("browse_web search finished: engine=duckduckgo query=%r results=%d", query, len(results))
    if not results:
        logger.info("browse_web search zero results: engine=duckduckgo query=%r", query)
    for index, result in enumerate(results, start=1):
        logger.info(
            "browse_web search result %d: title=%r url=%s snippet=%r",
            index,
            result["title"],
            result["url"],
            _truncate_text(result["snippet"], 160),
        )
    return results


def _follow_search_results(results: list[dict], max_follow: int = WEB_TOOL_FOLLOW_RESULTS) -> list[dict]:
    followed_pages: list[dict] = []
    for index, result in enumerate(results[:max_follow], start=1):
        url = result.get("url", "")
        try:
            page = _open_web_page(url)
            followed_pages.append(
                {
                    "rank": index,
                    "url": url,
                    "search_title": result.get("title", ""),
                    "search_snippet": result.get("snippet", ""),
                    "page_title": page.get("title", ""),
                    "content_preview": _truncate_text(page.get("content", ""), 1200),
                }
            )
            logger.info(
                "browse_web followed result %d: url=%s page_title=%r preview=%r",
                index,
                url,
                page.get("title", ""),
                _truncate_text(page.get("content", ""), 180),
            )
        except Exception as exc:
            logger.warning("browse_web follow failed for result %d url=%s: %s", index, url, exc)
            followed_pages.append(
                {
                    "rank": index,
                    "url": url,
                    "search_title": result.get("title", ""),
                    "search_snippet": result.get("snippet", ""),
                    "status": "error",
                    "message": str(exc),
                }
            )
    return followed_pages


def _search_web_google_cse(query: str) -> list[dict]:
    if not GOOGLE_SEARCH_API_KEY or not GOOGLE_SEARCH_CX:
        raise RuntimeError(
            "Google search engine is configured but GOOGLE_SEARCH_API_KEY or GOOGLE_SEARCH_CX is missing"
        )

    logger.info("browse_web search started: engine=google_cse query=%r", query)
    response = requests.get(
        "https://www.googleapis.com/customsearch/v1",
        params={
            "key": GOOGLE_SEARCH_API_KEY,
            "cx": GOOGLE_SEARCH_CX,
            "q": query,
            "num": min(WEB_TOOL_MAX_RESULTS, 10),
            "hl": "es",
        },
        headers={"User-Agent": WEB_TOOL_USER_AGENT},
        timeout=WEB_TOOL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    payload = response.json()
    items = payload.get("items", []) or []
    results: list[dict] = []
    for item in items[:WEB_TOOL_MAX_RESULTS]:
        title = _truncate_text(str(item.get("title", "")), 180)
        url = str(item.get("link", "")).strip()
        snippet = _truncate_text(str(item.get("snippet", "")), 280)
        if not url or not _is_public_web_url(url):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})

    logger.info("browse_web search finished: engine=google_cse query=%r results=%d", query, len(results))
    for index, result in enumerate(results, start=1):
        logger.info(
            "browse_web search result %d: title=%r url=%s snippet=%r",
            index,
            result["title"],
            result["url"],
            _truncate_text(result["snippet"], 160),
        )
    return results


def _search_web(query: str) -> list[dict]:
    if WEB_SEARCH_ENGINE == "google_cse":
        return _search_web_google_cse(query)
    if WEB_SEARCH_ENGINE == "duckduckgo":
        return _search_web_duckduckgo(query)
    raise RuntimeError(f"Unsupported WEB_SEARCH_ENGINE: {WEB_SEARCH_ENGINE}")


def _open_web_page(url: str) -> dict:
    if not _is_public_web_url(url):
        raise ValueError("Only public http/https URLs are allowed")

    logger.info("browse_web open started: url=%s", url)

    response = requests.get(
        url,
        headers={"User-Agent": WEB_TOOL_USER_AGENT},
        timeout=WEB_TOOL_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.IGNORECASE | re.DOTALL)
    title = _truncate_text(_extract_html_text(html.unescape(title_match.group(1))), 180) if title_match else url
    text = _extract_html_text(response.text)
    result = {
        "url": url,
        "title": title,
        "content": text,
    }
    logger.info(
        "browse_web open finished: url=%s title=%r content_preview=%r",
        url,
        title,
        _truncate_text(text, 220),
    )
    return result


BROWSE_WEB_TOOL = FunctionTool(
    name="browse_web",
    description="Search the public web or open a public web page to gather current information and references.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "open"],
                "description": "Use 'search' for web search, or 'open' to fetch a specific public web page.",
            },
            "query": {
                "type": "string",
                "description": "Search terms to use when action is 'search'.",
            },
            "url": {
                "type": "string",
                "description": "Public http/https URL to open when action is 'open'.",
            },
        },
        "required": ["action"],
    },
)

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(title="General Voice Companion Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def voice_endpoint(websocket: WebSocket):
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
        session.cleanup()


# ── Voice session ─────────────────────────────────────────────────────────────

class VoiceSession:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.connection = None
        self.ap: Optional[AudioProc] = None
        self._running = True
        self._active_response = False
        self._response_api_done = False
        self._audio_transcript_acc = ""
        self._last_spoken_chars = 0
        self._last_assistant_text = ""
        self._last_assistant_spoke_at = 0.0
        self._pending_barge_in = False
        self._pending_tool_followup_response = False
        self._avatar_answer_event = asyncio.Event()
        self._avatar_server_sdp: Optional[str] = None

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text.lower())).strip()

    def _is_probable_echo(self, transcript: str) -> bool:
        if not transcript or not self._last_assistant_text:
            return False
        if (time.monotonic() - self._last_assistant_spoke_at) > ECHO_SUPPRESS_WINDOW_SECONDS:
            return False

        user_norm = self._norm_text(transcript)
        ai_norm = self._norm_text(self._last_assistant_text)
        if not user_norm or not ai_norm:
            return False

        # Fast path: direct containment catches phrase repeats like "Let's get you started".
        if len(user_norm) >= 8 and user_norm in ai_norm:
            return True

        ratio = difflib.SequenceMatcher(None, user_norm, ai_norm).ratio()
        return ratio >= ECHO_SUPPRESS_SIMILARITY

    async def _send(self, msg: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:
            pass

    async def run(self) -> None:
        await self._send({"type": "status", "value": "connecting"})
        credential = AzureCliCredential()

        async with connect(endpoint=ENDPOINT, credential=credential, model=MODEL) as conn:
            self.connection = conn
            self.ap = AudioProc(conn)

            await self._configure_session()
            if ENABLE_LOCAL_PLAYBACK:
                self.ap.start_playback()
                logger.info("Local playback enabled on backend")
            else:
                logger.info("Local playback disabled on backend (avatar/browser audio only)")
            await self._send({"type": "connected"})

            frontend_task = asyncio.create_task(self._listen_frontend())
            try:
                async for event in conn:
                    if not self._running:
                        break
                    await self._handle(event)
            finally:
                frontend_task.cancel()

    async def _listen_frontend(self) -> None:
        try:
            while self._running:
                raw = await self.ws.receive_text()
                msg = json.loads(raw)
                if msg.get("type") == "stop":
                    self._running = False
                    break
                if msg.get("type") == "input_audio":
                    audio = msg.get("audio", "")
                    if audio and self.connection is not None:
                        await self.connection.input_audio_buffer.append(audio=audio)
                    continue
                if msg.get("type") == "avatar_offer":
                    await self._send({"type": "avatar_connecting"})
                    client_sdp = msg.get("sdp", "")
                    logger.info("Received avatar_offer from frontend")
                    server_sdp = await self._send_avatar_connect(client_sdp)
                    if server_sdp:
                        logger.info("Sending avatar_answer to frontend")
                        await self._send({"type": "avatar_answer", "sdp": server_sdp})
                    else:
                        logger.warning("No avatar SDP answer received from VoiceLive")
                        await self._send({
                            "type": "avatar_error",
                            "message": "Could not establish avatar connection for this endpoint/avatar.",
                        })
        except Exception:
            pass

    async def _send_avatar_connect(self, client_sdp: str) -> Optional[str]:
        conn = self.connection
        if conn is None:
            return None

        self._avatar_server_sdp = None
        self._avatar_answer_event.clear()

        try:
            await conn.send({"type": "session.avatar.connect", "client_sdp": client_sdp})
            await asyncio.wait_for(self._avatar_answer_event.wait(), timeout=30.0)
            return self._avatar_server_sdp
        except Exception as exc:
            logger.warning("Avatar connect failed: %s", exc)
            return None

    async def _configure_session(self) -> None:
        voice = build_voice_config(VOICE)
        
        assert self.connection is not None
        await self.connection.session.update(
            session=RequestSession(
                modalities=[Modality.TEXT, Modality.AUDIO, Modality.AVATAR],
                instructions=SYSTEM_INSTRUCTIONS,
                voice=voice,
                input_audio_format=InputAudioFormat.PCM16,
                output_audio_format=OutputAudioFormat.PCM16,
                turn_detection=ServerVad(
                    threshold=VAD_THRESHOLD,
                    prefix_padding_ms=VAD_PREFIX_PADDING_MS,
                    silence_duration_ms=VAD_SILENCE_MS,
                ),
                input_audio_echo_cancellation=AudioEchoCancellation(),
                input_audio_noise_reduction=AudioNoiseReduction(
                    type="azure_deep_noise_suppression"
                ),
                input_audio_transcription=AudioInputTranscriptionOptions(
                    model=TRANSCRIPTION_MODEL
                ),
                avatar=AvatarConfig(
                    type=AvatarConfigTypes.PHOTO_AVATAR,
                    character=AVATAR_CHARACTER,
                    model=AVATAR_MODEL,
                ),
                tools=[BROWSE_WEB_TOOL],
            )
        )
        logger.info(
            "Session configured — general voice companion ready with web browsing, photo_avatar=%s (model=%s)",
            AVATAR_CHARACTER,
            AVATAR_MODEL,
        )

    async def _handle(self, event) -> None:
        ap = self.ap
        conn = self.connection
        assert ap is not None and conn is not None

        etype = event.type
        logger.info("EVENT: %s", etype)

        if etype == ServerEventType.SESSION_UPDATED:
            session_avatar = getattr(getattr(event, "session", None), "avatar", None)
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
            await self._send({"type": "avatar_ice_servers", "servers": ice_servers})
            logger.info("Avatar ICE servers sent to frontend: %d", len(ice_servers))

            if CAPTURE_SOURCE == "local":
                ap.start_capture()
                logger.info("Capturing microphone from backend host via PyAudio")
            else:
                logger.info("Capturing microphone from browser websocket stream")
            await self._send({"type": "status", "value": "ready"})

        elif etype == ServerEventType.SESSION_AVATAR_CONNECTING or "avatar" in str(etype).lower():
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
                logger.info("Ignoring speech_started during mic duck window (likely echo)")
                return
            ap.skip_pending_audio()
            await self._send({"type": "status", "value": "listening"})
            if self._active_response and not self._response_api_done:
                # Defer cancellation until we receive a non-echo user transcription.
                # This prevents false cancels from speaker leakage.
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
            # Keep extending capture ducking during assistant playback to avoid feedback loops.
            ap.duck_capture(0.6)
            if ENABLE_LOCAL_PLAYBACK:
                ap.queue_audio(event.delta)

        # ── AI spoken text — accumulate deltas, send on done ──────────────
        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DELTA:
            # Avatar mode may not emit continuous audio.delta events; keep ducking active from transcript stream.
            # Keep short rolling duck windows; long windows block barge-in.
            ap.duck_capture(1.0)
            self._audio_transcript_acc += getattr(event, "delta", "") or ""

        elif etype == ServerEventType.RESPONSE_AUDIO_TRANSCRIPT_DONE:
            text = getattr(event, "transcript", None) or self._audio_transcript_acc
            self._last_spoken_chars = len(text)  # save before clearing for RESPONSE_DONE
            self._last_assistant_text = text or ""
            self._last_assistant_spoke_at = time.monotonic()
            self._audio_transcript_acc = ""
            # Apply tail duck: audio.done fires here but browser still has buffered audio to play.
            ap.duck_capture(MIC_DUCK_POST_RESPONSE + 0.8)
            if text:
                logger.info("AI spoke: %.120s", text)
                await self._send(
                    {
                        "type": "transcript",
                        "role": "assistant",
                        "text": text,
                        "html": _markdown_to_safe_html(text),
                    }
                )

        elif etype == ServerEventType.RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE:
            func_name = getattr(event, "name", "")
            call_id = getattr(event, "call_id", "")
            raw_args = getattr(event, "arguments", "{}") or "{}"
            logger.info("Function call '%s' args: %s", func_name, raw_args)

            if func_name == "browse_web":
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    logger.warning("Bad JSON in browse_web args: %s — %s", raw_args, exc)
                    args = {}

                action = (args.get("action") or "").strip().lower()
                tool_output: dict

                try:
                    if action == "search":
                        query = (args.get("query") or "").strip()
                        if not query:
                            raise ValueError("query is required for search")
                        results = _search_web(query)
                        followed_pages = _follow_search_results(results)
                        tool_output = {
                            "status": "ok",
                            "action": "search",
                            "query": query,
                            "results": results,
                            "followed_pages": followed_pages,
                        }
                    elif action == "open":
                        url = (args.get("url") or "").strip()
                        if not url:
                            raise ValueError("url is required for open")
                        page = _open_web_page(url)
                        tool_output = {
                            "status": "ok",
                            "action": "open",
                            **page,
                        }
                    else:
                        raise ValueError("action must be 'search' or 'open'")
                except Exception as exc:
                    logger.warning("browse_web failed: %s", exc)
                    tool_output = {
                        "status": "error",
                        "action": action or "unknown",
                        "message": str(exc),
                    }

                try:
                    await conn.conversation.item.create(
                        item=FunctionCallOutputItem(
                            call_id=call_id,
                            output=json.dumps(tool_output),
                        )
                    )
                    if self._active_response and not self._response_api_done:
                        self._pending_tool_followup_response = True
                        logger.info(
                            "Deferred response.create after browse_web because a response is still active"
                        )
                    else:
                        logger.info("Calling response.create immediately after browse_web")
                        await conn.response.create()
                except Exception as exc:
                    logger.warning("Could not return browse_web result: %s", exc)

        # ── User speech transcript ────────────────────────────────────────
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
                        pass
                    self._pending_barge_in = False
                logger.info("User said: %.120s", transcript)
                await self._send(
                    {
                        "type": "transcript",
                        "role": "user",
                        "text": transcript,
                        "html": _markdown_to_safe_html(transcript),
                    }
                )

        elif etype == ServerEventType.RESPONSE_DONE:
            self._active_response = False
            self._response_api_done = True
            self._pending_barge_in = False
            # Keep post-response duck bounded so interruptions remain possible quickly.
            spoken_chars = self._last_spoken_chars
            tail_guard = min(
                max(MIC_DUCK_POST_RESPONSE + 0.8, MIC_DUCK_MIN_TAIL_SECONDS),
                MIC_DUCK_MAX_TAIL_SECONDS,
            )
            ap.duck_capture(tail_guard)
            logger.info("Post-response mic duck: %.1fs (transcript ~%d chars)", tail_guard, spoken_chars)
            if self._pending_tool_followup_response:
                self._pending_tool_followup_response = False
                try:
                    logger.info("Calling deferred response.create after response.done")
                    await conn.response.create()
                except Exception as exc:
                    logger.warning("Deferred response.create failed: %s", exc)
                    await self._send({"type": "status", "value": "ready"})
            else:
                await self._send({"type": "status", "value": "ready"})

        elif etype == ServerEventType.RESPONSE_ANIMATION_BLENDSHAPES_DELTA:
            # Avatar blendshape animation data — silently handled by the connection
            pass

        elif etype == ServerEventType.RESPONSE_ANIMATION_VISEME_DELTA:
            # Avatar viseme (mouth shape) animation data — silently handled
            pass

        elif etype == ServerEventType.ERROR:
            msg = getattr(event.error, "message", str(event.error))
            if "no active response" not in msg.lower():
                logger.error("VoiceLive error: %s", msg)
                await self._send({"type": "error", "message": msg})

    def cleanup(self) -> None:
        self._running = False
        if self.ap:
            self.ap.shutdown()


# ── Audio processor ───────────────────────────────────────────────────────────

class AudioProc:
    loop: asyncio.AbstractEventLoop

    class _Pkt:
        __slots__ = ("seq_num", "data")

        def __init__(self, seq: int, data) -> None:
            self.seq_num = seq
            self.data = data

    def __init__(self, conn) -> None:
        self.conn = conn
        self.pa = pyaudio.PyAudio()
        self.fmt = pyaudio.paInt16
        self.ch = 1
        self.rate = 24000
        self.chunk = 1200
        self.input_stream = None
        self.output_stream = None
        self.pb_queue: queue.Queue = queue.Queue()
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
            asyncio.run_coroutine_threadsafe(
                self.conn.input_audio_buffer.append(audio=b64), self.loop
            )
            return (None, pyaudio.paContinue)

        self.input_stream = self.pa.open(
            format=self.fmt, channels=self.ch, rate=self.rate,
            input=True, frames_per_buffer=self.chunk, stream_callback=_cb,
        )

    def start_playback(self) -> None:
        if self.output_stream:
            return
        remaining = bytes()

        def _cb(_in, frame_count, _ti, _sf):
            nonlocal remaining
            need = frame_count * pyaudio.get_sample_size(pyaudio.paInt16)
            out = remaining[:need]
            remaining = remaining[need:]
            while len(out) < need:
                try:
                    pkt = self.pb_queue.get_nowait()
                except queue.Empty:
                    out += bytes(need - len(out))
                    continue
                if not pkt or not pkt.data:
                    break
                if pkt.seq_num < self.pb_base:
                    remaining = bytes()
                    continue
                take = need - len(out)
                out += pkt.data[:take]
                remaining = pkt.data[take:]
            return (out, pyaudio.paContinue if len(out) >= need else pyaudio.paComplete)

        self.output_stream = self.pa.open(
            format=self.fmt, channels=self.ch, rate=self.rate,
            output=True, frames_per_buffer=self.chunk, stream_callback=_cb,
        )

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def queue_audio(self, data) -> None:
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
        if self.pa:
            self.pa.terminate()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("General Voice Companion Server")
    print("=" * 40)
    print("WebSocket : ws://localhost:8765/ws")
    print("Health    : http://localhost:8765/health")
    print("Logs      : voice/logs/")
    print("Press Ctrl+C to stop")
    print()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
