"""First inspection: does the LLM read YOUR permits correctly, and do the roads connect on the map?

    python inspect_permits.py FOLDER_WITH_PDFS                 # reads each PDF with the LLM, checks it
    python inspect_permits.py FOLDER_WITH_PDFS --route         # also draws each permit that prints start AND end coordinates
    python inspect_permits.py FOLDER_WITH_PDFS --golden golden_trip.json   # compare roads with the hand-checked sample result

Free: built-in rules, then a local Ollama model for unknown layouts. Needs internet for map data only.  Results go to ./inspection/ .
"""
import argparse
import glob
import json
import os
import sys
from difflib import SequenceMatcher

import extract_route as X
import reader
import route_builder as RB


def road_keys(steps, state):
    keys = []
    for s in steps:
        if not s.get("road"):
            continue
        k, _ = X.canon(s["road"], state)
        k = k or "RAW" + RB.norm(s["road"])
        if not keys or keys[-1] != k:
            keys.append(k)
    return keys


def legs_from_golden(path):
    """Dry-run helper: turns the hand-checked golden file into legs so this script can be tested without the LLM."""
    gold = json.load(open(path))["permits"]
    out = {}
    for p in gold:
        r = p["route"]
        steps = []
        for s in r["steps"]:
            f = set(s["flags"])
            kind = ("not_permitted" if "route_not_permitted_contact_local_government" in f else "detour" if "detour" in f
                    else "local_or_county" if f & {"county_road", "non_state_or_local_road"} else "ramp" if "ramp" in f else "road")
            steps.append({"road": s["road"], "exit": s["exit"], "miles": s["miles"], "kind": kind, "text": s["instruction"]})
        def pl(raw, ll):
            d = {"text": raw}
            if ll: d["lat"], d["lon"] = ll
            return d
        tot = (p["extraction"]["checks"] or {}).get("declared_total")
        out[p["permit"]["state"]] = {"state": p["permit"]["state"], "permit_number": p["permit"]["number"],
                                     "origin": pl(r["origin_raw"], r["origin_latlon"]),
                                     "destination": pl(r["destination_raw"], r["destination_latlon"]),
                                     "total_miles": tot, "steps": steps}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--route", action="store_true")
    ap.add_argument("--golden")
    ap.add_argument("--mock-from", help="(testing only) use a golden file instead of calling the LLM")
    a = ap.parse_args()

    os.makedirs("inspection", exist_ok=True)
    gold = {p["permit"]["state"]: p for p in json.load(open(a.golden))["permits"]} if a.golden else {}
    mock = legs_from_golden(a.mock_from) if a.mock_from else None
    pdfs = sorted(glob.glob(os.path.join(a.folder, "*.pdf")))
    if not pdfs:
        sys.exit(f"No PDF files found in {a.folder}")
    legs, report, failed = [], [], 0

    for f in pdfs:
        name = os.path.basename(f)
        print(f"\n=== {name}")
        try:
            data = open(f, "rb").read()
            text = X.pdf_text(__import__("io").BytesIO(data))
            leg, method = None, None
            if mock:
                st = next((s for s in mock if f"_-_{s}" in name or f"-{s}." in name), None)
                leg = json.loads(json.dumps(mock[st])) if st else None
                method = "mock"
            if leg is None:
                leg, text, method = reader.read_permit(data)
            print(f"  read with: {method}")
        except Exception as e:  # noqa: BLE001 - show any failure to the user and go on
            print(f"  FAILED to read: {e}"); report.append(f"{name}: FAILED {e}"); failed += 1; continue
        problems, _ = X.validate(leg, text)
        leg = X.enrich(leg)
        json.dump(leg, open(os.path.join("inspection", name + ".json"), "w"), indent=2)
        print(f"  state {leg['state']}, {len(leg['steps'])} steps, from '{leg['origin']['text']}' to '{leg['destination']['text']}'")
        print("  checks:", "all passed" if not problems else "")
        for p in problems:
            print("   -", p)
        if any(s.get("kind") == "not_permitted" for s in leg["steps"]):
            print("   - the permit marks a step as not permitted / contact local government")
        if leg["state"] in gold:
            g = road_keys([{"road": s["road"]} for s in gold[leg["state"]]["route"]["steps"]], leg["state"])
            m = road_keys(leg["steps"], leg["state"])
            ratio = SequenceMatcher(None, g, m).ratio()
            print(f"  roads vs hand-checked sample: {ratio:.0%} match")
            if ratio < 1:
                print("   expected:", " > ".join(g)); print("   got     :", " > ".join(m))
        report.append(f"{name}: {leg['state']} steps={len(leg['steps'])} problems={len(problems)}")
        legs.append(leg)

    if len(legs) > 1:
        ordered, ok = X.order_legs(legs)
        print("\nTravel order:", " -> ".join(l["state"] for l in ordered), "(sure)" if ok else "(NOT sure)")
        gaps = X.chain_gaps(ordered)
        for g in gaps:
            print("  gap:", g)

    if a.route:
        for leg in legs:
            o, d = leg["origin"], leg["destination"]
            if o.get("lat") is None or d.get("lat") is None:
                print(f"\n{leg['state']}: permit prints no start/end coordinates; skipped (use the web page and click the points)")
                continue
            print(f"\n{leg['state']}: downloading map data and building the route (can take a minute) ...")
            try:
                gj, warns = RB.build_trip([leg], (o["lat"], o["lon"]), (d["lat"], d["lon"]))
            except RB.NoPath as e:
                print("  NO ROUTE:", e); continue
            except Exception as e:  # noqa: BLE001
                print("  map download failed:", e); continue
            json.dump(gj, open(os.path.join("inspection", f"{leg['state']}.geojson"), "w"))
            print("  route built:", [(ft["properties"]["road"], ft["properties"]["map_miles"], ft["properties"]["permit_miles"]) for ft in gj["features"]])
            for w in warns:
                print("   -", w)

    open(os.path.join("inspection", "report.txt"), "w").write("\n".join(report))
    print("\nSaved details in the 'inspection' folder.")


if __name__ == "__main__":
    main()
