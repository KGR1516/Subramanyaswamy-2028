"""
run_daily.py
------------
Headless entry point for the "Daily Analysis Digest" GitHub Actions workflow.

Runs ONE live analysis cycle — the same Scout -> Technician -> Fundamentalist
-> Newsdesk -> Bull/Bear -> Judge pipeline the dashboard's Live mode uses —
without Flask, threading, or a browser, then hands the verdicts to
build_report.py to make an Excel digest. Excel only — no Telegram.

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

    verdicts, fired, fundamentals_5y = [], [], []
    for ev in bundles:
        result = llm.evaluate(ev, engine=engine)
        vd = result["verdict"]
        tech = ev.get("technicals", {}) or {}
        fund = ev.get("fundamentals", {}) or {}
        f5y = ev.get("fundamentals_5y", {}) or {}
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
            # technical analysis (RSI/MACD/SMA50-200 cross/ADX) — see data_sources.py
            "rsi14": tech.get("rsi14"),
            "macd_bullish": tech.get("macd_bullish"),
            "sma_cross": tech.get("sma_cross"),
            "adx14": tech.get("adx14"),
            "volume_breakout": tech.get("volume_breakout"),
            # fundamental analysis snapshot
            "pe_trailing": fund.get("pe_trailing"),
            "roe_pct": fund.get("roe_pct"),
            "revenue_growth_yoy_pct": fund.get("revenue_growth_yoy_pct"),
            "debt_to_equity": fund.get("debt_to_equity"),
            # fundamental analysis — cash flow & profitability
            "free_cash_flow": fund.get("free_cash_flow"),
            "fcf_yield_pct": fund.get("fcf_yield_pct"),
            "operating_cash_flow": fund.get("operating_cash_flow"),
            "net_cash_flow": fund.get("net_cash_flow"),
            "ebit": fund.get("ebit"),
            "ebitda": fund.get("ebitda"),
            "cash_conversion_cycle_days": fund.get("cash_conversion_cycle_days"),
            # fundamental analysis — multi-year trend
            "revenue_cagr_pct": f5y.get("revenue_cagr_pct"),
            "net_income_cagr_pct": f5y.get("net_income_cagr_pct"),
            "fundamentals_years": f5y.get("years_available"),
        }
        row["fired"] = row["verdict"] == "BUY" and row["confidence"] >= confidence_threshold
        if row["fired"]:
            fired.append(row)
        verdicts.append(row)
        if f5y.get("years_available"):
            fundamentals_5y.append({
                "symbol": ev["symbol"],
                "name": ev.get("name", ev["symbol"]),
                "years": f5y.get("years", []),
                "revenue_by_year": f5y.get("revenue_by_year", {}),
                "net_income_by_year": f5y.get("net_income_by_year", {}),
                "revenue_cagr_pct": f5y.get("revenue_cagr_pct"),
                "net_income_cagr_pct": f5y.get("net_income_cagr_pct"),
                "avg_net_margin_pct": f5y.get("avg_net_margin_pct"),
            })
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
        "fundamentals_5y": fundamentals_5y,
    }


def _env_int(name, default):
    """os.environ.get(name, default) doesn't fall back on an empty string —
    and GitHub Actions sets an unset repo *variable* to "" rather than
    leaving it absent — so int(os.environ.get(...)) would crash on a repo
    that never configured this optional variable. Treat blank as unset."""
    val = (os.environ.get(name) or "").strip()
    return int(val) if val else default


def main():
    parser = argparse.ArgumentParser(description="Headless daily analysis run")
    parser.add_argument("--shortlist-per-bucket", type=int,
                         default=_env_int("SHORTLIST_PER_BUCKET", 4))
    parser.add_argument("--min-confidence", type=int,
                         default=_env_int("CONFIDENCE_THRESHOLD", 7))
    parser.add_argument("--mode", choices=["live", "demo"], default="live",
                         help="demo is for local testing only — the workflow always uses live")
    parser.add_argument("--output", default="daily_digest.xlsx")
    args = parser.parse_args()

    load_env()
    result = run_cycle(args.shortlist_per_bucket, args.min_confidence, mode=args.mode)

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
