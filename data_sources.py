"""
data_sources.py
---------------
Two data modes, one normalized evidence-bundle shape.

  demo : load pre-built bundles from demo_data/*.json (fully offline)
  live : pull NSE data via yfinance and build bundles on the fly

The evidence bundle shape is the contract the scoring engine depends on.
Keep it identical if you swap in a richer feed.

Live mode does a cheap first pass (1-month history) over the whole universe
to screen/shortlist by day-change, then a second, deeper pass — only for the
shortlisted names — that pulls ~1 year of price history (for RSI/MACD/
Bollinger/SMA50-200/ATR/OBV/ADX) plus annual financial statements (for a
multi-year revenue/earnings trend). This keeps the wide screen fast while
still getting real technical + fundamental depth on the names that matter.
"""

import os
import json
import glob
import math
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DIR = os.path.join(HERE, "demo_data")
UNIVERSE_PATH = os.path.join(HERE, "universe.json")

SMA_WINDOW = 20      # trading days for the SMA / window-return calculations
DEEP_HISTORY_PERIOD = "1y"   # history window for RSI/MACD/Bollinger/SMA50-200/ATR/OBV/ADX
FUNDAMENTALS_YEARS_TARGET = 5  # best-effort: Yahoo's free feed usually caps annual statements at ~4y


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


def _pct100(x, n=1):
    """yfinance expresses many ratios (ROE, margins, growth, dividend yield)
    as a fraction (0.153 == 15.3%). Convert to a percentage, tolerating the
    rare case where the source already returned a percentage-scale number."""
    try:
        if x is None:
            return None
        x = float(x)
        return round(x * 100.0, n)
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


# ---------------------------------------------------------------------------
# technical indicators — plain pandas, no extra dependency beyond what
# yfinance already pulls in (pandas/numpy).
# ---------------------------------------------------------------------------
def _rsi(closes, period=14):
    """Wilder-style RSI. None if there isn't enough history."""
    if closes is None or len(closes) < period + 1:
        return None
    import pandas as pd
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    last_gain, last_loss = avg_gain.iloc[-1], avg_loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return round(100 - (100 / (1 + rs)), 1)


def _macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line, signal_line, histogram) at the latest bar, or
    (None, None, None) if there isn't enough history."""
    if closes is None or len(closes) < slow + signal:
        return None, None, None
    import pandas as pd
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    m, s, h = macd_line.iloc[-1], signal_line.iloc[-1], hist.iloc[-1]
    if pd.isna(m) or pd.isna(s) or pd.isna(h):
        return None, None, None
    return round(float(m), 2), round(float(s), 2), round(float(h), 2)


def _bollinger(closes, period=20, num_std=2):
    """Returns (upper, lower, percent_b) where percent_b is 0-100 position
    of the last close within the bands. None triple if not enough history."""
    if closes is None or len(closes) < period:
        return None, None, None
    import pandas as pd
    sma = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    upper = (sma + num_std * std).iloc[-1]
    lower = (sma - num_std * std).iloc[-1]
    last_close = closes.iloc[-1]
    if pd.isna(upper) or pd.isna(lower) or upper == lower:
        return None, None, None
    percent_b = (last_close - lower) / (upper - lower) * 100.0
    return round(float(upper), 2), round(float(lower), 2), round(float(percent_b), 1)


def _sma_at(closes, window):
    if closes is None or len(closes) < window:
        return None
    import pandas as pd
    val = closes.rolling(window).mean().iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


def _true_range(highs, lows, closes):
    import pandas as pd
    prev_close = closes.shift(1)
    return pd.concat([
        highs - lows,
        (highs - prev_close).abs(),
        (lows - prev_close).abs(),
    ], axis=1).max(axis=1)


def _atr(highs, lows, closes, period=14):
    if closes is None or len(closes) < period + 1:
        return None
    import pandas as pd
    tr = _true_range(highs, lows, closes)
    val = tr.rolling(period).mean().iloc[-1]
    return None if pd.isna(val) else round(float(val), 2)


