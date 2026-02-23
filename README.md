# Personal Assistant Discord Bot

Discord bot + FastAPI backend that:
- Receives your Discord message
- Sends it to your local LLM endpoint (`LLAMA_URL`)
- Optionally forwards parsed commands to your Android command server

## Project Structure

- `src/main.py`: FastAPI backend (`/task`, `/command`, `/health`)
- `src/discord_bot.py`: Discord bot client
- `.env`: local secrets/config

## 1. Environment Variables

Create `.env` in project root:

```env
DISCORD_BOT_TOKEN=your_real_bot_token
AUTHORIZED_DISCORD_IDS=123456789012345678

SERVER_URL=http://localhost:8000
LLAMA_URL=http://localhost:8080/completion

ANDROID_URL=https://your-name.ngrok-free.app
ANDROID_LOCAL_URL=http://192.168.1.50:8081
ANDROID_AUTH_USER=assistant
ANDROID_AUTH_PASS=use_a_strong_password
```

Notes:
- `AUTHORIZED_DISCORD_IDS` supports multiple IDs: `id1,id2,id3`
- Keep token/password private
- If token was exposed, rotate it in Discord Developer Portal

## 2. Install Dependencies

## Linux/macOS (normal Python)

```bash
python -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Termux (Android)

Install base packages:

```bash
pkg update && pkg upgrade -y
pkg install -y python git
```

Create fresh venv:

```bash
cd ~/personal-assistant-ni-charles-discord-bot
python -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
```

If `pip install -r requirements.txt` fails on `pydantic-core`:
- Termux + Python 3.12 may fail building Rust wheels for Pydantic v2.
- Use Termux-safe dependency set:

```bash
pip install "fastapi<0.100" "pydantic<2" "uvicorn<0.25" "httpx>=0.27" "python-dotenv>=1.0" "discord.py>=2.4"
```

## 3. Run the Backend (FastAPI)

From project root (with venv activated):

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://localhost:8000/health
```

## 4. Run the Discord Bot

Open a second terminal, activate venv, then:

```bash
python src/discord_bot.py
```

Expected log:

```text
Bot online as <your_bot_name>
```

## 5. How URLs Differ

- `SERVER_URL`: Discord bot -> your FastAPI backend
- `LLAMA_URL`: FastAPI backend -> LLM server
- `ANDROID_LOCAL_URL`: FastAPI backend -> phone server on LAN (first try)
- `ANDROID_URL`: FastAPI backend -> phone server via internet/ngrok (fallback)

## 6. Quick Troubleshooting

- `No command uvicorn found`
  - Install failed or wrong environment.
  - Run `which python` and `which pip` and ensure both point to `.../venv/bin/...`.
  - Reinstall dependencies inside active venv.

- `Failed to build pydantic-core` (Termux)
  - Use the Termux-safe install command above (FastAPI/Pydantic v1 line).

- Bot replies to nobody
  - Check `AUTHORIZED_DISCORD_IDS` contains your exact numeric Discord user ID.

- Bot cannot call backend
  - Verify `SERVER_URL=http://localhost:8000` and backend is running.

- Backend cannot reach Android command server
  - Verify credentials (`ANDROID_AUTH_USER`, `ANDROID_AUTH_PASS`).
  - Verify LAN URL in `ANDROID_LOCAL_URL` and ngrok URL in `ANDROID_URL`.

## 7. Security

- Never commit `.env`
- Rotate `DISCORD_BOT_TOKEN` immediately if leaked
- Use a strong `ANDROID_AUTH_PASS`
