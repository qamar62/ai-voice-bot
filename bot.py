#!/usr/bin/env python3
"""Fiva — realtime voice assistant for Five Tours.

Fully streaming pipeline (Pipecat):

    browser mic ──WebRTC──▶ Deepgram STT ─▶ OpenRouter LLM ─▶ ElevenLabs TTS ──WebRTC──▶ browser speaker

Every stage streams, so the bot starts speaking while the LLM is still
generating. Silero VAD gives natural turn-taking and barge-in (you can
interrupt the bot mid-sentence just by speaking).

Run:  python bot.py   →  open http://localhost:7860/client
"""

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import aiohttp
import httpx
from dotenv import load_dotenv
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    LLMRunFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi.processor import RTVIProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.nvidia.llm import NvidiaLLMService
from pipecat.services.nvidia.stt import NvidiaSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.text.markdown_text_filter import MarkdownTextFilter

load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Keep responses short, natural, and "
    "conversational — they will be spoken aloud. Never use markdown, lists, "
    "or emojis. Spell out numbers as words."
)

# Supported call languages. The visitor picks one on the voice page before the
# call; it arrives via request_data in /api/offer (runner_args.body).
#  - English keeps the fast streaming Riva/Deepgram STT.
#  - Other languages use ElevenLabs Scribe STT (segmented, 90+ languages).
#  - TTS voice per language comes from .env (voice_env), falling back to the
#    default ELEVENLABS_VOICE_ID — eleven_flash_v2_5 is multilingual, so the
#    default voice can speak all of these.
LANGUAGES = {
    "en": {
        "name": "English",
        "language": Language.EN,
        "voice_env": "ELEVENLABS_VOICE_ID",
        "greeting": "",  # uses the configured first_message from agents.json
    },
    "ar": {
        "name": "Arabic",
        "language": Language.AR,
        "voice_env": "ELEVENLABS_VOICE_ID_AR",
        "greeting": "مساء الخير، معك فايف فيرتكس تورز. أنا فيفا، كيف يمكنني مساعدتك؟",
    },
    "ru": {
        "name": "Russian",
        "language": Language.RU,
        "voice_env": "ELEVENLABS_VOICE_ID_RU",
        "greeting": "Добрый день! Вы обратились в Five Vertex Tours. Меня зовут Фива. Чем я могу вам помочь?",
    },
    "es": {
        "name": "Spanish",
        "language": Language.ES,
        "voice_env": "ELEVENLABS_VOICE_ID_ES",
        "greeting": "¡Buenas tardes! Ha contactado con Five Vertex Tours. Soy Fiva, ¿en qué puedo ayudarle?",
    },
    "pt": {
        "name": "Portuguese",
        "language": Language.PT,
        "voice_env": "ELEVENLABS_VOICE_ID_PT",
        "greeting": "Boa tarde! Entrou em contacto com a Five Vertex Tours. Eu sou a Fiva, como posso ajudar?",
    },
}


