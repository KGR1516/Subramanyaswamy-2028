"""
llm.py
------
LLM debate engine with provider auto-detection, plus a guaranteed fallback
to the deterministic engine in scoring.py.

detect_engine() -> str    # "claude_code" | "anthropic" | "openai" | "deterministic"
evaluate(evidence) -> same shape as scoring.evaluate(), with "engine" set.

One combined call per stock: a six-seat panel (Bull, Bear, Fundamentals,
Technicals, News) + a Judge, returning strict JSON.

Grounding rule: the prompt forbids inventing numbers; the verifier in
scoring.py flags anything untraceable. If the LLM output fails to parse or
fails grounding, we fall back to deterministic scoring for that stock.
"""

import os
import json
import shutil
import subprocess

import scoring

_SYSTEM = (
    "You are an equity research panel. A BUY requires genuinely favorable "
    "risk/reward WITH confirmation (momentum and/or volume). WATCH if "
    "promising but unconfirmed. AVOID if poor. Ground every figure in the "
    "evidence provided; never invent numbers. If a needed value is missing, "
    "say 'data unavailable'. Respond with STRICT JSON only, no prose, no "
    "markdown fences."
)

_SCHEMA_HINT = """Return JSON exactly like:
{
  "panel": {
    "bull":           {"conviction": 0-100, "point": "<=25 words"},
    "bear":           {"conviction": 0-100, "point": "<=25 words"},
    "fundamentalist": {"conviction": 0-100, "point": "<=25 words"},
    "technician":     {"conviction": 0-100, "point": "<=25 words"},
    "newsdesk":       {"conviction": 0-100, "point": "<=25 words"}
  },
  "judge": {
    "winner": "Bull" | "Bear",
    "verdict": "BUY" | "WATCH" | "AVOID",
    "confidence": 1-10,
    "rationale": "<=2 lines",
    "key_catalyst": "one line"
  }
}"""


def _provider_override():
    return (os.environ.get("LLM_PROVIDER") or "").strip().lower() or None


def detect_engine():
    """Resolve the active engine using the spec's priority order."""
    forced = _provider_override()
    if forced == "claude_code":
        return "claude_code" if shutil.which("claude") else "deterministic"
    if forced == "anthropic":
        return "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "deterministic"
    if forced == "openai":
        return "openai" if os.environ.get("OPENAI_API_KEY") else "deterministic"

    # auto-detect, in priority order
    if shutil.which("claude"):
        return "claude_code"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "deterministic"


def _build_prompt(ev):
    return (
        f"{_SCHEMA_HINT}\n\n"
        f"Evidence bundle for {ev.get('symbol')} "
        f"({ev.get('cap_segment')} cap, {ev.get('sector')}):\n"
        f"{json.dumps(ev, ensure_ascii=False)}\n\n"
        "Debate the case, then have the Judge rule."
    )


# ---------------------------------------------------------------------------
# provider callers -> raw text
# ---------------------------------------------------------------------------
def _call_claude_code(prompt, model="haiku"):
    proc = subprocess.run(
        ["claude", "-p", f"{_SYSTEM}\n\n{prompt}",
         "--output-format", "json", "--model", model],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI exit {proc.returncode}: {proc.stderr[:200]}")
    envelope = json.loads(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError("claude CLI returned is_error=true")
    return envelope.get("result", "")


def _call_anthropic(prompt):
    import requests
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-3-5-haiku-latest",
            "max_tokens": 700,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def _call_openai(prompt):
    import requests
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "content-type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# parsing + shaping
# ---------------------------------------------------------------------------
def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[start:end + 1])


def _shape(parsed, ev):
    panel = parsed["panel"]
    judge = parsed["judge"]

    def seat(name):
        s = panel.get(name, {})
        conv = int(s.get("conviction", 50))
        return {"score": max(0, min(100, conv)), "reasons": [s.get("point", "")]}

    scores = {k: seat(k) for k in
              ("bull", "bear", "fundamentalist", "technician", "newsdesk")}

    bull_s = scores["bull"]["score"]
    bear_s = scores["bear"]["score"]
    verdict = {
        "winner": judge.get("winner", "Bull" if bull_s >= bear_s else "Bear"),
        "verdict": str(judge.get("verdict", "WATCH")).upper(),
        "confidence": int(judge.get("confidence", 5)),
        "rationale": judge.get("rationale", ""),
        "key_catalyst": judge.get("key_catalyst", ""),
        "bull_score": bull_s,
        "bear_score": bear_s,
        "net": bull_s - bear_s,
    }

    # grounding check across every point + the rationale
    cited = [s["reasons"][0] for s in scores.values()]
    cited += [verdict["rationale"], verdict["key_catalyst"]]
    flagged = scoring.verify_grounding(cited, ev)
    if flagged:
        raise ValueError(f"ungrounded numbers cited: {flagged}")

    return {"scores": scores, "verdict": verdict}


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def evaluate(evidence, engine=None):
    """Run the LLM debate; fall back to deterministic on any failure."""
    engine = engine or detect_engine()
    if engine == "deterministic":
        out = scoring.evaluate(evidence)
        out["engine"] = "deterministic"
        return out

    prompt = _build_prompt(evidence)
    try:
        if engine == "claude_code":
            raw = _call_claude_code(prompt)
        elif engine == "anthropic":
            raw = _call_anthropic(prompt)
        elif engine == "openai":
            raw = _call_openai(prompt)
        else:
            raise RuntimeError(f"unknown engine {engine}")

        shaped = _shape(_extract_json(raw), evidence)
        shaped["engine"] = engine
        return shaped
    except Exception as e:
        print(f"[llm] {engine} failed for {evidence.get('symbol')}: {e} -> deterministic")
        out = scoring.evaluate(evidence)
        out["engine"] = "deterministic"
        return out
