"""Permit Route server: the HTTP API used by the Permit Route app (../frontend: web, Android, iOS).
The app is hosted separately and only needs this server's address.

GET  /             -> {service, ok}  (shows the server is up)
POST /v1/login    {password} -> {token}            (only when APP_PASSWORD is set)
POST /v1/extract   multipart PDF  -> {leg, problems, needs_review}
POST /v1/route     {legs, start?, end?} -> {geojson, warnings, needs_review}
GET  /health       -> {ok, auth}

Run:  uvicorn app:app --host 0.0.0.0 --port 8000
Settings (environment variables):
  APP_PASSWORD   password for the web page and the apps; unset = no login (local use only!)
  APP_SECRET     signs login tokens; set a long random value on a public server
  WEB_ORIGINS    web addresses of the frontend allowed to call the API, comma-separated
                 (e.g. https://routes-app.example.com); unset = any address may call it.
                 Safe either way: the login is a token in a header, not a cookie, so the password protects the API.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import io
import secrets
import time
import traceback

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import extract_route as X
import geocode as G
import reader
import route_builder as RB

MAX_PDF_BYTES = 10 * 1024 * 1024
app = FastAPI(title="Permit route service")
# The web frontend is hosted at another address, so browsers need permission (CORS) to call
# this API.  The Android / iOS apps do not need it.
_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("WEB_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(CORSMiddleware,
                   allow_origins=_ORIGINS or ["*"],
                   # Flutter web while developing: flutter run -d chrome serves on localhost:<port>
                   allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?" if _ORIGINS else None,
                   allow_methods=["POST", "GET"], allow_headers=["*"])


@app.get("/")
def root():
    return {"service": "Permit Route server", "ok": True}

# ---- login ------------------------------------------------------------------------------
# One shared password (APP_PASSWORD).  A successful login returns a signed token that the
# page / app sends as "Authorization: Bearer <token>".  Tokens work across origins (the
# mobile apps), unlike cookies.
TOKEN_DAYS = 30


_FALLBACK_SECRET = secrets.token_hex(32)   # without APP_SECRET, a restart logs everyone out


def _secret():
    return (os.environ.get("APP_SECRET") or _FALLBACK_SECRET).encode()


def _sign(msg):
    return base64.urlsafe_b64encode(hmac.new(_secret(), msg.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token():
    exp = str(int(time.time()) + TOKEN_DAYS * 86400)
    return f"{exp}.{_sign(exp)}"


def token_ok(tok):
    try:
        exp, sig = tok.split(".", 1)
        return hmac.compare_digest(sig, _sign(exp)) and int(exp) > time.time()
    except (ValueError, AttributeError):
        return False


def require_login(authorization: str | None = Header(default=None)):
    if not os.environ.get("APP_PASSWORD"):
        return
    tok = (authorization or "").removeprefix("Bearer ").strip()
    if not token_ok(tok):
        raise HTTPException(401, "Please log in.")


class LoginRequest(BaseModel):
    password: str


@app.post("/v1/login")
def login(req: LoginRequest):
    pw = os.environ.get("APP_PASSWORD")
    if not pw:
        return {"token": ""}
    if not hmac.compare_digest(req.password.encode(), pw.encode()):
        time.sleep(1)      # slows down guessing
        raise HTTPException(401, "Wrong password.")
    return {"token": make_token(), "days": TOKEN_DAYS}

# swappable for tests
app.state.llm = None   # optional paid backend override (tests)
app.state.fetch = RB.fetch_leg_ways


class LatLon(BaseModel):
    lat: float
    lon: float


class OrderRequest(BaseModel):
    legs: list[dict]


class RouteRequest(BaseModel):
    legs: list[dict]          # route.schema.json objects, in travel order (driver-confirmed)
    start: LatLon | None = None
    end: LatLon | None = None


@app.post("/v1/order", dependencies=[Depends(require_login)])
def order(req: OrderRequest):
    ordered, confident = X.order_legs(req.legs)
    return {"legs": ordered, "confident": confident}


@app.get("/health")
def health():
    return {"ok": True, "auth": bool(os.environ.get("APP_PASSWORD"))}


@app.post("/v1/extract", dependencies=[Depends(require_login)])
def extract(file: UploadFile = File(...)):
    data = file.file.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "PDF larger than 10 MB")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "not a PDF")
    try:
        leg, text, method = reader.read_permit(data, llm=app.state.llm)
    except reader.NoBackend as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:      # e.g. scanned PDF without a text layer
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"Could not read this PDF: {type(e).__name__}: {str(e)[:200]}")
    problems, _info = X.validate(leg, text)
    leg = X.enrich(leg)
    return {"leg": leg, "problems": problems, "method": method, "needs_review": X.needs_review(leg, problems)}


@app.post("/v1/route", dependencies=[Depends(require_login)])
def route(req: RouteRequest):
    legs = req.legs
    if not legs:
        raise HTTPException(422, "no legs")
    gaps = X.chain_gaps(legs)

    notes = []

    def ll(p, leg, which, notes=notes):
        """Trip start = first permit's origin, trip end = last permit's destination.
        Order: point the user tapped > coordinates printed on the permit > state line > address search."""
        if p:
            return (p.lat, p.lon), None
        place = leg.get(which) or {}
        if place.get("lat") is not None:
            return (place["lat"], place["lon"]), None
        text, label = place.get("text") or "", "start" if which == "origin" else "end"
        nb = G.state_line(text, leg["state"])
        if nb:   # the permit road's closest point to the neighbouring state = where it crosses the line
            notes.append(f"Trip {label} placed where the permit road reaches the {nb} state line ('{text}'). Check it on the map.")
            return G.STATE_CENTER[nb], float("inf")
        hit, q = G.locate(text, leg["state"])
        if hit:
            notes.append(f"Trip {label} estimated from the permit address '{text}' (found '{q}'). Check it on the map.")
            return hit, 15000.0
        where = "where the first permit road meets the second" if which == "origin" else "just after joining the last permit road"
        notes.append(f"Could not find the permit address '{text}' on the map, so the route {label}s {where}. "
                     f"The first/last part of the trip may be missing.")
        return None, None

    # each state's map data is downloaded at most once per request, even if we build twice
    memo = {}

    def fetch(state, runs):
        key = (state, tuple(sorted({r.key for r in runs})))
        if key not in memo:
            try:
                memo[key] = (True, app.state.fetch(state, runs))
            except RB.MapDataError as e:
                memo[key] = (False, e)
        ok, val = memo[key]
        if not ok:
            raise val
        return val

    # never ask the user: trip start = first permit's origin, trip end = last permit's destination
    start, s_snap = ll(req.start, legs[0], "origin")
    end, e_snap = ll(req.end, legs[-1], "destination")
    try:
        gj, warnings = RB.build_trip(legs, start, end, fetch=fetch, start_snap_m=s_snap, end_snap_m=e_snap)
        return {"geojson": gj, "warnings": gaps + notes + warnings, "needs_review": bool(gaps + notes + warnings)}
    except (RB.NoPath, RB.MapDataError) as e:
        whole_error = e
    except Exception as e:  # noqa: BLE001
        whole_error = e
        _log_unexpected(e, req)
    if len(legs) == 1:
        _raise(whole_error, legs)

    # The whole trip could not be built in one go (one state's map data missing, or its roads
    # do not connect).  Draw every state that works on its own, and say which ones failed.
    features, warnings, failed, breaks = [], [], [], []
    for i, leg in enumerate(legs):
        st, leg_notes = leg["state"], []
        s, ss = ll(req.start if i == 0 else None, leg, "origin", leg_notes)
        e, es = ll(req.end if i == len(legs) - 1 else None, leg, "destination", leg_notes)
        try:
            g1, w1 = RB.build_trip([leg], s, e, fetch=fetch, start_snap_m=ss, end_snap_m=es)
        except Exception as ex:  # noqa: BLE001
            failed.append(st)
            if isinstance(ex, RB.NoPath):
                brk = route_break(ex, [leg], leg_index=i)
                breaks.append(brk)
                why = brk["message"]
            elif isinstance(ex, RB.MapDataError):
                why = str(ex).split(" Last server answer:")[0] + " The free map servers are busy; try again in a few minutes."
            else:
                why = str(ex)
            warnings.append(f"{st}: route NOT drawn: {why}")
            continue
        for f in g1["features"]:
            f["properties"]["state_leg"] = st
            features.append(f)
        warnings += [f"{st}: {w}" for w in leg_notes + w1]
    if not features:
        _raise(whole_error, legs)
    warnings.insert(0, f"Drawn state by state: {', '.join(failed)} could not be drawn (details below)."
                    if failed else "Drawn state by state (the states could not be joined into one route).")
    return {"geojson": {"type": "FeatureCollection", "features": features},
            "warnings": gaps + warnings, "needs_review": True, "failed_states": failed, "breaks": breaks}


def _step_refs(legs, refs):
    """[(leg, step), ...] -> 'TX step 12' / 'TX steps 12-13' / 'LA step 40, TX step 1' (1-based, as the app shows them)."""
    by_state = {}
    for li, si in refs:
        by_state.setdefault(legs[li]["state"], []).append(si + 1)
    parts = []
    for st, nums in by_state.items():
        nums = sorted(set(nums))
        if len(nums) == 1:
            parts.append(f"{st} step {nums[0]}")
        elif nums == list(range(nums[0], nums[-1] + 1)):
            parts.append(f"{st} steps {nums[0]}-{nums[-1]}")
        else:
            parts.append(f"{st} steps {', '.join(map(str, nums))}")
    return ", ".join(parts)


def route_break(e, legs, leg_index=None):
    """Where a route broke, in the permit's own terms: the steps involved and the spot on the map.
    leg_index: legs is a single permit drawn on its own; its steps are that leg of the whole trip."""
    runs = RB.build_runs(legs)
    msg, roads = str(e), []
    for n in e.roads:
        if not 1 <= n <= len(runs):
            continue
        r = runs[n - 1]
        roads.append({"road": r.raw,
                      "steps": [{"leg": li if leg_index is None else leg_index, "step": si} for li, si in r.steps]})
        # 'road #24' is the server's own count; the user sees the permit's step numbers
        msg = msg.replace(f"road #{n} '{r.raw}'", f"'{r.raw}' ({_step_refs(legs, r.steps)})")
    near = {"lat": e.near[0], "lon": e.near[1]} if e.near else None
    return {"message": msg, "roads": roads, "near": near}


_log = logging.getLogger("uvicorn.error")
FAILED_ROUTE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_failed_route.json")


def _log_unexpected(e, req):
    """A crash (not a 'no path' / 'map data' answer): print the traceback and keep the request,
    so the failure can be re-run with:  python -c "import app; app.replay()"."""
    _log.error("route building crashed:\n%s", "".join(traceback.format_exception(e)))
    try:
        with open(FAILED_ROUTE_FILE, "w", encoding="utf-8") as f:
            json.dump(req.model_dump(), f)
    except OSError:
        pass


def replay(path=FAILED_ROUTE_FILE):
    """Re-run the last crashed route request in this process (for debugging)."""
    with open(path, encoding="utf-8") as f:
        return route(RouteRequest(**json.load(f)))


def _raise(e, legs):
    if isinstance(e, RB.NoPath):
        raise HTTPException(409, {**route_break(e, legs), "road": e.road})
    if isinstance(e, RB.MapDataError):
        raise HTTPException(502, {"message": str(e), "hint": "The free public map servers are often busy. Try again in a few minutes, or try one permit alone."})
    raise HTTPException(500, {"message": f"Route building failed: {type(e).__name__}: {str(e)[:300]}"})
