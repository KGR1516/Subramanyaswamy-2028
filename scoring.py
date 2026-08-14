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


# ---------------------------------------------------------------------------
# individual agents -> conviction 0..100 + short grounded reasons
# ---------------------------------------------------------------------------
def _score_bull(ev):
    score, reasons = 0, []
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
        reasons.append(f"Price {vs_sma}% above SMA in an uptrend")
    drp = _g(ev, "technicals.day_range_position_pct")
    if drp is not None and drp >= 70:
        score += 8
        reasons.append(f"Closing strong in day range ({drp}%)")
    up = _g(ev, "analyst.upside_pct")
    if up is not None and up >= 10:
        score += 15
        reasons.append(f"Analyst upside {up}% to mean target")
    buy_pct = _g(ev, "analyst.buy_pct")
    if buy_pct is not None and buy_pct >= 80:
        score += 7
        reasons.append(f"{buy_pct}% analyst buys")
    if (_g(ev, "news.positive", 0) or 0) > (_g(ev, "news.negative", 0) or 0):
        score += 5
        reasons.append("Net-positive news flow")
    wr = _g(ev, "technicals.window_return_pct")
    if wr is not None and wr > 0:
        score += 5
        reasons.append(f"Positive window return {wr}%")
    return _clamp(score, 0, 100), reasons or ["No strong bullish confirmation in evidence"]


def _score_bear(ev):
    score, reasons = 0, []
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
        reasons.append("Downtrend / price below SMA")
    up = _g(ev, "analyst.upside_pct")
    if up is not None and up <= 0:
        score += 15
        reasons.append(f"No analyst headroom (upside {up}%)")
    buy_pct = _g(ev, "analyst.buy_pct")
    if buy_pct is not None and buy_pct < 55:
        score += 8
        reasons.append(f"Weak conviction ({buy_pct}% buys)")
    pfh = _g(ev, "range_52w.pct_from_high")
    if pfh is not None and pfh <= -20:
        score += 10
        reasons.append(f"{pfh}% off the 52w high")
    sell_pct = _g(ev, "analyst.sell_pct")
    if sell_pct is not None and sell_pct >= 20:
        score += 7
        reasons.append(f"{sell_pct}% analyst sells")
    if (_g(ev, "news.negative", 0) or 0) > (_g(ev, "news.positive", 0) or 0):
        score += 5
        reasons.append("Net-negative news flow")
    drp = _g(ev, "technicals.day_range_position_pct")
    if drp is not None and drp <= 30:
        score += 5
        reasons.append(f"Weak close in day range ({drp}%)")
    return _clamp(score, 0, 100), reasons or ["No strong bearish signals in evidence"]


def _score_technician(ev):
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
        reasons.append(f"{vs_sma}% vs SMA")
    return _clamp(score, 0, 100), reasons or ["Insufficient technical data"]


def _score_fundamentalist(ev):
    score, reasons = 50, []
    up = _g(ev, "analyst.upside_pct")
    consensus = _g(ev, "analyst.consensus")
    n = _g(ev, "analyst.num_analysts")
    if up is not None:
        score += _clamp(int(up), -25, 30)
        reasons.append(f"Upside {up}%")
    if consensus in ("buy", "strong_buy"):
        score += 12; reasons.append(f"Consensus {consensus}")
    elif consensus in ("sell", "strong_sell"):
        score -= 15; reasons.append(f"Consensus {consensus}")
    if n:
        reasons.append(f"{n} analysts")
    if up is None and consensus is None:
        return 45, ["Analyst data unavailable"]
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
