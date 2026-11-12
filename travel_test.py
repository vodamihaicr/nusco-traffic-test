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

FIELDS = ["timestamp_utc", "local_time", "weekday", "hour_slot", "origin", "route", "direction",
          "google_min", "google_no_traffic_min", "google_km",
          "waze_min", "waze_no_traffic_min", "waze_km", "errors"]
LEGACY_ORIGIN = "Nusco City faza 2"
DIR_MAP = {"Nusco -> destinatie": "dus", "destinatie -> Nusco": "intors"}


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


WAZE_HEADERS = {"User-Agent": UA, "Referer": "https://www.waze.com/live-map/",
                "Origin": "https://www.waze.com", "Accept": "application/json"}


def _waze_livemap(a, b):
    # API-ul folosit acum de harta live de pe waze.com
    res = http("https://www.waze.com/live-map/api/user-drive?geo_env=row",
               data={"from": {"y": a["lat"], "x": a["lng"]}, "to": {"y": b["lat"], "x": b["lng"]},
                     "nPaths": 1, "useCase": "LIVEMAP_PLANNING", "interval": 15, "arriveAt": False},
               headers={**WAZE_HEADERS, "Content-Type": "application/json"}, timeout=20)
    alts = res.get("alternatives") or []
    route = (alts[0] if alts else res).get("route") or {}
    secs = route.get("totalSeconds")
    if secs is None:
        raise RuntimeError(f"raspuns live-map neasteptat: {str(res)[:150]}")
    km = route.get("totalLength", 0) / 1000
    free = route.get("totalSecondsEmpty") or route.get("totalSecondsNoTraffic")
    return (round(secs / 60, 1), round(free / 60, 1) if free else None, round(km, 2))


def _waze_routing_manager(base):
    def call(a, b):
        params = {"from": f"x:{a['lng']} y:{a['lat']}", "to": f"x:{b['lng']} y:{b['lat']}",
                  "at": "0", "returnJSON": "true", "returnGeometries": "false",
                  "returnInstructions": "false", "timeout": "60000", "nPaths": "1",
                  "options": "AVOID_TRAILS:t"}
        res = http(base + "?" + urllib.parse.urlencode(params), headers=WAZE_HEADERS, timeout=20)
        resp = res.get("response") or (res.get("alternatives") or [{}])[0].get("response")
        if not resp or "results" not in resp:
            raise RuntimeError(f"raspuns neasteptat: {str(res)[:150]}")
        segs = resp["results"]
        rt = sum(x.get("crossTime", 0) for x in segs)
        nort = sum(x.get("crossTimeWithoutRealTime", x.get("crossTime", 0)) for x in segs)
        return round(rt / 60, 1), round(nort / 60, 1), round(sum(x.get("length", 0) for x in segs) / 1000, 2)
    return call


WAZE_ENDPOINTS = [
    ("live-map", _waze_livemap),
    ("routing-livemap-row", _waze_routing_manager("https://routing-livemap-row.waze.com/RoutingManager/routingRequest")),
    ("RoutingManager", _waze_routing_manager("https://www.waze.com/RoutingManager/routingRequest")),
    ("row-RoutingManager", _waze_routing_manager("https://www.waze.com/row-RoutingManager/routingRequest")),
]


class Waze:
    """Incearca adresele pe rand, o pastreaza pe prima care merge; daca nimic nu
    merge pe primele trasee, sare peste Waze in rularea curenta (nu blocheaza Google)."""

    def __init__(self):
        self.working = None
        self.failures = 0

    def route(self, a, b):
        if self.working is None and self.failures >= 2:
            raise RuntimeError("sarit: niciun endpoint Waze nu a raspuns in aceasta rulare")
        order = [e for e in WAZE_ENDPOINTS if e[0] == self.working] or WAZE_ENDPOINTS
        errs = []
        for name, fn in order:
            try:
                out = fn(a, b)
                self.working = name
                return out
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {type(e).__name__} {str(e)[:60]}")
        self.failures += 1
        raise RuntimeError(" | ".join(errs))


def attempt(fn, *args, tries=2):
    last = None
    for i in range(tries):
        try:
            return fn(*args), None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"[:300]
            if i < tries - 1:
                time.sleep(3 * (i + 1))
    return (None, None, None), last


def migrate_csv():
    """Fisierele vechi (doar Nusco) nu aveau coloana origin: le aduce la formatul nou."""
    if not CSV_PATH.exists():
        return
    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        header = rows[0].keys() if rows else []
    if "origin" in header:
        return
    for r in rows:
        r["origin"] = LEGACY_ORIGIN
        r["direction"] = DIR_MAP.get(r["direction"], r["direction"])
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        wr.writeheader()
        wr.writerows(rows)


def main():
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        sys.exit("Lipseste GOOGLE_MAPS_API_KEY")

    cfg = load_json(PLACES, {})
    now = dt.datetime.now(dt.timezone.utc)
    days = dt.timedelta(days=cfg.get("test_days", 7))

    state = load_json(STATE, {})
    if isinstance(state.get("started_utc"), str):  # format vechi
        state["started_utc"] = {LEGACY_ORIGIN: state["started_utc"]}
    starts = state.setdefault("started_utc", {})
    active = []
    for o in cfg["origins"]:
        starts.setdefault(o["name"], now.isoformat())
        if now <= dt.datetime.fromisoformat(starts[o["name"]]) + days:
            active.append(o)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    migrate_csv()
    if not active:
        print("Testul s-a incheiat pentru toate originile. Nimic de facut.")
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
    # Protectie la dubluri: daca intervalul de 30 min curent are deja o masuratoare
    # (de la cron-job.org sau de la programarea GitHub), nu mai scriem nimic.
    if CSV_PATH.exists():
        with CSV_PATH.open(encoding="utf-8") as f:
            deja = any(r["local_time"][:10] == local.strftime("%Y-%m-%d") and r["hour_slot"] == slot
                       for r in csv.DictReader(f))
        if deja:
            print(f"Intervalul {local:%Y-%m-%d} {slot} are deja masuratori. Nu scriu nimic.")
            return
    weekday = ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"][local.weekday()]

    waze = Waze()
    rows = []
    for o in active:
        for d in cfg["destinations"]:
            dest = cache[d["query"]]
            for direction, a, b in (("dus", o, dest), ("intors", dest, o)):
                g, ge = attempt(google_route, a, b, key)
                w, we = attempt(waze.route, a, b, tries=1)
                errs = "; ".join(f"{n}: {e}" for n, e in (("google", ge), ("waze", we)) if e)
                rows.append(dict(zip(FIELDS, [
                    now.strftime("%Y-%m-%dT%H:%M:%SZ"), local.strftime("%Y-%m-%d %H:%M"),
                    weekday, slot, o["name"], d["name"], direction, *g, *w, errs])))
                print(f"{o['name'][:12]:<12} {d['name'][:30]:<30} {direction:<6} G={g[0]} W={w[0]} {errs[:200]}")
                time.sleep(0.5)
    if waze.working:
        print(f"Waze endpoint folosit: {waze.working}")

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            wr.writeheader()
        wr.writerows(rows)


if __name__ == "__main__":
    main()
