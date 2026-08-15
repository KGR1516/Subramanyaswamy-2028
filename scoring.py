"""
scoring.py
----------
Deterministic fallback engine. MUST always work with no LLM, no key, no
network. Pure functions over the evidence bundle.

Public entry point:
    evaluate(evidence) -> {
        "scores":  {agent: {"score": 0-100, "reasons": [str, ...]}},
        "verdict": {winner, verdict, confidence, rationale,
                    key_catalyst, bull_score, bear_score, net},
        "engine":  "deterministic",
    }

Signal sources per agent (all optional — missing data just means that
signal contributes nothing, never a guess):
  Technical  (technicals.*):  RVOL, price-vs-SMA20, day-range position,
             RSI-14, MACD, Bollinger %B, SMA50/200 golden/death cross,
             ADX-14 (trend strength), volume breakout (fresh N-day high
             on 1.5x+ average volume).
  Fundamental (analyst.*, fundamentals.*, fundamentals_5y.*): analyst
             target upside/consensus, P/E, PEG, ROE, revenue growth (YoY
             and 5y CAGR), debt/equity, current ratio, free cash flow
             (+ FCF yield), operating cash flow, net cash flow, EBIT,
             EBITDA, cash conversion cycle (days), net profit 5y CAGR.
  News       (news.*): headline keyword tone.

Bull and Bear each read across ALL of the above — they're what actually
drives the Judge's verdict, not just the Technician/Fundamentalist display
scores — so a stock only fires BUY when technicals AND fundamentals AND
sentiment line up, not just one dimension.
"""


def _g(evidence, path, default=None):
    """Nested getter: _g(ev, 'technicals.rvol')."""
    cur = evidence
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    return cur


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _usd(x):
    """Format a dollar figure with correct sign placement: -$200,000, not $-200,000."""
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.0f}"


