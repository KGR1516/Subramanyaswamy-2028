"""
data_sources.py
---------------
Two data modes, one normalized evidence-bundle shape.

  demo : load pre-built bundles from demo_data/*.json (fully offline)
  live : pull NSE data via yfinance and build bundles on the fly

The evidence bundle shape is the contract the scoring engine depends on.
Keep it identical if you swap in a richer feed.
"""

import os
import json
import glob
import math
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "demo_data")
UNIVERSE_PATH = os.path.join(HERE, "universe.json")

SMA_WINDOW = 20  # trading days for the SMA / window-return calculations


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pct(a, b):
    """(a/b - 1) * 100, guarding against None / zero."""
    try:
        if a is None or b is None or b == 0:
            return None
        return round((a / b - 1.0) * 100.0, 2)
    except Exception:
        return None


def _round(x, n=2):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return None
        return round(float(x), n)
    except Exception:
        return None


def _safe(d, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


# ---------------------------------------------------------------------------
# demo mode
# ---------------------------------------------------------------------------
def load_demo_bundles():
    """Load every demo_data/*.json evidence bundle."""
    bundles = []
    for path in sorted(glob.glob(os.path.join(DEMO_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                bundles.append(json.load(f))
        except Exception as e:
            print(f"[data] skipped demo bundle {path}: {e}")
    return bundles


# ---------------------------------------------------------------------------
# live mode (yfinance)
# ---------------------------------------------------------------------------
def _load_universe():
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _classify_trend(price_vs_sma_pct, window_return_pct):
    if price_vs_sma_pct is None and window_return_pct is None:
        return None
    score = 0
    if price_vs_sma_pct is not None:
        score += 1 if price_vs_sma_pct > 1 else (-1 if price_vs_sma_pct < -1 else 0)
    if window_return_pct is not None:
        score += 1 if window_return_pct > 2 else (-1 if window_return_pct < -2 else 0)
    if score > 0:
        return "up"
    if score < 0:
        return "down"
    return "sideways"


def _news_sentiment(raw_news):
    """Very light keyword sentiment over yfinance .news titles."""
    pos_words = ("beat", "surge", "record", "profit", "growth", "upgrade",
                 "wins", "order", "rally", "high", "gain", "strong", "raise")
    neg_words = ("miss", "fall", "loss", "cut", "downgrade", "probe", "fraud",
                 "decline", "weak", "slump", "drop", "warn", "lawsuit")
    recent, pos, neg, neu = [], 0, 0, 0
    for item in (raw_news or [])[:8]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        if any(w in low for w in pos_words):
            tone = "positive"; pos += 1
        elif any(w in low for w in neg_words):
            tone = "negative"; neg += 1
        else:
            tone = "neutral"; neu += 1
        recent.append({"title": title[:140], "tone": tone})
    total = pos + neg + neu
    return {
        "total": total,
        "positive": pos,
        "negative": neg,
        "neutral": neu,
        "recent": recent[:5],
    }


def _build_live_bundle(symbol, cap_segment, hist, info, raw_news):
    """Turn raw yfinance data into ONE normalized evidence bundle."""
    gaps = []

    # --- price block -------------------------------------------------------
    closes = list(hist["Close"].dropna()) if hist is not None and not hist.empty else []
    vols = list(hist["Volume"].dropna()) if hist is not None and not hist.empty else []

    live = _safe(info, "currentPrice", "regularMarketPrice")
    if live is None and closes:
        live = closes[-1]
    prev_close = _safe(info, "regularMarketPreviousClose", "previousClose")
    if prev_close is None and len(closes) >= 2:
        prev_close = closes[-2]

    day_open = _safe(info, "regularMarketOpen", "open")
    day_high = _safe(info, "dayHigh", "regularMarketDayHigh")
    day_low = _safe(info, "dayLow", "regularMarketDayLow")
    volume = _safe(info, "volume", "regularMarketVolume")
    if volume is None and vols:
        volume = vols[-1]

    day_change_pct = _pct(live, prev_close)
    if day_change_pct is None:
        gaps.append("price.day_change_pct")

    price = {
        "live": _round(live),
        "day_open": _round(day_open),
        "day_high": _round(day_high),
        "day_low": _round(day_low),
        "prev_close": _round(prev_close),
        "day_change_pct": day_change_pct,
        "volume": int(volume) if volume else None,
    }
    for k, v in price.items():
        if v is None:
            gaps.append(f"price.{k}")

    # --- 52-week range -----------------------------------------------------
    hi52 = _safe(info, "fiftyTwoWeekHigh")
    lo52 = _safe(info, "fiftyTwoWeekLow")
    pct_from_high = _pct(live, hi52)
    position_pct = None
    if live is not None and hi52 is not None and lo52 is not None and hi52 != lo52:
        position_pct = round((live - lo52) / (hi52 - lo52) * 100.0, 1)
    range_52w = {
        "high": _round(hi52),
        "low": _round(lo52),
        "pct_from_high": pct_from_high,
        "position_pct": position_pct,
    }
    for k, v in range_52w.items():
        if v is None:
            gaps.append(f"range_52w.{k}")

    # --- technicals --------------------------------------------------------
    rvol = None
    if vols and len(vols) > 1 and volume:
        prior = vols[:-1]
        avg_prior = sum(prior) / len(prior) if prior else None
        if avg_prior:
            rvol = round(volume / avg_prior, 2)

    price_vs_sma_pct = None
    if len(closes) >= SMA_WINDOW and live is not None:
        sma = sum(closes[-SMA_WINDOW:]) / SMA_WINDOW
        price_vs_sma_pct = _pct(live, sma)

    window_return_pct = None
    if len(closes) >= 2:
        lookback = closes[-min(SMA_WINDOW, len(closes))]
        window_return_pct = _pct(closes[-1], lookback)

    swing_high = _round(max(closes)) if closes else None
    swing_low = _round(min(closes)) if closes else None

    day_range_position_pct = None
    if None not in (live, day_low, day_high) and day_high != day_low:
        day_range_position_pct = round((live - day_low) / (day_high - day_low) * 100.0, 1)

    technicals = {
        "rvol": rvol,
        "price_vs_sma_pct": price_vs_sma_pct,
        "window_return_pct": window_return_pct,
        "swing_high": swing_high,
        "swing_low": swing_low,
        "day_range_position_pct": day_range_position_pct,
        "trend": _classify_trend(price_vs_sma_pct, window_return_pct),
    }
    for k, v in technicals.items():
        if v is None:
            gaps.append(f"technicals.{k}")

    # --- analyst -----------------------------------------------------------
    target_mean = _safe(info, "targetMeanPrice")
    target_low = _safe(info, "targetLowPrice")
    target_high = _safe(info, "targetHighPrice")
    num_analysts = _safe(info, "numberOfAnalystOpinions")
    consensus = _safe(info, "recommendationKey")
    upside_pct = _pct(target_mean, live)

    analyst = {
        "consensus": consensus,
        "num_analysts": int(num_analysts) if num_analysts else None,
        # buy/hold/sell split isn't in .info; leave null + flag it.
        "buy_pct": None,
        "hold_pct": None,
        "sell_pct": None,
        "target_mean": _round(target_mean),
        "target_low": _round(target_low),
        "target_high": _round(target_high),
        "upside_pct": upside_pct,
    }
    for k, v in analyst.items():
        if v is None:
            gaps.append(f"analyst.{k}")

    # --- news --------------------------------------------------------------
    news = _news_sentiment(raw_news)
    if news["total"] == 0:
        gaps.append("news.total")

    bundle = {
        "symbol": symbol.replace(".NS", ""),
        "name": _safe(info, "longName", "shortName", default=symbol.replace(".NS", "")),
        "cap_segment": cap_segment,
        "sector": _safe(info, "sector"),
        "price": price,
        "range_52w": range_52w,
        "technicals": technicals,
        "analyst": analyst,
        "news": news,
        "data_gaps": gaps,
        "note": "Feed has no raw ratios (P/E, ROE); those are intentionally absent.",
    }
    return bundle


def fetch_live_bundles(shortlist_per_bucket=4, progress=None):
    """
    Pull NSE data via yfinance, screen each bucket by day-change,
    keep the top `shortlist_per_bucket`, return normalized bundles.

    `progress(scanned, shortlisted)` is called as work proceeds so the
    Scout agent's live stats can move.
    """
    import yfinance as yf  # imported lazily so demo mode needs no yfinance

    universe = _load_universe()
    scanned, shortlisted, bundles = 0, 0, []

    for segment, tickers in universe.items():
        scored = []
        for tk in tickers:
            scanned += 1
            if progress:
                progress(scanned, shortlisted)
            try:
                t = yf.Ticker(tk)
                hist = t.history(period="1mo", interval="1d")
                info = t.info or {}
                live = _safe(info, "currentPrice", "regularMarketPrice")
                prev = _safe(info, "regularMarketPreviousClose", "previousClose")
                dchg = _pct(live, prev) or 0.0
                scored.append((abs(dchg), tk, hist, info, getattr(t, "news", []) or []))
            except Exception as e:
                print(f"[data] {tk} fetch failed: {e}")

        scored.sort(key=lambda r: r[0], reverse=True)
        for _, tk, hist, info, news in scored[:shortlist_per_bucket]:
            bundles.append(_build_live_bundle(tk, segment, hist, info, news))
            shortlisted += 1
            if progress:
                progress(scanned, shortlisted)

    return bundles, scanned, shortlisted


def timestamp_ist():
    """Current time as an IST string without pulling in pytz."""
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")
