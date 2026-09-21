# Fiva — Future Integrations Roadmap

Ideas we've discussed or parked. Ordered roughly by value-for-effort.
Nothing here is urgent — the core system is live and complete.

## 1. Stripe payment links (already sketched)

After the team verifies an inquiry: a Django endpoint creates a Stripe
Payment Link from the quoted amount (backend already has full Stripe
integration for the website), n8n sends it to the customer via WhatsApp /
email, webhook marks the inquiry paid → convert to a real booking.
Payment AFTER human verification — fits the anti-prank design.
Effort: small (1 endpoint + 1 n8n branch).

## 2. Phone calls (Fiva answers a real number)

Pipecat supports Twilio/Telnyx/Plivo transports — the same `bot.py` works,
you add a `"twilio"` entry to `transport_params` and a webhook. Customers
call a UAE number and talk to Fiva exactly like the web widget.
Needs: telephony account (~$1–5/mo + per-minute), number purchase.
Effort: medium. Big credibility win for a tour operator.

## 3. LLM upgrade when revenue justifies it

llama-3.1-8b occasionally ignores instructions (we've engineered around the
big ones). When calls are real revenue: test `LLM_PROVIDER=openrouter` with
gpt-4o-mini (already wired, one .env line) or a paid NIM tier. Re-test the
booking flow — most prompt-slip issues disappear with a bigger model.

## 4. Inquiry → booking auto-conversion

Admin action (or n8n flow) that converts a verified VoiceInquiry into a real
ActivityBooking: creates/links a user, copies fields, sets the reference.
Today the team does this manually. Effort: small-medium (Django admin action).

## 5. Caller memory / CRM light

Recognize returning customers by phone/email: past inquiries greetable
("Welcome back, Qamar"). Needs a lookup tool hitting the backend + privacy
thinking. Later: sync inquiries into a proper CRM (HubSpot/Odoo) via n8n.

## 6. Call transcripts + analytics in the database

Store each call's transcript (already flows through RTVI) with the session
id — attach to the inquiry so the team reads the whole conversation before
calling back. Add a simple dashboard: calls/day, language split, conversion
to inquiry, drop-off points. Effort: medium.

## 7. Voice quality upgrades

- ElevenLabs Scribe *realtime* websocket STT for non-EN languages (service
  exists in pipecat — `ElevenLabsRealtimeSTTService`) — cuts the ~0.5s
  segmented-STT delay.
- Native per-language voices refresh as the library grows (swap = .env edit).
- Azure Neural TTS (~3× cheaper than ElevenLabs) once volume makes cost
  matter — pipecat-supported, config swap.

## 8. Website deep-links from chat/voice

Fiva already knows activity slugs — have `list_activities` return URLs and
the web UI render tappable activity cards under the chat (RTVI custom
messages). Customer taps → activity page → self-serve checkout. Bridges the
voice funnel into the existing paid checkout.

## 9. WhatsApp voice notes / chat bot

Same brain, third channel: Evolution API webhook → n8n → the backend voice
endpoints (or a text variant of the agent). Customers who message the
business number get the same catalog answers + inquiry creation.

## 10. Ops hardening

- Sentry for fiva-bot (backend already has it) — catch pipeline exceptions.
- Uptime monitoring on voice.five.tours + a daily n8n "voice bot health"
  check-call.
- coturn TLS (turns: on 5349 with the Let's Encrypt cert) for networks that
  block plain 3478.
- Log rotation + call-count metrics from pipecat's built-in usage metrics.

---

*Done so far (for context): realtime pipeline (NVIDIA STT/LLM + ElevenLabs
TTS), 5 languages, branded web client with chat log + typed input, tool
calling into Django (catalog, availability, inquiries), VoiceInquiry
workflow with admin, n8n notifications (Discord/email/WhatsApp), Chatwoot
website chat with the same inquiry integration, VPS deployment with SSL +
TURN, spoken-date parsing, anti-hallucination safeguards.*
