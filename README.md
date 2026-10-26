# Queen Watch – OLDIE ANTENNE

This worker checks the OLDIE ANTENNE song-search history and logs a notification when a recent entry is attributed to Queen.

## Important
- Safe mode is enabled by default: `DRY_RUN=true`. In this mode no WhatsApp message is sent.
- The page is a recent-play history, not a guaranteed real-time now-playing API. Alerts depend on how quickly the station updates this page.
- Only Queen entries within `MAX_SONG_AGE_MINUTES` (default 6 minutes) are eligible. Older Queen entries are ignored to avoid sending an alert for an old play on startup.
- The parser is based on the currently visible page layout and must be checked in Render logs before live sending.
- Do not assume OLDIE ANTENNE's public 0800 contact number can receive WhatsApp Cloud API messages. Use a confirmed, eligible recipient number in international format and comply with Meta opt-in/template requirements.
- Never commit Meta access tokens. Set them only as secret environment variables in Render.
- For persistent deduplication on Render, attach a persistent disk mounted at `/var/data`.

## Environment variables
- `DRY_RUN=true`
- `POLL_SECONDS=15`
- `MAX_SONG_AGE_MINUTES=6`
- `WHATSAPP_RECIPIENT=49...` (confirmed WhatsApp recipient, digits only)
- `META_ACCESS_TOKEN` (secret)
- `META_PHONE_NUMBER_ID`
- `META_API_VERSION=v23.0`
- `DB_PATH=/var/data/queen_watch.sqlite3`

## Deploy
Create a Render **Background Worker** from this repository using Docker in Frankfurt. Keep `DRY_RUN=true` until logs show the page is being parsed correctly and a dry-run alert is logged. Add the Meta variables only in Render's environment settings. Do not enable live sends until recipient eligibility has been verified.
