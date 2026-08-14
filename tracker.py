"""
tracker.py
----------
Track record / calibration for past BUY signals.

Every fired BUY verdict is already in audit.db with the price at the moment
it was called. This module revisits those calls after they've had time to
play out, pulls the *current* price via yfinance, and reports whether the
call was right — turning the audit log into a real accuracy scorecard
instead of a one-way log of opinions.

Only verdicts from `live` runs are scored: demo runs use fictional
companies (TITANFORGE, NIMBUSCLOUD, ...) that don't exist on NSE, so there
is no real price to check them against. Those are reported separately as
"untracked (demo)" rather than silently dropped, so the scorecard is
honest about what it did and didn't check.

Public entry point:
    compute_scorecard(db_path, min_age_days=3) -> {
        "eligible": int,          # live BUY verdicts old enough to check
        "tracked": int,           # of those, how many we got a live price for
        "untracked_demo": int,    # fired BUYs from demo runs (not checkable)
        "wins": int, "losses": int,
        "win_rate": float | None,
        "avg_return_pct": float | None,
        "positions": [ {...per-signal detail...} ],
    }
"""

import sqlite3
from datetime import datetime


def _connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def _fired_buys(db_path):
    con = _connect(db_path)
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT v.id, v.ts, v.symbol, v.cap, v.verdict, v.confidence,
                   v.price, v.fired, r.mode, r.started_at
            FROM verdicts v
            JOIN runs r ON v.run_id = r.id
            WHERE v.fired = 1
            ORDER BY r.started_at DESC
        """)
        return [dict(row) for row in cur.fetchall()]
    finally:
        con.close()


def compute_scorecard(db_path, min_age_days=3):
    """Check past BUY calls against their actual later price move.

    min_age_days: how long a call must have been outstanding before we
    bother checking it (a call made this morning hasn't had time to play
    out yet).
    """
    rows = _fired_buys(db_path)
    now = datetime.now()

    eligible, untracked_demo = [], 0
    for r in rows:
        if r["mode"] != "live":
            untracked_demo += 1
            continue
        try:
            called_at = datetime.fromisoformat(r["started_at"])
        except Exception:
            continue
        if (now - called_at).days < min_age_days:
            continue
        eligible.append(r)

    if not eligible:
        return {
            "eligible": 0,
            "tracked": 0,
            "untracked_demo": untracked_demo,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "positions": [],
        }

    import yfinance as yf  # lazy import, same pattern as data_sources.py

    positions = []
    wins, total_return, n = 0, 0.0, 0

    for r in eligible:
        entry_price = r["price"]
        if not entry_price:
            continue
        ticker = f"{r['symbol']}.NS"
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            closes = list(hist["Close"].dropna()) if hist is not None and not hist.empty else []
            if not closes:
                continue
            current_price = float(closes[-1])
        except Exception:
            continue

        return_pct = round((current_price / entry_price - 1.0) * 100.0, 2)
        won = return_pct > 0
        wins += 1 if won else 0
        total_return += return_pct
        n += 1

        called_at = r["started_at"]
        age_days = (now - datetime.fromisoformat(called_at)).days

        positions.append({
            "symbol": r["symbol"],
            "cap": r["cap"],
            "confidence": r["confidence"],
            "called_at": called_at,
            "age_days": age_days,
            "entry_price": entry_price,
            "current_price": round(current_price, 2),
            "return_pct": return_pct,
            "result": "win" if won else "loss",
        })

    return {
        "eligible": len(eligible),
        "tracked": n,
        "untracked_demo": untracked_demo,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n * 100.0, 1) if n else None,
        "avg_return_pct": round(total_return / n, 2) if n else None,
        "positions": positions,
    }
