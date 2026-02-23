#!/usr/bin/env python3
import json
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

app = FastAPI()

# ── Config ───────────────────────────────────────────────────────────────────
AUTHORIZED_DISCORD_IDS = {
    discord_id.strip()
    for discord_id in os.environ.get("AUTHORIZED_DISCORD_IDS", "").split(",")
    if discord_id.strip()
}

LLAMA_URL         = os.environ.get("LLAMA_URL",         "http://localhost:8080/completion")
ANDROID_URL       = os.environ.get("ANDROID_URL",       "http://localhost:8081")
ANDROID_LOCAL_URL = os.environ.get("ANDROID_LOCAL_URL", "http://192.168.1.x:8081")
ANDROID_AUTH      = (
    os.environ.get("ANDROID_AUTH_USER", "assistant"),
    os.environ.get("ANDROID_AUTH_PASS", "changeme"),
)

# ── Intent patterns ──────────────────────────────────────────────────────────
# Ordered by specificity — more specific patterns first
INTENT_PATTERNS = [
    # read_email_summary
    (re.compile(
        r"\b(read|summari[sz]e|show)\b.{0,40}\b(email|emails|inbox|gmail|outlook)\b",
        re.I), "read_email_summary"),
    # read_text_messages
    (re.compile(
        r"\b(read|summari[sz]e|show)\b.{0,40}\b(text|texts|sms|messages)\b",
        re.I), "read_text_messages"),
    # add_calendar_reminder
    (re.compile(
        r"\b(calendar|reminder|remind me|schedule|event|appointment)\b",
        re.I), "add_calendar_reminder"),
    # add_note
    (re.compile(
        r"\b(note|notes|notepad|memo|jot|write this down)\b",
        re.I), "add_note"),
    # send_sms
    (re.compile(
        r"\b(text|sms|tell|say to|msg|message)\b.{0,40}\b\w+\b",
        re.I), "send_sms"),
    # send_email
    (re.compile(
        r"\b(email|e-mail|mail)\b",
        re.I), "send_email"),
    # set_alarm
    (re.compile(
        r"\b(alarm|wake|remind|reminder)\b",
        re.I), "set_alarm"),
    # play_spotify
    (re.compile(
        r"\b(play|music|song|spotify|listen|queue|shuffle|track)\b",
        re.I), "play_spotify"),
    # get_notifications
    (re.compile(
        r"\b(notification|notif|alert|update|miss|inbox|read|check|catch up|show me|what did|anything new|what.s new)\b",
        re.I), "get_notifications"),
]

# Param extraction prompts — short and focused so the model stays on task
PARAM_PROMPTS = {
    "read_text_messages": (
        'Extract filters for reading/summarizing text messages.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"scope":"<all_unread|count|contact>","count":<number|null>,"contact":"<name or null>"}}\n\n'
        'User input: "{message}"'
    ),
    "read_email_summary": (
        'Extract filters for reading/summarizing unread emails.\n'
        'Provider must be gmail or outlook.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"provider":"<gmail|outlook>","count":<number>}}\n\n'
        'User input: "{message}"'
    ),
    "add_calendar_reminder": (
        'Extract the reminder title and time/date from the user input.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"title": "<reminder title>", "time": "<time or date/time>"}}\n\n'
        'User input: "{message}"'
    ),
    "add_note": (
        'Extract the note content from the user input.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"content": "<note text>"}}\n\n'
        'User input: "{message}"'
    ),
    "send_sms": (
        'Extract the recipient name and message from the user input.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"to": "<name>", "message": "<text>"}}\n\n'
        'User input: "{message}"'
    ),
    "send_email": (
        'Extract the email provider, recipient, subject, and message from the user input.\n'
        'Provider must be gmail or outlook.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"provider":"<gmail|outlook>", "to": "<name or email>", "subject": "<subject>", "message": "<text>"}}\n\n'
        'User input: "{message}"'
    ),
    "set_alarm": (
        'Extract the time from the user input.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"time": "<HH:MM AM/PM>"}}\n\n'
        'User input: "{message}"'
    ),
    "play_spotify": (
        'Extract the song, artist, or playlist from the user input.\n'
        'Reply with ONLY this JSON, no extra text:\n'
        '{{"query": "<song or artist>"}}\n\n'
        'User input: "{message}"'
    ),
    "get_notifications": None,  # no params needed
}