# ---------------------------------------------------------------------------
# individual agents -> conviction 0..100 + short grounded reasons
# ---------------------------------------------------------------------------
def _score_bull(ev):
    score, reasons = 0, []

    # -- technical: momentum / volume / position --------------------------
    rvol = _g(ev, "technicals.rvol")
    if rvol is not None and rvol > 1.2:
        add = _clamp(int((rvol - 1) * 20), 0, 30)
        score += add
        reasons.append(f"RVOL {rvol} shows above-normal participation")
    pos = _g(ev, "range_52w.position_pct")
    if pos is not None and pos >= 85:
        score += 20
        reasons.append(f"Near 52w high (position {pos}%) — breakout zone")
    vs_sma = _g(ev, "technicals.price_vs_sma_pct")
    if vs_sma is not None and vs_sma > 0 and _g(ev, "technicals.trend") == "up":
        score += 15
        reasons.append(f"Price {vs_sma}% above SMA20 in an uptrend")
    drp = _g(ev, "technicals.day_range_position_pct")
    if drp is not None and drp >= 70:
        score += 8
        reasons.append(f"Closing strong in day range ({drp}%)")
    wr = _g(ev, "technicals.window_return_pct")
    if wr is not None and wr > 0:
        score += 5
        reasons.append(f"Positive window return {wr}%")

    # -- technical: RSI / MACD / Bollinger / SMA50-200 / ADX ---------------
    rsi = _g(ev, "technicals.rsi14")
    if rsi is not None and 45 <= rsi <= 68:
        score += 8
        reasons.append(f"RSI {rsi} — healthy momentum, not overheated")
    if _g(ev, "technicals.macd_bullish") is True:
        score += 12
        reasons.append("MACD above its signal line — bullish momentum")
    bb = _g(ev, "technicals.bb_percent_b")
    if bb is not None and 60 <= bb <= 95:
        score += 6
        reasons.append(f"Bollinger %B {bb} — pushing the upper band without extreme overbought")
    if _g(ev, "technicals.sma_cross") == "golden":
        score += 10
        reasons.append("SMA50 above SMA200 — golden cross")
    adx = _g(ev, "technicals.adx14")
    if adx is not None and adx >= 25 and _g(ev, "technicals.trend") == "up":
        score += 6
        reasons.append(f"ADX {adx} confirms a strong uptrend")
    if _g(ev, "technicals.obv_trend") == "rising":
        score += 5
        reasons.append("OBV rising — volume confirms the move")
    if _g(ev, "technicals.volume_breakout") is True:
        score += 12
        reasons.append("Volume breakout — fresh 20d high on 1.5x+ average volume")

    # -- fundamental: analyst target ---------------------------------------
    up = _g(ev, "analyst.upside_pct")
    if up is not None and up >= 10:
        score += 15
        reasons.append(f"Analyst upside {up}% to mean target")
    buy_pct = _g(ev, "analyst.buy_pct")
    if buy_pct is not None and buy_pct >= 80:
        score += 7
        reasons.append(f"{buy_pct}% analyst buys")

    # -- fundamental: valuation / profitability / growth --------------------
    pe = _g(ev, "fundamentals.pe_trailing")
    if pe is not None and 0 < pe <= 25:
        score += 8
        reasons.append(f"P/E {pe} — reasonably valued")
    peg = _g(ev, "fundamentals.peg_ratio")
    if peg is not None and 0 < peg <= 1.5:
        score += 6
        reasons.append(f"PEG {peg} — priced reasonably vs growth")
    roe = _g(ev, "fundamentals.roe_pct")
    if roe is not None and roe >= 15:
        score += 8
        reasons.append(f"ROE {roe}% — strong capital efficiency")
    rev_growth = _g(ev, "fundamentals.revenue_growth_yoy_pct")
    if rev_growth is not None and rev_growth >= 10:
        score += 8
        reasons.append(f"Revenue growth {rev_growth}% YoY")
    rev_cagr = _g(ev, "fundamentals_5y.revenue_cagr_pct")
    if rev_cagr is not None and rev_cagr >= 12:
        score += 8
        reasons.append(f"Revenue CAGR {rev_cagr}% over {_g(ev, 'fundamentals_5y.years_available')}y")
    ni_cagr = _g(ev, "fundamentals_5y.net_income_cagr_pct")
    if ni_cagr is not None and ni_cagr >= 12:
        score += 6
        reasons.append(f"Net profit CAGR {ni_cagr}% over {_g(ev, 'fundamentals_5y.years_available')}y")

    # -- fundamental: cash flow / operating profitability -------------------
    fcf = _g(ev, "fundamentals.free_cash_flow")
    if fcf is not None and fcf > 0:
        score += 6
        reasons.append(f"Positive free cash flow ({_usd(fcf)})")
    fcf_yield = _g(ev, "fundamentals.fcf_yield_pct")
    if fcf_yield is not None and fcf_yield >= 4:
        score += 5
        reasons.append(f"FCF yield {fcf_yield}% — cash-generative vs market cap")
    ocf = _g(ev, "fundamentals.operating_cash_flow")
    if ocf is not None and ocf > 0:
        score += 5
        reasons.append(f"Positive operating cash flow ({_usd(ocf)})")
    ncf = _g(ev, "fundamentals.net_cash_flow")
    if ncf is not None and ncf > 0:
        score += 3
        reasons.append(f"Net cash flow positive ({_usd(ncf)})")
    ccc = _g(ev, "fundamentals.cash_conversion_cycle_days")
    if ccc is not None and ccc <= 45:
        score += 5
        reasons.append(f"Efficient cash conversion cycle ({ccc} days)")
    ebitda = _g(ev, "fundamentals.ebitda")
    if ebitda is not None and ebitda > 0:
        score += 3
        reasons.append(f"EBITDA positive ({_usd(ebitda)}) — operating profitability")
    ebit = _g(ev, "fundamentals.ebit")
    if ebit is not None and ebit > 0:
        score += 3
        reasons.append(f"EBIT positive ({_usd(ebit)})")

    # -- news -----------------------------------------------------------------
    if (_g(ev, "news.positive", 0) or 0) > (_g(ev, "news.negative", 0) or 0):
        score += 5
        reasons.append("Net-positive news flow")

    return _clamp(score, 0, 100), reasons or ["No strong bullish confirmation in evidence"]