def load_agent_config() -> dict:
    """Load agent config from data/agents.json, falling back to data/settings.json."""
    config = {
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "first_message": "",
        "assistant_speaks_first": False,
    }
    try:
        settings_path = DATA_DIR / "settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            for key in config:
                if settings.get(key):
                    config[key] = settings[key]
        agents_path = DATA_DIR / "agents.json"
        if agents_path.exists():
            agents = json.loads(agents_path.read_text(encoding="utf-8"))
            if agents and agents[0].get("system_prompt"):
                config["system_prompt"] = agents[0]["system_prompt"]
                if agents[0].get("first_message"):
                    config["first_message"] = agents[0]["first_message"]
                if "assistant_speaks_first" in agents[0]:
                    config["assistant_speaks_first"] = agents[0]["assistant_speaks_first"]
    except Exception as e:
        logger.warning(f"Could not load agent config, using defaults: {e}")

    # Template variables
    now = datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    config["system_prompt"] = config["system_prompt"].replace("{{now}}", now)

    # Voice-specific guardrails appended to whatever prompt is configured
    config["system_prompt"] += (
        "\n\n[Voice Output Rules]\n"
        "Your reply is converted to speech. Plain sentences only — no markdown, "
        "no bullet points, no labels like 'Activity:' or 'Date:', no emojis. "
        "Keep replies brief and natural.\n"
        "DATES: when speaking, ALWAYS say dates naturally — 'the twentieth of "
        "July' — never digit formats like 2026-07-20. Convert any date from tool "
        "results into natural words before speaking it. Customers say dates "
        "naturally too ('twenty July') — convert them yourself to YYYY-MM-DD for "
        "tools, assuming the next upcoming occurrence. NEVER ask the customer to "
        "use a date format.\n"
        "The call ALWAYS starts with your scripted greeting, which has already "
        "been spoken. If the customer just says hello, warmly ask how you can "
        "help — do NOT list activities unless they ask what's available."
        "\n\n[Tool Rules — CRITICAL]\n"
        "You have tools: list_activities, check_availability, create_booking, "
        "create_general_inquiry. NEVER invent activities, prices, availability, or "
        "references — only repeat what tools return. Dates passed to tools must be "
        "YYYY-MM-DD.\n"
        "- Customer asks what you offer → call list_activities, then mention only "
        "the TWO or THREE most relevant options woven into one natural spoken "
        "sentence with prices (e.g. 'We have the Evening Desert Safari at one "
        "hundred fifty dirhams per person, or the Premium Safari at five hundred "
        "ninety-nine'). NEVER read out the full list, never use asterisks or "
        "bullet points. Offer to mention more if they want.\n"
        "- Before stating any price, availability, or minimum-person rule → call "
        "check_availability, passing ONLY what the customer actually said. If "
        "they haven't given a date, do not send one and do not mention "
        "availability — quote the price and ask which date they'd like. NEVER "
        "state availability for a date the customer didn't request.\n"
        "- Activity booking: you MUST collect ALL of these before calling "
        "create_booking — activity, date, number of persons, sharing or private, "
        "pickup location, full name, phone number, and email address. Ask for "
        "missing items ONE at a time. For email and phone, tell the customer they "
        "can also TYPE it in the message box on the screen — typing is more "
        "accurate than spelling aloud. When they type something, treat it as "
        "exact and do not re-confirm the spelling. For spoken email, read it "
        "back once to confirm. "
        "Then read back the summary as ONE flowing natural sentence, for example: "
        "'So that's the Dubai City Tour combo for two people sharing, on the "
        "twentieth of July, pickup from Marina, under the name Qamar Shahzad — "
        "shall I confirm?' Do not read the phone or email back again in the "
        "summary, and never use list labels. After the customer confirms call "
        "create_booking. If the tool replies that fields are missing, ask for "
        "exactly those and try again. Give them the request reference number and "
        "explain the Five Tours team will contact them shortly to finalize. Do NOT "
        "say the booking is confirmed.\n"
        "- Anything else needing follow-up (tour packages, partnerships, jobs, "
        "suppliers, complaints, callback requests): collect topic, details, name, "
        "phone and preferred callback time, then call create_general_inquiry and "
        "confirm the team will call back.\n"
        "After ANY tool returns, immediately tell the customer the outcome — never "
        "stay silent or wait for them to ask. "
        "If a tool fails, apologize and offer a callback instead of guessing."
    )
    return config


