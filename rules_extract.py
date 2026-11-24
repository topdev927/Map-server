"""(rules backend) Prototype: read oversize permit PDFs into one canonical route model.

Usage: python3 -I permit_extract.py OUT_DIR file1.pdf file2.pdf ...

This prototype uses layout-specific parsing for the layouts seen in the
sample permits.  In the app, unknown layouts would go to an LLM that fills the
same schema; the checks at the bottom (distance sums, chain continuity)
apply to either source.
"""
import json
import re
import sys
import pdfplumber

MINUS = "−"


def page_lines(pdf):
    out = []
    for p in pdf.pages:
        out.append(p.extract_text(layout=False) or "")
    return "\n".join(out)


# ---------------------------------------------------------------- roads
def norm_road(tok, state=None):
    """Normalise road designations to one scheme: I-20, US-79, TX-43, ..."""
    t = tok.replace(MINUS, "-").strip().upper()
    t = re.sub(r"\s+", " ", t)
    flags = []
    m = re.match(r"^(?:IH|I)[-\s]?0*(\d+)\s*([NSEW]?FR)?\b", t)
    if m:
        if m.group(2):
            flags.append("frontage")
        return f"I-{int(m.group(1))}", flags
    m = re.match(r"^US[-\s]?0*(\d+)", t)
    if m:
        return f"US-{int(m.group(1))}", flags
    m = re.match(r"^(SH|SR|SC|GA|AL|MS|LA|NM|TX)[-\s]?0*(\d+)(?:\s*([NSEW]?FR))?\b", t)
    if m:
        pre = m.group(1)
        pre = state if pre in ("SH", "SR") and state else pre
        if m.group(3):
            flags.append("frontage")
        return f"{pre}-{int(m.group(2))}", flags
    m = re.match(r"^(SL|LP)[-\s]?0*(\d+)", t)
    if m:
        return f"{state or ''}-LOOP-{int(m.group(2))}", flags
    m = re.match(r"^(FM|RM|SS|BS)[-\s]?0*(\d+)", t)
    if m:
        return f"{state or ''}-{m.group(1)}-{int(m.group(2))}", flags
    m = re.match(r"^(CO RD|CR)[-\s]?0*(\d+)", t)
    if m:
        flags.append("county_road")
        return f"CR-{int(m.group(2))}", flags
    return t, flags


def classify_action(text):
    t = text.lower()
    if "detour" in t:
        return "detour"
    if "arrive" in t:
        return "arrive"
    if "ramp" in t or "exit" in t:
        return "ramp_exit"
    if "turn" in t or "bear" in t or "keep" in t:
        return "turn"
    if "merge" in t:
        return "merge"
    if "continue" in t:
        return "continue"
    return "other"


def exit_no(text):
    m = re.search(r"\bexit\s+(\d+[A-Z]?)", text, re.I)
    return m.group(1) if m else None


