"""
Jarvis Voice Assistant — FastAPI Backend
Runs alongside Hermes on the same host, calls the Hermes Gateway API for streaming.

Config is loaded entirely from environment variables (or a .env file at HERMES_HOME/.env).
No secrets are hard-coded here.
"""

import os
import base64
import asyncio
import subprocess
import httpx
import tempfile
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, UploadFile, File, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
import json as _json
import re

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Configuration ─────────────────────────────────────────────────────────────
# Set these in your .env file or environment before starting Jarvis.
HERMES_VENV         = os.getenv("HERMES_VENV", "/home/user/.hermes/hermes-agent/venv")
HERMES_BIN          = f"{HERMES_VENV}/bin/python"
USER_HOME           = os.getenv("USER_HOME", "/home/user")
HERMES_HOME         = os.getenv("HERMES_HOME", f"{USER_HOME}/.hermes")

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
STATIC_DIR          = os.getenv("STATIC_DIR", os.path.join(os.path.dirname(__file__), "static"))

# Hermes API server (OpenAI-compatible, real streaming)
HERMES_API_BASE     = os.getenv("HERMES_API_BASE", "http://127.0.0.1:8642/v1")
HERMES_API_KEY      = os.getenv("HERMES_API_KEY", "jarvis-api-key-local")
HERMES_API_MODEL    = os.getenv("HERMES_API_MODEL", "hermes-agent")

SPEECH_MAX_CHARS = int(os.getenv("SPEECH_MAX_CHARS", "300"))

# ── Server-side secrets scrubber ─────────────────────────────────────────────
# Defence-in-depth: even if the frontend check is bypassed, the backend
# refuses to forward messages that contain recognisable secrets to Hermes.
import re as _re

_SECRET_PATTERNS = [
    _re.compile(r'\bsk-[A-Za-z0-9_\-]{20,}\b'),           # OpenAI sk- keys
    _re.compile(r'\bsk_[A-Za-z0-9_\-]{20,}\b'),           # ElevenLabs sk_ keys
    _re.compile(r'\bghp_[A-Za-z0-9_]{36,}\b'),            # GitHub PATs
    _re.compile(r'\bgho_[A-Za-z0-9_]{36,}\b'),
    _re.compile(r'\bghx_[A-Za-z0-9_]{36,}\b'),
    _re.compile(r'\bglpat-[A-Za-z0-9_\-]{20,}\b'),        # GitLab PATs
    _re.compile(r'\bxox[baprs]-[0-9A-Za-z\-]{10,}\b'),    # Slack
    _re.compile(r'\bAIza[0-9A-Za-z\-_]{35}\b'),           # Google API keys
    _re.compile(r'\bhvs\.[A-Za-z0-9]{24,}\b'),            # Vault tokens
    _re.compile(r'\bAKIA[0-9A-Z]{16}\b'),                 # AWS key IDs
    _re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----'),
    _re.compile(
        r'(?:password|passwd|secret|api[_\s-]?key|token|bearer|private[_\s-]?key)'
        r'\s*[:=]\s*\S{8,}',
        _re.IGNORECASE,
    ),
]

def _contains_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRET_PATTERNS)

executor        = ThreadPoolExecutor(max_workers=8)
hermes_executor = ThreadPoolExecutor(max_workers=4)

