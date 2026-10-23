#!/usr/bin/env python3
"""
freeapi-health — assert what a free API *returns*, not that it answered.

A 200 OK with an empty list is a failure. An "updated" timestamp from last
Tuesday is a failure. A series of all nulls is a failure. A status-code check
calls all three healthy.

Stdlib only. No key, no install, no dependency.

    ./freeapi-health.py                    # run the built-in set
    ./freeapi-health.py --json             # machine-readable
    ./freeapi-health.py --spec mine.json   # check your own endpoints
    ./freeapi-health.py --only weather,iss

Write a spec where every entry says one thing that must be true of the payload
when the data is real:

    {"endpoints": [
      {"name": "my_api",
       "url": "https://example.com/api",
       "assert": {"min_items": 1, "path": "results"}},
      {"name": "my_series",
       "url": "https://example.com/series",
       "assert": {"required_paths": ["data.t", "data.v"],
                  "same_length": ["data.t", "data.v"],
                  "ranges": {"data.v": [-90, 60]},
                  "max_age_minutes": 120,
                  "timestamp_path": "data.t.0"}}
    ]}

Assertions available:
  path              where the payload lives (default: the whole body);
                    all other paths below are RELATIVE to it
  min_items         payload must have at least N items
  non_empty         payload must be a non-empty list/dict
  required_paths    dotted paths that must all exist and be non-null
  same_length       dotted paths whose arrays must have equal length
  ranges            {path: [lo, hi]} — every numeric value must be inside
  timestamp_path    dotted path to an ISO timestamp
  max_age_minutes   that timestamp must be newer than this
  equals            {path: value}
  min_count         {path: n} — a sub-array must have at least n entries

Exit code 0 if everything passes, 1 if anything fails.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 25
UA = "Mozilla/5.0 (X11; Linux x86_64) freeapi-health/1.0 (+payload assertions)"


def dig(obj, path):
    """Walk a dotted path, allowing integer indices. Returns (found, value)."""
    if not path:
        return True, obj
    cur = obj
    for part in path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False, None
        elif isinstance(cur, dict):
            if part not in cur:
                return False, None
            cur = cur[part]
        else:
            return False, None
    return True, cur


def parse_ts(value):
    """ISO-8601 strings, or epoch seconds/milliseconds (USGS uses millis)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        if v > 1e11:          # milliseconds
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        pass
    if value.isdigit():
        return parse_ts(int(value))
    return None


