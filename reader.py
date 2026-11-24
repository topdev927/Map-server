"""Reads one permit PDF into the route schema, with FREE backends first.

  rules   : built-in parsers for the layouts seen so far (offline, deterministic)
  ollama  : a local language model run by Ollama (free; for layouts the rules do not know)
  claude  : paid Anthropic API (optional; only used if ANTHROPIC_API_KEY is set and EXTRACTOR allows)

EXTRACTOR=auto (default) tries rules -> ollama -> claude.   Or set rules / ollama / claude.
Whatever the backend, extract_route.validate() checks the result against the PDF text.
"""
import io
import json
import os
import re
import urllib.error
import urllib.request

import extract_route as X
import rules_extract as R

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")


class NoBackend(Exception):
    pass


# ------------------------------------------------------------- rules backend
def leg_from_rules(data):
    state, p = R.parse_pdf(io.BytesIO(data))
    r = p["route"]
    steps = []
    for s in r["steps"]:
        f = set(s["flags"])
        kind = ("not_permitted" if "route_not_permitted_contact_local_government" in f else
                "detour" if "detour" in f else
                "local_or_county" if f & {"county_road", "non_state_or_local_road"} else
                "ramp" if "ramp" in f else "road")
        steps.append({"road": s["road"], "exit": s["exit"], "miles": s["miles"], "kind": kind, "text": s["instruction"]})

    def place(raw, ll):
        d = {"text": raw}
        if ll:
            d["lat"], d["lon"] = ll
        return d

    chk = p["extraction"]["checks"] or {}
    return {"state": state, "permit_number": p["permit"]["number"],
            "origin": place(r["origin_raw"], r["origin_latlon"]),
            "destination": place(r["destination_raw"], r["destination_latlon"]),
            "total_miles": chk.get("declared_total"), "steps": steps}


# ------------------------------------------------------------ ollama backend
# Plain schema (no $ref, no null types, optional fields simply omitted): it is the shape
# local models and their grammar converter handle most reliably.
OLLAMA_SCHEMA = {
    "type": "object",
    "required": ["state", "origin", "destination", "steps"],
    "properties": {
        "state": {"type": "string"},
        "permit_number": {"type": "string"},
        "origin": {"type": "object", "required": ["text"],
                   "properties": {"text": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}}},
        "destination": {"type": "object", "required": ["text"],
                        "properties": {"text": {"type": "string"}, "lat": {"type": "number"}, "lon": {"type": "number"}}},
        "total_miles": {"type": "number"},
        "steps": {"type": "array", "items": {
            "type": "object", "required": ["text"],
            "properties": {
                "road": {"type": "string"}, "exit": {"type": "string"}, "miles": {"type": "number"},
                "kind": {"type": "string", "enum": ["road", "ramp", "detour", "local_or_county", "not_permitted"]},
                "text": {"type": "string"}}}},
    },
}

OLLAMA_PROMPT = X.PROMPT + """
Reply with ONE JSON object only, in this shape (omit a field when the permit does not print it):
{"state":"TX","permit_number":"123","origin":{"text":"..."},"destination":{"text":"..."},
 "steps":[{"road":"IH20","miles":12.3,"kind":"road","text":"the permit's own line"}]}
Use two-letter state codes. Do not add commentary.
"""


