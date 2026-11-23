# AF-026: Telegram test-group access probe

This directory contains a one-shot local probe for issue #28. It uses only the
Python standard library, never sends messages, and does not write raw updates,
message text, usernames, or the bot token. It gives IDs random, per-run
pseudonyms. A text digest is held in memory only for matching and is not
included in the report. The report remains sensitive pseudonymous data: keep
it local and share only manually reviewed aggregate counts.

## Run locally

1. Run from an interactive PowerShell terminal. The script prompts for the
   token using hidden input if `TELEGRAM_BOT_TOKEN` is not already set. Type
   the token at the prompt; in some Windows terminals `Ctrl+V` can enter a
   control character instead of pasting. Never paste it into chat, a repository
   file, an issue, or a command line. Do not add it to `.env` for this probe.
2. In `@BotFather`, check and note the bot's Group Privacy setting. In `TEST`,
   send a fresh ordinary text and reply while this initial setting is active.
   Use a dedicated test bot: the setting applies to the bot in every group.
3. If privacy is enabled, turn it off in `@BotFather`, then remove and re-add
   the bot to `TEST` while keeping it a regular member. This re-add is needed
   for the setting change to take effect. If privacy was already off, still
   remove and re-add the bot to test the membership event.
4. After re-adding, send fresh plain text, reply, edit a message, and add a
   harmless non-text item (for example a sticker). Do all actions within 24
   hours of running the probe.
5. Export only `TEST` from Telegram Desktop as JSON to a location outside the
   repository. The export contains real account metadata even when message
   text is synthetic; do not upload or commit it.
6. Run from the repository root, replacing the sample export path:

   ```powershell
   python experiments/telegram_access/probe.py --timeout 5 --export "C:\Users\you\Downloads\TEST\result.json"
   ```

7. The report defaults to `%LOCALAPPDATA%\ActionFlow\telegram-access\` and
   stays outside the repository. If you override `--output`, choose a private,
   non-synced directory. Review it locally. The probe never confirms
   (acknowledges) updates, so repeated runs can return pending events again.
   The report contains pseudonymous event metadata and comparison results. Do
   not share the report file, source export, or token; share only manually
   reviewed aggregate counts.
8. If you changed Group Privacy, restore its prior value in `@BotFather` after
   the probe. If the bot was re-added only for this test, remove it from `TEST`
   when finished.

Do not grant administrator rights just to make message visibility work.

## Expected limits

- `getUpdates` long polling and webhooks are mutually exclusive. The probe
  refuses to run if a webhook is configured; it does not delete or alter it.
- Telegram retains pending updates for no longer than 24 hours. Re-reading an
  unconfirmed update can return it again. Confirming with a higher offset
  discards all earlier updates. A crash after processing but before saving an
  offset can therefore cause duplicates; deduplicate with `update_id`.
- `update_id` is the transport cursor, not a chat/message identity. Use
  `(chat_id, message_id)` for message identity and a resolved
  `(chat_id, reply_to_message_id)` for reply links. Never join by display name.
- The Bot API delivers new updates; it is not a method for recovering complete
  historical group messages. Telegram Desktop export is a separate source.
  Deletes and edits made before the bot observed them cannot be reconstructed
  reliably from the bot update stream.
- A missing message alone does not prove access loss. Track `my_chat_member`
  membership changes, API errors, polling health, cursor gaps, and expected
   test events. This one-shot probe does not monitor polling health or cursor
   gaps over time. A gap or an absent event is an alert requiring investigation,
   not proof of a particular cause.
- A regular non-admin bot can receive all group messages when privacy mode is
  disabled. Sending messages is a separate capability; this probe never calls
  send methods. The pilot must not depend on send permission.

## Status of evidence

Official behavior is documented in the protocol at
[`docs/experiment/af-026-telegram-access.md`](../../docs/experiment/af-026-telegram-access.md).
The probe's unit tests use synthetic updates. They verify redaction and field
normalization, not Telegram connectivity. Live message visibility, the actual
Telegram Desktop export schema/IDs, non-text delivery, and membership-change
delivery remain unverified until the owner runs the steps above.