CAPABILITIES_TEXT = """\
Hi! I'm Sepher. Here's what I can do:
1) **set_alarm** — Set an alarm (e.g. "set alarm for 7am")
2) **send_sms** — Send a text (e.g. "text Stefanie I love you")
3) **play_spotify** — Play music (e.g. "play lo-fi")
4) **send_email** — Send email with provider (e.g. "send gmail email to john@example.com about meeting")
5) **get_notifications** — Read your notifications (e.g. "read my notifications")
6) **add_calendar_reminder** — Add a calendar reminder (e.g. "remind me tomorrow at 9am to call mom")
7) **add_note** — Add a new note (e.g. "note: buy milk and eggs")
8) **read_text_messages** — Summarize unread texts (e.g. "summarize unread texts" or "read 5 texts from Sam")
9) **read_email_summary** — Summarize unread emails (e.g. "read first 10 unread gmail emails")

Just tell me what you need!\
"""


# ── Auth ─────────────────────────────────────────────────────────────────────
def verify_discord_id(discord_id: str):
    if discord_id not in AUTHORIZED_DISCORD_IDS:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Intent matching ───────────────────────────────────────────────────────────
def match_intent(message: str) -> str | None:
    """Fast regex-based intent detection. Returns action name or None."""
    for pattern, action in INTENT_PATTERNS:
        if pattern.search(message):
            return action
    return None


def is_greeting(message: str) -> bool:
    pattern = re.compile(
        r"^\s*(hi|hello|hey|sup|what can you do|help|capabilities|commands|menu|start)\b",
        re.I,
    )
    return bool(pattern.match(message))


def strip_emojis(text: str) -> str:
    # Remove common emoji/pictograph Unicode blocks.
    return re.sub(
        r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF\u2600-\u26FF]+",
        "",
        text,
    ).strip()


# ── LLM param extraction ──────────────────────────────────────────────────────
async def extract_params(action: str, message: str) -> dict:
    """
    Use the LLM only to extract params from the message,
    given that we already know the action via regex.
    Much more reliable than asking the model to figure out both.
    """
    prompt_template = PARAM_PROMPTS.get(action)

    # no params needed for this action
    if prompt_template is None:
        return {}

    prompt = f"### User:\n{prompt_template.format(message=message)}\n\n### Assistant:\n"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(LLAMA_URL, json={
                "prompt":         prompt,
                "n_predict":      60,
                "temperature":    0.1,
                "top_k":          10,
                "top_p":          0.9,
                "repeat_penalty": 1.1,
                "cache_prompt":   True,
                "stop":           ["### User:", "\n###", "\n\n", "```"],
            })
            response.raise_for_status()
            payload = response.json()

            raw = (
                payload.get("content")
                or payload.get("response")
                or ""
            ).strip()

            print(f"DEBUG params | action={action} | raw={raw!r}")

            # strip markdown fences just in case
            clean = raw.strip("`").strip()
            if clean.startswith("json"):
                clean = clean[4:].strip()

            return json.loads(clean)

    except json.JSONDecodeError:
        print(f"DEBUG params | JSON parse failed, returning empty params")
        return {}
    except Exception as e:
        print(f"DEBUG params | error: {e}, returning empty params")
        return {}


async def query_llm_chat(message: str) -> str:
    """
    For messages that don't match any intent pattern,
    let the LLM respond conversationally.
    """
    prompt = (
        "### System:\n"
        "You are Sepher, AI Assistant ni Charles, a personal phone assistant. "
        "Your personality is cool, funky, and confident while staying helpful and clear. "
        "You can set alarms, send texts, play Spotify, send emails, read notifications, add calendar reminders, add notes, summarize unread text messages, and summarize unread emails. "
        "If asked who you are or what you do, introduce yourself briefly. "
        "If asked something you cannot do, say so politely and suggest what you can do instead. "
        "Keep replies short and friendly. "
        "Do not use emojis in any response.\n\n"
        f"### User:\n{message}\n\n"
        "### Assistant:\n"
    )

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(LLAMA_URL, json={
            "prompt":         prompt,
            "n_predict":      120,
            "temperature":    0.7,
            "top_k":          40,
            "top_p":          0.9,
            "repeat_penalty": 1.1,
            "cache_prompt":   True,
            "stop":           ["### User:", "\n###"],
        })
        response.raise_for_status()
        payload = response.json()
        raw = (payload.get("content") or payload.get("response") or "").strip()
        return strip_emojis(raw)