# ---------------------------------------------------------- table layout
def parse_table_layout(pdf, state):
    """Layout used by MS, TX, GA, NM: Miles | Route | To | Distance | Est. Time."""
    steps = []
    origin = dest = None
    for page in pdf.pages:
        words = page.extract_words(keep_blank_chars=False)
        rows = {}
        for w in words:
            rows.setdefault(round(w["top"] / 3), []).append(w)
        keys = sorted(rows)
        header = None
        for k in keys:
            texts = [w["text"] for w in sorted(rows[k], key=lambda w: w["x0"])]
            if texts[:3] == ["Miles", "Route", "To"] or (texts[:1] == ["Miles"] and "Route" in texts):
                ws = sorted(rows[k], key=lambda w: w["x0"])
                x = {w["text"]: w["x0"] for w in ws}
                header = (x["Route"] - 3, x["To"] - 3, x["Distance"] - 25)
                break
        if not header:
            continue
        rx, tx, dx = header
        cur = None
        for k in keys:
            ws = sorted(rows[k], key=lambda w: w["x0"])
            if ws[0]["top"] <= [w for w in words if w["text"] == "Miles"][0]["top"]:
                continue
            cells = {"m": [], "r": [], "t": [], "d": []}
            for w in ws:
                if w["x0"] < rx:
                    cells["m"].append(w["text"])
                elif w["x0"] < tx:
                    cells["r"].append(w["text"])
                elif w["x0"] < dx:
                    cells["t"].append(w["text"])
                else:
                    cells["d"].append(w["text"])
            m = " ".join(cells["m"]).replace("< ", "<")
            r = " ".join(cells["r"])
            t = " ".join(cells["t"])
            d = cells["d"]
            if re.fullmatch(r"<?\s*[\d.]+", m):
                cur = {"miles_raw": m, "route_raw": r, "to_raw": t,
                       "cum": float(d[0]) if d and re.fullmatch(r"[\d.]+", d[0]) else None}
                steps.append(cur)
            elif cur is not None and (r or t) and not m:
                # wrapped continuation of the previous row
                if r:
                    cur["route_raw"] += " " + r
                if t:
                    cur["to_raw"] += " " + t
            else:
                txt = " ".join(w["text"] for w in ws)
                mm = re.match(r"(Origin|Destination|Final Destination):\s*(.*)", txt)
                if mm and not m:
                    if mm.group(1) == "Origin":
                        origin = origin or mm.group(2)
                    elif mm.group(1) == "Final Destination":
                        dest = mm.group(2)
    steps = [s for s in steps if s["route_raw"].strip()]  # drop Origin/Destination marker rows
    out = []
    for i, s in enumerate(steps):
        mi = s["miles_raw"]
        miles = 0.05 if mi.startswith("<") else float(mi)  # "< 0.1": store as 0.05 + flag
        road_tok = re.sub(r"\s+(Ramp|Connector)\b.*$", "", s["route_raw"], flags=re.I)
        road_tok = re.sub(r"\s+[nsew]{1,2}$", "", road_tok.strip(), flags=re.I)
        if re.match(r"^exit\b", road_tok.strip(), re.I):
            road, flags = None, ["ramp"]
        else:
            road, flags = norm_road(road_tok, state)
        if mi.startswith("<"):
            flags.append("miles_lt_0.1")
        if re.search(r"ramp|connector", s["route_raw"], re.I):
            flags.append("ramp")
        to = s["to_raw"]
        out.append({
            "seq": i + 1,
            "road": road,
            "direction": (re.findall(r"\b([nsew]{1,2})\s*$", s["route_raw"], re.I) or [None])[0],
            "action": classify_action(to),
            "exit": exit_no(to),
            "miles": miles,
            "cumulative_miles": s["cum"],
            "instruction": to,
            "route_label_raw": s["route_raw"],
            "flags": sorted(set(flags + (["detour"] if "detour" in to.lower() else []))),
        })
    return out, origin, dest


# -------------------------------------------------------------- SC layout
def parse_sc(text):
    seg = text.split("ROUTE INFORMATION", 1)[1]
    origin = re.search(r"Origin:\s*(.*)", seg).group(1).strip()
    dest = re.search(r"Destination:\s*(.*)", seg).group(1).strip()
    body = seg.split("Instruction")[1].split("Destination:")[0]
    steps = []
    cum = 0.0
    for line in body.splitlines():
        m = re.match(r"^(.*\S)\s+([\d.]+)$", line.strip())
        if not m or line.startswith("SOUTH CAROLINA") or "SINGLE TRIP" in line or "Valid From" in line:
            continue
        instr, miles = m.group(1), float(m.group(2))
        road_m = None
        for pat in (r"\b(I-\d+)\b", r"\b(US-\d+)\b", r"\b(SC-\d+)\b", r"\b([A-Z]-\d+(?:-\d+)?)\b"):
            road_m = re.search(pat, instr)
            if road_m:
                break
        road, flags = norm_road(road_m.group(1), "SC") if road_m else (None, [])
        if "Not Permitable" in instr or "not permitable" in instr.lower():
            flags.append("route_not_permitted_contact_local_government")
        if re.search(r"State Rd S-|\bL-\d+", instr):
            flags.append("non_state_or_local_road")
        if re.search(r"ramp|exit", instr, re.I):
            flags.append("ramp")
        cum = round(cum + miles, 2)
        dm = re.search(r"\b(?:[NSEW]\s*){1,2}$", instr.split(")")[-1])
        steps.append({
            "seq": len(steps) + 1,
            "road": road,
            "direction": None,
            "action": classify_action(instr),
            "exit": exit_no(instr),
            "miles": miles,
            "cumulative_miles": cum,
            "instruction": re.sub(r"\s+", " ", instr),
            "route_label_raw": instr,
            "flags": sorted(set(flags)),
        })
    return steps, origin, dest


