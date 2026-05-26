# Progressive / Sentence-Level Streaming TTS Design

## Problem

Currently Jarvis waits for the **full reply** to arrive before sending it to ElevenLabs.  
For long responses (30–60 seconds of text) the user reads everything before hearing anything — a choppy experience.

## Proposed Solution: Sentence-Boundary Dispatch

Split TTS into parallel sentence-level requests dispatched **as tokens arrive**, so the first sentence starts playing within ~1–2 seconds.

### Backend changes (`server.py` — `/chat/stream`)

```python
import re

SENTENCE_END = re.compile(r'(?<=[.!?])\s+')

async def sentence_tts_stream(full_reply_gen, voice_id, api_key):
    """
    Yields audio events sentence-by-sentence as text streams in.
    """
    buffer = ""
    sentences = []

    async for token in full_reply_gen:
        buffer += token
        # Check for sentence boundary
        parts = SENTENCE_END.split(buffer)
        if len(parts) > 1:
            for sentence in parts[:-1]:
                sentence = sentence.strip()
                if sentence:
                    sentences.append(sentence)
                    audio = await tts_call(sentence, voice_id, api_key)
                    if audio:
                        yield audio          # emit audio chunk ASAP
            buffer = parts[-1]              # keep partial last sentence

    # Flush remaining
    if buffer.strip():
        audio = await tts_call(buffer.strip(), voice_id, api_key)
        if audio:
            yield audio
```

### Frontend changes

Add a sentence audio queue instead of waiting for a single `audio` event:

```js
// New SSE event: sentence_audio
// Payload: { "audio": "<base64>", "index": 0 }

let sentenceQueue = [];
let sentencePlaying = false;

function enqueueSentenceAudio(b64) {
    sentenceQueue.push(b64);
    if (!sentencePlaying) drainSentenceQueue();
}

function drainSentenceQueue() {
    if (!sentenceQueue.length) { sentencePlaying = false; return; }
    sentencePlaying = true;
    const b64 = sentenceQueue.shift();
    const audio = new Audio('data:audio/mpeg;base64,' + b64);
    audio.onended = drainSentenceQueue;
    audio.play();
}
```

### SSE event additions

| Event | Payload | When |
|-------|---------|------|
| `sentence_audio` | `{"audio":"<b64>","index":N}` | Each sentence completes TTS |
| `audio` | (existing) | Removed / kept for non-streaming fallback |

## Tradeoffs

| Approach | Latency to first audio | Complexity | Interruption |
|----------|----------------------|------------|--------------|
| Current (wait for full reply) | High (~reply time + TTS) | Low | Easy |
| Sentence-level (this doc) | Low (~first sentence + TTS) | Medium | Need queue drain |
| Word-level (WebSocket TTS) | Lowest | High | Needs ElevenLabs WS |

## ElevenLabs Streaming TTS (alternative)

ElevenLabs offers a WebSocket-based streaming TTS endpoint that accepts text chunks and returns audio chunks in real time. This would be the lowest-latency solution but requires:
- WebSocket connection management on the backend
- Chunked audio reassembly + playback on the frontend (Web Audio API)

Reference: https://elevenlabs.io/docs/api-reference/websockets

## Implementation Status

- [ ] Sentence boundary detection in event_stream()
- [ ] `sentence_audio` SSE event
- [ ] Frontend sentence audio queue
- [ ] Testing with Hermes long-form replies
- [ ] Optional: ElevenLabs WebSocket streaming