def _score_bear(ev):
    score, reasons = 0, []

    # -- technical: weak momentum / volume / position ----------------------
    rvol = _g(ev, "technicals.rvol")
    if rvol is not None and rvol < 1:
        score += 15
        reasons.append(f"Thin volume (RVOL {rvol}) — no conviction")
    pos = _g(ev, "range_52w.position_pct")
    if pos is not None and pos < 30:
        score += 20
        reasons.append(f"Near 52w low (position {pos}%)")
    if _g(ev, "technicals.trend") == "down":
        score += 15
        reasons.append("Downtrend / price below SMA20")
    pfh = _g(ev, "range_52w.pct_from_high")
    if pfh is not None and pfh <= -20:
        score += 10
        reasons.append(f"{pfh}% off the 52w high")
    drp = _g(ev, "technicals.day_range_position_pct")
    if drp is not None and drp <= 30:
        score += 5
        reasons.append(f"Weak close in day range ({drp}%)")

    # -- technical: RSI / MACD / Bollinger / SMA50-200 / ADX ---------------
    rsi = _g(ev, "technicals.rsi14")
    if rsi is not None and rsi >= 75:
        score += 8
        reasons.append(f"RSI {rsi} — overbought, stretched risk")
    elif rsi is not None and rsi <= 30:
        score += 6
        reasons.append(f"RSI {rsi} — breaking down, no base yet")
    if _g(ev, "technicals.macd_bullish") is False and _g(ev, "technicals.macd_hist") is not None:
        score += 12
        reasons.append("MACD below its signal line — bearish momentum")
    bb = _g(ev, "technicals.bb_percent_b")
    if bb is not None and bb <= 20:
        score += 6
        reasons.append(f"Bollinger %B {bb} — pinned to the lower band")
    if _g(ev, "technicals.sma_cross") == "death":
        score += 10
        reasons.append("SMA50 below SMA200 — death cross")
    adx = _g(ev, "technicals.adx14")
    if adx is not None and adx >= 25 and _g(ev, "technicals.trend") == "down":
        score += 6
        reasons.append(f"ADX {adx} confirms a strong downtrend")
    if _g(ev, "technicals.obv_trend") == "falling":
        score += 5
        reasons.append("OBV falling — volume confirms distribution")

    # -- fundamental: analyst target ---------------------------------------
    up = _g(ev, "analyst.upside_pct")
    if up is not None and up <= 0:
        score += 15
        reasons.append(f"No analyst headroom (upside {up}%)")
    sell_pct = _g(ev, "analyst.sell_pct")
    if sell_pct is not None and sell_pct >= 20:
        score += 7
        reasons.append(f"{sell_pct}% analyst sells")
    buy_pct = _g(ev, "analyst.buy_pct")
    if buy_pct is not None and buy_pct < 55:
        score += 8
        reasons.append(f"Weak conviction ({buy_pct}% buys)")

    # -- fundamental: valuation / profitability / growth / leverage --------
    pe = _g(ev, "fundamentals.pe_trailing")
    if pe is not None and (pe > 40 or pe <= 0):
        score += 8
        reasons.append(f"P/E {pe} — expensive or unprofitable")
    roe = _g(ev, "fundamentals.roe_pct")
    if roe is not None and roe < 0:
        score += 8
        reasons.append(f"ROE {roe}% — destroying capital")
    rev_growth = _g(ev, "fundamentals.revenue_growth_yoy_pct")
    if rev_growth is not None and rev_growth < 0:
        score += 8
        reasons.append(f"Revenue growth {rev_growth}% YoY — shrinking")
    rev_cagr = _g(ev, "fundamentals_5y.revenue_cagr_pct")
    if rev_cagr is not None and rev_cagr < 0:
        score += 8
        reasons.append(f"Revenue CAGR {rev_cagr}% over {_g(ev, 'fundamentals_5y.years_available')}y — declining business")
    ni_cagr = _g(ev, "fundamentals_5y.net_income_cagr_pct")
    if ni_cagr is not None and ni_cagr < 0:
        score += 6
        reasons.append(f"Net profit CAGR {ni_cagr}% over {_g(ev, 'fundamentals_5y.years_available')}y — shrinking profits")
    d2e = _g(ev, "fundamentals.debt_to_equity")
    if d2e is not None and d2e > 200:
        score += 6
        reasons.append(f"Debt/Equity {d2e} — highly leveraged")
    cr = _g(ev, "fundamentals.current_ratio")
    if cr is not None and cr < 1:
        score += 5
        reasons.append(f"Current ratio {cr} — liquidity risk")

    # -- fundamental: cash flow / operating profitability -------------------
    fcf = _g(ev, "fundamentals.free_cash_flow")
    if fcf is not None and fcf < 0:
        score += 8
        reasons.append(f"Negative free cash flow ({_usd(fcf)}) — burning cash")
    ocf = _g(ev, "fundamentals.operating_cash_flow")
    if ocf is not None and ocf < 0:
        score += 8
        reasons.append(f"Negative operating cash flow ({_usd(ocf)}) — core operations burning cash")
    ncf = _g(ev, "fundamentals.net_cash_flow")
    if ncf is not None and ncf < 0:
        score += 3
        reasons.append(f"Net cash flow negative ({_usd(ncf)})")
    ccc = _g(ev, "fundamentals.cash_conversion_cycle_days")
    if ccc is not None and ccc > 90:
        score += 5
        reasons.append(f"Bloated cash conversion cycle ({ccc} days) — cash tied up in working capital")
    ebitda = _g(ev, "fundamentals.ebitda")
    if ebitda is not None and ebitda <= 0:
        score += 6
        reasons.append(f"Negative EBITDA ({_usd(ebitda)}) — burning cash at the operating level")
    ebit = _g(ev, "fundamentals.ebit")
    if ebit is not None and ebit <= 0:
        score += 6
        reasons.append(f"Operating loss (EBIT {_usd(ebit)})")

    # -- news -----------------------------------------------------------------
    if (_g(ev, "news.negative", 0) or 0) > (_g(ev, "news.positive", 0) or 0):
        score += 5
        reasons.append("Net-negative news flow")

    return _clamp(score, 0, 100), reasons or ["No strong bearish signals in evidence"]


