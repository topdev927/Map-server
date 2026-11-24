import json, sys, glob
sys.path.insert(0, "/home/claude/out/server"); sys.path.insert(0, "/home/claude/out/v2")
from fastapi.testclient import TestClient
import app as A
import route_builder as rb
import test_route_builder as T   # reuse synthetic roads (prints its own PASS lines)
from test_validation import to_leg, gold, pdfs

c = TestClient(A.app)
ok = True
def check(n, cond, extra=""):
    global ok; ok &= bool(cond); print(("PASS " if cond else "FAIL ") + n, extra)

check("health", c.get("/health").json() == {"ok": True})

sc = next(to_leg(p) for p in gold if p["permit"]["state"] == "SC")
A.app.state.llm = lambda text: json.loads(json.dumps(sc))      # stand-in for the LLM
r = c.post("/v1/extract", files={"file": ("sc.pdf", open(pdfs["SC"], "rb").read(), "application/pdf")})
j = r.json()
check("extract SC pdf", r.status_code == 200 and j["problems"] == [] and j["needs_review"] and j["leg"]["steps"][1]["road_canonical"] == "SC-35", f"status={r.status_code}")
check("rejects non-PDF", c.post("/v1/extract", files={"file": ("x.pdf", b"hello", "application/pdf")}).status_code == 415)
check("rejects oversize", c.post("/v1/extract", files={"file": ("x.pdf", b"%PDF" + b"0" * (11 * 2**20), "application/pdf")}).status_code == 413)

legs = [{"state": "LA", "origin": {"text": "MS line"}, "destination": {"text": "TX line"},
         "steps": [dict(road="I-20 W", miles=5.8, kind="road", text="I-20 W"), dict(road="I-220 W", miles=23.0, kind="road", text="I-220 W"), dict(road="I-20 W", miles=5.8, kind="road", text="I-20 W")]}]
A.app.state.fetch = lambda state, runs, **kw: {"elements": [T.I20, T.I220]}
r = c.post("/v1/route", json={"legs": legs, "start": {"lat": 32.0, "lon": -91.0}, "end": {"lat": 32.0, "lon": -90.6}})
j = r.json()
check("route built", r.status_code == 200 and len(j["geojson"]["features"]) == 3 and not j["warnings"], f"status={r.status_code} warn={j.get('warnings')}")
r = c.post("/v1/route", json={"legs": legs})
check("asks for start/end when permit prints none", r.status_code == 422 and r.json()["detail"]["needs"] == ["start", "end"])
A.app.state.fetch = lambda state, runs, **kw: {"elements": [T.I20]}
r = c.post("/v1/route", json={"legs": legs, "start": {"lat": 32.0, "lon": -91.0}, "end": {"lat": 32.0, "lon": -90.6}})
check("missing road -> 409 with hint", r.status_code == 409, str(r.json())[:90])
print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
