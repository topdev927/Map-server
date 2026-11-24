"""Tests the free readers: rules on the 7 sample permits, and the Ollama fallback
(against a FAKE local Ollama server, so this checks the plumbing, not real model quality)."""
import glob, io, json, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
sys.path.insert(0, "/home/claude/out/server")
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import extract_route as X, reader

ok = True
def check(n, c, extra=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n, extra)

# 1. rules reader on the seven real permits
pdfs = {p.split("_-_")[1].split(".")[0].replace("_1", ""): p for p in glob.glob("/mnt/user-data/uploads/*.pdf")}
for st, p in sorted(pdfs.items()):
    leg, text, method = reader.read_permit(open(p, "rb").read(), mode="rules")
    problems, _ = X.validate(leg, text)
    check(f"rules {st}", method == "rules" and not problems and len(leg["steps"]) > 0, f"steps={len(leg['steps'])}")

# 2. a permit from a state the rules have never seen
def fake_pdf():
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=letter); y = 750
    lines = ["STATE OF UTOPIA DEPARTMENT OF TRANSPORTATION", "OVERSIZE LOAD PERMIT NO. 77-1234  Valid 11/20/2026 - 11/27/2026",
             "Vehicle: 14 ft wide, 15 ft high, 99,000 lbs. Escort required at night. Daylight travel only.",
             "ROUTE: From the Atlantis State Line to Eldorado Port of Entry",
             "1. Begin on US-12 East for 14.2 miles to SR-34", "2. Turn left onto SR-34 North for 8.0 miles to I-70",
             "3. Take I-70 West, Exit 41, for 22.5 miles to Eldorado Port of Entry",
             "General conditions: the permittee must carry this permit in the vehicle at all times and obey all traffic laws."]
    for l in lines:
        c.drawString(40, y, l); y -= 18
    c.save(); return buf.getvalue()
utopia = fake_pdf()
try:
    reader.read_permit(utopia, mode="rules"); check("unknown layout rejected by rules", False)
except reader.NoBackend as e:
    check("unknown layout rejected by rules", True)
reader.OLLAMA_URL = "http://127.0.0.1:9"   # nothing there
try:
    reader.read_permit(utopia); check("no Ollama: clear instruction", False)
except reader.NoBackend as e:
    check("no Ollama: clear instruction", "ollama pull" in str(e) and "Ollama" in str(e), str(e)[:70])

# 3. fake Ollama server
GOOD = {"state": "ut", "permit_number": 77, "origin": {"text": "Atlantis State Line"}, "destination": {"text": "Eldorado Port of Entry"},
        "steps": [{"road": "US-12", "miles": "14.2", "text": "Begin on US-12 East for 14.2 miles to SR-34"},
                  {"road": "SR-34", "miles": 8.0, "kind": "road", "text": "Turn left onto SR-34 North for 8.0 miles to I-70"},
                  {"road": "I-70", "exit": "41", "miles": 22.5, "kind": "weird", "text": "Take I-70 West, Exit 41, for 22.5 miles to Eldorado Port of Entry"},
                  {"road": "", "text": ""}]}
INVENTED = json.loads(json.dumps(GOOD)); INVENTED["steps"][1] = {"road": "US-99", "miles": 8, "text": "Turn right onto US-99 South toward Atlantis City"}
state = {"reply": GOOD, "reject_think": False, "seen": []}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200, {"models": [{"name": "qwen3:8b"}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state["seen"].append(req)
        if state["reject_think"] and "think" in req:
            return self._send(400, {"error": "does not support thinking"})
        self._send(200, {"message": {"content": "<think>hmm</think>" + json.dumps(state["reply"])}})
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
reader.OLLAMA_URL = f"http://127.0.0.1:{srv.server_port}"

leg, text, method = reader.read_permit(utopia)
problems, _ = X.validate(leg, text)
check("auto falls back to ollama", method == "ollama", method)
check("messy model output repaired", leg["state"] == "UT" and leg["steps"][0]["miles"] == 14.2 and leg["steps"][2]["kind"] == "road" and len(leg["steps"]) == 3, [s["kind"] for s in leg["steps"]])
check("repaired output passes the PDF checks", problems == [], problems)
req = state["seen"][-1]
check("request carries schema, temperature 0, big enough context", req["format"]["type"] == "object" and req["options"]["temperature"] == 0 and req["options"]["num_ctx"] >= 8192)

state["reply"] = INVENTED
leg, text, method = reader.read_permit(utopia)
check("invented step is caught", any("US-99" in p or "different order" in p or "words occur" in p for p in X.validate(leg, text)[0]), X.validate(leg, text)[0])

state["reply"] = GOOD; state["reject_think"] = True
leg, _, method = reader.read_permit(utopia)
check("model without thinking mode: retried without the flag", method == "ollama" and "think" not in state["seen"][-1])

reader.OLLAMA_MODEL = "not-installed:1b"
try:
    reader.read_permit(utopia, mode="ollama"); check("missing model: clear instruction", False)
except reader.NoBackend as e:
    check("missing model: clear instruction", "ollama pull not-installed:1b" in str(e))
reader.OLLAMA_MODEL = "qwen3:8b"

# 4. through the HTTP API
from fastapi.testclient import TestClient
import app as A
c = TestClient(A.app)
r = c.post("/v1/extract", files={"file": ("u.pdf", utopia, "application/pdf")})
j = r.json()
check("API: unknown state via ollama", r.status_code == 200 and j["method"] == "ollama" and j["problems"] == [] and j["leg"]["steps"][0]["road_canonical"] == "US-12", f"{r.status_code} {j.get('method')}")
r = c.post("/v1/extract", files={"file": ("s.pdf", open(pdfs["SC"], "rb").read(), "application/pdf")})
check("API: known state via rules", r.status_code == 200 and r.json()["method"] == "rules" and r.json()["needs_review"])
reader.OLLAMA_URL = "http://127.0.0.1:9"
r = c.post("/v1/extract", files={"file": ("u.pdf", utopia, "application/pdf")})
check("API: unknown state, no Ollama -> 422 with instructions", r.status_code == 422 and "ollama" in r.json()["detail"].lower(), r.json()["detail"][:80])
print("\nALL PASS" if ok else "\nFAILURES"); sys.exit(0 if ok else 1)