def _score_technician(ev):
    """Display score for the Technician seat — a summary of trend +
    momentum + volume confirmation, independent of the Bull/Bear debate."""
    score, reasons = 50, []
    rvol = _g(ev, "technicals.rvol")
    trend = _g(ev, "technicals.trend")
    vs_sma = _g(ev, "technicals.price_vs_sma_pct")
    if rvol is not None:
        score += _clamp(int((rvol - 1) * 15), -20, 25)
        reasons.append(f"RVOL {rvol}")
    if trend == "up":
        score += 12; reasons.append("Uptrend")
    elif trend == "down":
        score -= 12; reasons.append("Downtrend")
    if vs_sma is not None:
        score += _clamp(int(vs_sma), -15, 15)
        reasons.append(f"{vs_sma}% vs SMA20")

    rsi = _g(ev, "technicals.rsi14")
    if rsi is not None:
        if rsi >= 70:
            score -= 8; reasons.append(f"RSI {rsi} overbought")
        elif rsi <= 30:
            score -= 8; reasons.append(f"RSI {rsi} oversold/weak")
        else:
            score += 5; reasons.append(f"RSI {rsi}")

    macd_hist = _g(ev, "technicals.macd_hist")
    if macd_hist is not None:
        score += _clamp(int(macd_hist * 10), -12, 12)
        reasons.append(f"MACD hist {macd_hist} ({'bullish' if macd_hist > 0 else 'bearish'})")

    sma_cross = _g(ev, "technicals.sma_cross")
    if sma_cross == "golden":
        score += 12; reasons.append("Golden cross (SMA50>SMA200)")
    elif sma_cross == "death":
        score -= 12; reasons.append("Death cross (SMA50<SMA200)")

    adx = _g(ev, "technicals.adx14")
    if adx is not None:
        if adx >= 25:
            reasons.append(f"ADX {adx} — trending")
        else:
            reasons.append(f"ADX {adx} — range-bound")

    vb = _g(ev, "technicals.volume_breakout")
    if vb is True:
        score += 10; reasons.append("Volume breakout confirmed (20d high + 1.5x+ volume)")
    elif vb is False:
        reasons.append("No volume breakout")

    return _clamp(score, 0, 100), reasons or ["Insufficient technical data"]


