"""State-independent permit route extraction.

Design: the LLM only COPIES route lines from the PDF into route.schema.json
(verbatim text, road as printed).  Plain code then
  1. canonicalises road names (IH20 -> I-20, SH43 -> TX-43 ...), and
  2. verifies the LLM output against the PDF text, so a new state's layout
     cannot silently produce an invented or reordered route.

    python3 -I extract_route.py permit.pdf            # needs ANTHROPIC_API_KEY
    python3 -I extract_route.py permit.pdf --mock leg.json   # skip the LLM

NOTE: call_llm() has not been run in the authoring environment (no API access
there); everything else is covered by test_validation.py.
"""
import json
import os
import re
import sys

import pdfplumber

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "route.schema.json")
MODEL = "claude-sonnet-5-5"
MINUS = "−"

PROMPT = """You are reading a US oversize/overweight vehicle permit. Extract ONLY the
authorised route, in order, into the tool schema.

Rules:
- Copy, never infer. 'text' is the permit's own wording for each step; 'road' is the
  designation exactly as printed (do not convert IH20 to I-20).
- Include every step of the route, in printed order, including ramps and detours.
- Ignore vehicle data, fees, escort/time restrictions and legal boilerplate.
- If the permit prints distances, copy them into 'miles'; if not, use null.
- If a line says a road is not permitted or needs local approval, kind='not_permitted'.
- origin/destination: copy as printed; give lat/lon only if printed.
- If the permit has no specific route (blanket/statewide), return one step whose text
  says so and road=null.
"""


# ------------------------------------------------------------ PDF -> text
def pdf_text(path_or_file):
    """path_or_file: a file path or a binary file object (e.g. io.BytesIO)."""
    with pdfplumber.open(path_or_file) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if len(text.strip()) < 200:
        raise RuntimeError("No text layer: run OCR first (ocrmypdf, Apple Vision, ML Kit...)")
    return text.replace(MINUS, "-")


# ------------------------------------------------------ road canonicaliser
STATES = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
_ST = "|".join(STATES)
ROAD_RE = re.compile(
    r"\b(?P<p>IH|I|US|SH|SR|SL|LP|LOOP|FM|RM|SS|BS|SPUR|CO\s?RD|CR|" + _ST + r")[-\s]?0*(?P<n>\d{1,4})(?P<s>[NSEW]?FR|[A-Z]?)\b",
    re.I,
)


def canon(token, state=None):
    """Return (key, display) for a road designation, or (None, None)."""
    t = token.replace(MINUS, "-").strip()
    m = ROAD_RE.search(t)
    if not m:
        return None, None
    p, n = m.group("p").upper().replace(" ", ""), int(m.group("n"))
    if p in ("IH", "I"):
        return f"I{n}", f"I-{n}"
    if p == "US":
        return f"US{n}", f"US-{n}"
    if p in ("SL", "LP", "LOOP"):
        return f"LOOP{n}", f"{state or 'ST'}-LOOP-{n}"
    if p in ("SS", "SPUR"):                 # TX state spur: permits print SS 16, the map 'Spur 16'
        return f"SPUR{n}", f"{state or 'ST'}-SPUR-{n}"
    if p in ("COR", "CORD", "CR"):
        return f"CR{n}", f"CR-{n}"
    if p in ("SH", "SR") or p in STATES:
        st = state if p in ("SH", "SR") else p
        return f"ST{n}", f"{st or 'ST'}-{n}"
    return f"{p}{n}", f"{state or 'ST'}-{p}-{n}"


def roads_in_text(text):
    return {canon(m.group(0))[0] for m in ROAD_RE.finditer(text)}


# ---------------------------------------------------------------- verify
def _tokens(s):
    return re.findall(r"[A-Za-z0-9]+", s.lower())


