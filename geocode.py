"""Turn a permit's printed origin/destination text into a rough lat/lon.

Uses OpenStreetMap Nominatim (free; max 1 request per second, results cached).
The point is only a hint: route_builder snaps it to the nearest part of the
permit's first / last road, so a town centre is good enough.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from extract_route import ROAD_RE, STATES

URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
CACHE = os.path.join(os.environ.get("OVERPASS_CACHE_DIR",
                                    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".overpass_cache")),
                     "geocode.json")
_lock = threading.Lock()
_last = [0.0]

# words that describe a spot on a road rather than a place
_NOISE = re.compile(r"\b(STATE\s+LINE|LINE|STATELINE|BORDER|JCT|JUNCTION|EXIT|MM|MILE\s*MARKER|"
                    r"\d+(\.\d+)?\s*MI(LES?)?|[NSEW]B?|NORTH|SOUTH|EAST|WEST|BOUND|AT|OF)\b", re.I)
_STATE_SET = set(STATES)


# rough geographic centres of the lower 48 states (lat, lon)
STATE_CENTER = {
    "AL": (32.8, -86.8), "AZ": (34.3, -111.7), "AR": (34.9, -92.4), "CA": (37.2, -119.5), "CO": (39.0, -105.5),
    "CT": (41.6, -72.7), "DE": (39.0, -75.5), "FL": (28.6, -82.4), "GA": (32.7, -83.4), "ID": (44.4, -114.6),
    "IL": (40.0, -89.2), "IN": (39.9, -86.3), "IA": (42.1, -93.5), "KS": (38.5, -98.4), "KY": (37.5, -85.3),
    "LA": (31.1, -92.0), "ME": (45.4, -69.2), "MD": (39.0, -76.8), "MA": (42.3, -71.8), "MI": (44.3, -85.4),
    "MN": (46.3, -94.3), "MS": (32.7, -89.7), "MO": (38.4, -92.5), "MT": (47.0, -109.6), "NE": (41.5, -99.8),
    "NV": (39.3, -116.6), "NH": (43.7, -71.6), "NJ": (40.2, -74.7), "NM": (34.4, -106.1), "NY": (42.9, -75.5),
    "NC": (35.6, -79.4), "ND": (47.5, -100.5), "OH": (40.3, -82.8), "OK": (35.6, -97.5), "OR": (43.9, -120.6),
    "PA": (40.9, -77.8), "RI": (41.7, -71.5), "SC": (33.9, -80.9), "SD": (44.4, -100.2), "TN": (35.9, -86.4),
    "TX": (31.5, -99.3), "UT": (39.3, -111.7), "VT": (44.1, -72.7), "VA": (37.5, -78.9), "WA": (47.4, -120.5),
    "WV": (38.6, -80.6), "WI": (44.6, -89.9), "WY": (43.0, -107.6),
}
_LINE = re.compile(r"\b([A-Z]{2})\b[^A-Z0-9]*(STATE\s*)?LINE\b", re.I)


def state_line(text, own_state):
    """'MS STATE LINE', 'I-20/US-80 LA Line' -> 'MS' / 'LA' (the neighbouring state), else None."""
    for m in _LINE.finditer(text or ""):
        st = m.group(1).upper()
        if st in STATE_CENTER and st != own_state:
            return st
    # 'I-20;AL;Waco' / 'NM-136;TX;Santa Teresa': road; neighbouring state; border town
    for part in re.split(r"[;,]", text or ""):
        st = part.strip().upper()
        if st in STATE_CENTER and st != own_state and ";" in text:
            return st
    return None


def candidates(text, state):
    """Search strings to try, most specific first."""
    text = re.sub(r"\[[^\]]*\]", " ", text).strip()     # printed [lat,lon] is handled elsewhere
    t = text.replace("@", ";")
    parts = [p.strip() for p in re.split(r"[;,/]", t) if p.strip()]
    places = []
    for p in parts:
        q = ROAD_RE.sub(" ", p)
        q = _NOISE.sub(" ", q)
        q = re.sub(r"[^A-Za-z .'-]", " ", q)
        q = re.sub(r"\s+", " ", q).strip(" .-")
        if len(q) < 3 or q.upper() in _STATE_SET:
            continue
        places.append(q)
    out = []
    if len(parts) > 1 and not ROAD_RE.search(text):
        out.append(f"{text}, {state}, USA")             # e.g. a street address
    for q in places:
        out.append(f"{q}, {state}, USA")
    seen, uniq = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower()); uniq.append(q)
    return uniq


def _load():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(c):
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        with open(CACHE, "w", encoding="utf-8") as f:
            json.dump(c, f)
    except OSError:
        pass


def _query(q):
    with _lock:                                   # Nominatim policy: at most 1 request per second
        wait = 1.1 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        url = URL + "?" + urllib.parse.urlencode({"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "us"})
        req = urllib.request.Request(url, headers={"User-Agent": "permit-route/0.1 (oversize permit route viewer)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.loads(r.read())
        finally:
            _last[0] = time.time()
    return (float(res[0]["lat"]), float(res[0]["lon"])) if res else None


def locate(text, state):
    """Return ((lat, lon), query_used) or (None, None)."""
    if not text:
        return None, None
    cache = _load()
    for q in candidates(text, state):
        if q in cache:
            hit = cache[q]
        else:
            try:
                hit = _query(q)
            except Exception:  # noqa: BLE001  network trouble: fall back to asking the user
                return None, None
            cache[q] = hit
            _save(cache)
        if hit:
            return tuple(hit), q
    return None, None