def _score_fundamentalist(ev):
    """Display score for the Fundamentalist seat — valuation,
    profitability, growth (current + 5y trend), and balance-sheet health."""
    score, reasons = 50, []
    any_data = False

    up = _g(ev, "analyst.upside_pct")
    consensus = _g(ev, "analyst.consensus")
    n = _g(ev, "analyst.num_analysts")
    if up is not None:
        score += _clamp(int(up), -25, 30)
        reasons.append(f"Analyst upside {up}%")
        any_data = True
    if consensus in ("buy", "strong_buy"):
        score += 12; reasons.append(f"Consensus {consensus}"); any_data = True
    elif consensus in ("sell", "strong_sell"):
        score -= 15; reasons.append(f"Consensus {consensus}"); any_data = True
    if n:
        reasons.append(f"{n} analysts")

    pe = _g(ev, "fundamentals.pe_trailing")
    if pe is not None:
        any_data = True
        if 0 < pe <= 25:
            score += 8; reasons.append(f"P/E {pe} — reasonable valuation")
        elif pe > 40:
            score -= 8; reasons.append(f"P/E {pe} — expensive")
        elif pe <= 0:
            score -= 5; reasons.append("Unprofitable (negative P/E)")

    peg = _g(ev, "fundamentals.peg_ratio")
    if peg is not None and peg > 0:
        any_data = True
        if peg <= 1.5:
            score += 6; reasons.append(f"PEG {peg} — growth-adjusted value")
        elif peg > 3:
            score -= 6; reasons.append(f"PEG {peg} — pricey vs growth")

    roe = _g(ev, "fundamentals.roe_pct")
    if roe is not None:
        any_data = True
        score += _clamp(int(roe / 2), -10, 10)
        reasons.append(f"ROE {roe}%")

    rev_growth = _g(ev, "fundamentals.revenue_growth_yoy_pct")
    if rev_growth is not None:
        any_data = True
        score += _clamp(int(rev_growth), -10, 10)
        reasons.append(f"Revenue growth {rev_growth}% YoY")

    rev_cagr = _g(ev, "fundamentals_5y.revenue_cagr_pct")
    years = _g(ev, "fundamentals_5y.years_available")
    if rev_cagr is not None:
        any_data = True
        score += _clamp(int(rev_cagr), -10, 10)
        reasons.append(f"Revenue CAGR {rev_cagr}% ({years}y)")

    d2e = _g(ev, "fundamentals.debt_to_equity")
    if d2e is not None:
        any_data = True
        if d2e > 200:
            score -= 6; reasons.append(f"Debt/Equity {d2e} — leveraged")
        elif d2e < 50:
            score += 4; reasons.append(f"Debt/Equity {d2e} — conservative balance sheet")

    cr = _g(ev, "fundamentals.current_ratio")
    if cr is not None:
        any_data = True
        if cr >= 1.5:
            score += 5; reasons.append(f"Current ratio {cr} — healthy liquidity")
        elif cr < 1:
            score -= 5; reasons.append(f"Current ratio {cr} — liquidity risk")

    ni_cagr = _g(ev, "fundamentals_5y.net_income_cagr_pct")
    if ni_cagr is not None:
        any_data = True
        score += _clamp(int(ni_cagr), -10, 10)
        reasons.append(f"Net profit CAGR {ni_cagr}% ({years}y)")

    fcf = _g(ev, "fundamentals.free_cash_flow")
    if fcf is not None:
        any_data = True
        if fcf > 0:
            score += 6; reasons.append(f"Free cash flow positive ({_usd(fcf)})")
        else:
            score -= 6; reasons.append(f"Free cash flow negative ({_usd(fcf)})")

    fcf_yield = _g(ev, "fundamentals.fcf_yield_pct")
    if fcf_yield is not None:
        any_data = True
        if fcf_yield >= 4:
            score += 4; reasons.append(f"FCF yield {fcf_yield}%")

    ocf = _g(ev, "fundamentals.operating_cash_flow")
    if ocf is not None:
        any_data = True
        if ocf > 0:
            score += 4; reasons.append(f"Operating cash flow positive ({_usd(ocf)})")
        else:
            score -= 4; reasons.append(f"Operating cash flow negative ({_usd(ocf)})")

    ncf = _g(ev, "fundamentals.net_cash_flow")
    if ncf is not None:
        any_data = True
        reasons.append(f"Net cash flow {'positive' if ncf >= 0 else 'negative'} ({_usd(ncf)})")

    ccc = _g(ev, "fundamentals.cash_conversion_cycle_days")
    if ccc is not None:
        any_data = True
        if ccc <= 45:
            score += 4; reasons.append(f"Efficient cash conversion cycle ({ccc} days)")
        elif ccc > 90:
            score -= 4; reasons.append(f"Bloated cash conversion cycle ({ccc} days)")

    ebitda = _g(ev, "fundamentals.ebitda")
    if ebitda is not None:
        any_data = True
        reasons.append(f"EBITDA {_usd(ebitda)}")

    ebit = _g(ev, "fundamentals.ebit")
    if ebit is not None:
        any_data = True
        reasons.append(f"EBIT {_usd(ebit)}")

    if not any_data:
        return 45, ["Analyst and fundamentals data unavailable"]
    return _clamp(score, 0, 100), reasons


