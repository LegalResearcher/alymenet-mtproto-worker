import os
from datetime import datetime, timezone

import requests
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.patched import MessageService

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


def rich_message_text(node, depth=0):
    """Flatten Telegram RichMessage blocks to their visible text only."""
    if node is None or depth > 12:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, (list, tuple)):
        return "\n".join(part for part in (rich_message_text(item, depth + 1) for item in node) if part)

    parts = []
    for field in ("text", "texts", "title", "subtitle", "caption", "author", "date", "items", "rows", "cells", "blocks"):
        value = getattr(node, field, None)
        if value is not None:
            text = rich_message_text(value, depth + 1)
            if text and text not in parts:
                parts.append(text)
    return "\n".join(parts)


def copy_to_bot(client, target, source, message):
    rich = getattr(message, "rich_message", None)
    if rich is not None:
        if getattr(rich, "photos", None) or getattr(rich, "documents", None):
            raise RuntimeError("RichMessage contains embedded media not supported by the inbox text bridge")
        text = rich_message_text(getattr(rich, "blocks", None)).strip()
        if not text:
            raise RuntimeError("RichMessage contained no extractable text")
        # Bot API's inbox handler only accepts ordinary text/captions and known
        # media fields, so convert this new rich-message type to plain text.
        client.send_message(target, text, parse_mode=None)
        return
    # For regular Telegram messages, drop_author copies the complete server-side
    # payload (including supported media) without a forwarded-from header.
    client.forward_messages(target, message, from_peer=source, drop_author=True)


def main():
    cursor = get_cursor()
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    copied = 0
    try:
        with client:
            if not client.is_user_authorized():
                raise RuntimeError("TELEGRAM_SESSION_STRING is not authorized")
            source = client.get_entity(SOURCE)
            target = client.get_entity(bot_username())
            test_message_id = os.environ.get("TEST_MESSAGE_ID", "").strip()
            inspect_only = os.environ.get("INSPECT_ONLY", "").lower() in {"1", "true", "yes"}
            if inspect_only and not test_message_id:
                raise ValueError("INSPECT_ONLY requires TEST_MESSAGE_ID")
            if test_message_id:
                message_id = int(test_message_id)
                if message_id <= 0:
                    raise ValueError("TEST_MESSAGE_ID must be a positive Telegram message ID")
                result = client.get_messages(source, ids=message_id)
                message = result[0] if isinstance(result, (list, tuple)) else result
                if message is None:
                    raise RuntimeError(f"Source message {message_id} was not found")
                if inspect_only:
                    media = getattr(message, "media", None)
                    rich = getattr(message, "rich_message", None)
                    raw_message = message.to_dict() if hasattr(message, "to_dict") else {}
                    def shape(value, depth=0):
                        if value is None:
                            return None
                        if isinstance(value, str):
                            return {"type": "str", "length": len(value)}
                        if depth >= 7:
                            return {"type": type(value).__name__, "truncated": True}
                        if isinstance(value, (list, tuple)):
                            result = [shape(item, depth + 1) for item in value[:20]]
                            if len(value) > 20:
                                result.append({"omitted_items": len(value) - 20})
                            return result
                        if hasattr(value, "__dict__"):
                            return {
                                "type": type(value).__name__,
                                "fields": {key: shape(item, depth + 1) for key, item in vars(value).items()},
                            }
                        return {"type": type(value).__name__}
                    hidden_fields = {"message", "media", "entities", "from_id", "peer_id", "saved_peer_id", "fwd_from"}
                    optional_objects = {}
                    for field in ("action", "reply_markup", "suggested_post", "factcheck", "replies", "reactions"):
                        value = getattr(message, field, None)
                        if value is not None:
                            optional_objects[field] = type(value).__name__
                    print({
                        "mode": "read_only_inspection",
                        "source": SOURCE,
                        "message_id": message_id,
                        "message_class": type(message).__name__,
                        "telegram_constructor": raw_message.get("_"),
                        "rich_message_shape": shape(getattr(message, "rich_message", None)),
                        "rich_text_length": len(rich_message_text(getattr(rich, "blocks", None))) if rich is not None else 0,
                        "present_fields": sorted(
                            key for key, value in raw_message.items()
                            if value is not None and key not in hidden_fields and key != "_"
                        ),
                        "media_class": type(media).__name__ if media is not None else None,
                        "media_fields": sorted(vars(media).keys()) if media is not None and hasattr(media, "__dict__") else [],
                        "optional_object_classes": optional_objects,
                        "has_text": bool(getattr(message, "raw_text", "")),
                        "text_length": len(getattr(message, "raw_text", "") or ""),
                    })
                    return
                copy_to_bot(client, target, source, message)
                print({"mode": "single_message_test", "source": SOURCE, "message_id": message_id, "copied": True})
                return
            messages = list(client.iter_messages(source, min_id=cursor, limit=50, reverse=True))
            messages = [
                m for m in messages
                if m.id > cursor and not isinstance(m, MessageService)
            ]
            for message in messages:
                if not claim(message.id):
                    continue
                try:
                    copy_to_bot(client, target, source, message)
                    finish(message.id, True)
                    copied += 1
                except Exception:
                    release(message.id)
                    raise
    finally:
        client.disconnect()
    print({"source": SOURCE, "cursor": cursor, "inspected": len(messages), "copied": copied})


if __name__ == "__main__":
    main()