def _obv_trend(closes, vols, lookback=10):
    """On-Balance Volume direction over the last `lookback` bars — a volume-
    confirmation signal (is money flowing in or out under the price move)."""
    if closes is None or len(closes) < lookback + 1:
        return None
    import pandas as pd
    direction = closes.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (direction * vols.fillna(0)).cumsum()
    recent = obv.iloc[-lookback:]
    if len(recent) < 2:
        return None
    slope = recent.iloc[-1] - recent.iloc[0]
    if slope > 0:
        return "rising"
    if slope < 0:
        return "falling"
    return "flat"


def _adx(highs, lows, closes, period=14):
    """Average Directional Index — trend *strength* (not direction). >25 is
    conventionally read as "trending", <20 as "range-bound/no trend"."""
    if closes is None or len(closes) < period * 2:
        return None
    import pandas as pd
    up_move = highs.diff()
    down_move = -lows.diff()
    plus_dm = ((up_move > down_move) & (up_move > 0)) * up_move
    minus_dm = ((down_move > up_move) & (down_move > 0)) * down_move
    tr = _true_range(highs, lows, closes)
    atr = tr.rolling(period).mean()
    plus_di = (100 * (plus_dm.rolling(period).mean() / atr)).replace([float("inf"), float("-inf")], pd.NA)
    minus_di = (100 * (minus_dm.rolling(period).mean() / atr)).replace([float("inf"), float("-inf")], pd.NA)
    di_sum = plus_di + minus_di
    dx = (100 * (plus_di - minus_di).abs() / di_sum).replace([float("inf"), float("-inf")], pd.NA)
    adx = dx.rolling(period).mean()
    val = adx.iloc[-1]
    return None if pd.isna(val) else round(float(val), 1)


def _compute_technicals(hist_long):
    """All technical indicators from a >=1y OHLCV DataFrame. Returns a dict;
    any indicator without enough history is left as None (never guessed)."""
    if hist_long is None or hist_long.empty:
        return {}
    closes = hist_long["Close"].dropna()
    highs = hist_long["High"].dropna()
    lows = hist_long["Low"].dropna()
    vols = hist_long["Volume"].dropna()
    # align lengths defensively (yfinance rows can have sparse NaNs)
    idx = closes.index.intersection(highs.index).intersection(lows.index)
    closes, highs, lows = closes.loc[idx], highs.loc[idx], lows.loc[idx]

    rsi14 = _rsi(closes)
    macd_line, macd_signal, macd_hist = _macd(closes)
    bb_upper, bb_lower, bb_percent_b = _bollinger(closes)
    sma50 = _sma_at(closes, 50)
    sma200 = _sma_at(closes, 200)
    atr14 = _atr(highs, lows, closes)
    obv_trend = _obv_trend(closes, vols.reindex(closes.index))
    adx14 = _adx(highs, lows, closes)

    sma_cross = None
    if sma50 is not None and sma200 is not None:
        sma_cross = "golden" if sma50 > sma200 else ("death" if sma50 < sma200 else "flat")

    return {
        "rsi14": rsi14,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_hist": macd_hist,
        "macd_bullish": (macd_hist is not None and macd_hist > 0),
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "bb_percent_b": bb_percent_b,
        "sma50": sma50,
        "sma200": sma200,
        "sma_cross": sma_cross,          # "golden" | "death" | "flat" | None
        "atr14": atr14,
        "obv_trend": obv_trend,          # "rising" | "falling" | "flat" | None
        "adx14": adx14,
    }