# -------------------------------------------------------------- LA layout
def parse_la(text):
    ri = re.search(r"Origin:\s*(.*?)\s+Destination:\s*(.*?)\s+Total Miles:\s*([\d.]+)", text, re.S)
    origin, dest, total = [re.sub(r"\s+", " ", g) for g in ri.groups()]
    seg = text.split("Driving Instructions", 1)[1]
    steps = []
    cum = 0.0
    pending_restr = []
    for line in seg.splitlines():
        line = line.strip()
        m = re.match(r"^([\d.]+) mi\s+(.*)$", line)
        if m:
            miles, label = float(m.group(1)), m.group(2)
            cum = round(cum + miles, 2)
            road, flags = norm_road(label, "LA") if not label.upper().startswith("EXIT") else (None, ["ramp"])
            steps.append({
                "seq": len(steps) + 1, "road": road,
                "direction": (re.findall(r"\b([NSEW])$", label) or [None])[0],
                "action": "ramp_exit" if label.upper().startswith("EXIT") else "continue",
                "exit": exit_no(label), "miles": miles, "cumulative_miles": cum,
                "instruction": label, "route_label_raw": label, "flags": flags,
            })
    return steps, origin, dest, float(total)


# -------------------------------------------------------------- AL layout
def parse_al(text):
    t = text.replace(MINUS, "-")
    route = re.search(r"AUTHORIZED ROUTE\s+(.*)", t).group(1).strip()
    frm = re.search(r"ROUTE FROM\s+(.*?)\s+TO\s+(.*)", t)
    origin, dest = frm.group(1).strip(), frm.group(2).strip()
    toks = [x.strip() for x in route.split(",")]
    steps = []
    in_detour = False
    for tok in toks:
        flags = []
        if tok.upper().startswith("DETOUR:"):
            in_detour = True
            tok = tok.split(":", 1)[1].strip()
            flags.append("detour_marker")
        label = tok
        is_exit = tok.upper().startswith("EXIT")
        road, f2 = (None, ["ramp"]) if is_exit else norm_road(tok, "AL")
        flags += f2
        if in_detour:
            flags.append("detour")
        steps.append({
            "seq": len(steps) + 1, "road": road, "direction": None,
            "action": "ramp_exit" if is_exit else "continue",
            "exit": exit_no(tok), "miles": None, "cumulative_miles": None,
            "instruction": label, "route_label_raw": tok, "flags": flags,
        })
        if is_exit and tok.upper() == "EXIT 17":
            in_detour = False  # rejoins the interstate
    return steps, origin, dest


# --------------------------------------------------- permit-level fields
def sect(text, start, ends):
    if start not in text:
        return ""
    s = text.split(start, 1)[1]
    cut = len(s)
    for e in ends:
        i = s.find(e)
        if i != -1:
            cut = min(cut, i)
    return s[:cut]


def tag_restriction(txt):
    t = txt.lower()
    tags = []
    if re.search(r"escort", t):
        tags.append("escort")
    if re.search(r"daylight|sunrise|sunset", t):
        tags.append("daylight_only")
    if re.search(r"\d{1,2}:\d{2}\s*(am|pm)|\d\s*(am|pm)|a\.m\.|p\.m\.|curfew", t):
        tags.append("time_window")
    if re.search(r"holiday|sunday|weekend|saturday", t):
        tags.append("day_restriction")
    if re.search(r"closure|closed", t):
        tags.append("closure")
    if re.search(r"detour|bypass", t):
        tags.append("detour")
    if re.search(r"weather|fog|rain|wind", t):
        tags.append("weather")
    return tags or ["other"]


def bullets(block):
    items, cur = [], []
    for line in block.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^[•\-\*]\s*", s) or re.match(r"^\*\*", s):
            if cur:
                items.append(" ".join(cur))
            cur = [re.sub(r"^[•\-\*]+\s*", "", s)]
        else:
            cur.append(s)
    if cur:
        items.append(" ".join(cur))
    return [re.sub(r"\s+", " ", i).strip("* ").strip() for i in items if len(i) > 8]


def latlon(txt):
    m = re.search(r"\[(-?\d+\.\d+),\s*(-?\d+\.\d+)\]", txt or "")
    return [float(m.group(1)), float(m.group(2))] if m else None


class UnknownLayout(Exception):
    """The PDF does not match any layout this module knows."""