def _post(path, payload, timeout):
    req = urllib.request.Request(OLLAMA_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ollama_models():
    """List of installed model names, or None if Ollama is not running."""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2) as r:
            return [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def normalize_leg(raw):
    """Repair what small models get wrong: missing keys, numbers as strings, empty strings."""
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def place(p):
        p = p if isinstance(p, dict) else {"text": str(p or "")}
        return {"text": str(p.get("text", "")), "lat": num(p.get("lat")), "lon": num(p.get("lon"))}

    steps = []
    for s in raw.get("steps") or []:
        if not isinstance(s, dict) or not str(s.get("text", "")).strip():
            continue
        kind = s.get("kind") if s.get("kind") in ("road", "ramp", "detour", "local_or_county", "not_permitted") else "road"
        steps.append({"road": (str(s["road"]).strip() or None) if s.get("road") else None,
                      "exit": (str(s["exit"]).strip() or None) if s.get("exit") else None,
                      "miles": num(s.get("miles")), "kind": kind, "text": str(s["text"]).strip()})
    return {"state": str(raw.get("state", "")).strip().upper()[:2] or "??",
            "permit_number": (str(raw["permit_number"]) if raw.get("permit_number") else None),
            "origin": place(raw.get("origin")), "destination": place(raw.get("destination")),
            "total_miles": num(raw.get("total_miles")), "steps": steps}


def leg_from_ollama(text, model=None, timeout=900):
    models = ollama_models()
    if models is None:
        raise NoBackend(f"Ollama is not running at {OLLAMA_URL}. Install it from ollama.com and start it.")
    model = model or OLLAMA_MODEL
    if not any(m == model or m.split(":")[0] == model.split(":")[0] and ":" not in model for m in models):
        raise NoBackend(f"Ollama has no model '{model}'. Run:  ollama pull {model}")
    num_ctx = min(32768, max(8192, len(text) // 2 + 4096))
    payload = {"model": model, "stream": False, "format": OLLAMA_SCHEMA,
               "options": {"temperature": 0, "num_ctx": num_ctx},
               "messages": [{"role": "system", "content": OLLAMA_PROMPT},
                            {"role": "user", "content": text}]}
    try:
        res = _post("/api/chat", {**payload, "think": False}, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400:      # 400 can mean "this model has no thinking mode": retry without the flag
            raise
        res = _post("/api/chat", payload, timeout)
    content = res["message"]["content"]
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    return normalize_leg(json.loads(content))


# ------------------------------------------------------------------ dispatcher
def read_permit(data, mode=None, llm=None):
    """Returns (leg, text, method).  `llm` (optional) replaces the paid backend, for tests."""
    mode = (mode or os.environ.get("EXTRACTOR", "auto")).lower()
    text = X.pdf_text(io.BytesIO(data))
    tried, best = [], None

    def consider(leg, method):
        nonlocal best
        problems, _ = X.validate(leg, text)
        if best is None or len(problems) < len(best[3]):
            best = (leg, text, method, problems)
        return not problems

    if mode in ("auto", "rules"):
        try:
            leg = leg_from_rules(data)
            if leg["steps"] and consider(leg, "rules"):
                return best[:3]
            tried.append("rules (layout recognised but the result failed its checks)" if leg["steps"] else "rules (no route steps found)")
        except R.UnknownLayout:
            tried.append("rules (layout not known)")
        except Exception as e:  # noqa: BLE001  - an odd variant of a known layout
            tried.append(f"rules (error: {type(e).__name__})")
        if mode == "rules":
            if best:
                return best[:3]
            raise NoBackend("The built-in rules do not know this permit layout. " + "; ".join(tried))

    if mode in ("auto", "ollama"):
        try:
            if consider(leg_from_ollama(text), "ollama"):
                return best[:3]
        except NoBackend as e:
            tried.append(f"ollama ({e})")
            if mode == "ollama":
                raise
        except Exception as e:  # noqa: BLE001
            tried.append(f"ollama (error: {type(e).__name__}: {str(e)[:150]})")
            if mode == "ollama":
                raise NoBackend(tried[-1])

    if mode == "claude" or (mode == "auto" and (llm or os.environ.get("ANTHROPIC_API_KEY"))):
        leg = (llm or X.call_llm)(text)
        consider(leg, "claude")

    if best:
        return best[:3]   # nothing passed cleanly: return the best attempt; the caller shows its problems
    raise NoBackend("No free reader could read this permit. Tried: " + "; ".join(tried) +
                    ". To handle new layouts, install Ollama (ollama.com) and run: ollama pull " + OLLAMA_MODEL)
