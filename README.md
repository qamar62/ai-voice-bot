# Fiva — Realtime Voice Assistant (v2)

Complete rebuild of the voice pipeline on [Pipecat](https://pipecat.ai). Everything streams:

```
browser mic ──WebRTC──▶ Deepgram STT ─▶ OpenRouter LLM ─▶ ElevenLabs TTS ──WebRTC──▶ speaker
                          (streaming)     (streaming)        (streaming)
```

Typical voice-to-voice latency is under one second, with natural turn-taking (Silero VAD) and barge-in — you can interrupt the bot mid-sentence just by speaking.

## Why the old version wasn't realtime

The old `audio-processor.py` waited for a full transcript, then a full LLM reply, then generated a complete MP3 file, saved it to disk, and had the browser download and play it — several seconds per turn, no interruption possible. Server-side `RealtimeSTT` also listened on the *server's* microphone, which can't work in Docker or for remote users.

## Setup (macOS)

Requires Python 3.11+.

```bash
cd realtime
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Add your NVIDIA API key (from https://build.nvidia.com) to `.env` — it powers both the LLM and speech-to-text. The ElevenLabs key was carried over from your old settings.

## Run

```bash
python bot.py
```

Open **http://localhost:7860/client**, allow the microphone, click **Connect**, and talk.

First run downloads the Silero VAD model (~20s); later runs start fast.

## Agent configuration

The bot reads your existing `../data/agents.json` (Fiva / Five Tours system prompt, first message, `assistant_speaks_first`) and falls back to `../data/settings.json`. `{{now}}` in the prompt is replaced with the current date/time. Edit those files to change the agent's behavior.

Voice, models, and keys are configured in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | — | LLM (NIM) **and** streaming speech-to-text (Riva Parakeet) |
| `NVIDIA_MODEL` | `meta/llama-3.3-70b-instruct` | any model on build.nvidia.com |
| `ELEVENLABS_API_KEY` | — | streaming text-to-speech |
| `ELEVENLABS_VOICE_ID` | `SOYHLrjzK2X1ezoPC6cr` | your voice |
| `ELEVENLABS_MODEL` | `eleven_flash_v2_5` | fastest EL model |
| `DEEPGRAM_API_KEY` | (empty) | optional — if set, STT switches to Deepgram |

## Next steps (not yet wired)

- **Phone calls**: the same `bot.py` supports Twilio/Telnyx transports via Pipecat — add a telephony key and a `twilio` entry in `transport_params`.
- **Booking tools**: replace the old keyword-based n8n webhook with proper LLM function-calling (`ToolsSchema` in Pipecat) so Fiva can check availability and create bookings mid-call.
- **Dashboard**: the Next.js dashboard can manage `data/agents.json` as before; the bot picks up changes on restart.

## Security note

Your old `data/settings.json` contains live ElevenLabs and OpenRouter API keys in plain text (also copied into `realtime/.env` for convenience). Rotate them if this repo is ever shared or pushed to git, and never commit `.env`.
# ai-voice-bot