def make_booking_tools() -> ToolsSchema:
    """Tool definitions exposed to the LLM (executed via the Five Vertex API)."""
    list_activities = FunctionSchema(
        name="list_activities",
        description=(
            "Search the real activity catalog. Call this when the customer asks "
            "what activities/tours are offered, or to find the correct activity "
            "name before checking availability. Returns titles and prices."
        ),
        properties={
            "search": {
                "type": "string",
                "description": "Optional search term, e.g. 'desert', 'water', 'dubai'. Empty = top activities.",
            },
        },
        required=[],
    )
    create_general_inquiry = FunctionSchema(
        name="create_general_inquiry",
        description=(
            "Register a general inquiry that is NOT a direct activity booking: "
            "tour packages, partnerships, job applications, suppliers, complaints, "
            "or anything the team must follow up on. Collect topic, details, name, "
            "phone and preferred callback time first."
        ),
        properties={
            "topic": {"type": "string", "description": "Short topic, e.g. 'Tour package for family of 6'"},
            "details": {"type": "string", "description": "Everything the customer said about their request"},
            "inquiry_type": {
                "type": "string",
                "enum": ["general", "tour_package", "callback"],
                "description": "Category of the inquiry",
            },
            "customer_name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "preferred_callback_time": {"type": "string", "description": "As spoken, e.g. 'tomorrow morning'"},
        },
        required=["topic", "details", "customer_name", "phone"],
    )
    check_availability = FunctionSchema(
        name="check_availability",
        description=(
            "Check pricing and minimum-person rules for an activity, and — only "
            "if the customer stated a date — its day-wise availability. Pass ONLY "
            "details the customer actually said. NEVER invent a date or persons "
            "count; omit them if not given."
        ),
        properties={
            "activity": {"type": "string", "description": "Activity or tour name"},
            "date": {
                "type": "string",
                "description": "YYYY-MM-DD — ONLY if the customer stated a date. Omit otherwise.",
            },
            "persons": {
                "type": "integer",
                "description": "ONLY if the customer stated how many people. Omit otherwise.",
            },
            "booking_type": {
                "type": "string",
                "enum": ["sharing", "private"],
                "description": "Sharing or private booking",
            },
        },
        required=["activity"],
    )
    create_booking = FunctionSchema(
        name="create_booking",
        description=(
            "Register the customer's booking REQUEST after they confirmed all "
            "details. Returns a request reference number. The Five Tours team "
            "then contacts the customer to finalize the booking."
        ),
        properties={
            "activity": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "persons": {"type": "integer"},
            "booking_type": {"type": "string", "enum": ["sharing", "private"]},
            "pickup_location": {"type": "string"},
            "pickup_time": {"type": "string"},
            "customer_name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "quoted_price": {
                "type": "number",
                "description": "Total price quoted from check_availability",
            },
            "special_requests": {"type": "string"},
        },
        required=[
            "activity",
            "date",
            "persons",
            "booking_type",
            "pickup_location",
            "customer_name",
            "phone",
            "email",
        ],
    )
    return ToolsSchema(
        standard_tools=[list_activities, check_availability, create_booking, create_general_inquiry]
    )


