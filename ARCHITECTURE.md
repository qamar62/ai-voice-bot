# Fiva Voice Bot — Architecture & Flow

How the realtime voice assistant is built, step by step. Read this before
changing anything — every piece below exists for a reason (usually a bug we
hit and fixed).

## The big picture

```
                          Cloud VPS (Euronodes, public IP)
                        ┌─────────────────────────────────────────┐
 Visitor's browser      │  nginx (443, SSL via Let's Encrypt)     │
 voice.five.tours ─────▶│    /            → web/index.html (UI)   │
                        │    /api/offer   → fiva-bot :7860        │
                        │    /client      → pipecat test UI       │
        ▲               │                                         │
        │ WebRTC audio  │  fiva-bot (pipecat, python 3.12)        │
        │ (UDP, direct  │  coturn  (TURN relay :3478, fallback)   │
        │  or via TURN) └───────────────┬─────────────────────────┘
        │                               │ HTTPS (X-API-Key)
        ▼                               ▼
   turn.five.tours              five.tours/api/v1  (Django backend)
   (grey-cloud DNS)             /bookings/voice/activities/
                                /bookings/voice/availability/
                                /bookings/voice/inquiries/  → VoiceInquiry table
                                                                  │
                                                     n8n automations (Discord,
                                                     email, WhatsApp, team flow)
```

## Docker services (docker-compose.yml)

| Service | What it does |
|---|---|
| `fiva-bot` | The pipecat voice agent (`bot.py`). Host networking — WebRTC needs raw UDP. |
| `voice-web` | nginx: serves the branded UI, terminates SSL, proxies `/api/` signaling. |
| `fiva-turn` | coturn TURN relay for clients on UDP-hostile networks (port 3478 + relay range 49160–49200). |

Config lives in `.env` (never committed): NVIDIA, ElevenLabs, backend API keys
and per-language voice IDs. Agent persona lives in `data/agents.json`
(committed — no secrets): system prompt, greeting, `assistant_speaks_first`.

## Call lifecycle, step by step

1. **Page load** — `web/index.html` (vanilla JS, no SDK). Visitor picks a
   language, presses Start.
2. **Mic + WebRTC setup** — `getUserMedia`, `RTCPeerConnection` with STUN +
   TURN ice servers. An AudioContext is created *inside the click handler*
   (browsers keep it suspended otherwise — this powers the orb animation).
3. **Signaling** — browser POSTs `{sdp, type, request_data:{language}}` to
   `/api/offer`. The pipecat runner creates a `SmallWebRTCConnection`, spawns
   a fresh bot instance (`bot()` → `run_bot()`), and returns the SDP answer.
4. **Media path** — ICE picks the best route: direct UDP to the VPS public IP
   (usual case) or relayed through coturn (strict mobile/corporate networks).
5. **Client ready** — the page opens a data channel labeled `rtvi-ai` and
   sends `client-ready`. This is the moment audio is guaranteed flowing, so
   the bot greets HERE (`on_client_ready` → `TTSSpeakFrame(greeting)`).
   A 3s fallback greets anyway if no client-ready arrives (e.g. test UI).
6. **Conversation loop** — see pipeline below. Transcripts and speaking
   events stream back over the same data channel; the page renders the chat
   log and animates the orb from live audio levels.
7. **Hang up** — page closes the connection; `on_client_disconnected` cancels
   the pipeline task. Every call is stateless — fresh context, no carry-over.

## The pipecat pipeline (the heart of it)

Pipecat moves "frames" (audio chunks, text fragments, control events) through
a chain of processors. Ours, in order:

```
transport.input()        browser mic audio arrives (16 kHz PCM frames)
      │
NvidiaSTTService         streaming speech-to-text (Riva Parakeet, EN)
      │                  — non-EN languages use ElevenLabs Scribe instead
LLMUserAggregator        collects transcript fragments into a user turn.
      │                  Silero VAD (local) detects speech start/stop;
      │                  SpeechTimeoutUserTurnStopStrategy(0.8s) ends the
      │                  turn after a pause. VAD start = barge-in: speaking
      │                  over the bot interrupts it instantly.
NvidiaLLMService         streaming LLM (NIM, llama-3.1-8b-instruct).
      │                  Has 4 registered tools (function calling) — see below.
ElevenLabsTTSService     streaming text-to-speech (flash v2.5, websocket).
      │                  MarkdownTextFilter strips *,#,tables before speech.
      │                  Voice ID per language from .env (must be added to
      │                  "My Voices" in the ElevenLabs account!).
transport.output()       audio streams back to the browser
      │
LLMAssistantAggregator   records what the bot actually said into context
```

