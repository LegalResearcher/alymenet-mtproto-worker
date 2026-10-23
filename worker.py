import os
from datetime import datetime, timezone

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

SOURCE = "ALYEMENNET"
STATE_TABLE = "alymenet_mtproto_state"
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION_STRING"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DB_HEADERS = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
}


def db(path, method="GET", payload=None, headers=None):
    response = requests.request(
        method,
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={**DB_HEADERS, **(headers or {})},
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response


def get_cursor():
    rows = db(f"{STATE_TABLE}?select=source_message_id&order=source_message_id.desc&limit=1").json()
    return int(rows[0]["source_message_id"]) if rows else 0


def claim(message_id):
    response = db(
        STATE_TABLE,
        method="POST",
        payload={"source_message_id": message_id, "status": "processing"},
        headers={"Prefer": "resolution=ignore-duplicates,return=representation"},
    )
    return bool(response.json())


def finish(message_id, ok):
    db(
        f"{STATE_TABLE}?source_message_id=eq.{message_id}",
        method="PATCH",
        payload={
            "status": "forwarded" if ok else "failed",
            "forwarded_at": datetime.now(timezone.utc).isoformat() if ok else None,
        },
        headers={"Prefer": "return=minimal"},
    )


def release(message_id):
    db(f"{STATE_TABLE}?source_message_id=eq.{message_id}", method="DELETE")


def bot_username():
    response = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=30)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok") or not data.get("result", {}).get("username"):
        raise RuntimeError(f"Telegram getMe failed: {data}")
    return data["result"]["username"]


def main():
    cursor = get_cursor()
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    forwarded = 0
    text_fallback = 0
    try:
        with client:
            if not client.is_user_authorized():
                raise RuntimeError("TELEGRAM_SESSION_STRING is not authorized")
            source = client.get_entity(SOURCE)
            target = client.get_entity(bot_username())
            messages = list(client.iter_messages(source, min_id=cursor, limit=50, reverse=True))
            messages = [m for m in messages if m.id > cursor]
            for message in messages:
                if not claim(message.id):
                    continue
                try:
                    try:
                        client.forward_messages(target, message, from_peer=source)
                    except Exception:
                        text = (message.raw_text or "").strip()
                        if not text:
                            raise
                        client.send_message(target, text)
                        text_fallback += 1
                    finish(message.id, True)
                    forwarded += 1
                except Exception:
                    release(message.id)
                    raise
    finally:
        client.disconnect()
    print({"source": SOURCE, "cursor": cursor, "inspected": len(messages), "forwarded": forwarded, "text_fallback": text_fallback})


if __name__ == "__main__":
    main()
