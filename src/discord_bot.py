import json
import os
from pathlib import Path

import discord
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8000").rstrip("/")
AUTHORIZED_DISCORD_IDS = {
    discord_id.strip()
    for discord_id in os.environ.get("AUTHORIZED_DISCORD_IDS", "").split(",")
    if discord_id.strip()
}

if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN is missing in .env")

if not AUTHORIZED_DISCORD_IDS:
    raise RuntimeError("AUTHORIZED_DISCORD_IDS is missing in .env")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
DISCORD_MESSAGE_LIMIT = 2000


@client.event
async def on_ready():
    print(f"Bot online as {client.user}")


async def safe_edit(target_message: discord.Message, content: str):
    text = (content or "").strip() or "No response"
    if len(text) > DISCORD_MESSAGE_LIMIT:
        text = text[: DISCORD_MESSAGE_LIMIT - 20] + "\n\n[truncated]"
    await target_message.edit(content=text)


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # In guild channels, only respond when the bot is mentioned.
    # In DMs, keep responding without mention.
    if message.guild is not None and client.user not in message.mentions:
        return

    if str(message.author.id) not in AUTHORIZED_DISCORD_IDS:
        return

    content = message.content
    if message.guild is not None and client.user is not None:
        # Remove direct mention token before forwarding to backend.
        content = content.replace(client.user.mention, "").strip()

    if not content:
        return

    processing = await message.reply("Processing...")

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            response = await http.post(
                f"{SERVER_URL}/task",
                json={
                    "discord_id": str(message.author.id),
                    "message": content,
                },
            )
            response.raise_for_status()
            data = response.json()

        if not data.get("ok"):
            print(f"DEBUG task response (not ok): {data}")
            await safe_edit(processing, f"Server error: {data}")
            return

        action = data.get("action")
        print(f"DEBUG task response action={action!r} payload={data}")

        if action == "chat":
            reply = data.get("reply", "")
            print(f"DEBUG reply: '{reply}'")  # add this temporarily
            await safe_edit(processing, reply or "No response")
            return

        params = data.get("params", {})
        android = data.get("android_response", {})
        params_json = json.dumps(params, indent=2, ensure_ascii=True)

        await safe_edit(
            processing,
            (
                f"Completed: **{action}**\n"
                f"```json\n{params_json}\n```\n"
                f"Phone status: {android.get('status', 'done')}\n"
                f"Reason: {android.get('reason', android.get('message', 'n/a'))}"
            ),
        )

    except httpx.TimeoutException:
        await safe_edit(processing, "Timed out: server or phone did not respond in time")
    except httpx.HTTPStatusError as exc:
        await safe_edit(processing, f"HTTP error: {exc.response.status_code} {exc.response.text}")
    except Exception as exc:
        await safe_edit(processing, f"Error: {exc}")


if __name__ == "__main__":
    client.run(TOKEN)