def hermes_env():
    env = os.environ.copy()
    env["HOME"]        = USER_HOME
    env["USER"]        = os.getenv("USER", "user")
    env["PATH"]        = f"{HERMES_VENV}/bin:/usr/local/bin:/usr/bin:/bin"
    env["HERMES_HOME"] = HERMES_HOME
    env.pop("DISPLAY", None)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    dotenv_path = os.path.join(HERMES_HOME, ".env")
    if os.path.exists(dotenv_path):
        with open(dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    return env

# Persistent session tracking
_session_state = {"id": None}
_session_lock = __import__('threading').Lock()


def clean_for_speech(text: str) -> str:
    text = text.replace('\\n', '\n').replace('\\t', '\t')
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`[^`]+`', lambda m: m.group(0)[1:-1], text)
    lines = text.splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^[|+\-=: ]+$', stripped) and len(stripped) > 3:
            continue
        if stripped.startswith('|') and stripped.endswith('|'):
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            cells = [c for c in cells if c and not re.match(r'^[-: ]+$', c)]
            if cells:
                clean_lines.append(', '.join(cells))
            continue
        clean_lines.append(line)
    text = '\n'.join(clean_lines)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    text = re.sub(r'^\s*[-*+•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-=_*]{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def summarize_for_speech_sync(text: str) -> str:
    text = text.replace('\\n', '\n').replace('\\t', '\t')
    cleaned = clean_for_speech(text)
    lines = [l.strip() for l in cleaned.splitlines() if l.strip()]
    sections = [l for l in lines if not l.startswith('-') and len(l) < 60 and not l.endswith('.')]
    if len(sections) >= 3:
        joined = ', '.join(sections[:6])
        if len(sections) > 6:
            joined += f', and {len(sections) - 6} more'
        return f'I can help with {joined}.'
    sentences = re.split(r'(?<=[.!?])\s+', cleaned)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    summary = ' '.join(sentences[:3])
    if len(summary) > 280:
        summary = summary[:277] + '...'
    return summary if summary else cleaned[:200]


class ChatRequest(BaseModel):
    message: str
    voice_id: Optional[str] = None


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(f"{STATIC_DIR}/index.html") as f:
        return f.read()


@app.post("/chat")
async def chat(req: ChatRequest):
    """Non-streaming fallback — calls Hermes API synchronously."""
    if _contains_secret(req.message):
        raise HTTPException(
            status_code=400,
            detail="Message blocked: appears to contain a secret or credential. "
                   "Never send API keys, passwords, or tokens to Jarvis.",
        )
    with _session_lock:
        session_id = _session_state["id"]

    headers = {
        "Authorization": f"Bearer {HERMES_API_KEY}",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id

    payload = {
        "model": HERMES_API_MODEL,
        "messages": [{"role": "user", "content": req.message}],
        "stream": False,
    }

    try:
        async with httpx.AsyncClient(timeout=130) as client:
            resp = await client.post(f"{HERMES_API_BASE}/chat/completions", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            reply_text = data["choices"][0]["message"]["content"].strip()
            new_sid = resp.headers.get("X-Hermes-Session-Id")
            if new_sid:
                with _session_lock:
                    _session_state["id"] = new_sid
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Hermes API error: {e}")

    if not reply_text:
        raise HTTPException(status_code=502, detail="Hermes returned empty response")

    speech_text = summarize_for_speech_sync(reply_text) if len(reply_text) > SPEECH_MAX_CHARS else clean_for_speech(reply_text)
    voice_id = req.voice_id or ELEVENLABS_VOICE_ID
    audio_b64 = None
    if ELEVENLABS_API_KEY and voice_id:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                tts = await client.post(
                    f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                    headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                    json={"text": speech_text, "model_id": "eleven_monolingual_v1",
                          "voice_settings": {"stability": 0.45, "similarity_boost": 0.80}},
                )
                if tts.status_code == 200:
                    audio_b64 = base64.b64encode(tts.content).decode()
        except Exception:
            pass

    return {"reply": reply_text, "audio": audio_b64}


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    SSE streaming via Hermes Gateway API (OpenAI-compatible).
    Tokens stream word-by-word as Hermes generates them.
    TTS audio is sent as a final SSE event after the full reply is assembled.
    """
    if _contains_secret(req.message):
        raise HTTPException(
            status_code=400,
            detail="Message blocked: appears to contain a secret or credential. "
                   "Never send API keys, passwords, or tokens to Jarvis.",
        )

    async def event_stream():
        with _session_lock:
            session_id = _session_state["id"]

        headers = {
            "Authorization": f"Bearer {HERMES_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        payload = {
            "model": HERMES_API_MODEL,
            "messages": [{"role": "user", "content": req.message}],
            "stream": True,
        }

        full_reply = ""
        got_content = False

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{HERMES_API_BASE}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f"event: error\ndata: {_json.dumps({'detail': f'Hermes API {resp.status_code}: {body.decode()[:200]}'})}\n\n"
                        return

                    new_sid = resp.headers.get("X-Hermes-Session-Id")
                    if new_sid:
                        with _session_lock:
                            _session_state["id"] = new_sid

                    buf = ""
                    async for chunk in resp.aiter_text():
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                data = _json.loads(data_str)
                            except Exception:
                                continue

                            hermes_event = data.get("hermes_event")
                            if hermes_event:
                                evt_text = hermes_event.get("text", "") or hermes_event.get("tool", "")
                                if evt_text:
                                    yield f"event: progress\ndata: {_json.dumps({'text': evt_text})}\n\n"
                                continue

                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                got_content = True
                                full_reply += content
                                yield f"event: token\ndata: {_json.dumps({'token': content})}\n\n"

        except httpx.TimeoutException:
            yield f"event: error\ndata: {_json.dumps({'detail': 'Hermes API timed out'})}\n\n"
            return
        except Exception as e:
            yield f"event: error\ndata: {_json.dumps({'detail': str(e)})}\n\n"
            return

        if not full_reply.strip():
            yield f"event: error\ndata: {_json.dumps({'detail': 'No response from Hermes'})}\n\n"
            return

        yield f"event: done\ndata: {_json.dumps({'reply': full_reply.strip()})}\n\n"

        # TTS after full reply
        voice_id = req.voice_id or ELEVENLABS_VOICE_ID
        if ELEVENLABS_API_KEY and voice_id:
            try:
                loop = asyncio.get_event_loop()
                if len(full_reply) > SPEECH_MAX_CHARS:
                    speech_text = await loop.run_in_executor(hermes_executor, summarize_for_speech_sync, full_reply)
                else:
                    speech_text = clean_for_speech(full_reply)

                async with httpx.AsyncClient(timeout=20) as client:
                    tts_resp = await client.post(
                        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                        json={"text": speech_text, "model_id": "eleven_monolingual_v1",
                              "voice_settings": {"stability": 0.45, "similarity_boost": 0.80}},
                    )
                    if tts_resp.status_code == 200:
                        audio_b64 = base64.b64encode(tts_resp.content).decode()
                        yield f"event: audio\ndata: {_json.dumps({'audio': audio_b64})}\n\n"
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/voices")
async def list_voices():
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
            )
            resp.raise_for_status()
        voices = resp.json().get("voices", [])
        voices.sort(key=lambda v: v["name"])
        return [{"id": v["voice_id"], "name": v["name"]} for v in voices]
    except Exception:
        return []


@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    suffix = ".webm"
    if audio.filename:
        ext = os.path.splitext(audio.filename)[1]
        if ext:
            suffix = ext

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await audio.read())
        tmp_path = tmp.name

    def run_whisper(path):
        script = f"""
import sys
sys.path.insert(0, '{HERMES_VENV}/lib/python3.12/site-packages')
from faster_whisper import WhisperModel
model = WhisperModel("base", device="cpu", compute_type="int8")
segments, _ = model.transcribe("{path}", beam_size=5, language="en")
print(" ".join(s.text for s in segments).strip())
"""
        result = subprocess.run(
            [HERMES_BIN, "-c", script],
            capture_output=True, text=True, timeout=30,
            env=hermes_env(),
        )
        return result.stdout.strip()

    text = ""
    try:
        loop = asyncio.get_event_loop()
        text = await asyncio.wait_for(
            loop.run_in_executor(executor, run_whisper, tmp_path),
            timeout=35
        )
    except Exception:
        pass
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return {"text": text}


@app.get("/health")
async def health():
    return {"status": "ok", "backend": "hermes-api"}


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None


@app.post("/tts")
async def tts_only(req: TTSRequest):
    voice_id = req.voice_id or ELEVENLABS_VOICE_ID
    if not ELEVENLABS_API_KEY or not voice_id:
        return {"audio": None}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            tts = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": req.text, "model_id": "eleven_monolingual_v1",
                      "voice_settings": {"stability": 0.45, "similarity_boost": 0.80}},
            )
            if tts.status_code == 200:
                return {"audio": base64.b64encode(tts.content).decode()}
    except Exception:
        pass
    return {"audio": None}