def parse_pdf(f):
    """f: path or binary file object.  Returns (state, result) or raises UnknownLayout."""
    pdf = pdfplumber.open(f)
    text = page_lines(pdf)
    flat = text.replace(MINUS, "-")
    if "OF ALABAMA" in flat.upper():
        state = "AL"
    elif "Louisiana Department" in flat:
        state = "LA"
    elif "STATE OF NEW MEXICO" in flat.upper():
        state = "NM"
    elif "Texas Oversize" in flat:
        state = "TX"
    elif "GEORGIA SINGLE TRIP" in flat:
        state = "GA"
    elif "SOUTH CAROLINA" in flat:
        state = "SC"
    elif "Mississippi" in flat or "MDOT" in flat.upper() or "goMDOT" in flat:
        state = "MS"
    else:
        raise UnknownLayout("no known state layout matched")

    total_declared = None
    if state in ("MS", "TX", "GA", "NM"):
        steps, origin, dest = parse_table_layout(pdf, state)
        mo = re.search(r"^Origin:\s*(.*?)(?:\s+Destination:.*)?$", flat, re.M)
        md = re.search(r"^Final Destination:\s*(.+?)(?:\s+[\d.]+)?(?:\s+\d\d:\d\d)?$", flat, re.M)
        origin = mo.group(1).strip() if mo else origin
        dest = md.group(1).strip() if md else dest
        layout = "table_miles_route_to_distance"
    elif state == "SC":
        steps, origin, dest = parse_sc(text)
        layout = "instruction_miles_list"
    elif state == "LA":
        steps, origin, dest, total_declared = parse_la(text)
        layout = "driving_instructions_miles_first"
    else:
        steps, origin, dest = parse_al(text)
        layout = "road_list_no_distances"

    # permit-level fields
    num = {
        "AL": r"PERMIT NO\.\s*\*?\s*(\d+)", "LA": r"Permit Number:\s*([\d-]+)",
        "NM": r"Permit Number:\s*(\w+)", "TX": r"Permit Number:\s*(\d+)",
        "GA": r"PERMIT # (\w+)", "SC": r"PERMIT #:\s*(\w+)", "MS": r"Permit #\s*(\d+)",
    }[state]
    pn = re.search(num, flat).group(1)
    dates = {
        "AL": r"Valid\s+(\d\d/\d\d/\d{4})\s*-\s*(\d\d/\d\d/\d{4})",
        "LA": r"Start Date:\s*(\S+)\s+End Date:\s*(\S+)",
        "NM": r"Date Effective:\s*(\S+)\s.*?Date Expires:\s*(\S+)",
        "TX": r"Effective:\s*(\S+)\s.*?Expiration:\s*(\S+)",
        "GA": r"Valid:\s*(\S+) to (\S+)", "SC": r"Valid From:\s*(\S+)\s+To:\s*(\S+)",
        "MS": r"Valid From:\s*(\S+)\s+To:\s*(\S+)",
    }[state]
    d = re.search(dates, flat, re.S)

    def dim(label_patterns):
        for p in label_patterns:
            m = re.search(p, flat)
            if m:
                return m.group(1)
        return None

    dims = {
        "width": dim([r"Overall Width:\s*(\d+'\s*\d*\"?)", r"Loaded Width:\s*(\d+'\s*\d*\"?)", r"Max\. Width:\s*(\d+'\s*\d*)", r"WIDTH\s+(\d+'\s*\d*\"?)", r"Width:\s*(\d+['\-]\s*\d*\"?)"]),
        "height": dim([r"Overall Height:\s*(\d+'\s*\d*\"?)", r"Loaded Height:\s*(\d+'\s*\d*\"?)", r"Max Height:\s*(\d+'\s*\d*)", r"HEIGHT\s+(\d+'\s*\d*\"?)", r"Height:\s*(\d+['\-]\s*\d*\"?)"]),
        "length": dim([r"Overall Length:\s*(\d+'\s*\d*\"?)", r"Loaded Length:\s*(\d+'\s*\d*\"?)", r"Max\. Length:\s*(\d+'\s*\d*)", r"LENGTH\s+(\d+'\s*\d*\"?)", r"(?<!Trailer )(?<!Load )Length:\s*(\d+['\-]\s*\d*\"?)"]),
        "gross_weight_lbs": dim([r"Gross Vehicle Weight \(lbs\):\s*([\d,]+)", r"Gross Weight:\s*([\d,]+)", r"WEIGHT\(LBS\)\s+(\d+)", r"Loaded Weight:\s*([\d,]+)", r"GVW:\s*([\d,]+)", r"Weight:\s+([\d,]+)\s+Axles"]),
    }
    dims = {k: (v.strip() if isinstance(v, str) else v) for k, v in dims.items()}
    dims["gross_weight_lbs"] = int(dims["gross_weight_lbs"].replace(",", "")) if dims["gross_weight_lbs"] else None

    # restrictions: only the permit-specific blocks, not the boilerplate
    if state == "AL":
        blk = sect(flat, "SPECIAL REQUIREMENTS", ["THIS IS AN ORIGINAL"])
        items = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\.,\s*", blk) if x.strip()]
    elif state == "LA":
        blk = sect(flat, "Permit Restrictions", ["Standard Restrictions"])
        items = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n(?=\s*W\s)", blk) if len(x.strip()) > 20 and "Severity" not in x[:12]]
        items += ["Hours of operation are from sunrise to sunset.", "Louisiana permitted Civilian Escort required", "All permit loads are not to exceed 55 MPH."]
    elif state == "MS":
        items, buf = [], None
        for ln in sect(flat, "Special Restrictions", ["ROUTING AND SPECIAL"]).splitlines():
            ln = ln.strip()
            if not ln:
                continue
            if buf is not None:
                buf += " " + ln
                if ln.endswith("**"):
                    items.append(buf.strip("* ")); buf = None
            elif ln.startswith("**") and not ln.endswith("**"):
                buf = ln
            else:
                items.append(ln.strip("* "))
    elif state == "TX":
        items = [re.sub(r"\s+", " ", x).strip("* ").strip() for x in re.split(r"\*\*\s*", sect(flat, "Route Conditions:", ["General Conditions"])) if len(x.strip()) > 8]
    elif state == "GA":
        items = bullets(sect(flat, "SPECIAL CIRCUMSTANCES", ["APP#"]))
        for pat in (r"A vehicle Rear Escort[^\n]*", r"Travel allowed on all authorized[^\n]*\n[^\n]*", r"No permitted load is authorized to travel during inclement weather[^\n]*"):
            mm = re.search(pat, flat)
            if mm:
                items.append(re.sub(r"\s+", " ", mm.group(0)))
    elif state == "SC":
        items = bullets(sect(flat, "PERMIT RESTRICTIONS", ["GENERAL CONDITIONS"]))
    else:  # NM: numbered conditions; keep the movement-limiting ones
        items = ["Daylight movement only, 7-day permit; no travel on national holidays or in inclement weather (wind >= 25 mph, visibility < 1000 ft).",
                 "Maximum speed 65 mph."]
    restrictions = [{"text": i, "tags": tag_restriction(i)} for i in items]

    cum_final = max((s["cumulative_miles"] for s in steps if s.get("cumulative_miles") is not None), default=None)
    miles_sum = round(sum(s["miles"] for s in steps if s["miles"] is not None), 2) if any(s["miles"] is not None for s in steps) else None
    cum_check = None
    if state in ("MS", "TX", "GA", "NM") and cum_final is not None:
        # permit's own running total vs the sum of the Miles column (cumulative column is authoritative)
        cum_check = {"sum_of_miles": miles_sum, "final_cumulative": cum_final,
                     "ok": abs(miles_sum - cum_final) <= 0.1 * max(1, len(steps)) * 0.1 + 0.5}
    elif state == "LA":
        cum_check = {"sum_of_miles": miles_sum, "declared_total": total_declared, "ok": abs(miles_sum - total_declared) <= 0.2}
    elif state == "SC":
        cum_check = {"sum_of_miles": miles_sum, "declared_total": None, "ok": None}

    return state, {
        "permit": {
            "state": state, "number": pn,
            "valid_from": d.group(1) if d else None, "valid_to": d.group(2) if d else None,
            "usdot": "3435067",
            "dimensions_raw": {k: v for k, v in dims.items()},
        },
        "route": {
            "origin_raw": origin, "destination_raw": dest,
            "origin_latlon": latlon(origin), "destination_latlon": latlon(dest),
            "step_count": len(steps), "steps": steps,
            "roads_in_order": [s["road"] for s in steps if s["road"] and "ramp" not in s["flags"]],
        },
        "restrictions": restrictions,
        "extraction": {"layout": layout, "checks": cum_check,
                       "has_distances": any(s["miles"] is not None for s in steps),
                       "has_coordinates": bool(re.search(r"\[-?\d+\.\d+,-?\d+\.\d+\]", flat))},
    }

