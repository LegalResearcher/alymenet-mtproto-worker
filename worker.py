import os
import re
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


def strip_source_promo_footer(text):
    lines = text.splitlines()
    header = re.compile(r"قناة\s*#?\s*اليمن[\s_ـ-]*نت.*(?:تيليجرام|تليجرام|تلغرام|روابط?|الرابط)", re.IGNORECASE)
    alias = re.compile(r"ALYEMENNET|ALYMENET|YEMENNET|YMENET", re.IGNORECASE)
    for index, line in enumerate(lines):
        if not header.search(line):
            continue
        tail = lines[index + 1:]
        has_alias = any(alias.search(item) for item in tail)
        footer_only = all(
            not item.strip() or alias.search(item) or re.fullmatch(r"[\s\W_]+", item)
            for item in tail
        )
        if has_alias and footer_only:
            if index > 0 and re.fullmatch(r"ـ{5,}", lines[index - 1].strip()):
                index -= 1
            return "\n".join(lines[:index]).rstrip()
    suffix_start = len(lines)
    while suffix_start > 0:
        item = lines[suffix_start - 1]
        if not item.strip() or alias.search(item) or re.search(r"t\.me/", item, re.IGNORECASE) or re.fullmatch(r"[\s\W_]+", item):
            suffix_start -= 1
        else:
            break
    suffix = lines[suffix_start:]
    if suffix and any(alias.search(item) for item in suffix) and any(re.search(r"t\.me/", item, re.IGNORECASE) for item in suffix):
        footer_start = suffix_start
        yemen_net = re.compile(r"اليمن[\s\u200b\u200c\u200d_ـ-]*نت", re.IGNORECASE)
        promo_marker = re.compile(r"قناة|رسمي|موثوق|تيلي|تلي|روابط?|الرابط|اشتراك", re.IGNORECASE)
        for index in range(suffix_start - 1, max(-1, suffix_start - 9), -1):
            if yemen_net.search(lines[index]) and any(promo_marker.search(item) for item in lines[index:suffix_start]):
                footer_start = index
                break
        if footer_start > 0 and re.fullmatch(r"ـ{5,}", lines[footer_start - 1].strip()):
            footer_start -= 1
        return "\n".join(lines[:footer_start]).rstrip()
    return text


