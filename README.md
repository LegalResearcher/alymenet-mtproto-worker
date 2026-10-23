# ALYEMENNET MTProto bridge

This public repository contains a scheduled GitHub Actions worker that signs in with a Telegram user session, reads new posts from `ALYEMENNET`, and copies them to the Telegram bot used by the Hasad Alyoum inbox without a forwarded-from header. It uses Telethon's server-side copy behavior so captions, formatting, and supported media are not reduced to plain text. The existing Supabase inbox remains responsible for the current filtering, cleaning, deduplication, media handling, and footer.

## Required GitHub Actions secrets

Add these repository secrets under **Settings → Secrets and variables → Actions**:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_SESSION_STRING`
- `TELEGRAM_BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Never commit any of these values to the repository, workflow, issues, or logs.

The workflow runs every five minutes and can also be started manually from the Actions tab. For a one-off test, enter a source `message_id`; the worker copies only that message and does not change the deduplication cursor. Select `inspect_only` with a message ID to print safe type/field metadata without copying it. Leave both inputs blank for the normal scheduled batch. GitHub-hosted schedules are best-effort and may be delayed during high load; they are not real-time delivery.

The Supabase table `alymenet_mtproto_state` stores the source message IDs that have been claimed and copied. It is initialized from the existing publisher history so old posts are not replayed.
