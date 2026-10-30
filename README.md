# ALYEMENNET MTProto bridge

This repository contains the MTProto worker that signs in with a Telegram user session, reads posts from `ALYEMENNET`, and copies them to the Telegram bot used by the Hasad Alyoum inbox without a forwarded-from header. It uses Telethon's server-side copy behavior so captions, formatting, and supported media are preserved. Telegram `RichMessage` posts are converted from their rich-text blocks into ordinary text because Bot API inbox updates do not expose that MTProto field as `text`. The Supabase inbox remains responsible for filtering, cleaning, deduplication, media handling, and the footer.

The MTProto worker and direct publisher share the Supabase delivery ledger and source-run control. The ledger serializes delivery claims, enforces the configured interval across both paths, and prevents a worker from advancing the MTProto cursor while a message is waiting or publishing is temporarily paused. Supabase Cron controls the production cadence. GitHub Actions can also be started manually for inspection or one-off testing.

## Telegram commands

Send commands to the bot as plain text, without quotation marks:

- `تشغيل يمن نت بدون` — enable publishing and skip source posts up to the latest source message observed when the command is processed. Newer posts can publish normally.
- `تشغيل يمن نت 1`, `تشغيل يمن نت 2`, `تشغيل يمن نت 30`, and so on — enable publishing and drain accumulated posts in source order, with at least the specified number of minutes between successful deliveries. The accepted interval is 1–1440 minutes.
- `ايقاف يمن نت 10 بدون` — pause delivery for 10 minutes, then resume with new posts only; posts observed during the pause are skipped.
- `ايقاف يمن نت 10` — pause delivery for 10 minutes, then resume with accumulated posts at a minimum one-minute interval.
- Replace `10` with another duration, such as `30` or `120`, to set the pause duration. The accepted pause is 1–10080 minutes.
- `جدولة 20`, `جدولة20`, `جدوله 20`, or `جدوله20` — set the interval between publishing cycles to 20 minutes. The number is not a delay between individual posts inside a cycle. The accepted cycle interval is 1–10080 minutes.
- `ايقاف جدولة` or `ايقاف جدوله` — reset the custom cycle interval to the normal defaults: direct publisher every 15 seconds and MTProto every 5 minutes. This command changes schedule only; it does not turn publishing on or off.
- The existing `تشغيل يمن نت` command remains supported and restores legacy mode. `ايقاف يمن نت` still stops publishing until manually enabled. `تشغيل فيديو` and `ايقاف فيديو` remain supported.

During a timed pause, the scheduled workers stay active but the shared delivery gate blocks all sends. In `بدون` mode, source IDs observed during the pause are added to the cutoff. In accumulated mode, the first eligible delivery after the pause starts the one-minute spacing gate. Unknown strings beginning with `تشغيل يمن نت` or `ايقاف يمن نت` receive command guidance rather than being passed to the inbox as publishable text.

The bot command router lives in Supabase (`alymenet-bot-router`); the GitHub worker consumes the same shared delivery policy from Supabase.

Send `الأوامر`, `/commands`, `/help`, or `/start` in the bot's private chat to show the command guide and a persistent reply-keyboard with common shortcuts. Numbered buttons are examples; replace the number in the message text to choose another duration or schedule. `إخفاء القائمة` removes the keyboard.

## Required GitHub Actions secrets

Add these repository secrets under **Settings → Secrets and variables → Actions**:

- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_SESSION_STRING`
- `TELEGRAM_BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Never commit any of these values to the repository, workflow, issues, or logs.

For a one-off test, enter a source `message_id`; the worker copies only that message and does not change the deduplication cursor. Select `inspect_only` with a message ID to print safe type/field metadata without copying it. Leave both inputs blank for a normal batch. GitHub-hosted schedules are best-effort and may be delayed during high load; they are not real-time delivery.

The Supabase table `alymenet_mtproto_state` stores source message IDs already claimed or copied. `alymenet_delivery_ledger` coordinates delivery across the MTProto and direct-publisher paths. The migration files under `supabase/migrations/` define shared run modes, timed pauses, cycle scheduling, cutoffs, and spacing controls.
