"""Consolidated multi-year, multi-source build for the County Hazard Atlas (v2).

Harvests NOAA Storm Events, NRC incident reports, and FEMA disaster declarations
for a range of years, resolves every record to a 5-digit county FIPS, and
aggregates to monthly per-county counts.

Fixes applied vs v1:
  * FEMA: county-unique key (fipsState+fipsCounty), so per-county designations
    survive instead of collapsing to one row per disaster.
  * NRC: county-name + state -> FIPS via a Census gazetteer + alias table.
  * NOAA forecast zones (CZ_TYPE=Z): mapped to member counties via the NWS
    zone->county correlation file (each member county gets the event).

Output: analytics/viz_data.json  (consumed by atlas_template.html)
"""
from __future__ import annotations
import csv, gzip, json, os, re, sys, time, collections, urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from event_harvester.connectors.noaa import download_noaa_year, parse_noaa_datetime
from event_harvester.connectors.nrc import bulk_events as nrc_bulk_events
from event_harvester.connectors.fema import fetch_raw_fema_data, normalize_incident_type

CACHE = Path(os.environ.get("ATLAS_CACHE", "/tmp"))
GEOJSON = CACHE / "us_counties.geojson"
ZONE_DBX = CACHE / "zone_county.dbx"
GEOJSON_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
ZONE_DBX_URL = "https://www.weather.gov/source/gis/Shapefiles/County/bp18mr25.dbx"
OUT = Path(os.environ.get("ATLAS_VIZ_OUT", str(ROOT / "analytics/viz_data.json")))
TEMPLATE = os.environ.get("ATLAS_TEMPLATE")     # optional: inject data into this template file
INDEX_OUT = os.environ.get("ATLAS_INDEX_OUT")   # optional: write the injected page here