# ---------------------------------------------------------------------------
# fundamentals — snapshot ratios (from yfinance .info, no extra API call)
# ---------------------------------------------------------------------------
def _compute_fundamentals(info, live_price):
    market_cap = _safe(info, "marketCap")
    fcf = _safe(info, "freeCashflow")
    fcf_yield = None
    if fcf is not None and market_cap:
        try:
            fcf_yield = round(float(fcf) / float(market_cap) * 100.0, 2)
        except Exception:
            fcf_yield = None

    return {
        "pe_trailing": _round(_safe(info, "trailingPE"), 2),
        "pe_forward": _round(_safe(info, "forwardPE"), 2),
        "peg_ratio": _round(_safe(info, "pegRatio"), 2),
        "pb_ratio": _round(_safe(info, "priceToBook"), 2),
        "ev_ebitda": _round(_safe(info, "enterpriseToEbitda"), 2),
        "roe_pct": _pct100(_safe(info, "returnOnEquity")),
        "roa_pct": _pct100(_safe(info, "returnOnAssets")),
        "revenue_growth_yoy_pct": _pct100(_safe(info, "revenueGrowth")),
        "earnings_growth_yoy_pct": _pct100(_safe(info, "earningsGrowth", "earningsQuarterlyGrowth")),
        "operating_margin_pct": _pct100(_safe(info, "operatingMargins")),
        "net_margin_pct": _pct100(_safe(info, "profitMargins")),
        "debt_to_equity": _round(_safe(info, "debtToEquity"), 1),
        "current_ratio": _round(_safe(info, "currentRatio"), 2),
        "free_cash_flow": _round(fcf, 0),
        "fcf_yield_pct": fcf_yield,
        "dividend_yield_pct": _pct100(_safe(info, "dividendYield")),
        "market_cap": _round(market_cap, 0),
    }


# ---------------------------------------------------------------------------
# fundamentals — multi-year history (revenue / net income / CAGR)
# ---------------------------------------------------------------------------
def _year_label(col):
    try:
        return str(col.date().year)
    except Exception:
        s = str(col)
        return s[:4] if len(s) >= 4 else s


def _extract_yearly_row(df, row_names):
    """Pull one row (by any of several possible label spellings — yfinance's
    naming has shifted across versions) from an annual financial-statement
    DataFrame, keyed by year label, oldest first."""
    if df is None or df.empty:
        return {}
    for name in row_names:
        if name in df.index:
            row = df.loc[name].dropna()
            out = {}
            for col, v in row.items():
                try:
                    out[_year_label(col)] = float(v)
                except Exception:
                    continue
            return out
    return {}


def _cagr(values_oldest_to_newest):
    vals = [v for v in values_oldest_to_newest if v is not None]
    if len(vals) < 2:
        return None
    first, last = vals[0], vals[-1]
    years = len(vals) - 1
    if first is None or last is None or first <= 0 or years <= 0:
        return None
    try:
        return round(((last / first) ** (1.0 / years) - 1.0) * 100.0, 1)
    except Exception:
        return None


def _compute_fundamentals_5y(financials, net_income_df=None):
    """Best-effort multi-year revenue / net-income trend. Yahoo's free feed
    (what yfinance reads) typically only exposes ~4 years of ANNUAL
    statements, not literal 5 — we surface whatever is actually available
    and are explicit about the shortfall rather than padding or guessing."""
    revenue_by_year = _extract_yearly_row(financials, ["Total Revenue", "TotalRevenue"])
    net_income_by_year = _extract_yearly_row(
        net_income_df if net_income_df is not None else financials,
        ["Net Income", "NetIncome", "Net Income Common Stockholders"],
    )

    years = sorted(set(revenue_by_year) | set(net_income_by_year))
    revenue_series = [revenue_by_year.get(y) for y in years]
    net_income_series = [net_income_by_year.get(y) for y in years]

    margins = [
        round(ni / rev * 100.0, 1)
        for ni, rev in zip(net_income_series, revenue_series)
        if ni is not None and rev not in (None, 0)
    ]

    return {
        "years": years,
        "years_available": len(years),
        "years_target": FUNDAMENTALS_YEARS_TARGET,
        "revenue_by_year": revenue_by_year,
        "net_income_by_year": net_income_by_year,
        "revenue_cagr_pct": _cagr(revenue_series),
        "net_income_cagr_pct": _cagr(net_income_series),
        "avg_net_margin_pct": round(sum(margins) / len(margins), 1) if margins else None,
    }