def _score_newsdesk(ev):
    pos = _g(ev, "news.positive", 0) or 0
    neg = _g(ev, "news.negative", 0) or 0
    total = _g(ev, "news.total", 0) or 0
    if total == 0:
        return 50, ["No recent headlines"]
    net = pos - neg
    score = _clamp(50 + net * 12, 0, 100)
    tone = "net-positive" if net > 0 else ("net-negative" if net < 0 else "mixed")
    return score, [f"{total} headlines, {tone} ({pos}+/{neg}-)"]


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------
def _judge(bull_score, bear_score, ev, bull_reasons):
    net = bull_score - bear_score
    pos = _g(ev, "range_52w.position_pct")
    rvol = _g(ev, "technicals.rvol")
    leadership = (pos is not None and pos >= 60) or (rvol is not None and rvol >= 3)

    if net >= 25 and leadership:
        verdict = "BUY"
    elif net <= -15:
        verdict = "AVOID"
    else:
        verdict = "WATCH"

    confidence = _clamp(round(4 + net / 15), 1, 10)
    if verdict == "BUY":
        confidence = max(confidence, 7)
    else:
        confidence = min(confidence, 6)

    winner = "Bull" if net >= 0 else "Bear"
    key_catalyst = bull_reasons[0] if (verdict == "BUY" and bull_reasons) else (
        "Confirmation absent — waiting for momentum/volume" if verdict == "WATCH"
        else "Risk/reward unfavorable on current evidence")

    if verdict == "BUY":
        rationale = f"Bull outweighs Bear by {net} with market leadership; risk/reward favorable."
    elif verdict == "AVOID":
        rationale = f"Bear case dominates (net {net}); no confirmation to justify entry."
    else:
        rationale = f"Promising but unconfirmed (net {net}, leadership {'yes' if leadership else 'no'})."

    return {
        "winner": winner,
        "verdict": verdict,
        "confidence": int(confidence),
        "rationale": rationale,
        "key_catalyst": key_catalyst,
        "bull_score": int(bull_score),
        "bear_score": int(bear_score),
        "net": int(net),
    }


# ---------------------------------------------------------------------------
# grounding verifier
# ---------------------------------------------------------------------------
def _collect_numbers(ev):
    """Every numeric value present in the evidence, as strings, for tracing."""
    seen = set()

    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
        elif isinstance(x, (int, float)):
            seen.add(str(x))
            seen.add(str(abs(x)))
    walk(ev)
    return seen


def verify_grounding(text_numbers, ev):
    """Return numbers cited that don't trace to the evidence bundle."""
    import re
    known = _collect_numbers(ev)
    flagged = []
    for token in text_numbers:
        nums = re.findall(r"-?\d+\.?\d*", token)
        for n in nums:
            if n not in known and n.lstrip("-") not in known:
                # ignore small counts (1..10) used as generic scale references
                try:
                    if abs(float(n)) <= 10:
                        continue
                except ValueError:
                    pass
                flagged.append(n)
    return flagged


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def evaluate(evidence):
    bull_score, bull_reasons = _score_bull(evidence)
    bear_score, bear_reasons = _score_bear(evidence)
    tech_score, tech_reasons = _score_technician(evidence)
    fund_score, fund_reasons = _score_fundamentalist(evidence)
    news_score, news_reasons = _score_newsdesk(evidence)

    verdict = _judge(bull_score, bear_score, evidence, bull_reasons)

    return {
        "scores": {
            "bull": {"score": bull_score, "reasons": bull_reasons},
            "bear": {"score": bear_score, "reasons": bear_reasons},
            "technician": {"score": tech_score, "reasons": tech_reasons},
            "fundamentalist": {"score": fund_score, "reasons": fund_reasons},
            "newsdesk": {"score": news_score, "reasons": news_reasons},
        },
        "verdict": verdict,
        "engine": "deterministic",
    }
