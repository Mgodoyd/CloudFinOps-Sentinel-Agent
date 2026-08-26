#!/usr/bin/env bash
# Resolve the chat id for the configured bot and write it into .env.
#
# A Telegram bot cannot open a conversation, so getUpdates stays empty until a
# person messages it first. That ordering is the whole reason this script
# exists: without it the token looks configured and nothing is ever delivered.
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)
[ -n "$TOKEN" ] || { echo "TELEGRAM_BOT_TOKEN is not set in .env"; exit 1; }

BOT=$(curl -s "https://api.telegram.org/bot$TOKEN/getMe" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["username"])')
echo "Bot: @$BOT"

CHAT=$(curl -s "https://api.telegram.org/bot$TOKEN/getUpdates" | python3 -c '
import json, sys
for u in json.load(sys.stdin).get("result", []):
    chat = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
    if chat.get("id"):
        print(chat["id"]); break
')

if [ -z "$CHAT" ]; then
  echo "No chat yet. Send @$BOT any message, then run this again."
  exit 1
fi

python3 - "$CHAT" <<'PY'
import re, sys
chat = sys.argv[1]
env = open(".env").read()
env = (re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={chat}", env, flags=re.M)
       if re.search(r"^TELEGRAM_CHAT_ID=", env, flags=re.M)
       else env.rstrip("\n") + f"\nTELEGRAM_CHAT_ID={chat}\n")
open(".env", "w").write(env)
PY
echo "TELEGRAM_CHAT_ID=$CHAT written to .env"
