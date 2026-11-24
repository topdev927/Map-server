"""Turn permit steps into a line on real roads.

Idea: the permit names the roads and their ORDER.  Instead of asking a router for
"the best way" (which may leave the permitted roads), search only through road
segments that match the permit's roads, in the permit's order:

    state = (map node, index of the permit road we are currently on)

  * you may move along a segment only if it belongs to the CURRENT permit road
    (or is a ramp/link, which has no name of its own);
  * you may advance to the NEXT permit road only at a node that lies on it.

So a detour such as I-20 -> I-220 -> I-20 is followed exactly, and the result can
never use a road the permit doesn't list.  No path found = the permit and the map
disagree, which is reported instead of guessed.

Map data comes from OpenStreetMap through Overpass (fetch_leg_ways) or any other
source that yields the same JSON.  fetch_leg_ways has NOT been run against the live
service in the authoring environment; solve() is tested on a synthetic network.
"""
import functools
import hashlib
import heapq
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from extract_route import canon, ROAD_RE

STATE_CODE = lambda st: f"US-{st}"


def norm(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def haversine_m(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


# ----------------------------------------------------------------- runs
class Run:
    def __init__(self, key, raw, leg, state):
        self.key, self.raw = key, raw
        self.legs, self.states = {leg}, {state}
        self.names, self.miles, self.flags = set(), 0.0, set()
        self.state_miles = {}     # permit miles per state (a road such as I-20 often runs on across a state line)
        self.steps = []           # (leg index, step index) of the permit steps this road was built from

    def label(self):
        return self.raw


_ABBR = {"ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE", "RD": "ROAD", "BLVD": "BOULEVARD", "DR": "DRIVE",
         "HWY": "HIGHWAY", "PKWY": "PARKWAY", "LN": "LANE", "CT": "COURT", "PL": "PLACE", "EXT": "EXTENSION",
         "EXPY": "EXPRESSWAY", "FWY": "FREEWAY", "CIR": "CIRCLE", "TRL": "TRAIL", "TER": "TERRACE", "SQ": "SQUARE",
         "BYP": "BYPASS", "MT": "MOUNT", "FT": "FORT", "SPGS": "SPRINGS", "CTR": "CENTER", "XING": "CROSSING"}
_DIRS = {"N": "NORTH", "S": "SOUTH", "E": "EAST", "W": "WEST", "NE": "NORTHEAST", "NW": "NORTHWEST",
         "SE": "SOUTHEAST", "SW": "SOUTHWEST"}
_DIR_WORDS = set(_DIRS) | set(_DIRS.values())
# bracketed text that is not a street name
_NOT_A_NAME = re.compile(r"^(STATE (ROAD|RD)|RAMP|EXIT|INTERSTATE|ROUTE|US HWY|U S|CONTACT|NOT |LOADED|DESTINATION|"
                         r"TRUCK|BUSINESS|BYPASS|FRONTAGE|IH ?HIGHWAY|EMPTY|ORIGIN)", re.I)
_FRONTAGE_CODE = re.compile(r"[NSEW]FR$", re.I)     # 'CRAIG W FR' style frontage-road codes


TRUNCATED_NAME_LEN = 25   # permits cut long names ('AMERICAN LEGION MEMORIAL HIGHW'); match those by prefix


def name_tokens(s):
    """'N 12TH ST EXT' and 'North 12th Street Extension' -> ('12TH', 'STREET', 'EXTENSION')."""
    toks = [_ABBR.get(t, t) for t in re.findall(r"\d+(?:ST|ND|RD|TH)\b|\d+|[A-Z]+", s.upper())]
    while len(toks) > 1 and toks[0] in _DIR_WORDS:      # directional prefix / suffix is often dropped
        toks.pop(0)
    while len(toks) > 1 and toks[-1] in _DIR_WORDS:
        toks.pop()
    return tuple(toks)


@functools.lru_cache(maxsize=200_000)    # called for every map road against every permit road
def name_key(s):
    return " ".join(name_tokens(s))


def _names(text):
    found = re.findall(r"[\[(]([^\])]+)[\])]", text)
    return {name_key(x) for x in found
            if not ROAD_RE.fullmatch(x.strip()) and not _NOT_A_NAME.match(x.strip())
            and not _FRONTAGE_CODE.search(norm(x)) and not ROAD_RE.search(x) and len(norm(x)) > 3}


def _name_regex(key):
    """Overpass regex (used with ,i) matching any OSM spelling of a normalised name."""
    inv = {}
    for a, full in _ABBR.items():
        inv.setdefault(full, {full}).add(a)
    alts = []
    for t in key.split():
        # fixed order (longest first, then alphabetical): the query text is the cache key
        opts = sorted(inv.get(t, {t}), key=lambda s: (-len(s), s))
        alts.append(opts[0] if len(opts) == 1 else "(" + "|".join(opts) + ")")
    d = "(" + "|".join(sorted(_DIR_WORDS, key=lambda s: (-len(s), s))) + ")"
    if len(key) >= TRUNCATED_NAME_LEN:     # permit printed a cut-off name: match its beginning only
        return f"^({d}[^a-z0-9]+)?" + "[^a-z0-9]*".join(alts)
    return f"^({d}[^a-z0-9]+)?" + "[^a-z0-9]*".join(alts) + f"([^a-z0-9]+{d})?$"


_SROAD = re.compile(r"\b(?:STATE\s+(?:ROAD|RD)\s+)?([A-Z])-?(\d{1,3})-(\d{1,5})\b", re.I)


def raw_key(s):
    """Key for designations canon() does not know, e.g. SC secondary 'S-32-280' / 'State Road S-32-280'."""
    m = _SROAD.search(s)
    if m:
        return f"RAW{m.group(1).upper()}{int(m.group(2))}-{int(m.group(3))}"
    return "RAW" + norm(s)


def build_runs(legs):
    """Collapse the permit steps into the ordered list of roads to follow."""
    runs = []
    for li, leg in enumerate(legs):
        for si, s in enumerate(leg["steps"]):
            road = s.get("road")
            if not road:
                continue  # ramp / exit line: handled by link segments
            key, _ = canon(road, leg["state"])
            key = key or raw_key(road)
            if runs and runs[-1].key == key:
                r = runs[-1]
                r.legs.add(li)
                r.states.add(leg["state"])
            else:
                r = Run(key, road, li, leg["state"])
                runs.append(r)
            r.steps.append((li, si))
            r.miles += s.get("miles") or 0.0
            r.state_miles[leg["state"]] = r.state_miles.get(leg["state"], 0.0) + (s.get("miles") or 0.0)
            r.names |= _names(s.get("text") or "")
            if s.get("kind") == "not_permitted":
                r.flags.add("not_permitted")
    return runs


# OSM writes Texas loops and spurs number first: 'TX 375 Loop', 'TX 16 Spur' (permits: 'Loop 375', 'SS 16').
# canon() would read 'TX 375 Loop' as state highway TX-375, a different road.
_SUFFIX_REF = re.compile(r"(?:[A-Z]{2}\s+)?0*(\d+)\s+(LOOP|SPUR)", re.I)


@functools.lru_cache(maxsize=200_000)
def ref_key(ref):
    """Road key of one map 'ref' value."""
    m = _SUFFIX_REF.fullmatch(ref.strip())
    if m:
        return f"{m.group(2).upper()}{int(m.group(1))}"
    k, _ = canon(ref)
    return k or raw_key(ref)


def way_matches(run, tags, way_legs):
    if not (run.legs & way_legs):
        return False
    for ref in re.split(r"[;,]", tags.get("ref", "")):
        ref = ref.strip()
        if ref and ref_key(ref) == run.key:
            return True
    for t in ("name", "alt_name", "old_name"):
        nm = tags.get(t)
        if nm:
            k = name_key(nm)
            if k in run.names or any(len(n) >= TRUNCATED_NAME_LEN and k.startswith(n) for n in run.names):
                return True
    return False


# ---------------------------------------------------------------- graph
class Graph:
    def __init__(self):
        self.coord, self.adj, self.ways = {}, {}, {}

    def add_overpass(self, data, leg_index):
        for el in data.get("elements", []):
            if el.get("type") != "way" or "geometry" not in el:
                continue
            wid, tags = el["id"], el.get("tags", {})
            if wid in self.ways:
                self.ways[wid]["legs"].add(leg_index)
                continue
            nodes, geom = el["nodes"], el["geometry"]
            pts = [(g["lat"], g["lon"]) for g in geom]
            hw = tags.get("highway", "")
            one = tags.get("oneway", "")
            fwd = bwd = True
            if one in ("yes", "true", "1") or (not one and (hw in ("motorway", "motorway_link") or tags.get("junction") == "roundabout")):
                bwd = False
            elif one == "-1":
                fwd = False
            self.ways[wid] = {"tags": tags, "legs": {leg_index}, "link": hw.endswith("_link"), "nodes": nodes}
            for i in range(len(nodes) - 1):
                a, b = nodes[i], nodes[i + 1]
                self.coord[a], self.coord[b] = pts[i], pts[i + 1]
                d = haversine_m(pts[i], pts[i + 1])
                if fwd:
                    self.adj.setdefault(a, []).append((b, d, wid))
                if bwd:
                    self.adj.setdefault(b, []).append((a, d, wid))


class NoPath(Exception):
    def __init__(self, msg, road=None, roads=None, near=None):
        super().__init__(msg)
        self.road = road          # 1-based permit road number where the route breaks, if known
        # every road involved (1-based), e.g. both sides of a missing junction; lets the app point at the permit steps
        self.roads = roads or ([road] if road else [])
        self.near = near          # (lat, lon) of the break on the map, if known


def _closest_pair(g, a_nodes, b_nodes, cap=4000):
    """Closest pair of nodes between two roads (sampled for big roads); returns (metres, a, b)."""
    a = list(a_nodes)[:: max(1, len(a_nodes) // cap)]
    b = list(b_nodes)[:: max(1, len(b_nodes) // cap)]
    cell = 0.01   # ~1 km grid
    grid = {}
    for y in b:
        la, lo = g.coord[y]
        grid.setdefault((int(la / cell), int(lo / cell)), []).append(y)
    best = (float("inf"), None, None)
    for x in a:
        la, lo = g.coord[x]
        ci, cj = int(la / cell), int(lo / cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for y in grid.get((ci + di, cj + dj), ()):
                    d = haversine_m(g.coord[x], g.coord[y])
                    if d < best[0]:
                        best = (d, x, y)
    return best


def _nearest(g, nodes, ll, limit_m, spread_m=200):
    """Nodes of the road nearest the point.  Everything within spread_m of the closest one is
    returned, so on a divided highway both carriageways are candidates and the search picks
    the one that runs the right way."""
    dist = {n: haversine_m(g.coord[n], ll) for n in nodes}
    best = min(dist.values(), default=None)
    if best is None or best > limit_m:
        raise NoPath(f"no permitted road within {limit_m/1000:.0f} km of {ll[0]:.5f},{ll[1]:.5f}", near=ll)
    return {n for n, d in dist.items() if d <= best + spread_m}


def min_lengths(runs):
    """Least distance (m) to drive on each permit road before leaving it: half the permit's
    miles minus a mile.  Stops shortcuts such as leaving a ring road at the first junction
    with the next road, while tolerating rounded or slightly wrong permit miles."""
    return [max(0.0, (0.5 * r.miles - 1.0) * 1609.344) if r.miles else 0.0 for r in runs]


# Between two permit roads the route may use this much of a road the permit does not name
# (frontage roads, short pieces of cross street).  The permit often folds such a piece into
# the next road's line ("Take IH10 Ramp", 0.05 mi), and without it a junction can be impossible:
# the search then wanders off the wrong way and the break is reported somewhere else entirely.
HOP_MAX_M = 400
HOP_STEP_M = 10        # hop metres are counted in these steps (part of the search state)
HOP_COST = 3.0         # a hop costs triple, so permit roads and ramps are always preferred
HOP_SPENT = 2 + HOP_MAX_M // HOP_STEP_M   # 'moved' value once a hop has ended: no hop metres left


def _search(g, K, masks, nodes_by_run, starts, start_cost, ends, extra=None, min_len=None):
    """Shortest drive through permit roads 0..K-1 in order.

    state = (node, road index, driven_on_this_road, previous node).
    A road counts as followed only if at least one of its segments was driven (touching it at a
    junction is not enough), U-turns are not allowed (otherwise "drive onto it and straight back"
    would satisfy it), and the route may leave road k only after min_len[k] metres on it.

    Each queue entry also carries the metres driven since joining the current road, capped at
    min_len[k].  A label is dropped when an earlier one for the same state had driven at least as
    far (it got there sooner and is no less free to leave).  Keeping the join node in the state
    instead multiplies the states by the number of possible join points and runs out of memory
    on long multi-state trips.
    Returns (list of states from start to goal, or None;  furthest road index driven on)."""
    min_len = min_len or [0.0] * K
    labels = []      # i -> (state, parent label index or -1)
    done = {}        # state -> most metres-on-road of a label already expanded there
    full = {}        # state -> least distance of a queued label that has driven min_len already
    pq = []

    def push(d, st, on_road, parent):
        cap = min(on_road, min_len[st[1]])
        if done.get(st, -1.0) >= cap or d >= full.get(st, 1e18):
            return
        if cap >= min_len[st[1]]:
            full[st] = d
        labels.append((st, parent))
        heapq.heappush(pq, (d, len(labels) - 1, cap))

    for s in starts:
        c = start_cost(s)
        push(c, (s, 0, 0, None), c, -1)
    furthest = 0
    while pq:
        d, i, on_road = heapq.heappop(pq)
        st = labels[i][0]
        if done.get(st, -1.0) >= on_road:
            continue
        done[st] = on_road
        n, k, moved, p = st
        if moved and k > furthest:
            furthest = k
        long_enough = on_road >= min_len[k]
        if n in ends and k == K - 1 and moved and long_enough:
            chain = []
            while i >= 0:
                chain.append(labels[i][0])
                i = labels[i][1]
            chain.reverse()
            return chain, furthest
        nxt = []
        if moved and long_enough and k + 1 < K and n in nodes_by_run[k + 1]:
            nxt.append(((n, k + 1, 0, p), 0.0))
        # leaving road k for road k+1: a few hundred metres of a road the permit does not name
        # (e.g. 'turn right onto the IH10 frontage road, take the IH10 ramp') may be driven
        hop_ok = moved and long_enough and k + 1 < K
        for m, length, wid in g.adj.get(n, ()):
            if m == p:
                continue
            on_road = k in masks[wid]
            if on_road or g.ways[wid]["link"]:
                # moved > 1 means some hop metres are used up.  Back on the permit road the hop
                # is over: mark it as fully spent (one value, not the exact metres, which would
                # split the rest of the road into up to HOP_MAX_M / HOP_STEP_M copies of the search)
                nm = HOP_SPENT if on_road and moved > 1 else (max(moved, 1) if on_road else moved)
                nxt.append(((m, k, nm, n), length))
            elif hop_ok:
                used = moved - 1 + math.ceil(length / HOP_STEP_M)
                if used * HOP_STEP_M <= HOP_MAX_M:
                    nxt.append(((m, k, 1 + used, n), length * HOP_COST))
        if extra and k in extra:          # median crossings on a turnaround cross street
            for m, length in extra[k].get(n, ()):
                if m != p:
                    nxt.append(((m, k, 1, n), length))
        for ns, c in nxt:
            # joining the next road restarts its metre count
            push(d + c, ns, on_road + c if ns[1] == k else 0.0, i)
    return None, furthest


def _along(g, masks, k, src, limit_m):
    """Driving distance from node src along road k (and ramps), up to limit_m."""
    dist, pq = {src: 0.0}, [(0.0, src)]
    while pq:
        d, n = heapq.heappop(pq)
        if d > dist[n]:
            continue
        for m, length, wid in g.adj.get(n, ()):
            if k in masks[wid] or g.ways[wid]["link"]:
                nd = d + length
                if nd <= limit_m and nd < dist.get(m, 1e18):
                    dist[m] = nd
                    heapq.heappush(pq, (nd, m))
    return dist


def _within(g, nodes, centres, radius_m):
    """True if any of nodes lies within radius_m of any centre node."""
    cell = 0.01
    grid = {}
    for c in centres:
        la, lo = g.coord[c]
        grid.setdefault((int(la / cell), int(lo / cell)), []).append(g.coord[c])
    for n in nodes:
        la, lo = g.coord[n]
        ci, cj = int(la / cell), int(lo / cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for c in grid.get((ci + di, cj + dj), ()):
                    if haversine_m(g.coord[n], c) <= radius_m:
                        return True
    return False


MEDIAN_M = 30          # widest median a turnaround may cross where the map has no crossover drawn
MEDIAN_COST = 3.0      # crossing costs triple, so a mapped connection is always preferred


def _median_crossings(g, wids):
    """Virtual links between the two halves of a divided cross street (nodes of different
    ways within MEDIAN_M of each other).  OSM often draws the ramp ending on one half only,
    which would make the permit's left turn / turnaround impossible."""
    owner, cell = {}, 0.0005   # ~50 m grid
    for wid in wids:
        for n in g.ways[wid]["nodes"]:
            owner.setdefault(n, set()).add(wid)
    grid = {}
    for n in owner:
        la, lo = g.coord[n]
        grid.setdefault((int(la / cell), int(lo / cell)), []).append(n)
    out = {}
    for n in owner:
        la, lo = g.coord[n]
        ci, cj = int(la / cell), int(lo / cell)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for m in grid.get((ci + di, cj + dj), ()):
                    if m != n and not (owner[n] & owner[m]):
                        d = haversine_m(g.coord[n], g.coord[m])
                        if d <= MEDIAN_M:
                            out.setdefault(n, []).append((m, d * MEDIAN_COST + 5))
    return out


# A permit road this short is usually the cross street of a turnaround or an interchange
# ("exit, cross GA-260, re-enter I-285 the other way").  The map often lacks its route
# number there, so an interchange cross street may stand in for it (reported as a warning),
# but only around the spot the permit's miles on the previous road point to.
CONNECTOR_MAX_MILES = 0.5


def solve(g, runs, start_ll, end_ll, snap_m=3000, start_snap_m=None, end_snap_m=None):
    K = len(runs)
    masks, nodes_by_run = {}, [set() for _ in range(K)]
    # same result as way_matches() for every (road, way) pair, but each way is looked up by its
    # numbers and names instead of being compared with every permit road
    by_key, by_name, cut_names = {}, {}, []
    for k, r in enumerate(runs):
        by_key.setdefault(r.key, []).append(k)
        for n in r.names:
            by_name.setdefault(n, []).append(k)
            if len(n) >= TRUNCATED_NAME_LEN:
                cut_names.append((n, k))
    for wid, w in g.ways.items():
        refs, names = _way_keys(w["tags"])
        cand = set()
        for rf in refs:
            cand.update(by_key.get(rf, ()))
        for nm in names:
            cand.update(by_name.get(nm, ()))
            cand.update(k for n, k in cut_names if nm.startswith(n))
        m = {k for k in cand if runs[k].legs & w["legs"]}
        masks[wid] = m
        for k in m:
            nodes_by_run[k].update(w["nodes"])
    real_nodes = [set(s) for s in nodes_by_run]
    short = [k for k in range(1, K - 1) if 0 < runs[k].miles <= CONNECTOR_MAX_MILES]
    for k in range(K):
        if not nodes_by_run[k] and k not in short:
            raise NoPath(f"road #{k+1} '{runs[k].raw}' not found in the map data", road=k + 1)
    # start_ll / end_ll may be None when the permit's address could not be located: the route
    # then begins anywhere on the first road (in practice where it meets the second road) and
    # ends as soon as it has driven onto the last road.
    starts = _nearest(g, nodes_by_run[0], start_ll, start_snap_m or snap_m) if start_ll else nodes_by_run[0]
    ends = _nearest(g, nodes_by_run[K - 1], end_ll, end_snap_m or snap_m) if end_ll else nodes_by_run[K - 1]
    # several start nodes (see _nearest): each begins at a cost equal to its distance from the point
    if start_ll and start_snap_m is None:
        def start_cost(s):
            return haversine_m(g.coord[s], start_ll)
    else:
        def start_cost(s):
            return 0.0

    cross = [wid for wid, w in g.ways.items() if not masks[wid] and not w["link"]]
    extra = {}
    min_len = min_lengths(runs)
    for k in short:
        cands = [wid for wid in cross if g.ways[wid]["legs"] & runs[k].legs]
        allowed = cands
        if runs[k - 1].miles > 0:
            # where did the route join the previous road, and how far along it should it leave it?
            chain, _ = _search(g, k, masks, nodes_by_run, starts, start_cost, real_nodes[k - 1],
                               extra, min_len[:k - 1] + [0.0])
            if chain:
                entry = next(st[0] for st in chain if st[1] == k - 1)
                want = runs[k - 1].miles * 1609.344
                lo, hi = max(0.0, 0.6 * want - 500), 1.4 * want + 800
                along = _along(g, masks, k - 1, entry, hi)
                window = [n for n, d in along.items() if d >= lo and n in real_nodes[k - 1]]
                if window:
                    allowed = [wid for wid in cands if _within(g, g.ways[wid]["nodes"], window, 600)]
        for wid in allowed:
            masks[wid] = masks[wid] | {k}
            nodes_by_run[k].update(g.ways[wid]["nodes"])
        if not nodes_by_run[k]:
            raise NoPath(f"road #{k+1} '{runs[k].raw}' not found in the map data", road=k + 1)
        extra[k] = _median_crossings(g, [wid for wid, m in masks.items() if k in m and not g.ways[wid]["link"]])

    chain, furthest = _search(g, K, masks, nodes_by_run, starts, start_cost, ends, extra, min_len)
    if chain is None and any(min_len):
        # the permit's miles may simply be wrong: try again without the minimum lengths
        # (to_geojson then reports the length mismatch)
        chain, furthest = _search(g, K, masks, nodes_by_run, starts, start_cost, ends, extra)
    if chain is None:
        k = furthest
        if k + 1 < K:
            gap, a, b = _closest_pair(g, nodes_by_run[k], nodes_by_run[k + 1])
            where = (f" They come within {gap:.0f} m of each other near "
                     f"{g.coord[a][0]:.5f},{g.coord[a][1]:.5f}, but the map has no connecting road or ramp there."
                     if a is not None else " They are more than 1 km apart in the map data.")
            raise NoPath(f"Could not get from road #{k+1} '{runs[k].raw}' onto road #{k+2} '{runs[k+1].raw}'."
                         + where, road=k + 2, roads=[k + 1, k + 2], near=g.coord[a] if a is not None else None)
        where = f" {end_ll[0]:.5f},{end_ll[1]:.5f}" if end_ll else ""
        raise NoPath(f"Reached the last road '{runs[-1].raw}' but could not follow it to the end point"
                     f"{where} (one-way or a break in the map data).", road=K, near=end_ll)

    per_run = [{"coords": [], "metres": 0.0} for _ in range(K)]
    per_run[0]["coords"].append(g.coord[chain[0][0]])
    on_real = [False] * K
    for a_, b_ in zip(chain, chain[1:]):
        k = b_[1]
        per_run[k]["coords"].append(g.coord[b_[0]])
        if a_[1] == b_[1]:
            per_run[k]["metres"] += haversine_m(g.coord[a_[0]], g.coord[b_[0]])
            if max(a_[2], 1) < b_[2] < HOP_SPENT:    # a hop piece (see HOP_MAX_M)
                hop = per_run[k].setdefault("hop", {"metres": 0.0, "at": g.coord[a_[0]], "names": []})
                hop["metres"] += haversine_m(g.coord[a_[0]], g.coord[b_[0]])
                wid = next((w for m, _, w in g.adj.get(a_[0], ()) if m == b_[0]), None)
                t = g.ways[wid]["tags"] if wid is not None else {}
                nm = t.get("name") or t.get("ref") or t.get("highway") or "unnamed road"
                if nm not in hop["names"]:
                    hop["names"].append(nm)
            if a_[0] in real_nodes[k] and b_[0] in real_nodes[k]:
                on_real[k] = True
    for k in short:
        if not on_real[k]:
            per_run[k]["substitute"] = True
    return per_run


def to_geojson(runs, per_run, tolerance=0.25, min_slack_miles=2.0):
    feats, warnings = [], []
    for k, (r, p) in enumerate(zip(runs, per_run)):
        miles = p["metres"] / 1609.344
        props = {"seq": k + 1, "road": r.raw, "states": sorted(r.states),
                 "map_miles": round(miles, 2), "permit_miles": round(r.miles, 2) if r.miles else None}
        if p.get("substitute"):
            props["substitute"] = True
            warnings.append(f"road #{k+1} {r.raw}: not labelled with that number in the map here; "
                            f"drawn on the cross street at the interchange. Check it on the map.")
        if p.get("hop") and k + 1 < len(runs):
            h = p["hop"]
            props["hop_m"] = round(h["metres"])
            warnings.append(f"road #{k+1} {r.raw} -> road #{k+2} {runs[k+1].raw}: joined via {h['metres']:.0f} m of "
                            f"'{', '.join(h['names'])}' (not named in the permit) near {h['at'][0]:.5f},{h['at'][1]:.5f}. "
                            f"Check it on the map.")
        if r.flags:
            props["flags"] = sorted(r.flags)
            warnings.append(f"road #{k+1} {r.raw}: permit marks it as not authorised / needs local approval")
        if r.miles and abs(miles - r.miles) > max(min_slack_miles, tolerance * r.miles):
            warnings.append(f"road #{k+1} {r.raw}: map length {miles:.1f} mi vs permit {r.miles:.1f} mi")
            props["length_mismatch"] = True
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "LineString", "coordinates": [[lo, la] for la, lo in p["coords"]]}})
    return {"type": "FeatureCollection", "features": feats}, warnings


# --------------------------------------------------- Overpass
# how OSM writes each road type in the 'ref' tag (case-insensitive); keys are canon() prefixes
_REF_PREFIX = {
    "I": "I|IH|Interstate",
    "US": "US|US Hwy|US Highway|U.S.",
    "ST": "{st}|SH|SR|State Highway|State Route|State Road|Highway|Hwy",
    "LOOP": "Loop|SL|LP",
    "SPUR": "Spur|SS",
    "CR": "CR|County Road|Co Rd",
    "FM": "FM|Farm to Market Road|Farm-to-Market Road",
    "RM": "RM|Ranch to Market Road|Ranch-to-Market Road",
    "BS": "BS|Business",
}


def overpass_query(state, runs_in_leg, bbox=None, road_only=False):
    """bbox=(s, w, n, e): only this tile of the state (long roads are fetched tile by tile).
    road_only: just the permit roads themselves, no ramps / cross streets (small and quick;
    used to find out which tiles a long road passes through)."""
    sroads = sorted({m.groups() for r in runs_in_leg if (m := re.fullmatch(r"RAW([A-Z])(\d+)-(\d+)", r.key))})
    # number + road type ("SH 6", not every 'CR 6' / 'FM 6' in the state)
    fams = {}
    for r in runs_in_leg:
        m = re.fullmatch(r"([A-Z]+?)(\d+)", r.key)
        if m and not r.key.startswith("RAW"):
            fams.setdefault(m.group(1), set()).add(m.group(2))
    names = sorted({n for r in runs_in_leg for n in r.names})
    parts = []
    for fam in sorted(fams):
        pre = _REF_PREFIX.get(fam, fam).format(st=state)
        nums = "|".join(sorted(fams[fam], key=int))
        parts.append(f'way(area.a)["highway"]["ref"~"(^|[^a-z0-9])({pre})[ -]?0*({nums})([^0-9]|$)",i];')
        if fam in ("LOOP", "SPUR"):      # map writes 'TX 375 Loop' (see ref_key)
            word = fam.capitalize()
            parts.append(f'way(area.a)["highway"]["ref"~"(^|[^a-z0-9])0*({nums}) {word}([^a-z]|$)",i];')
    for p, c, n in sroads:           # e.g. SC secondary roads, tagged 'S-32-280' or 'State Road S-32-280'
        parts.append(f'way(area.a)["highway"]["ref"~"(^|[^a-z0-9]){p}-?0*{c}-0*{n}([^0-9]|$)",i];')
    for n in names:
        parts.append(f'way(area.a)["highway"]["name"~"{_name_regex(n)}",i];')
    body = "\n  ".join(parts) or "way(area.a)[\"highway\"][\"ref\"];"
    head = "[out:json][timeout:180]"
    if bbox:
        head += "[bbox:%s]" % ",".join(f"{v:.3f}" for v in bbox)
    sel = (f'{head};\narea["ISO3166-2"="{STATE_CODE(state)}"]["admin_level"="4"]->.a;\n'
           f'(\n  {body}\n)->.sel;\n')
    if road_only:
        return sel + ".sel out geom;"
    # The query text is also the cache key: changing it makes every saved download unusable.
    # Ramps longer than 3 pieces are completed afterwards by _complete_ramps (small queries).
    return (sel + 'node(w.sel)->.n;\nway(bn.n)["highway"~"_link$"]->.l1;\n'
            # ramps are often chains of several link ways: follow them a few hops out
            f'node(w.l1)->.n1;\nway(bn.n1)["highway"~"_link$"]->.l2;\n'
            f'node(w.l2)->.n2;\nway(bn.n2)["highway"~"_link$"]->.l3;\n(.l1;.l2;.l3;)->.links;\n'
            # cross streets at interchanges: short permit hops (turnarounds) may use them, see CONNECTOR_MAX_MILES
            f'node(w.links)->.ln;\nway(bn.ln)["highway"~"^(trunk|primary|secondary|tertiary|unclassified|residential)$"]->.x;\n'
            # one hop further: divided cross streets have short pieces under the bridge that touch no ramp
            f'node(w.x)->.xn;\nway(bn.xn)["highway"~"^(trunk|primary|secondary|tertiary)$"]->.x2;\n'
            f'(.sel;.links;.x;.x2;);\nout geom;')


class MapDataError(Exception):
    """Road data could not be downloaded."""


# Paid server: OVERPASS_ENDPOINTS="https://paid.example/api/interpreter[,free backup,...]".  If the
# provider wants its key in a header, OVERPASS_HEADER="Name: value" (e.g. "Authorization: Bearer abc").
# The header goes ONLY to the first address, so the key never reaches a free backup server.
# Without these settings the paid overpass.ch plan below is used, with the free public servers
# as backups if it does not answer.
# SECRET: this file contains the paid API key.  Keep the code private (private repository only).
_PAID_ENDPOINT = "https://api.overpass.ch/v1/interpreter"
_PAID_HEADER = "X-API-Key: ovp_0GenBoqBBzHn1l0wnuF9aFsF3KYBIWex"
_FREE_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
_CUSTOM_ENDPOINTS = [e.strip() for e in os.environ.get("OVERPASS_ENDPOINTS", "").split(",") if e.strip()]
_h = os.environ.get("OVERPASS_HEADER", "") if _CUSTOM_ENDPOINTS else _PAID_HEADER
OVERPASS_KEY_HEADER = {_h.split(":", 1)[0].strip(): _h.split(":", 1)[1].strip()} if ":" in _h else {}
OVERPASS_ENDPOINTS = _CUSTOM_ENDPOINTS or [_PAID_ENDPOINT] + _FREE_ENDPOINTS
CACHE_DIR = os.environ.get("OVERPASS_CACHE_DIR",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), ".overpass_cache"))
CACHE_MAX_AGE_S = 7 * 24 * 3600          # road geometry hardly changes; a week is safe
RETRY_WAITS_S = (0, 10, 30)              # one pass over all mirrors per entry
FETCH_BUDGET_S = int(os.environ.get("FETCH_BUDGET_S", 240))   # max time spent downloading one state
_BUSY = (429, 502, 503, 504)
_GAVE_UP = "gave up: time limit for this state reached"


def _cache_path(q):
    return os.path.join(CACHE_DIR, hashlib.sha256(q.encode()).hexdigest()[:32] + ".json")


def _cache_get(q):
    p = _cache_path(q)
    try:
        if time.time() - os.path.getmtime(p) < CACHE_MAX_AGE_S:
            with open(p, "rb") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    return None


_index_lock = __import__("threading").Lock()


def _cache_put(q, data, state=None, roads=None, part=False):
    """Save one answer.  The index records its state and which permit roads it fully
    contains (roads), or that it is only one tile of a long road (part)."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _cache_path(q) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _cache_path(q))
        if state:
            entry = {"state": state, "part": True} if part else {"state": state, "roads": sorted(roads or ())}
            with _index_lock:
                idx = _index_load()
                idx[os.path.basename(_cache_path(q))] = entry
                _index_save(idx)
    except OSError:
        pass                              # caching is best-effort


# ---- reuse of earlier downloads ---------------------------------------------------------
# The cache is keyed by the exact query text, so any change to the query (or a permit read
# slightly differently) would otherwise force a new download from the often-busy servers.
# Every saved download is also filed under its state; if the state's earlier downloads
# already contain all of this permit's roads, they are used and nothing is downloaded.
def _index_path():
    return os.path.join(CACHE_DIR, "index.json")


def _index_load():
    try:
        with open(_index_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _index_save(idx):
    try:
        with open(_index_path(), "w", encoding="utf-8") as f:
            json.dump(idx, f)
    except OSError:
        pass


def _guess_state(data):
    """State of a download saved before the index existed: the state centre nearest the
    median of its points."""
    from geocode import STATE_CENTER
    la, lo = [], []
    for el in data.get("elements", []):
        for p in el.get("geometry", ())[:1]:
            la.append(p["lat"]); lo.append(p["lon"])
    if not la:
        return None
    la.sort(); lo.sort()
    m = (la[len(la) // 2], lo[len(lo) // 2])
    return min(STATE_CENTER, key=lambda s: haversine_m(STATE_CENTER[s], m))


def _state_downloads(state):
    """Earlier complete downloads for this state, newest first (only those younger than
    CACHE_MAX_AGE_S), as (path, roads it fully contains or None if not recorded)."""
    idx, changed, out = _index_load(), False, []
    try:
        files = [f for f in os.listdir(CACHE_DIR) if f.endswith(".json") and f not in ("index.json", "geocode.json")]
    except OSError:
        return []
    for f in files:
        p = os.path.join(CACHE_DIR, f)
        if time.time() - os.path.getmtime(p) >= CACHE_MAX_AGE_S:
            continue
        if f not in idx:
            try:
                with open(p, "rb") as fh:
                    idx[f] = _guess_state(json.load(fh))
            except (OSError, ValueError):
                idx[f] = None
            changed = True
        e = idx[f]
        if e == state:                                   # saved before roads were recorded
            out.append((p, None))
        elif isinstance(e, dict) and e.get("state") == state and not e.get("part"):
            out.append((p, set(e.get("roads", ()))))
    if changed:
        with _index_lock:
            cur = _index_load()
            cur.update({k: v for k, v in idx.items() if k not in cur})
            _index_save(cur)
    return sorted(out, key=lambda t: os.path.getmtime(t[0]), reverse=True)


def _merge(datas):
    seen, elements = set(), []
    for data in datas:
        for el in data.get("elements", []):
            k = (el.get("type"), el.get("id"))
            if k not in seen:
                seen.add(k)
                elements.append(el)
    return {"elements": elements}


def _saved(state):
    """Everything downloaded earlier for this state (any permit), merged, and the road keys
    that some download recorded as fully contained."""
    datas, complete = [], set()
    for p, roads in _state_downloads(state):
        try:
            with open(p, "rb") as fh:
                datas.append(json.load(fh))
        except (OSError, ValueError):
            continue
        complete |= roads or set()
    return _merge(datas), complete


WHOLE_ROAD_MIN_MILES = 10   # a downloaded road is the whole road across the state; less is a cross-street scrap


def _way_keys(tags):
    refs = set()
    for ref in re.split(r"[;,]", tags.get("ref", "")):
        ref = ref.strip()
        if ref:
            refs.add(ref_key(ref))
    names = {name_key(tags[t]) for t in ("name", "alt_name", "old_name") if tags.get(t)}
    return refs, names


def _map_miles(data, run):
    m = 0.0
    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        refs, names = _way_keys(el.get("tags", {}))
        if run.key in refs or names & run.names:
            g = el.get("geometry") or []
            m += sum(haversine_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(g, g[1:]))
    return m / 1609.344


def _saved_roads(have, complete, runs_in_leg, state):
    """Permit roads that need no download: recorded as complete by an earlier download, or
    (downloads saved before that was recorded) present in at least the length the permit
    drives on them in this state and at least WHOLE_ROAD_MIN_MILES.  Pieces of a road that only
    came along as another road's cross street do not count (a permit without miles would
    otherwise accept a few hundred metres of US-11 as 'the road')."""
    out = set()
    for rs in _road_groups(runs_in_leg):
        k = rs[0].key
        here = sum(r.state_miles.get(state, 0.0) for r in rs)    # not the miles of the next state's permit
        if k in complete or _map_miles(have, rs[0]) >= max(here, WHOLE_ROAD_MIN_MILES):
            out.add(k)
    return out


def _reuse_earlier(state, runs_in_leg):
    merged, complete = _saved(state)
    need = {r.key for r in runs_in_leg if not (0 < r.miles <= CONNECTOR_MAX_MILES)}
    return merged if merged["elements"] and need <= _saved_roads(merged, complete, runs_in_leg, state) else None


# ---- download road by road ----------------------------------------------------------------
# One request per permit road (and, for long roads, per map tile along the road).  Every
# answer is saved at once, so when the servers cut us off half-way the next attempt only
# asks for the roads / tiles that are still missing.
LONG_ROAD_MILES = 100     # longer roads (e.g. I-10 across Texas) are fetched tile by tile
TILE_DEG = 1.0            # tile size in degrees (about 100 km)
TILE_PAD_DEG = 0.05       # overlap, so interchanges on a tile edge are complete


def _road_groups(runs_in_leg):
    """Runs grouped by road (a road the permit uses twice is fetched once), permit order."""
    groups = {}
    for r in runs_in_leg:
        groups.setdefault(r.key, []).append(r)
    return list(groups.values())


def _tiles(skeleton):
    """Tiles (s, w, n, e) that the road's geometry passes through."""
    cells = set()
    for el in skeleton.get("elements", []):
        for p in el.get("geometry", ()):
            cells.add((math.floor(p["lat"] / TILE_DEG), math.floor(p["lon"] / TILE_DEG)))
    return [(a * TILE_DEG - TILE_PAD_DEG, b * TILE_DEG - TILE_PAD_DEG,
             (a + 1) * TILE_DEG + TILE_PAD_DEG, (b + 1) * TILE_DEG + TILE_PAD_DEG) for a, b in sorted(cells)]


def _fetch_road(state, rs, endpoints, timeout, waits, use_cache, deadline):
    """One permit road with its ramps and cross streets.  Saved under the state when complete."""
    q = overpass_query(state, rs)
    if use_cache and (hit := _cache_get(q)) is not None:
        return hit
    if sum(r.miles for r in rs) <= LONG_ROAD_MILES:
        return _fetch_query(state, q, endpoints, timeout, waits, use_cache, deadline, roads={rs[0].key})
    # long road: first the bare road (small), then ramps / cross streets tile by tile.
    # Tiles are saved one by one (not yet filed under the state, the road is incomplete).
    skeleton = _fetch_query(state, overpass_query(state, rs, road_only=True),
                            endpoints, timeout, waits, use_cache, deadline, part=True)
    tiles = _tiles(skeleton)
    datas = [skeleton]
    for i, bb in enumerate(tiles):
        try:
            datas.append(_fetch_query(state, overpass_query(state, rs, bbox=bb),
                                      endpoints, timeout, waits, use_cache, deadline, part=True))
        except MapDataError as e:
            raise MapDataError(f"{e} [map tile {i + 1} of {len(tiles)}]") from None
    data = _merge(datas)
    if use_cache:
        _cache_put(q, data, state, roads={rs[0].key})     # complete: from now on it counts as saved for this state
    return data


def fetch_leg_ways(state, runs_in_leg, endpoints=None, timeout=200, waits=RETRY_WAITS_S, use_cache=True):
    """Download the permit's roads for one state from public Overpass servers, then complete
    ramps that the download cut off (see _complete_ramps).

    Roads already saved from earlier downloads (any permit of this state) are not asked for
    again.  The rest is fetched one road at a time, long roads one map tile at a time, and
    every answer is saved immediately: if the servers stop answering half-way, the next
    attempt continues with what is still missing.
    """
    data = _fetch_roads(state, runs_in_leg, endpoints, timeout, waits, use_cache)
    return _complete_ramps(state, data, endpoints, timeout, waits, use_cache)


# ---- completing ramps ---------------------------------------------------------------------
# The road download follows ramps 3 pieces out.  Big interchanges draw one ramp as a chain of
# more pieces (I-20 West -> I-459 South near Birmingham AL is 7), so its middle can be missing:
# the ramp then ends in mid-air, the route cannot take it and has to loop back to another
# junction.  Afterwards, ask only for the ramp pieces at such loose ends, a few rounds deep.
RAMP_ROUNDS = 8
RAMP_BUDGET_S = int(os.environ.get("RAMP_BUDGET_S", 120))
_RAMP_CHUNK = 300          # node ids per request


def _loose_ramp_ends(elements):
    """End nodes of ramp ways that touch no other way in the data."""
    count = {}
    for el in elements:
        if el.get("type") == "way":
            for n in set(el.get("nodes", ())):
                count[n] = count.get(n, 0) + 1
    out = set()
    for el in elements:
        if el.get("type") == "way" and el.get("tags", {}).get("highway", "").endswith("_link") and el.get("nodes"):
            for n in (el["nodes"][0], el["nodes"][-1]):
                if count.get(n) == 1:
                    out.add(n)
    return out


# Which loose ends were already asked about is remembered per state (ramps_<STATE>.dat in the
# cache folder).  Without it every route asks again: many loose ends are real dead ends that
# stay loose forever, so the question text never repeats and the answer cache never hits.
RAMP_RETRY_S = 6 * 3600     # a question the servers failed to answer is retried after this long


def _ramp_path(state):
    return os.path.join(CACHE_DIR, f"ramps_{state}.dat")


def _ramp_load(state):
    try:
        if time.time() - os.path.getmtime(_ramp_path(state)) < CACHE_MAX_AGE_S:
            with open(_ramp_path(state), encoding="utf-8") as f:
                rec = json.load(f)
            return {"asked": {int(n): t for n, t in rec.get("asked", {}).items()},
                    "failed": {int(n): t for n, t in rec.get("failed", {}).items()},
                    "elements": rec.get("elements", [])}
    except (OSError, ValueError, AttributeError):
        pass
    return {"asked": {}, "failed": {}, "elements": []}


def _ramp_save(state, rec):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = _ramp_path(state) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, _ramp_path(state))
    except OSError:
        pass


def _complete_ramps(state, data, endpoints=None, timeout=200, waits=RETRY_WAITS_S, use_cache=True):
    elements = list(data.get("elements", []))
    have = {(el.get("type"), el.get("id")) for el in elements}
    with _index_lock:
        rec = _ramp_load(state) if use_cache else {"asked": {}, "failed": {}, "elements": []}
    for el in rec["elements"]:           # ramp pieces found by earlier routes of this state
        k = (el.get("type"), el.get("id"))
        if k not in have:
            have.add(k)
            elements.append(el)
    now = time.time()
    asked = set(rec["asked"]) | {n for n, t in rec["failed"].items() if now - t < RAMP_RETRY_S}
    deadline = now + RAMP_BUDGET_S
    changed = False
    try:
        for _ in range(RAMP_ROUNDS):
            todo = sorted(_loose_ramp_ends(elements) - asked)
            if not todo:
                break
            asked.update(todo)
            new = []
            for i in range(0, len(todo), _RAMP_CHUNK):
                chunk = todo[i:i + _RAMP_CHUNK]
                ids = ",".join(map(str, chunk))
                q = f'[out:json][timeout:90];\nnode(id:{ids});\nway(bn)["highway"~"_link$"];\nout geom;'
                changed = True
                try:
                    got = _fetch_query(state, q, endpoints, timeout, waits, use_cache, deadline, roads=())
                except MapDataError:
                    t = time.time()
                    rec["failed"].update((n, t) for n in todo[i:])   # this and the rest of the round
                    raise
                t = time.time()
                for n in chunk:
                    rec["asked"][n] = t
                    rec["failed"].pop(n, None)
                for el in got.get("elements", []):
                    k = (el.get("type"), el.get("id"))
                    if k not in have:
                        have.add(k)
                        new.append(el)
            if not new:
                break
            elements += new
            rec["elements"] += new
    except MapDataError:
        pass       # servers busy: route with what we have (a resulting loop is reported as a warning)
    if use_cache and changed:
        with _index_lock:
            _ramp_save(state, rec)
    return {"elements": elements}


def _fetch_roads(state, runs_in_leg, endpoints=None, timeout=200, waits=RETRY_WAITS_S, use_cache=True):
    if use_cache:
        whole = _cache_get(overpass_query(state, runs_in_leg))
        if whole is not None:
            return whole
        have, complete = _saved(state)
    else:
        have, complete = {"elements": []}, set()
    found = _saved_roads(have, complete, runs_in_leg, state)
    todo = [rs for rs in _road_groups(runs_in_leg)
            if rs[0].key not in found
            # a road asked for before but not in the map under that name: don't ask again
            and not (use_cache and _cache_get(overpass_query(state, rs)) is not None)]
    if not todo:
        return have
    # short roads first: they are cheap, and a long road (many tiles) must not use up the
    # whole time budget before they were even tried
    todo.sort(key=lambda rs: sum(r.miles for r in rs) > LONG_ROAD_MILES)
    deadline = time.time() + FETCH_BUDGET_S
    got, failed, last_err = [have], [], None
    for rs in todo:
        try:
            got.append(_fetch_road(state, rs, endpoints, timeout, waits, use_cache, deadline))
        except MapDataError as e:
            failed.append(rs[0].raw)
            # keep the first real server answer; later roads often only say "time limit reached"
            if last_err is None or _GAVE_UP in str(last_err):
                last_err = e
    data = _merge(got)
    if failed:
        need = {r.key for r in runs_in_leg if not (0 < r.miles <= CONNECTOR_MAX_MILES)}
        if not need <= {rs[0].key for rs in _road_groups(runs_in_leg) if rs[0].raw not in failed}:
            done = len(todo) - len(failed)
            raise MapDataError(
                f"Could not download road data for {state}: {len(failed)} of {len(todo)} roads still missing "
                f"({', '.join(failed)}). {done} road(s) were downloaded and saved this time; the next try "
                f"only asks for the missing ones. Last server answer: {last_err}")
    return data


def _fetch_query(state, q, endpoints=None, timeout=200, waits=RETRY_WAITS_S, use_cache=True, deadline=None,
                 roads=None, part=False):
    """Run one Overpass query: every mirror, then wait and try again (public servers are often
    briefly overloaded), but give up at `deadline` so the page does not hang for half an hour.
    Successful answers are cached on disk, so a repeat costs nothing.  roads: the permit roads
    this answer fully contains; part=True: only one tile of a long road (not complete)."""
    if use_cache and (hit := _cache_get(q)) is not None:
        return hit
    deadline = deadline or time.time() + FETCH_BUDGET_S
    errors, empty_from = [], set()
    for attempt, wait in enumerate(waits):
        if wait:
            if time.time() + wait >= deadline - 20:
                break
            time.sleep(wait)
        retry_worthwhile = False
        for ep in endpoints or OVERPASS_ENDPOINTS:
            left = deadline - time.time()
            if left < 20:
                errors.append(_GAVE_UP)
                break
            req = urllib.request.Request(
                ep, data=urllib.parse.urlencode({"data": q}).encode(), method="POST",
                headers={"User-Agent": "permit-route/0.1 (oversize permit route viewer)", "Accept": "application/json",
                         **(OVERPASS_KEY_HEADER if ep == OVERPASS_ENDPOINTS[0] else {})})
            tag = f"{ep} (try {attempt + 1})"
            try:
                with urllib.request.urlopen(req, timeout=min(timeout, left)) as r:
                    body = r.read()
                data = json.loads(body)
                if data.get("remark") and not data.get("elements"):
                    # Overpass reports timeouts / memory limits here, with HTTP 200
                    errors.append(f"{tag}: {data['remark'][:160]}")
                    retry_worthwhile = True
                    continue
                if roads and not data.get("elements") and not part:
                    # "no such road" for a permit road: mirrors sometimes answer empty when their copy of
                    # the state boundary is incomplete (seen for TX Loop 375).  Believe it only when a
                    # second server says the same; never save a single empty answer as the road.
                    empty_from.add(ep)
                    if len(empty_from) < 2:
                        errors.append(f"{tag}: empty answer, asking another server")
                        retry_worthwhile = True
                        continue
                if use_cache and not data.get("remark"):
                    _cache_put(q, data, state, roads=roads, part=part)
                return data
            except urllib.error.HTTPError as e:
                busy = e.code in _BUSY
                retry_worthwhile |= busy
                errors.append(f"{tag}: HTTP {e.code}{' (server busy)' if busy else ''}")
            except Exception as e:  # noqa: BLE001  (timeouts, DNS, bad JSON)
                retry_worthwhile = True
                errors.append(f"{tag}: {type(e).__name__}: {str(e)[:120]}")
        if not retry_worthwhile:
            break
    if empty_from:
        return {"elements": []}     # one server says "no such road", none confirmed it: not saved, asked again next time
    raise MapDataError(f"Could not download road data for {state}. " + " | ".join(errors))


def build_trip(legs, start_ll, end_ll, fetch=fetch_leg_ways, start_snap_m=None, end_snap_m=None):
    """start_snap_m / end_snap_m: how far the point may be from the permit's first / last road
    (bigger for points estimated from a town name or a state line)."""
    runs = build_runs(legs)
    g = Graph()
    for li, leg in enumerate(legs):
        in_leg = [r for r in runs if li in r.legs]
        g.add_overpass(fetch(leg["state"], in_leg), li)
    per_run = solve(g, runs, start_ll, end_ll, start_snap_m=start_snap_m, end_snap_m=end_snap_m)
    gj, warnings = to_geojson(runs, per_run)
    rep = _repeated_miles(per_run)
    if rep >= REPEAT_WARN_MILES:
        warnings.insert(0, f"The route drives {rep:.0f} mi of the same road twice (a loop). Usually a ramp is missing "
                           f"in the map data, so the route had to turn around at another junction. Check it on the map.")
    return gj, warnings


REPEAT_WARN_MILES = 1.0


def _repeated_miles(per_run):
    """Miles of road driven more than once in the same direction (a loop such as I-20 -> I-459 -> back on I-20)."""
    seen, rep, prev = set(), 0.0, None
    for p in per_run:
        for c in p["coords"]:
            if prev is not None and c != prev:
                if (prev, c) in seen:
                    rep += haversine_m(prev, c)
                seen.add((prev, c))
            prev = c
    return rep / 1609.344