# ── Android forwarder ─────────────────────────────────────────────────────────
async def send_to_android(command: dict) -> dict:
    """Forward command to Sepher Android app via LAN first, then fallback URL."""
    action_aliases = {
        "add_note": "create_note",
        "add_calendar_reminder": "add_calendar",
        "read_text_messages": "summarize_sms",
        "read_email_summary": "summarize_email",
    }

    action = command.get("action")
    normalized_action = action_aliases.get(action, action)
    payload = {
        "action": normalized_action,
        "params": command.get("params", {}),
    }

    def build_command_url(base_url: str) -> str:
        return f"{base_url.rstrip('/')}/command"

    urls = []
    if ANDROID_LOCAL_URL.strip():
        urls.append(("local", build_command_url(ANDROID_LOCAL_URL)))
    if ANDROID_URL.strip() and ANDROID_URL.strip() != ANDROID_LOCAL_URL.strip():
        urls.append(("remote", build_command_url(ANDROID_URL)))

    last_error = None
    async with httpx.AsyncClient(timeout=20) as client:
        for mode, url in urls:
            try:
                response = await client.post(
                    url,
                    json=payload,
                    auth=ANDROID_AUTH,
                )
                response.raise_for_status()
                data = response.json()
                data["_forwarded_via"] = mode
                data["_forwarded_url"] = url
                return data
            except Exception as e:
                last_error = f"{mode}:{url} -> {e}"

    raise RuntimeError(last_error or "No Android URL configured")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(LLAMA_URL.replace("/completion", "/health"))
            llm_status = "ok" if r.status_code == 200 else "unreachable"
    except Exception:
        llm_status = "unreachable"
    return f"server=ok llm={llm_status}"


@app.post("/task")
async def create_task(request: Request):
    """
    Main entry point called by the Discord bot.

    Flow:
    1. Greeting?     → return capabilities menu
    2. Regex match?  → extract params via LLM → forward to Android
    3. No match?     → ask user to clarify

    Body:    { "discord_id": "123", "message": "tell Stefanie I love her" }
    Returns: { "ok": true, "action": "send_sms", "params": {...}, "android_response": {...} }
    """
    data = await request.json()
    verify_discord_id(data.get("discord_id", ""))

    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    print(f"DEBUG task | message={message!r}")

    # 1. Greeting / help request
    if is_greeting(message):
        return {"ok": True, "action": "chat", "reply": CAPABILITIES_TEXT}

    # 2. Regex intent detection
    action = match_intent(message)

    if action is None:
        # Fall through to LLM for conversational response
        try:
            llm_reply = await query_llm_chat(message)
        except Exception:
            llm_reply = "Sorry, I didn't understand that. Try saying 'help' to see what I can do."
        return {"ok": True, "action": "chat", "reply": llm_reply}

    print(f"DEBUG task | matched action={action}")

    # 3. Extract params via LLM (focused task — much more reliable)
    params = await extract_params(action, message)

    command = {"action": action, "params": params}
    print(f"DEBUG task | command={command}")

    # 4. Forward to Android
    try:
        android_response = await send_to_android(command)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Android unreachable: {e}")

    return {
        "ok":               True,
        "action":           action,
        "params":           params,
        "android_response": android_response,
    }


@app.post("/command")
async def raw_command(request: Request):
    """
    Bypass LLM entirely — send a raw command directly to Android.
    Useful for testing individual actions without intent parsing.

    Body: { "discord_id": "...", "action": "set_alarm", "params": { "time": "7:00 AM" } }
    """
    data = await request.json()
    verify_discord_id(data.get("discord_id", ""))

    command = {
        "action": data.get("action"),
        "params": data.get("params", {}),
    }
    if not command["action"]:
        raise HTTPException(status_code=400, detail="action is required")

    try:
        android_response = await send_to_android(command)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Android unreachable: {e}")

    return {"ok": True, "android_response": android_response}
