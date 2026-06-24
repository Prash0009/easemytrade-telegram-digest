# EaseMyTrade — Telegram Signal Digest

Standalone notifier that posts EaseMyTrade trade-signal digests to Telegram.

It lives in its **own public repo** so it runs on GitHub Actions' **unlimited free
tier** (private repos only get 2,000 Actions minutes/month, which the every-5-minute
schedule exceeds). The main application and the signal engine stay in a separate
**private** repository.

## How it works
- A scheduled GitHub Action runs every 5 minutes during market hours (09:00–16:00 IST, Mon–Fri).
- `scripts/send_telegram_signal_digest.py` reads the **public** live-market API
  (`https://www.easemytrade.in/api/live-market/?scope=public`) — it needs no engine
  code, just the published signal data.
- If the signal digest has changed since the last run, it posts to Telegram and
  records the new signature in `data/telegram_signal_state.json` (committed back).

## No secrets in this repo
Bot tokens and chat IDs are **GitHub Actions secrets**, never committed. Required
secrets (add under Settings → Secrets and variables → Actions):

| Secret | Purpose |
|--------|---------|
| `TELEGRAM_SIGNAL_BOT_TOKEN` | Bot token for the signal channel |
| `TELEGRAM_SIGNAL_CHAT_ID` | Chat/channel ID for signals |
| `TELEGRAM_BOT_TOKEN` | Fallback bot token |
| `TELEGRAM_CHAT_ID` | Fallback chat ID |

## Manual run
Actions tab → "EaseMyTrade Telegram Signal Digest" → **Run workflow**
(`always_send: true` to force a send even if unchanged).
