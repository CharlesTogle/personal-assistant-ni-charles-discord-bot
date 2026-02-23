import os
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
import httpx

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

app = FastAPI()

# ── Auth ────────────────────────────────────────────────────────────────────
AUTHORIZED_DISCORD_IDS = {
    discord_id.strip()
    for discord_id in os.environ.get("AUTHORIZED_DISCORD_IDS", "").split(",")
    if discord_id.strip()
}

LLAMA_URL   = os.environ.get("LLAMA_URL",   "http://localhost:8080/completion")
ANDROID_URL = os.environ.get("ANDROID_URL", "http://localhost:8081")
ANDROID_LOCAL_URL = os.environ.get("ANDROID_LOCAL_URL", "http://192.168.1.x:8081")

ANDROID_AUTH = (
    os.environ.get("ANDROID_AUTH_USER", "assistant"),
    os.environ.get("ANDROID_AUTH_PASS", "password"),
)

def verify_discord_id(discord_id: str):
    if discord_id not in AUTHORIZED_DISCORD_IDS:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── LLM ─────────────────────────────────────────────────────────────────────
async def query_llm(prompt: str) -> str:
    """Send a prompt to TinyLlama and return the raw text response."""
    SYSTEM_PROMPT = """You are a phone assistant. Reply with ONE of:
- Plain text: when greeted or asked capabilities (list: set_alarm, send_sms, play_spotify, send_email, get_notifications)
- Raw JSON only: {"action":"<name>","params":{}} when user picks an action
Never use markdown or backticks."""

    formatted = (
        f"### System:\n{SYSTEM_PROMPT}\n\n"
        f"### User:\n{prompt}\n\n"
        f"### Assistant:\n"
    )

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(LLAMA_URL, json={
            "prompt": formatted,
            "n_predict": 64,
            "temperature": 0.1,
            "top_k": 10,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "cache_prompt": True,
            "stop": ["### User:", "\n###", "\n\n"],
        })
        response.raise_for_status()
        payload = response.json()
        # llama.cpp /completion -> {"content": "..."}
        # local proxy (8081)      -> {"response": "..."}
        text = payload.get("content")
        if text is None:
            text = payload.get("response")
        if text is None and isinstance(payload.get("choices"), list) and payload["choices"]:
            text = payload["choices"][0].get("text", "")

        result = (text or "").strip()
        print(
            "DEBUG query_llm",
            {
                "url": LLAMA_URL,
                "status": response.status_code,
                "keys": list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
                "result_len": len(result),
                "preview": result[:120],
            },
        )
        return result


# ── Android command forwarder ────────────────────────────────────────────────
async def send_to_android(command: dict) -> dict:
    """
    Try local IP first (fast, same WiFi).
    Fall back to ngrok if local times out.
    """
    async with httpx.AsyncClient(timeout=2) as client:
        try:
            r = await client.post(
                f"{ANDROID_LOCAL_URL}/command",
                json=command,
                auth=ANDROID_AUTH,
            )
            return r.json()
        except Exception:
            pass  # local failed, try ngrok

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{ANDROID_URL}/command",
            json=command,
            auth=ANDROID_AUTH,
        )
        return r.json()


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/", response_class=PlainTextResponse)
async def root():
    return "OK"


@app.get("/health", response_class=PlainTextResponse)
async def health():
    """Quick liveness check — also pings llama-server."""
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

    Expected body:
    {
        "discord_id": "123456789",
        "message":    "set an alarm for 7am"
    }

    Returns:
    {
        "ok": true,
        "action": "set_alarm",
        "android_response": { ... }   # only if command was forwarded
    }
    """
    data = await request.json()

    # Auth
    verify_discord_id(data.get("discord_id", ""))

    message = data.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    # Ask TinyLlama to parse intent
    try:
        llm_reply = await query_llm(message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    # Try to parse as a command
    import json
    try:
        command = json.loads(llm_reply)
        action  = command.get("action", "unknown")

        if action == "unknown":
            # Conversational reply — just return the text
            return {"ok": True, "action": "chat", "reply": llm_reply}

        # Forward to Android app
        try:
            android_response = await send_to_android(command)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Android unreachable: {e}")

        return {
            "ok":               True,
            "action":           action,
            "params":           command.get("params", {}),
            "android_response": android_response,
        }

    except json.JSONDecodeError:
        # Not a command — pure conversational reply
        return {"ok": True, "action": "chat", "reply": llm_reply}


@app.post("/command")
async def raw_command(request: Request):
    """
    Bypass LLM — send a command directly to the Android app.
    Useful for testing without going through TinyLlama.

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