def copy_to_bot(client, target, source, message, test_nonce=None):
    rich = getattr(message, "rich_message", None)
    if rich is not None:
        if getattr(rich, "photos", None) or getattr(rich, "documents", None):
            raise RuntimeError("RichMessage contains embedded media not supported by the inbox text bridge")
        text = strip_source_promo_footer(rich_message_text(getattr(rich, "blocks", None))).strip()
        if not text:
            raise RuntimeError("RichMessage contained no extractable text")
        if test_nonce:
            text += f"\nhttps://example.invalid/mtproto-test-{test_nonce}"
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
            export_text = os.environ.get("EXPORT_TEXT", "").lower() in {"1", "true", "yes"}
            if export_text and not inspect_only:
                raise ValueError("EXPORT_TEXT requires INSPECT_ONLY")
            probe_bot = os.environ.get("PROBE_BOT", "").lower() in {"1", "true", "yes"}
            if probe_bot:
                if test_message_id or inspect_only:
                    raise ValueError("PROBE_BOT cannot be combined with TEST_MESSAGE_ID or INSPECT_ONLY")
                probe_result = client.get_messages(source, ids=319910)
                probe_message = probe_result[0] if isinstance(probe_result, (list, tuple)) else probe_result
                rich = getattr(probe_message, "rich_message", None)
                body = rich_message_text(getattr(rich, "blocks", None)) if rich is not None else ""
                if not body:
                    raise RuntimeError("Source probe has no rich text")
                probe_text = "قناة #اليمن_نت\n" + body
                sent = client.send_message(target, probe_text, parse_mode=None)
                print({"mode": "non_publishing_bot_probe", "message_id": sent.id, "text_length": len(probe_text), "sent": True})
                return
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
                    blocks = getattr(rich, "blocks", []) if rich is not None else []
                    session_user = client.get_me()
                    webhook_response = requests.get(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=30
                    )
                    webhook_info = webhook_response.json().get("result", {}) if webhook_response.ok else {}
                    source_text = rich_message_text(getattr(rich, "blocks", None)) if rich is not None else ""
                    clean_source_text = strip_source_promo_footer(source_text)
                    blocked_phrases = [
                        "ALYMENET رابط اليمن نت الرسمي", "قنوات مزورة", "القنوات المزورة", "الصفحات مشبوهة",
                        "صفحات مشبوهه", "وتنتحل اسم وشعار قناة #اليمن_نت", "تنتحل اسم وشعار قناة #اليمن_نت",
                        "قنواتهم المزورة", "قنواتهم المزوره", "لذلك وجب التنويه", "لذلك وجب التنوية",
                        "ونخلي مسؤوليتنا عن اي محتوى", "نخلي مسؤوليتنا عن اي محتوى", "قناة اليمن نت الرئيسية",
                        "قناة #اليمن_نت الرئيسية", "سجل القناة الرئيسية", "متابعينا الكرام", "يمكنكم متابعة",
                        "أخبار اليمن نت", "اخبار اليمن نت", "قناتنا الرئيسية", "قناتنا الرئيسيه",
                        "قناتنا المؤقتة", "القناة مؤقتا", "عبر الرابط", "مصدرك الأول لأخبار #اليمن", "قناة #اليمن_نت",
                    ]
                    matched_blocked_phrases = [p for p in blocked_phrases if p.casefold() in clean_source_text.casefold()]
                    text_without_urls = re.sub(r"(?:https?://|ftp://|www\.)[^\s<>()\[\]{}\"']+", " ", clean_source_text)
                    bad_yemen_net_match = bool(re.search(r"اليمن[\s\u200b\u200c\u200d_ـ-]*نت", text_without_urls, re.IGNORECASE))
                    bad_hizam_asad_match = bool(re.search(r"حزام[\s\u200b\u200c\u200d_ـ-]*ال[أا]سد", text_without_urls, re.IGNORECASE))
                    bad_source_link_match = bool(re.search(r"t\.me/(?:s/)?ALYMENET", text_without_urls, re.IGNORECASE))
                    print({
                        "mode": "read_only_inspection",
                        "source": SOURCE,
                        "message_id": message_id,
                        "session_account_is_bot": bool(getattr(session_user, "bot", False)),
                        "target_is_bot": bool(getattr(target, "bot", False)),
                        "webhook_pending_updates": webhook_info.get("pending_update_count"),
                        "webhook_last_error_date": webhook_info.get("last_error_date"),
                        "message_class": type(message).__name__,
                        "rich_message_class": type(rich).__name__ if rich is not None else None,
                        "rich_block_classes": [type(block).__name__ for block in blocks[:30]],
                        "rich_text_length": len(source_text),
                        "cleaned_rich_text_length": len(clean_source_text),
                        "source_footer_removed": len(clean_source_text) < len(source_text),
                        "blocked_channel_phrase_remains": "قناة #اليمن_نت" in clean_source_text,
                        "source_alias_remains": bool(re.search(r"ALYEMENNET|ALYMENET", clean_source_text, re.IGNORECASE)),
                        "matched_blocked_phrases": matched_blocked_phrases,
                        "bad_yemen_net_match": bad_yemen_net_match,
                        "bad_hizam_asad_match": bad_hizam_asad_match,
                        "bad_source_link_match": bad_source_link_match,
                        "media_class": type(media).__name__ if media is not None else None,
                        "has_text": bool(getattr(message, "raw_text", "")),
                        "text_length": len(getattr(message, "raw_text", "") or ""),
                        "extracted_text": source_text if export_text else None,
                        "cleaned_extracted_text": clean_source_text if export_text else None,
                    })
                    return
                nonce = f"{message_id}-{int(datetime.now(timezone.utc).timestamp())}"
                copy_to_bot(client, target, source, message, test_nonce=nonce)
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