def ensure_reference():
    CACHE.mkdir(parents=True, exist_ok=True)
    if not GEOJSON.exists():
        print("fetching county geojson ...")
        urllib.request.urlretrieve(GEOJSON_URL, GEOJSON)
    if not ZONE_DBX.exists():
        try:
            print("fetching NWS zone-county crosswalk ...")
            req = urllib.request.Request(ZONE_DBX_URL, headers={"User-Agent": "atlas/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                ZONE_DBX.write_bytes(r.read())
        except Exception as e:
            print(f"  zone crosswalk fetch failed ({repr(e)[:80]}); forecast-zone events will be dropped")


def iter_rings(geom):
    if geom["type"] == "Polygon":
        yield from geom["coordinates"]
    elif geom["type"] == "MultiPolygon":
        for poly in geom["coordinates"]:
            yield from poly


def adjacency(gj, keep):
    """Queen contiguity (shared boundary points) among kept counties."""
    pt2 = collections.defaultdict(set)
    for feat in gj["features"]:
        fips = feat["id"]
        if fips not in keep:
            continue
        for ring in iter_rings(feat["geometry"]):
            for x, y in ring:
                pt2[(round(x, 4), round(y, 4))].add(fips)
    adj = collections.defaultdict(set)
    for fs in pt2.values():
        if len(fs) > 1:
            fl = list(fs)
            for i in range(len(fl)):
                for j in range(i + 1, len(fl)):
                    adj[fl[i]].add(fl[j]); adj[fl[j]].add(fl[i])
    return {f: sorted(v) for f, v in adj.items()}

START_YEAR = int(os.environ.get("ATLAS_START_YEAR", "2000"))
END_YEAR = int(os.environ.get("ATLAS_END_YEAR", str(date.today().year)))
YEARS = list(range(START_YEAR, END_YEAR + 1))   # 2000 .. current year (recent years may be partial)
BASE_MONTH = YEARS[0] * 12          # global month index origin
N_MONTHS = len(YEARS) * 12

STFIPS = {"01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE",
"11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS",
"21":"KY","22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO",
"30":"MT","31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND",
"39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN","48":"TX",
"49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY"}
ABBR2FIPS = {v: k for k, v in STFIPS.items()}

# NRC/FEMA county-name aliases: (state_abbr, RAW_NAME_UPPER) -> 5-digit FIPS
NRC_ALIAS = {
    ("FL","DADE"): "12086", ("NY","NEW YORK (MANHATTAN)"): "36061",
    ("NY","KINGS(BROOKLYN)"): "36047", ("NY","KINGS (BROOKLYN)"): "36047",
    ("NY","BRONX"): "36005", ("NY","QUEENS"): "36081", ("NY","RICHMOND (STATEN ISLAND)"): "36085",
    ("LA","E. BATON ROUGE"): "22033", ("LA","W. BATON ROUGE"): "22121",
    ("LA","ST. JOHN THE BAPTIST"): "22095", ("VA","VIRGINIA BEACH"): "51810",
}


def norm_county(s: str) -> str:
    s = (s or "").upper().strip()
    s = re.sub(r"\b(COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPIO|CITY AND BOROUGH|CITY)\b", "", s)
    s = s.replace(".", "").replace("'", "")
    s = re.sub(r"^SAINT\b", "ST", s)
    s = s.replace("SAINTE", "STE")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("DE KALB", "DEKALB").replace("LA SALLE", "LASALLE").replace("DU PAGE", "DUPAGE")
    return s


def load_reference():
    gj = json.load(open(GEOJSON))
    gaz = {}                 # (abbr, normname) -> fips
    names = {}               # fips -> proper name
    geo_fips = set()
    for f in gj["features"]:
        fips = f["id"]; geo_fips.add(fips)
        abbr = STFIPS.get(fips[:2])
        nm = f["properties"]["NAME"]
        names[fips] = nm
        if abbr:
            gaz[(abbr, norm_county(nm))] = fips

    zone_map = collections.defaultdict(set)   # (state_fips, zone_int) -> {fips}
    for line in (open(ZONE_DBX, encoding="utf-8", errors="replace") if ZONE_DBX.exists() else []):
        p = line.rstrip("\n").split("|")
        if len(p) < 7:
            continue
        abbr = p[0].strip()
        try:
            zone = int(p[1])
        except ValueError:
            continue
        cf = p[6].strip()
        sf = ABBR2FIPS.get(abbr)
        if sf and len(cf) == 5:
            zone_map[(sf, zone)].add(cf)
    return gj, gaz, names, geo_fips, zone_map


def month_index(d: date) -> int | None:
    idx = d.year * 12 + (d.month - 1) - BASE_MONTH
    return idx if 0 <= idx < N_MONTHS else None


# counts[type_id] = { fips: {monthIdx: count} } ; meta[type_id] = (display, source)
counts: dict = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(int)))
meta: dict = {}
diag = collections.Counter()


def add(type_id, display, source, fips, d: date):
    mi = month_index(d)
    if mi is None:
        diag["date_out_of_range"] += 1
        return
    counts[type_id][fips][mi] += 1
    meta[type_id] = (display, source)
    diag[f"kept_{source}"] += 1


