"""Tests solve()/build_runs()/to_geojson() on small synthetic road networks."""
import sys
sys.path.insert(0, "/home/claude/out/server")
import route_builder as rb


def way(wid, node_ids, coords, **tags):
    return {"type": "way", "id": wid, "nodes": node_ids,
            "geometry": [{"lat": la, "lon": lo} for la, lo in coords], "tags": tags}


def leg(state, steps):
    return {"state": state, "steps": [dict(road=r, miles=m, kind="road", text=t or r) for r, m, t in steps]}


def graph(*ways, legidx=0):
    g = rb.Graph(); g.add_overpass({"elements": list(ways)}, legidx); return g


A, B, C, D, E = (32.0, -91.0), (32.0, -90.9), (32.0, -90.8), (32.0, -90.7), (32.0, -90.6)
P6, P7 = (32.1, -90.88), (32.1, -90.72)
I20 = way(100, [1, 2, 3, 4, 5], [A, B, C, D, E], highway="motorway", ref="I 20", oneway="no")
I220 = way(200, [2, 6, 7, 4], [B, P6, P7, D], highway="motorway", ref="I 220", oneway="no")

ok = True
def check(name, cond, extra=""):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + name, extra)

# 1. detour I-20 -> I-220 -> I-20 must be followed even though I-20 straight is shorter
L = [leg("LA", [("I-20 W", 5.8, None), ("I-220 W", 23.0, None), ("I-20 W", 5.8, None)])]
runs = rb.build_runs(L)
res = rb.solve(graph(I20, I220), runs, A, E)
used6 = any(abs(c[1] - -90.88) < 1e-9 for c in res[1]["coords"])
check("detour followed through I-220", used6 and len(runs) == 3, f"runs={[r.key for r in runs]}")
gj, warns = rb.to_geojson(runs, res)
check("geojson has one feature per road", len(gj["features"]) == 3, f"map_miles={[f['properties']['map_miles'] for f in gj['features']]}")

# 2. without the I-220 requirement the router would go straight (control)
runs2 = rb.build_runs([leg("LA", [("I-20", 40, None)])])
res2 = rb.solve(graph(I20, I220), runs2, A, E)
check("control: plain I-20 stays on I-20", all(abs(c[0] - 32.0) < 1e-9 for c in res2[0]["coords"]))

# 3. roads that do not connect -> NoPath, not a guess
I220b = way(200, [2, 6, 7, 40], [B, P6, P7, (32.0, -90.7001)], highway="motorway", ref="I 220", oneway="no")
I20b = way(100, [1, 2, 3, 4, 5], [A, B, C, (32.0, -90.7), E], highway="motorway", ref="I 20", oneway="no")
try:
    rb.solve(graph(I20b, I220b), runs, A, E); check("disconnected roads rejected", False)
except rb.NoPath as e:
    check("disconnected roads rejected", True, str(e)[:60])

# 4. ramp link between two roads
R1 = way(300, [11, 12], [(33.0, -90.0), (33.0, -89.9)], highway="motorway", ref="I 20", oneway="no")
LK = way(350, [12, 13, 14], [(33.0, -89.9), (33.01, -89.89), (33.02, -89.9)], highway="motorway_link", oneway="yes")
R2 = way(400, [14, 15], [(33.02, -89.9), (33.1, -89.8)], highway="primary", ref="US 80", oneway="no")
runs4 = rb.build_runs([leg("MS", [("I-20", 5, None), ("US-80", 7, None)])])
res4 = rb.solve(graph(R1, LK, R2), runs4, (33.0, -90.0), (33.1, -89.8))
check("ramp (link) joins I-20 to US-80", len(res4[1]["coords"]) >= 2)
try:
    rb.solve(graph(R1, R2), runs4, (33.0, -90.0), (33.1, -89.8)); check("no link -> no path", False)
except rb.NoPath:
    check("no link -> no path", True)

# 5. one-way carriageway cannot be driven backwards
ONE = way(100, [1, 2, 3, 4, 5], [A, B, C, D, E], highway="motorway", ref="I 20", oneway="yes")
try:
    rb.solve(graph(ONE), rb.build_runs([leg("LA", [("I-20", 40, None)])]), E, A); check("wrong-way travel rejected", False)
except rb.NoPath:
    check("wrong-way travel rejected", True)

# 6. TX-style names normalise to the same road as OSM refs
for printed, osm in [("IH20", "I 20"), ("SH43", "TX 43"), ("US79", "US 79"), ("FM107", "FM 107"), ("SL256", "Loop 256"), ("IH45WFR", "I 45")]:
    r = rb.build_runs([leg("TX", [(printed, 1, None)])])[0]
    check(f"{printed} matches OSM ref '{osm}'", rb.way_matches(r, {"ref": osm}, {0}))
r = rb.build_runs([leg("TX", [("US79", 1, None)])])[0]
check("same number, other road type does not match", not rb.way_matches(r, {"ref": "FM 79"}, {0}) and not rb.way_matches(r, {"ref": "I 79"}, {0}))
check("multi-ref OSM way matches", rb.way_matches(r, {"ref": "US 79;TX 43"}, {0}))

# 7. local street found by name (SC-style 'GLENN ST')
rn = rb.build_runs([leg("SC", [("L-166", 0.1, "Turn LEFT onto L-166 (GLENN ST)(Route not Permitable)")])])[0]
check("street matched by name", rb.way_matches(rn, {"name": "Glenn St"}, {0}) and "not_permitted" not in rn.flags)

# 8. length mismatch is reported
bad = rb.build_runs([leg("LA", [("I-20", 200.0, None)])])
gj, w = rb.to_geojson(bad, rb.solve(graph(I20), bad, A, E))
check("length mismatch warned", len(w) == 1, w[0] if w else "")

# 9. not_permitted flag surfaces
legs9 = [{"state": "SC", "steps": [{"road": "L-166", "miles": 0.1, "kind": "not_permitted", "text": "L-166"}]}]
check("not_permitted carried to run", "not_permitted" in rb.build_runs(legs9)[0].flags)

# 10. overpass query is well-formed text
q = rb.overpass_query("TX", rb.build_runs([leg("TX", [("IH20", 1, None), ("SH43", 1, "Turn left onto SH43 (MARSHALL AVE)")])]))
check("overpass query built", "ISO3166-2\"=\"US-TX" in q and "out geom" in q
      and "(I|IH|Interstate)[ -]?0*(20)" in q and "(TX|SH|SR|" in q and ")[ -]?0*(43)" in q)
check("query asks for road type + number, not bare numbers", "(20|43)" not in q)
check("spur: permit SS 16 and map 'Spur 16' are the same road", rb.canon("SS16")[0] == rb.canon("Spur 16")[0])
nm = rb._names("Turn onto US-281 [AMERICAN LEGION MEMORIAL HIGHW] [TRUCK] [LOADED ROUTE DESTINATION SH 0178 NM LINE] [CRAIG W FR]")
check("junk bracket text is not a street name", nm == {"AMERICAN LEGION MEMORIAL HIGHW"}, nm)
r_trunc = rb.Run("US281", "US-281", 0, "TX"); r_trunc.names = nm
check("cut-off permit name matches the full map name",
      rb.way_matches(r_trunc, {"name": "American Legion Memorial Highway"}, {0}))
if __name__ == "__main__":
    print("\nALL PASS" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)
