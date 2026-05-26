# J.A.R.V.I.S — Voice Assistant Frontend

A sleek, mobile-first AI voice assistant UI powered by **Hermes** (an OpenAI-compatible streaming agent backend) and **ElevenLabs** TTS.

## Features

- 🎤 **Voice input** — tap-to-speak with Whisper STT (via Hermes venv)
- ⚡ **Real-time streaming** — tokens stream word-by-word as Hermes responds
- 🔊 **ElevenLabs TTS** — high-quality voice responses; smart summarization for long replies
- 📱 **Mobile-first PWA** — works on iOS Safari and Android Chrome (HTTPS required for mic)
- 🧠 **Persistent sessions** — Hermes remembers conversation context across turns
- ⏹ **Stop button** — abort mid-stream at any time

## Requirements

| Component | Notes |
|-----------|-------|
| Python 3.10+ | For the FastAPI backend |
| Hermes agent | Running at `localhost:8642/v1` (OpenAI-compatible API) |
| ElevenLabs API key | For TTS; get one at https://elevenlabs.io |
| `faster-whisper` | Installed in the Hermes venv for STT |
| `authbind` | Optional — only needed to bind port 443 as non-root |

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/bhigg-code/jarvis.git
cd jarvis
```

### 2. Create a Python venv and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
nano .env   # fill in your ElevenLabs key, voice ID, and paths
```

**Minimum required variables:**

```
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=<your_voice_id>
USER_HOME=/home/youruser
HERMES_VENV=/home/youruser/.hermes/hermes-agent/venv
```

### 4. (Optional) TLS — required for mic access on mobile

```bash
mkdir ssl
# Self-signed (dev only):
openssl req -x509 -newkey rsa:2048 -keyout ssl/key.pem -out ssl/cert.pem -days 365 -nodes
# Install authbind so non-root can bind port 443:
sudo apt install authbind
sudo touch /etc/authbind/byport/443
sudo chmod 755 /etc/authbind/byport/443
```

For production, use Let's Encrypt / certbot and place certs in `ssl/`.

### 5. Start Jarvis

```bash
chmod +x start.sh
./start.sh
```

Then open `https://<your-host>/` in a browser. If using a self-signed cert, accept the browser warning once.

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ELEVENLABS_API_KEY` | — | ElevenLabs API key (required for TTS) |
| `ELEVENLABS_VOICE_ID` | — | ElevenLabs voice ID |
| `HERMES_API_BASE` | `http://127.0.0.1:8642/v1` | Hermes OpenAI-compat base URL |
| `HERMES_API_KEY` | `jarvis-api-key-local` | Bearer token for Hermes |
| `HERMES_API_MODEL` | `hermes-agent` | Model name sent to Hermes |
| `USER_HOME` | `/home/user` | Home directory for Hermes env |
| `HERMES_HOME` | `$USER_HOME/.hermes` | Hermes data directory |
| `HERMES_VENV` | `$USER_HOME/.hermes/hermes-agent/venv` | Path to Hermes Python venv |
| `STATIC_DIR` | `./static` | Path to the static files directory |
| `SPEECH_MAX_CHARS` | `300` | Max chars before TTS summary kicks in |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serve the Jarvis UI |
| `POST` | `/chat` | Non-streaming chat (returns full reply + audio) |
| `POST` | `/chat/stream` | **SSE streaming** — tokens, progress, done, audio events |
| `POST` | `/transcribe` | Upload audio → Whisper STT → text |
| `POST` | `/tts` | Text → ElevenLabs audio (base64) |
| `GET` | `/voices` | List available ElevenLabs voices |
| `GET` | `/health` | Health check |

### SSE Event Types (`/chat/stream`)

| Event | Payload | Description |
|-------|---------|-------------|
| `token` | `{"token": "..."}` | Incremental text chunk |
| `progress` | `{"text": "..."}` | Tool/agent activity (shown while waiting) |
| `done` | `{"reply": "..."}` | Full reply assembled |
| `audio` | `{"audio": "<base64 mp3>"}` | TTS audio ready to play |
| `error` | `{"detail": "..."}` | Something went wrong |

## Streaming vs Wait-for-Full-Reply (TTS)

Currently the flow is:
1. Tokens stream live to the UI (text appears word-by-word ✅)
2. Once `done` fires, the full reply is summarised and sent to ElevenLabs
3. The audio plays after the text is complete

**Roadmap — Sentence-level progressive TTS:**  
The next enhancement is to detect sentence boundaries (`. ! ?`) as tokens arrive and dispatch ElevenLabs requests sentence-by-sentence. This means the first sentence starts playing within ~1–2 seconds while the rest of the text continues streaming. See `STREAMING_TTS.md` for the design plan.

## Security Notes

- No secrets are stored in source code — use `.env` (excluded from git via `.gitignore`)
- CORS is currently open (`allow_origins=["*"]`) — restrict to your domain in production
- SSL certs belong in `ssl/` which is also gitignored
- Whisper runs inside the existing Hermes venv — no separate install needed if Hermes is set up

## License

MIT
