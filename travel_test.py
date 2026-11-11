"""Masoara timpii de condus in timp real (Google Maps + Waze) intre Nusco City faza 2
si 8 destinatii din Bucuresti, in ambele sensuri. Ruleaza o data per apel; e pornit
la fiecare 30 de minute de GitHub Actions. Doar biblioteca standard Python."""

import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PLACES = ROOT / "places.json"
GEOCACHE = ROOT / "geocoded.json"
STATE = ROOT / "state.json"
CSV_PATH = ROOT / "data" / "measurements.csv"
TZ = ZoneInfo("Europe/Bucharest")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

FIELDS = ["timestamp_utc", "local_time", "weekday", "hour_slot", "route", "direction",
          "google_min", "google_no_traffic_min", "google_km",
          "waze_min", "waze_no_traffic_min", "waze_km", "errors"]


def http(url, data=None, headers=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def geocode(query, key):
    url = ("https://maps.googleapis.com/maps/api/geocode/json?"
           + urllib.parse.urlencode({"address": query, "region": "ro", "key": key}))
    res = http(url)
    if res.get("status") != "OK":
        raise RuntimeError(f"Geocoding esuat pentru '{query}': {res.get('status')} "
                           f"{res.get('error_message', '')}")
    loc = res["results"][0]["geometry"]["location"]
    return {"lat": loc["lat"], "lng": loc["lng"],
            "resolved": res["results"][0].get("formatted_address", "")}


def google_route(a, b, key):
    res = http(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data={
            "origin": {"location": {"latLng": {"latitude": a["lat"], "longitude": a["lng"]}}},
            "destination": {"location": {"latLng": {"latitude": b["lat"], "longitude": b["lng"]}}},
            "travelMode": "DRIVE",
            # Cel mai apropiat de ce arata aplicatia Google Maps, cu traficul de acum.
            "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
            "languageCode": "ro",
            "units": "METRIC",
        },
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "routes.duration,routes.staticDuration,routes.distanceMeters"},
    )
    r = res["routes"][0]
    secs = lambda s: float(s.rstrip("s"))
    return (round(secs(r["duration"]) / 60, 1),
            round(secs(r.get("staticDuration", r["duration"])) / 60, 1),
            round(r.get("distanceMeters", 0) / 1000, 2))


def waze_route(a, b):
    # Serviciul de rutare folosit de harta web Waze (fara API oficial).
    params = {
        "from": f"x:{a['lng']} y:{a['lat']}",
        "to": f"x:{b['lng']} y:{b['lat']}",
        "at": "0",  # plecare acum
        "returnJSON": "true", "returnGeometries": "false", "returnInstructions": "false",
        "timeout": "60000", "nPaths": "1", "options": "AVOID_TRAILS:t",
    }
    url = "https://www.waze.com/row-RoutingManager/routingRequest?" + urllib.parse.urlencode(params)
    res = http(url, headers={"User-Agent": UA, "Referer": "https://www.waze.com/live-map/",
                             "Accept": "application/json"})
    resp = res.get("response") or (res.get("alternatives") or [{}])[0].get("response")
    if not resp or "results" not in resp:
        raise RuntimeError(f"Raspuns Waze neasteptat: {str(res)[:200]}")
    segs = resp["results"]
    rt = sum(s.get("crossTime", 0) for s in segs)
    nort = sum(s.get("crossTimeWithoutRealTime", s.get("crossTime", 0)) for s in segs)
    km = sum(s.get("length", 0) for s in segs) / 1000
    return round(rt / 60, 1), round(nort / 60, 1), round(km, 2)


def attempt(fn, *args, tries=2):
    last = None
    for i in range(tries):
        try:
            return fn(*args), None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:160]
            time.sleep(3 * (i + 1))
    return (None, None, None), last


def main():
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        sys.exit("Lipseste GOOGLE_MAPS_API_KEY")

    cfg = load_json(PLACES, {})
    origin = cfg["origin"]
    if origin.get("lat") is None or origin.get("lng") is None:
        sys.exit("Completeaza coordonatele Nusco City faza 2 in places.json")

    now = dt.datetime.now(dt.timezone.utc)
    state = load_json(STATE, {})
    if "started_utc" not in state:
        state["started_utc"] = now.isoformat()
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    end = dt.datetime.fromisoformat(state["started_utc"]) + dt.timedelta(days=cfg.get("test_days", 7))
    if now > end:
        print(f"Testul s-a incheiat la {end.astimezone(TZ):%Y-%m-%d %H:%M}. Nimic de facut.")
        return

    cache = load_json(GEOCACHE, {})
    changed = False
    for d in cfg["destinations"]:
        if d["query"] not in cache:
            cache[d["query"]] = geocode(d["query"], key)
            changed = True
    if changed:
        GEOCACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

    local = now.astimezone(TZ)
    slot = f"{local.hour:02d}:{'00' if local.minute < 30 else '30'}"
    weekday = ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"][local.weekday()]

    rows = []
    for d in cfg["destinations"]:
        dest = cache[d["query"]]
        for direction, a, b in (("Nusco -> destinatie", origin, dest),
                                ("destinatie -> Nusco", dest, origin)):
            g, ge = attempt(google_route, a, b, key)
            w, we = attempt(waze_route, a, b)
            errs = "; ".join(f"{n}: {e}" for n, e in (("google", ge), ("waze", we)) if e)
            rows.append(dict(zip(FIELDS, [
                now.strftime("%Y-%m-%dT%H:%M:%SZ"), local.strftime("%Y-%m-%d %H:%M"),
                weekday, slot, d["name"], direction, *g, *w, errs])))
            print(f"{d['name']:<48} {direction:<20} G={g[0]} W={w[0]} {errs}")
            time.sleep(1.5)

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            wr.writeheader()
        wr.writerows(rows)


if __name__ == "__main__":
    main()