async def call_backend(method: str, path: str, **kwargs) -> dict:
    """Call the Five Vertex Django API (deterministic — no LLM in the backend)."""
    base = os.getenv("BACKEND_API_URL", "http://localhost:8000/api/v1").rstrip("/")
    headers = {"X-API-Key": os.getenv("BACKEND_API_KEY", "")}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(method, f"{base}{path}", headers=headers, **kwargs)
            data = resp.json()
            if resp.status_code >= 400 and "summary" not in data:
                data["summary"] = f"Backend error {resp.status_code}."
            return data
    except Exception as e:
        logger.error(f"Backend API error on {path}: {e}")
        return {"summary": "The booking system is temporarily unreachable."}


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments):
    agent = load_agent_config()
    session_id = str(uuid.uuid4())

    # --- Language (picked on the voice page before the call) --------------
    body = getattr(runner_args, "body", None) or {}
    lang_code = str(body.get("language", "en")).lower() if isinstance(body, dict) else "en"
    if lang_code not in LANGUAGES:
        lang_code = "en"
    lang = LANGUAGES[lang_code]
    logger.info(f"Starting Fiva realtime voice bot — language: {lang['name']}")

    if lang_code != "en":
        # Scripted greeting in the caller's language
        if lang["greeting"]:
            agent["first_message"] = lang["greeting"]
        # Keep the whole conversation in that language; tools stay English/ISO
        agent["system_prompt"] += (
            f"\n\n[Language — CRITICAL]\n"
            f"The customer speaks {lang['name']}. Conduct the ENTIRE conversation "
            f"in {lang['name']} — every reply, question, and confirmation. "
            f"Never switch to English unless the customer does. "
            f"Tools are the exception: pass dates as YYYY-MM-DD and activity "
            f"names in English to tools, then explain the results to the "
            f"customer in {lang['name']}. Spell out numbers as words in "
            f"{lang['name']}."
        )

    # --- AI services (all streaming) -------------------------------------
    nvidia_api_key = os.getenv("NVIDIA_API_KEY", "")
    elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY", "")
    aiohttp_session: aiohttp.ClientSession | None = None

    if lang_code != "en":
        # Non-English: ElevenLabs Scribe STT (segmented, transcribes after each
        # turn — slightly slower than streaming Riva, but supports 90+ languages).
        aiohttp_session = aiohttp.ClientSession()
        stt = ElevenLabsSTTService(
            api_key=elevenlabs_api_key,
            aiohttp_session=aiohttp_session,
            params=ElevenLabsSTTService.InputParams(language=lang["language"]),
        )
        logger.info(f"STT: ElevenLabs Scribe ({lang['name']})")
    elif os.getenv("DEEPGRAM_API_KEY"):
        # If you ever prefer Deepgram, set DEEPGRAM_API_KEY and it takes over.
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
        logger.info("STT: Deepgram")
    else:
        # STT: NVIDIA Riva Parakeet (streaming ASR) — uses the same NVIDIA key.
        stt = NvidiaSTTService(api_key=nvidia_api_key)
        logger.info("STT: NVIDIA Riva (parakeet)")

    default_voice = os.getenv("ELEVENLABS_VOICE_ID", "DODLEQrClDo8wCz460ld")
    voice_id = os.getenv(lang["voice_env"], "") or default_voice
    tts = ElevenLabsTTSService(
        api_key=elevenlabs_api_key,
        voice_id=voice_id,
        model=os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5"),
        # Pin pronunciation/accent to the call language (flash v2.5 is multilingual)
        params=ElevenLabsTTSService.InputParams(language=lang["language"]),
        # Strip any markdown (*, **, #, tables) the LLM sneaks in before speaking
        text_filters=[MarkdownTextFilter()],
    )

    # LLM: NVIDIA NIM by default; set LLM_PROVIDER=openrouter to switch.
    if os.getenv("LLM_PROVIDER", "nvidia").lower() == "openrouter":
        from pipecat.services.openrouter.llm import OpenRouterLLMService

        llm = OpenRouterLLMService(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            settings=OpenRouterLLMService.Settings(
                model=os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            ),
        )
        logger.info("LLM: OpenRouter")
    else:
        llm = NvidiaLLMService(
            api_key=nvidia_api_key,
            settings=NvidiaLLMService.Settings(
                model=os.getenv("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
            ),
        )
        logger.info(f"LLM: NVIDIA NIM ({os.getenv('NVIDIA_MODEL', 'meta/llama-3.1-8b-instruct')})")

    # --- Booking tools (call the Five Vertex Django API directly) ----------
    async def handle_booking_tool(params):
        args = params.arguments or {}
        if params.function_name == "list_activities":
            data = await call_backend(
                "GET",
                "/bookings/voice/activities/",
                params={"search": args.get("search", "")},
            )
        elif params.function_name == "create_general_inquiry":
            data = await call_backend(
                "POST",
                "/bookings/voice/inquiries/",
                json={
                    "session_id": session_id,
                    "inquiry_type": args.get("inquiry_type", "general"),
                    "activity_name": args.get("topic", ""),
                    "customer_name": args.get("customer_name", ""),
                    "contact_phone": args.get("phone", ""),
                    "contact_email": args.get("email", ""),
                    "preferred_callback_time": args.get("preferred_callback_time", ""),
                    "conversation_notes": args.get("details", ""),
                },
            )
        elif params.function_name == "check_availability":
            data = await call_backend(
                "GET",
                "/bookings/voice/availability/",
                params={
                    "activity": args.get("activity", ""),
                    "date": args.get("date", ""),
                    "persons": args.get("persons", 1),
                    "booking_type": args.get("booking_type", "sharing"),
                },
            )
        else:  # create_booking -> creates a booking INQUIRY (team confirms later)
            data = await call_backend(
                "POST",
                "/bookings/voice/inquiries/",
                json={
                    "session_id": session_id,
                    "activity_name": args.get("activity", ""),
                    "requested_date": args.get("date"),
                    "participants": args.get("persons", 1),
                    "booking_type": args.get("booking_type", "sharing"),
                    "pickup_location": args.get("pickup_location", ""),
                    "pickup_time": args.get("pickup_time", ""),
                    "customer_name": args.get("customer_name", ""),
                    "contact_phone": args.get("phone", ""),
                    "contact_email": args.get("email", ""),
                    "quoted_price": args.get("quoted_price"),
                    "special_requests": args.get("special_requests", ""),
                },
            )
        summary = data.get("summary", json.dumps(data))
        logger.info(f"tool {params.function_name} -> {summary[:200]}")
        # Small models sometimes go silent after a tool result. Make the next
        # step unmissable, and explicitly force the LLM to run again.
        data["instruction"] = (
            "Tell the customer this result now, in one or two short spoken sentences."
        )
        await params.result_callback(
            data, properties=FunctionCallResultProperties(run_llm=True)
        )

    llm.register_function("list_activities", handle_booking_tool)
    llm.register_function("check_availability", handle_booking_tool)
    llm.register_function("create_booking", handle_booking_tool)
    llm.register_function("create_general_inquiry", handle_booking_tool)

    # --- Conversation context --------------------------------------------
    context = LLMContext(
        messages=[{"role": "system", "content": agent["system_prompt"]}],
        tools=make_booking_tools(),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),  # turn-taking + barge-in
            user_turn_strategies=UserTurnStrategies(
                # Simple + reliable: end the user's turn after a 0.8s pause.
                # (The default smart-turn ML model can stall on some setups.)
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
            ),
        ),
    )

    # --- Pipeline ----------------------------------------------------------
    pipeline = Pipeline(
        [
            transport.input(),      # mic audio in from browser (WebRTC)
            stt,                    # streaming speech-to-text
            user_aggregator,        # collect user turn into context
            llm,                    # streaming LLM tokens
            tts,                    # streaming text-to-speech
            transport.output(),     # audio out to browser
            assistant_aggregator,   # save bot reply into context
        ]
    )

    rtvi = RTVIProcessor()

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        rtvi_processor=rtvi,
    )

    greeted = False

    async def greet():
        nonlocal greeted
        if greeted or not agent["assistant_speaks_first"]:
            return
        greeted = True
        if agent["first_message"]:
            # Speak the exact scripted greeting, and record it in context
            context.add_message(
                {"role": "assistant", "content": agent["first_message"]}
            )
            await task.queue_frames([TTSSpeakFrame(agent["first_message"])])
        else:
            context.add_message(
                {"role": "system", "content": "Greet the caller briefly."}
            )
            await task.queue_frames([LLMRunFrame()])

    # Best timing: greet when the client says it's ready (data channel open,
    # audio path fully established) — no audio gets lost.
    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi_proc):
        logger.info("Client ready — greeting")
        await greet()  # greet FIRST — set_bot_ready must never block it
        try:
            await rtvi_proc.set_bot_ready()
        except Exception as e:
            logger.warning(f"set_bot_ready failed (non-fatal): {e}")

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")

        # Fallback: if the client never sends client-ready (e.g. a plain
        # WebRTC client without RTVI), greet a few seconds after connect.
        async def delayed_greet():
            await asyncio.sleep(3)
            if not greeted:
                logger.info("No client-ready received — fallback greeting")
                await greet()

        asyncio.create_task(delayed_greet())

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await task.cancel()

    runner = PipelineRunner(handle_sigint=runner_args.handle_sigint)
    try:
        await runner.run(task)
    finally:
        if aiohttp_session:
            await aiohttp_session.close()


async def bot(runner_args: RunnerArguments):
    """Entry point used by the Pipecat runner."""
    transport_params = {
        "webrtc": lambda: TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        ),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