def _safe_statement(ticker, attr_names):
    """yfinance has renamed these across versions (financials/income_stmt,
    balance_sheet/balance_sheet, cashflow/cash_flow) — try each, swallow
    whatever that ticker/ that yfinance version doesn't support."""
    for name in attr_names:
        try:
            df = getattr(ticker, name, None)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return None


def _build_live_bundle(symbol, cap_segment, hist, info, raw_news,
                        hist_long=None, financials=None):
    """Turn raw yfinance data into ONE normalized evidence bundle.

    `hist` is the cheap 1-month history used for the price block (unchanged
    behaviour). `hist_long` (~1y) and `financials` (annual income statement)
    are only fetched for shortlisted stocks and add the technicals /
    fundamentals_5y blocks; both are optional so this still works if a deep
    fetch fails for one ticker.
    """
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

    # --- technicals (short-window, from the cheap 1mo history) -------------
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

    # --- deeper technicals (RSI/MACD/Bollinger/SMA50-200/ATR/OBV/ADX) ------
    # computed off ~1y history — only present for shortlisted stocks that
    # got a deep fetch; absent (not guessed) otherwise.
    technicals.update(_compute_technicals(hist_long))

    for k, v in technicals.items():
        if v is None:
            gaps.append(f"technicals.{k}")

    # --- analyst (unchanged — target price / consensus) --------------------
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

    # --- fundamentals (valuation / profitability / leverage snapshot) ------
    fundamentals = _compute_fundamentals(info, live)
    for k, v in fundamentals.items():
        if v is None:
            gaps.append(f"fundamentals.{k}")

    # --- fundamentals_5y (multi-year revenue / net income trend) -----------
    fundamentals_5y = _compute_fundamentals_5y(financials)
    if fundamentals_5y["years_available"] < FUNDAMENTALS_YEARS_TARGET:
        gaps.append(
            f"fundamentals_5y.years_available({fundamentals_5y['years_available']}/"
            f"{FUNDAMENTALS_YEARS_TARGET} — Yahoo's free feed usually caps annual "
            f"statements below 5y)"
        )

    # --- news ----------------------------------------------------------------
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
        "fundamentals": fundamentals,
        "fundamentals_5y": fundamentals_5y,
        "news": news,
        "data_gaps": gaps,
    }
    return bundle


def fetch_live_bundles(shortlist_per_bucket=4, progress=None):
    """
    Pull NSE data via yfinance in two passes:

      1. Cheap screen — 1mo history + .info for every ticker in the
         universe, ranked by |day change|, to pick the shortlist.
      2. Deep fetch — for the shortlist only: ~1y history (technicals) and
         annual financials (5y fundamentals trend), then build the full
         evidence bundle.

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
                scored.append((abs(dchg), tk, t, hist, info))
            except Exception as e:
                print(f"[data] {tk} fetch failed: {e}")

        scored.sort(key=lambda r: r[0], reverse=True)
        for _, tk, t, hist, info in scored[:shortlist_per_bucket]:
            hist_long, financials, news = None, None, []
            try:
                hist_long = t.history(period=DEEP_HISTORY_PERIOD, interval="1d")
            except Exception as e:
                print(f"[data] {tk} deep history fetch failed: {e}")
            try:
                financials = _safe_statement(t, ["financials", "income_stmt"])
            except Exception as e:
                print(f"[data] {tk} financials fetch failed: {e}")
            try:
                news = getattr(t, "news", []) or []
            except Exception as e:
                print(f"[data] {tk} news fetch failed: {e}")

            bundles.append(_build_live_bundle(tk, segment, hist, info, news,
                                               hist_long=hist_long, financials=financials))
            shortlisted += 1
            if progress:
                progress(scanned, shortlisted)

    return bundles, scanned, shortlisted


def timestamp_ist():
    """Current time as an IST string without pulling in pytz."""
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S IST")