Everything streams: the bot starts speaking while the LLM is still
generating. Voice-to-voice latency ≈ 1 second.

Wrapped around the pipeline: `PipelineTask` (with our `RTVIProcessor` for the
data-channel protocol) and `PipelineRunner` (lifecycle).

## Tool calling (how Fiva knows real data)

The LLM has 4 functions. The handler (`handle_booking_tool`) translates each
call into a Django API request — **no LLM anywhere in the backend**, so
lookups are fast (~ms) and can't hallucinate:

| Tool | Backend endpoint | Purpose |
|---|---|---|
| `list_activities` | GET `/bookings/voice/activities/?search=` | real catalog, compact payload |
| `check_availability` | GET `/bookings/voice/availability/` | TimeSlot day-check + pricing + min-persons |
| `create_booking` | POST `/bookings/voice/inquiries/` | booking REQUEST (`inquiry_type=booking`) |
| `create_general_inquiry` | POST `/bookings/voice/inquiries/` | callbacks, packages, complaints |

Key safeguards (each fixed a real observed failure):

- `normalize_spoken_date()` — customer dates arrive as spoken ("twenty nine
  of July", "29 июля") and are parsed server-side with dateparser. The model
  is forbidden from ever mentioning date formats to customers.
- `FunctionCallResultProperties(run_llm=True)` + an embedded `instruction`
  field in every tool result — forces the model to speak the outcome
  immediately (small models otherwise go silent after tool calls).
- Backend enforces required fields per inquiry type and answers with an
  LLM-friendly "missing: X, Y — ask and retry" summary.
- Email normalization backend-side ("name at gmail dot com" → address), plus
  a typed-input box in the UI injected via RTVI `send-text` (typing beats
  spelling aloud).
- HONESTY prompt rule: never claim anything was registered without a tool
  success + reference number.
- Bookings are NEVER written to the real booking tables — only to
  `VoiceInquiry`, which the team verifies (protects against prank calls).

## Multilingual (EN / AR / RU / ES / PT)

Language is chosen on the page before the call and travels in
`request_data.language`. Per language, `LANGUAGES` in bot.py sets: the STT
service (Riva for EN, ElevenLabs Scribe otherwise), the ElevenLabs voice ID
(`.env`), a translated greeting, and a "speak only X" prompt directive.
flash v2.5 is multilingual, so any voice can speak any language.

**Gotcha that cost us an evening**: library voices MUST be added to
"My Voices" in the ElevenLabs account (⊕ icon in the Voice Library) or the
streaming API rejects them with "voice does not exist" — even though the ID
looks valid and REST previews work.

## Hard-won decisions log

- **Why not the default smart-turn model**: pipecat's ML turn-detector
  stalled on our setup → replaced with simple 0.8s speech-timeout strategy.
- **Why the LLM is llama-3.1-8b**: 70b on free NIM had 18–42s first-token
  latency (calls felt dead and barge-in cancelled replies). 8b answers <1s.
  Known cost: occasionally ignores subtle prompt rules → the engineering
  safeguards above. Upgrade path: bigger model when budget allows.
- **Why n8n was removed from the live call path**: the n8n AI-agent loop
  took minutes per tool call on free NIM. n8n now handles what it's great
  at: notifications and team automation off the inquiry table.
- **Why TURN exists even with a public IP**: some mobile/corporate networks
  block arbitrary UDP; TURN on 3478 (+TCP fallback in the client config)
  covers them.
- **Why greeting waits for client-ready**: greeting on `connected` raced the
  audio path and got swallowed. RTVI client-ready = audio guaranteed up.
- **Fresh context per call**: no cross-call memory by design (privacy +
  simplicity). Long-term memory is a roadmap item.

## Key files

```
realtime/
├── bot.py              the whole agent: pipeline, tools, prompts, languages
├── web/index.html      branded client: WebRTC, chat log, orb, typed input
├── data/agents.json    Fiva persona: system prompt, greeting (committed)
├── .env                all keys + voice IDs (NEVER commit)
├── docker-compose.yml  fiva-bot + voice-web + fiva-turn
├── nginx.conf          SSL, static UI, /api proxy, /client test UI
├── Dockerfile          python 3.12-slim + opencv system libs
└── DEPLOY.md           VPS/LXC deployment recipes
```

Backend counterpart: `travel/backend/bookings/` — `VoiceInquiry` model,
voice API views (`app_label = "bookings"` required for API-key auth on plain
APIViews), admin with inquiry workflow (new → contacted → converted/spam).
