# Multi-agent stock-analysis desk

A local, one-click web dashboard that runs a panel of named agents to analyze
Indian (NSE) stocks, debates each pick with an LLM (or a deterministic engine),
and sends BUY signals to your Telegram. Everything runs on your machine — no
cloud backend. No orders are ever placed; this is analysis only, and not
investment advice.

## What it does

1. **Scout** screens a stock universe for movers.
2. **Technician / Fundamentalist / Newsdesk** read price action, valuation, and news.
3. **Bull** and **Bear** argue the case; the **Judge** issues a verdict + confidence.
4. **Messenger** posts every BUY (confidence ≥ threshold) to Telegram, plus a daily summary.

A single **Start agents** button drives the whole pipeline; the page polls
`/status` and animates each agent `offline → working → done`. Every run and
verdict is written to a local SQLite audit (`audit.db`).

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env      # then edit .env (see below)
python app.py             # open http://127.0.0.1:5000  →  Start agents
```

Use **Demo** first — it runs fully offline from `demo_data/*.json`. Use
**Live** during NSE market hours (Mon–Fri 09:15–15:30 IST).

## LLM engine (optional — deterministic fallback always works)

The scoring engine auto-detects a provider in this priority:

1. **claude_code** — if the `claude` CLI is on your PATH. Install Claude Code,
   run `claude`, then `/login` with your Claude plan (Pro/Max). No API key, no
   per-call billing — it uses your subscription.
2. **anthropic** — set `ANTHROPIC_API_KEY` in `.env`.
3. **openai** — set `OPENAI_API_KEY` in `.env`.

Force one with `LLM_PROVIDER=claude_code|anthropic|openai|deterministic`. With
none of the LLM providers available, it uses the built-in **deterministic**
rule engine — which needs no LLM, key, or network and never crashes on missing
data. Setting `LLM_PROVIDER=deterministic` explicitly opts out of LLM debate
even if the `claude` CLI happens to be on your PATH or a key happens to be
set — useful if you want fast, reproducible scoring with zero network calls.
Either way, every figure an agent cites is verified against the evidence
bundle; ungrounded numbers cause that stock to fall back to deterministic
scoring.

## Telegram

1. Message **@BotFather** → create a bot → copy the token.
2. Message **@userinfobot** → copy your chat id.
3. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

If Telegram isn't configured, the run still completes and logs — it just skips
sending. The bot token is scrubbed from all logs and UI.

## Track record

Every fired BUY is already logged to `audit.db` with the price at the moment
it was called. The **Track record** panel (and `GET /scorecard`) revisits
past **Live**-mode BUY calls a few days later, pulls the current price via
yfinance, and reports win rate and average return — so the desk has a real,
checkable accuracy signal instead of just a running opinion log. Demo-mode
calls use fictional companies and can't be checked against a real price;
they're counted separately as "untracked (demo)" rather than silently
dropped. Tune how long a call must season before it's checked with
`?min_age_days=` (default `3`).

## Automated daily digest (GitHub Actions)

This repo can run itself on a schedule — no laptop required, no Telegram.
`.github/workflows/daily-digest.yml` runs the **same Live-mode pipeline** as
the dashboard (real yfinance data, not a scrape — GitHub's runners have
normal internet access) every weekday at **exactly 3:00 PM IST**, builds a
formula-driven Excel digest with `build_report.py`, and emails **only that
Excel file** to your Gmail with `send_email.py`. Nothing is posted anywhere
else — this workflow doesn't touch Telegram at all.

**Setup — add these in the repo's *Settings → Secrets and variables →
Actions*:**

| Secret | Required? | Purpose |
|--------|-----------|---------|
| `GMAIL_ADDRESS` | **Yes** | Gmail address to send from |
| `GMAIL_APP_PASSWORD` | **Yes** | a Gmail **App Password** (not your normal password) — generate one at myaccount.google.com/apppasswords |
| `RECIPIENT_EMAIL` | No | who receives the email (defaults to `GMAIL_ADDRESS`) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | No | enables real LLM debate instead of the deterministic engine (the `claude` CLI isn't available on a runner, so `claude_code` never applies here) |

Optional repo *Variables* (not secrets): `LLM_PROVIDER`, `CONFIDENCE_THRESHOLD`,
`SHORTLIST_PER_BUCKET` — same meaning as the `.env` keys below.

Only `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` are required — everything else
falls back to the same defaults as running locally. Trigger a run manually
any time from the **Actions** tab (`workflow_dispatch`) to test it without
waiting for the schedule. Each run also uploads the workbook as a build
artifact for 30 days, so you can pull the raw file even without email.

## Config (`.env`)

| Key | Purpose | Default |
|-----|---------|---------|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | signal delivery | — |
| `LLM_PROVIDER` | force an engine | auto-detect |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | LLM keys | — |
| `BRAND` | header brand name | `Bourse` |
| `CONFIDENCE_THRESHOLD` | min confidence to fire a BUY | `7` |
| `AGENT_DELAY` | visual pacing (seconds) | `0.6` |
| `SHORTLIST_PER_BUCKET` | live picks kept per cap bucket | `4` |
| `PORT` | server port | `5000` |

## Files

| File | Role |
|------|------|
| `app.py` | Flask server, state machine, Telegram, SQLite |
| `scoring.py` | deterministic agents + Judge + grounding verifier |
| `llm.py` | LLM debate, provider detection, fallback |
| `data_sources.py` | demo loader + yfinance adapter + evidence builder |
| `tracker.py` | track record: scores past live BUY calls against actual price moves |
| `dashboard.html` | self-contained UI (inline CSS/JS, no build step) |
| `universe.json` | editable NSE tickers per cap bucket |
| `demo_data/*.json` | offline evidence bundles |
| `run_daily.py` | headless entry point for the scheduled workflow — runs one Live cycle, no Flask, no Telegram |
| `build_report.py` | builds the formula-driven Excel digest from a run's verdicts |
| `send_email.py` | emails the digest via Gmail SMTP (App Password) |
| `.github/workflows/daily-digest.yml` | GitHub Actions schedule: weekdays, 15:00 IST |

## Swapping the data feed

`data_sources.py` uses yfinance for portability. To use a broker API, paid
data, or an MCP connector, replace the adapter but **keep the evidence-bundle
shape identical** so the scoring engine is unchanged. The bundle shape is
documented at the top of `data_sources.py`; missing values are `null` and named
in each bundle's `data_gaps`.

## Acceptance check

`python app.py` → open the URL → **Start** → agents animate → verdict feed
populates → BUY signals + summary go to Telegram → footer shows engine +
timestamp, and everything is logged to SQLite. Uses the `claude` CLI when
logged in; falls back to deterministic otherwise; never crashes on missing data;
the bot token is never printed; no orders are ever placed.