def harvest_noaa(year, geo_fips, zone_map):
    gz, csvp = download_noaa_year(year, ROOT / "data/raw/noaa")
    with open(csvp, "r", newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            etype = re.sub(r"[^a-z0-9]+", "_", (row.get("EVENT_TYPE") or "").strip().lower()).strip("_")
            if not etype:
                continue
            d = parse_noaa_datetime(row.get("BEGIN_DATE_TIME"))
            if d is None:
                continue
            d = d.date()
            sf = (row.get("STATE_FIPS") or "").strip()
            cz = (row.get("CZ_FIPS") or "").strip()
            if not sf or not cz:
                continue
            sf2 = f"{int(sf):02d}"
            tid = "noaa_" + etype
            disp = etype.replace("_", " ")
            if (row.get("CZ_TYPE") or "").strip().upper() == "C":
                fips = sf2 + f"{int(cz):03d}"
                if fips in geo_fips:
                    add(tid, disp, "NOAA", fips, d)
                else:
                    diag["noaa_county_nomatch"] += 1
            else:  # zone
                members = zone_map.get((sf2, int(cz))) if cz.isdigit() else None
                if members:
                    for fips in members:
                        if fips in geo_fips:
                            add(tid, disp, "NOAA", fips, d)
                else:
                    diag["noaa_zone_nomatch"] += 1


def harvest_nrc(year, gaz, geo_fips):
    raw_dir = ROOT / "data/raw/nrc/bulk" / str(year)
    for ev in nrc_bulk_events(year, raw_dir=raw_dir):
        abbr = (ev.state or "").upper().strip()
        cty = ev.county
        if not abbr or not cty:
            diag["nrc_no_geo"] += 1
            continue
        key = (abbr, norm_county(cty))
        fips = gaz.get(key) or NRC_ALIAS.get((abbr, (cty or "").upper().strip()))
        if not fips or fips not in geo_fips:
            diag["nrc_nomatch"] += 1
            continue
        d = ev.event_start
        d = d.date() if hasattr(d, "date") else d
        if not isinstance(d, date):
            continue
        add("nrc_" + ev.event_type, ev.event_type.replace("_", " "), "NRC", fips, d)


def harvest_fema(geo_fips):
    recs = fetch_raw_fema_data(date(YEARS[0], 1, 1), date(YEARS[-1] + 1, 1, 1))
    for r in recs:
        sf = r.get("fipsStateCode"); cf = r.get("fipsCountyCode")
        if not sf or not cf:
            diag["fema_no_fips"] += 1
            continue
        fips = f"{int(sf):02d}{int(cf):03d}"
        if fips not in geo_fips:
            diag["fema_nomatch"] += 1
            continue
        etype = normalize_incident_type(r.get("incidentType"))
        raw_d = r.get("incidentBeginDate") or r.get("declarationDate")
        if not raw_d:
            continue
        try:
            d = datetime.fromisoformat(str(raw_d).replace("Z", "+00:00")).date()
        except ValueError:
            continue
        add("fema_" + etype, etype.replace("_", " "), "FEMA", fips, d)


def build_paths(gj, geo_fips):
    """Composite Albers-USA -> SVG paths (pure Python)."""
    import math
    W, H = 975, 610
    def albers(lon, lat, lon0, p1, p2):
        r = math.radians
        phi1, phi2, phi = r(p1), r(p2), r(lat); lam = r(lon - lon0)
        n = (math.sin(phi1) + math.sin(phi2)) / 2
        C = math.cos(phi1) ** 2 + 2 * n * math.sin(phi1)
        phi0 = r((p1 + p2) / 2)
        rho0 = math.sqrt(C - 2 * n * math.sin(phi0)) / n
        rho = math.sqrt(C - 2 * n * math.sin(phi)) / n
        th = n * lam
        return rho * math.sin(th), rho0 - rho * math.cos(th)
    PARAMS = {"CONUS": (-96, 29.5, 45.5), "AK": (-152, 55, 65), "HI": (-157, 8, 18)}
    def region(fips):
        if fips.startswith("02"): return "AK"
        if fips.startswith("15"): return "HI"
        if fips.startswith(("72", "78", "60", "66", "69")): return None
        return "CONUS"
    def rings(g):
        if g["type"] == "Polygon":
            yield from g["coordinates"]
        elif g["type"] == "MultiPolygon":
            for poly in g["coordinates"]:
                yield from poly
    proj = {}; reg_fips = {"CONUS": [], "AK": [], "HI": []}
    for feat in gj["features"]:
        fips = feat["id"]; reg = region(fips)
        if reg is None: continue
        lon0, p1, p2 = PARAMS[reg]
        proj[fips] = (reg, [[albers(x, y, lon0, p1, p2) for x, y in ring] for ring in rings(feat["geometry"])])
        reg_fips[reg].append(fips)
    boxes = {"CONUS": (8, 8, 967, 585), "AK": (18, 372, 232, 600), "HI": (250, 470, 350, 585)}
    def fitter(fl, box):
        xs = [x for f in fl for ring in proj[f][1] for x, y in ring]
        ys = [y for f in fl for ring in proj[f][1] for x, y in ring]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        x0, y0, x1, y1 = box
        s = min((x1 - x0) / (maxx - minx), (y1 - y0) / (maxy - miny))
        ox = x0 + ((x1 - x0) - s * (maxx - minx)) / 2
        oy = y0 + ((y1 - y0) - s * (maxy - miny)) / 2
        return lambda x, y: (ox + (x - minx) * s, oy + (maxy - y) * s)
    tfs = {r: fitter(reg_fips[r], boxes[r]) for r in reg_fips}
    out = {}
    for fips, (reg, rgs) in proj.items():
        tf = tfs[reg]; parts = []
        for ring in rgs:
            pts = []; last = None
            for x, y in ring:
                X, Y = tf(x, y); X = round(X, 1); Y = round(Y, 1)
                if last and abs(X - last[0]) < 0.4 and abs(Y - last[1]) < 0.4:
                    continue
                pts.append((X, Y)); last = (X, Y)
            if len(pts) >= 3:
                parts.append("M" + "L".join(f"{X},{Y}" for X, Y in pts) + "Z")
        if parts:
            out[fips] = "".join(parts)
    return out, W, H


def main():
    ensure_reference()
    print("loading reference data ...")
    gj, gaz, names, geo_fips, zone_map = load_reference()

    for y in YEARS:
        t = time.time()
        try:
            harvest_noaa(y, geo_fips, zone_map)
        except Exception as e:
            diag[f"noaa_year_fail_{y}"] += 1
            print(f"  [NOAA {y}] SKIP: {repr(e)[:120]}")
        try:
            harvest_nrc(y, gaz, geo_fips)
        except Exception as e:
            diag[f"nrc_year_fail_{y}"] += 1
            print(f"  [NRC {y}] SKIP: {repr(e)[:120]}")
        print(f"  {y}: done ({time.time()-t:.1f}s)")
    t = time.time()
    try:
        harvest_fema(geo_fips)
        print(f"  FEMA {YEARS[0]}-{YEARS[-1]} done ({time.time()-t:.1f}s)")
    except Exception as e:
        print(f"  [FEMA] SKIP: {repr(e)[:120]}")

    print("projecting counties ...")
    paths, W, H = build_paths(gj, geo_fips)

    # --- flatten to compact format: interned FIPS + flat [idx, month, count] ---
    fips_universe = set(paths) | {f for per in counts.values() for f in per}
    fips_list = sorted(fips_universe)
    fidx = {f: i for i, f in enumerate(fips_list)}
    events = {}
    types = []
    for tid, per_fips in counts.items():
        disp, source = meta[tid]
        flat = []; total = 0
        for fips, months in per_fips.items():
            fi = fidx[fips]
            for mi, c in months.items():
                flat.append(fi); flat.append(mi); flat.append(c); total += c
        events[tid] = flat
        types.append({"id": tid, "name": disp, "source": source, "total": total})
    types.sort(key=lambda t: (t["source"], -t["total"]))

    counties = {f: {"n": names.get(f, ""), "d": paths[f]} for f in paths}
    month_labels = [[(BASE_MONTH + i) // 12, (BASE_MONTH + i) % 12 + 1] for i in range(N_MONTHS)]

    print("computing county adjacency ...")
    adj = adjacency(gj, set(paths))

    out = {
        "schema": "v3", "years": [YEARS[0], YEARS[-1]], "nMonths": N_MONTHS,
        "monthYM": month_labels, "viewW": W, "viewH": H,
        "fipsList": fips_list, "types": types, "counties": counties, "adj": adj, "events": events,
    }
    data_str = json.dumps(out, separators=(",", ":"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(data_str)
    print("\n--- diagnostics ---")
    for k, v in sorted(diag.items()):
        print(f"  {k}: {v}")
    n_rec = sum(len(a) for a in events.values()) // 3
    print(f"\ntypes: {len(types)}  | counties: {len(fips_list)}  | records: {n_rec}")
    print(f"wrote {OUT}  ({len(data_str)/1e6:.2f} MB)")

    if TEMPLATE and INDEX_OUT:
        tpl = Path(TEMPLATE).read_text()
        if "/*__DATA__*/" not in tpl:
            raise SystemExit("template missing /*__DATA__*/ marker")
        Path(INDEX_OUT).write_text(tpl.replace("/*__DATA__*/", data_str))
        print(f"wrote {INDEX_OUT}  ({Path(INDEX_OUT).stat().st_size/1e6:.2f} MB)")


if __name__ == "__main__":
    main()