def check(ep):
    """Fetch one endpoint and run its assertions. Returns (state, ms, note)."""
    name = ep.get("name", ep["url"])
    started = time.time()
    req = urllib.request.Request(
        ep["url"],
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=ep.get("timeout", TIMEOUT)) as r:
            raw = r.read(2_000_000)
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return "broken", int((time.time() - started) * 1000), "HTTP %d" % e.code
    except Exception as e:
        kind = type(e).__name__
        # A timeout on a free API is not the same as a broken one.
        state = "slow" if "timeout" in kind.lower() or "timeout" in str(e).lower() else "broken"
        return state, int((time.time() - started) * 1000), kind

    ms = int((time.time() - started) * 1000)

    try:
        if "json" in ctype or raw[:1] in (b"{", b"["):
            payload = json.loads(raw)
        else:
            payload = raw.decode("utf-8", "replace")
    except Exception:
        return "broken", ms, "unparseable body"

    a = ep.get("assert", {})
    found, subject = dig(payload, a.get("path", ""))
    if not found:
        return "broken", ms, "missing path %s" % a.get("path")

    if "non_empty" in a:
        if not subject or (isinstance(subject, (list, dict)) and len(subject) == 0):
            return "broken", ms, "payload is empty"

    if "min_items" in a:
        try:
            n = len(subject)
        except TypeError:
            return "broken", ms, "payload is not a collection"
        if n < a["min_items"]:
            return "broken", ms, "only %d items, want >= %d" % (n, a["min_items"])

    def sub(p):
        """Resolve a dotted path relative to the `path` subject."""
        return dig(subject, p)

    for p in a.get("required_paths", []):
        ok, v = sub(p)
        if not ok or v is None:
            return "broken", ms, "required path missing/null: %s" % p

    for p in a.get("same_length", []):
        ok, v = sub(p)
        if not ok or not isinstance(v, list):
            return "broken", ms, "cannot measure length of %s" % p
        if len(v) != len(sub(a["same_length"][0])[1]):
            return "broken", ms, "arrays out of sync: %s" % p

    for p, (lo, hi) in a.get("ranges", {}).items():
        ok, v = sub(p)
        if not ok or not isinstance(v, list):
            return "broken", ms, "cannot range-check %s" % p
        for x in v:
            if x is None or not isinstance(x, (int, float)):
                return "broken", ms, "non-numeric value in %s" % p
            if not (lo <= x <= hi):
                return "broken", ms, "%s value %.3g outside [%g, %g]" % (p, x, lo, hi)

    for p, want in a.get("equals", {}).items():
        ok, v = sub(p)
        if not ok or v != want:
            return "broken", ms, "%s != %r" % (p, want)

    for p, n in a.get("min_count", {}).items():
        ok, v = sub(p)
        if not ok or not isinstance(v, list) or len(v) < n:
            return "broken", ms, "%s has fewer than %d entries" % (p, n)

    if "timestamp_path" in a and "max_age_minutes" in a:
        ok, v = sub(a["timestamp_path"])
        ts = parse_ts(v) if ok else None
        if ts is None:
            return "broken", ms, "no parseable timestamp at %s" % a["timestamp_path"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
        if age > a["max_age_minutes"]:
            return "broken", ms, "stale: %.0f min old" % age

    return "ok", ms, "%d items" % len(subject) if isinstance(subject, (list, dict)) else "ok"


def report(results):
    width = max(len(r["name"]) for r in results)
    print("%-*s  %-6s  %7s  %s" % (width, "endpoint", "state", "ms", "detail"))
    print("%s  %-6s  %7s  %s" % ("-" * width, "-" * 6, "-" * 7, "-" * 30))
    for r in results:
        print("%-*s  %-6s  %7d  %s" % (width, r["name"], r["state"], r["ms"], r["detail"]))
    ok = sum(1 for r in results if r["state"] == "ok")
    slow = sum(1 for r in results if r["state"] == "slow")
    print("\n%d/%d ok, %d slow, %d broken" % (ok, len(results), slow, len(results) - ok - slow))
    print("slow = timed out (retry later or use an alternate); broken = answered with an error.")


BUILTIN = {
    "endpoints": [
        {"name": "usgs_quakes", "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson",
         "assert": {"path": "features", "non_empty": True,
                    "timestamp_path": "0.properties.updated", "max_age_minutes": 180}},
        {"name": "open_meteo", "url": "https://api.open-meteo.com/v1/forecast?latitude=59.33&longitude=18.07&hourly=temperature_2m,cloud_cover",
         "assert": {"path": "hourly", "required_paths": ["temperature_2m", "cloud_cover"],
                    "same_length": ["temperature_2m", "cloud_cover"],
                    "min_count": {"temperature_2m": 24},
                    "ranges": {"temperature_2m": [-90, 60], "cloud_cover": [0, 100]}}},
        # CelesTrak returns 403 to this datacenter IP for every User-Agent I tried, so it
        # is not a fair entry for a green baseline. adsb.lol is the same open-data spirit.
        {"name": "adsb_aircraft", "url": "https://api.adsb.lol/v2/lat/59.33/lon/18.07/dist/50",
         "assert": {"path": "ac", "required_paths": ["0.hex"], "min_items": 1}},
        {"name": "citybikes", "url": "https://api.citybik.es/v2/networks",
         "assert": {"path": "networks", "min_items": 50, "required_paths": ["0.name", "0.location"]}},
        {"name": "radio_browser", "url": "https://de1.api.radio-browser.info/json/stations/search?name=bbc&limit=5",
         "assert": {"min_items": 1, "required_paths": ["0.name"]}},
        {"name": "worldbank", "url": "https://api.worldbank.org/v2/country/SWE/indicator/SP.POP.TOTL?format=json&per_page=5",
         "assert": {"min_items": 2, "required_paths": ["1.0.value"]}},
    ]
}


def main(argv):
    spec = BUILTIN
    only = None
    as_json = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            as_json = True
        elif arg == "--only" and i + 1 < len(argv):
            only = set(argv[i + 1].split(","))
            i += 1
        elif arg == "--spec" and i + 1 < len(argv):
            with open(argv[i + 1], encoding="utf-8") as f:
                spec = json.load(f)
            i += 1
        elif arg in ("-h", "--help"):
            print(__doc__)
            return 0
        i += 1

    eps = spec["endpoints"]
    if only:
        eps = [e for e in eps if e.get("name") in only]

    results = []
    for ep in eps:
        state, ms, note = check(ep)
        results.append({"name": ep.get("name", "?"), "state": state, "ms": ms, "detail": note})

    if as_json:
        print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "results": results}, indent=2))
    else:
        report(results)

    return 0 if all(r["state"] == "ok" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
