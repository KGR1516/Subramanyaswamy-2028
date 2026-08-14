"""
run_daily.py
------------
Headless entry point for the "Daily Analysis Digest" GitHub Actions workflow.

Runs ONE live analysis cycle — the same Scout -> Technician -> Fundamentalist
-> Newsdesk -> Bull/Bear -> Judge pipeline the dashboard's Live mode uses —
without Flask, threading, or a browser, then hands the verdicts to
build_report.py to make an Excel digest and (optionally) posts fired BUY
signals to Telegram, exactly like app.py's Messenger agent does.

This is meant to run on GitHub Actions, which has normal outbound internet
access, so yfinance and the LLM providers work here even in environments
(like a locked-down sandbox) where they can't.

Run:
    python run_daily.py                        # writes daily_digest.xlsx
    python run_daily.py --min-confidence 6
    python run_daily.py --shortlist-per-bucket 3 --output out.xlsx
"""
import argparse
import os
from datetime import datetime

import data_sources
import llm

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    """Mirror app.py's tiny .env loader. No-op on GitHub Actions, where real
    environment variables (from repo secrets) are already set; useful for a
    local dry run."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def run_cycle(shortlist_per_bucket, confidence_threshold, mode="live"):
    """Run one full cycle and return a result dict. mode="demo" is only used
    for local testing — the scheduled workflow always runs mode="live"."""
    engine = llm.detect_engine()
    print(f"[run_daily] engine={engine} mode={mode}")

    if mode == "live":
        bundles, scanned, shortlisted = data_sources.fetch_live_bundles(shortlist_per_bucket)
    else:
        bundles = data_sources.load_demo_bundles()
        scanned = shortlisted = len(bundles)

    ts = data_sources.timestamp_ist()
    print(f"[run_daily] scanned={scanned} shortlisted={shortlisted} bundles={len(bundles)}")

    verdicts, fired = [], []
    for ev in bundles:
        result = llm.evaluate(ev, engine=engine)
        vd = result["verdict"]
        row = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "symbol": ev["symbol"],
            "name": ev.get("name", ev["symbol"]),
            "cap": ev["cap_segment"],
            "verdict": vd["verdict"],
            "confidence": vd["confidence"],
            "winner": vd["winner"],
            "rationale": vd["rationale"],
            "key_catalyst": vd["key_catalyst"],
            "price": ev["price"].get("live"),
            "day_change_pct": ev["price"].get("day_change_pct"),
            "engine": result.get("engine", engine),
            "fired": False,
        }
        row["fired"] = row["verdict"] == "BUY" and row["confidence"] >= confidence_threshold
        if row["fired"]:
            fired.append(row)
        verdicts.append(row)
        print(f"  {ev['symbol']:<14} {vd['verdict']:<6} conf={vd['confidence']}  "
              f"{'(FIRED)' if row['fired'] else ''}")

    return {
        "timestamp": ts,
        "engine": engine,
        "mode": mode,
        "confidence_threshold": confidence_threshold,
        "universe": scanned,
        "in_debate": len(bundles),
        "buy_signals": len(fired),
        "verdicts": verdicts,
        "fired": fired,
    }


def _tg_send(token, chat_id, text):
    import requests
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    return r.ok and r.json().get("ok", False)


def maybe_send_telegram(result):
    """Optional — reuses app.py's Telegram behavior if TELEGRAM_BOT_TOKEN /
    TELEGRAM_CHAT_ID secrets are set on the repo. Skipped silently otherwise,
    same as the local app."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[run_daily] Telegram not configured — skipping")
        return
    for v in result["fired"]:
        text = (
            f"\U0001F7E2 <b>BUY SIGNAL — {v['symbol']}</b> ({v['cap']} cap)\n\n"
            f"Verdict: BUY | Confidence: {v['confidence']}/10\n"
            f"Winner: {v['winner']}\nWhy: {v['rationale']}\n"
            f"Key catalyst: {v['key_catalyst']}\n"
            f"Live price: ₹{v['price']} | Day change: {v['day_change_pct']}%\n\n"
            f"— Analysis only. No trade was placed. Not investment advice."
        )
        try:
            _tg_send(token, chat_id, text)
        except Exception as e:
            print(f"[run_daily] Telegram send failed for {v['symbol']}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Headless daily analysis run")
    parser.add_argument("--shortlist-per-bucket", type=int,
                         default=int(os.environ.get("SHORTLIST_PER_BUCKET", "4")))
    parser.add_argument("--min-confidence", type=int,
                         default=int(os.environ.get("CONFIDENCE_THRESHOLD", "7")))
    parser.add_argument("--mode", choices=["live", "demo"], default="live",
                         help="demo is for local testing only — the workflow always uses live")
    parser.add_argument("--output", default="daily_digest.xlsx")
    args = parser.parse_args()

    load_env()
    result = run_cycle(args.shortlist_per_bucket, args.min_confidence, mode=args.mode)
    maybe_send_telegram(result)

    import build_report
    build_report.build(result, args.output)
    print(f"[run_daily] saved {args.output}")

    # hand the BUY count to the next workflow step (send_email.py) via env file
    gh_env = os.environ.get("GITHUB_ENV")
    if gh_env:
        with open(gh_env, "a", encoding="utf-8") as f:
            f.write(f"BUY_COUNT={result['buy_signals']}\n")


if __name__ == "__main__":
    main()