def validate(leg, text):
    """Return a list of problems; an empty list means the extraction is internally consistent
    and grounded in the PDF.  Works for any layout."""
    problems = []
    src = set(_tokens(text))
    src_roads = roads_in_text(text)
    steps = leg["steps"]

    # 1. grounding: step wording must come from the PDF
    for i, s in enumerate(steps, 1):
        toks = _tokens(s["text"])
        if toks:
            cov = sum(t in src for t in toks) / len(toks)
            if cov < 0.9:
                problems.append(f"step {i}: only {cov:.0%} of its words occur in the PDF -> possible invention")
        if s.get("road"):
            key, _ = canon(s["road"])
            if key is None:
                # unusual designation (county/local id): accept only if printed verbatim
                if re.sub(r"\s+", "", s["road"]).lower() not in re.sub(r"\s+", "", text).lower():
                    problems.append(f"step {i}: road '{s['road']}' never appears in the PDF")
            elif key not in src_roads:
                problems.append(f"step {i}: road '{s['road']}' never appears in the PDF")

    # 2. order: walk forward through the PDF; each step's wording must be found after the
    #    previous one.  A step found only EARLIER in the document was moved.
    flat = " ".join(_tokens(text))
    last, moved = 0, []
    for i, s in enumerate(steps, 1):
        toks = _tokens(s["text"])
        if len(toks) < 3:
            continue
        key = " ".join(toks[:8])
        at = flat.find(key, last)
        if at != -1:
            last = at + 1
        elif flat.find(key) != -1:
            moved.append(i)
    if moved:
        problems.append(f"steps {moved[:6]} appear in a different order than in the PDF")

    # 3. coverage: every road named in the route section should be in the output
    out_roads = {canon(s["road"])[0] for s in steps if s.get("road") and canon(s["road"])[0]}
    missing = {r for r in src_roads if r not in out_roads}
    # roads in boilerplate (restrictions, legal text) are expected to be missing; report as info only
    info = sorted(missing)

    # 4. distances
    miles = [s["miles"] for s in steps if s.get("miles") is not None]
    if miles and leg.get("total_miles") is not None:
        if abs(sum(miles) - leg["total_miles"]) > max(0.5, 0.005 * leg["total_miles"]):
            problems.append(f"step miles sum to {sum(miles):.1f} but permit total is {leg['total_miles']}")
    if miles and len(miles) != len(steps):
        problems.append("some steps have miles and some do not")
    return problems, info


def needs_review(leg, problems):
    """Single flag the UI can use: show the confirm screen with these items highlighted."""
    return bool(problems) or any(s.get("kind") == "not_permitted" for s in leg["steps"])


def chain_gaps(legs):
    """Cheap continuity check: consecutive legs should meet at a state line.
    Flags legs whose origin text does not mention the previous leg's state."""
    out = []
    for a, b in zip(legs, legs[1:]):
        o = b["origin"]["text"].upper()
        if a["state"].upper() not in o:
            out.append(f"{a['state']} -> {b['state']}: next origin '{b['origin']['text']}' does not mention {a['state']}")
    return out


def order_legs(legs):
    """Put permits in travel order using the origin text (each next permit starts at the
    previous permit's state line).  Returns (ordered_legs, confident)."""
    def mentions(text, st):
        return re.search(rf"(?<![A-Z]){st}(?![A-Z])", (text or "").upper()) is not None
    states = {l["state"] for l in legs}
    firsts = [l for l in legs if not any(mentions(l["origin"]["text"], s) for s in states if s != l["state"])]
    if len(firsts) != 1:
        return list(legs), False
    cur = firsts[0]
    ordered, rest = [cur], [l for l in legs if l is not cur]
    while rest:
        nxt = [l for l in rest if mentions(l["origin"]["text"], cur["state"])]
        if len(nxt) != 1:
            return ordered + rest, False
        cur = nxt[0]
        ordered.append(cur)
        rest.remove(cur)
    return ordered, True


# ------------------------------------------------------------------- LLM
def call_llm(text):
    import anthropic  # pip install anthropic
    schema = json.load(open(SCHEMA_PATH))
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=16000,
        system=PROMPT,
        tools=[{"name": "record_route", "description": "Record the permit route", "input_schema": schema}],
        tool_choice={"type": "tool", "name": "record_route"},
        messages=[{"role": "user", "content": text}],
    )
    return next(b.input for b in resp.content if b.type == "tool_use")


def enrich(leg):
    """Add canonical road names for the map step (deterministic, not LLM)."""
    for s in leg["steps"]:
        s["road_canonical"] = canon(s["road"], leg["state"])[1] if s.get("road") else None
    return leg


def main():
    path = sys.argv[1]
    text = pdf_text(path)
    leg = json.load(open(sys.argv[sys.argv.index("--mock") + 1])) if "--mock" in sys.argv else call_llm(text)
    problems, info = validate(leg, text)
    leg = enrich(leg)
    print(json.dumps({"leg": leg, "problems": problems, "needs_review": needs_review(leg, problems)}, indent=2))


if __name__ == "__main__":
    main()
