#!/usr/bin/env python3
import json
import os
import traceback
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
    os.environ.get("ANDROID_AUTH_PASS", "password"),
)
SIMULATE_ANDROID = os.environ.get("SIMULATE_ANDROID", "true").lower() in {"1", "true", "yes", "on"}

SYSTEM_PROMPT = """You are PhoneBot. You only do two things:
1. If the user says hi or asks what you can do, reply with exactly this:
Hi! I can help you with:
1) set_alarm - Set an alarm
2) send_sms - Send a text
3) play_spotify - Play music
4) send_email - Send an email
5) get_notifications - Read notifications
Which would you like?

2. If the user picks an action, reply with ONLY a raw JSON object. No markdown. No backticks. No explanation.
Examples:
{"action":"set_alarm","params":{"time":"7:00 AM"}}
{"action":"send_sms","params":{"to":"John","message":"I'll be late"}}
{"action":"play_spotify","params":{"query":"lo-fi beats"}}
{"action":"send_email","params":{"to":"boss@work.com","subject":"Sick day","body":"I'm not feeling well today."}}
{"action":"get_notifications","params":{}}

Do not explain. Do not write code. Do not write examples outside of JSON."""

N_PREDICT = 128
STOP_WORDS = ["### User:", "\n###"]


# ── Auth ─────────────────────────────────────────────────────────────────────
def verify_discord_id(discord_id: str):
    if discord_id not in AUTHORIZED_DISCORD_IDS:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── LLM ──────────────────────────────────────────────────────────────────────
async def query_llm(prompt: str) -> str:
    formatted = (
        f"### System:\n{SYSTEM_PROMPT}\n\n"
        f"### User:\n{prompt}\n\n"
        f"### Assistant:\n"
    )

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(LLAMA_URL, json={
            "prompt":         formatted,
            "n_predict":      N_PREDICT,
            "temperature":    0.1,
            "top_k":          10,
            "top_p":          0.9,
            "repeat_penalty": 1.1,
            "cache_prompt":   True,
            "stop":           STOP_WORDS,
        })
        response.raise_for_status()
        payload = response.json()

        # support llama.cpp /completion → "content" and proxy → "response"
        text = (
            payload.get("content")
            or payload.get("response")
            or (payload.get("choices") or [{}])[0].get("text", "")
        )
        text = (text or "").strip()

        if not text:
            try:
                payload_preview = json.dumps(payload, ensure_ascii=True)[:500]
            except Exception:
                payload_preview = str(payload)[:500]
            print(
                "DEBUG LLM EMPTY"
                f" | url={LLAMA_URL}"
                f" | status={response.status_code}"
                f" | keys={list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}"
                f" | payload={payload_preview!r}"
            )
        else:
            print(f"DEBUG LLM | url={LLAMA_URL} | len={len(text)} | preview={text[:120]!r}")
        return text


def extract_action_json(text: str) -> dict | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    snippet = text[start : idx + 1]
                    try:
                        obj = json.loads(snippet)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and "action" in obj:
                        # Common typo from tiny models
                        if obj.get("action") == "set_alairm":
                            obj["action"] = "set_alarm"
                        obj.setdefault("params", {})
                        return obj
                    break
        start = text.find("{", start + 1)
    return None


# ── Android forwarder ─────────────────────────────────────────────────────────
async def send_to_android(command: dict) -> dict:
    if SIMULATE_ANDROID:
        return {
            "status": "simulated_success",
            "message": f"Simulated: executed {command.get('action')}",
        }

    """Try local first (fast), fall back to ngrok."""
    for url, timeout in [(ANDROID_LOCAL_URL, 2), (ANDROID_URL, 15)]:
        try:
            print(f"DEBUG ANDROID try url={url} timeout={timeout}s command={command}")
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{url}/command", json=command, auth=ANDROID_AUTH)
                print(f"DEBUG ANDROID success url={url} status={r.status_code} body={r.text[:160]!r}")
                return r.json()
        except Exception as e:
            print(f"DEBUG ANDROID fail url={url} error={type(e).__name__}: {e}")
            continue
    raise RuntimeError("Android app unreachable on both local and ngrok")


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(LLAMA_URL.replace("/completion", "/health"))
            llm = "ok" if r.status_code == 200 else "unreachable"
    except Exception:
        llm = "unreachable"
    return f"server=ok llm={llm}"


@app.post("/task")
async def create_task(request: Request):
    """
    Called by the Discord bot.
    Body:    { "discord_id": "123", "message": "set an alarm for 7am" }
    Returns: { "ok": true, "action": "chat"|"set_alarm"|..., "reply"?: "...", "android_response"?: {...} }
    """
    data = await request.json()
    verify_discord_id(data.get("discord_id", ""))

    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    try:
        llm_reply = await query_llm(message)
    except Exception as e:
        print(f"DEBUG TASK llm_error type={type(e).__name__} error={e}")
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    if not llm_reply:
        return {"ok": True, "action": "chat", "reply": "The model returned an empty response, please try again."}

    # Try strict JSON first, then JSON extraction from noisy output.
    try:
        command = json.loads(llm_reply)
    except json.JSONDecodeError:
        command = extract_action_json(llm_reply)
        if command is None:
            return {"ok": True, "action": "chat", "reply": llm_reply}

    action = command.get("action", "unknown")
    if action == "unknown":
        return {"ok": True, "action": "chat", "reply": llm_reply}

    try:
        android_response = await send_to_android(command)
    except Exception as e:
        print(f"DEBUG TASK android_error type={type(e).__name__} error={e}")
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=str(e))

    return {
        "ok": True,
        "action": action,
        "params": command.get("params", {}),
        "android_response": android_response,
    }


@app.post("/command")
async def raw_command(request: Request):
    """
    Bypass LLM — send a command directly to Android. Good for testing.
    Body: { "discord_id": "...", "action": "set_alarm", "params": { "time": "7:00 AM" } }
    """
    data = await request.json()
    verify_discord_id(data.get("discord_id", ""))

    command = {"action": data.get("action"), "params": data.get("params", {})}
    if not command["action"]:
        raise HTTPException(status_code=400, detail="action is required")

    try:
        android_response = await send_to_android(command)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"ok": True, "android_response": android_response}
