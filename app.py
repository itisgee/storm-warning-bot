import os
import re
import hmac
import math
import time
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape
from shapely.ops import transform, nearest_points
from pyproj import Transformer

app = Flask(__name__)
CORS(app)

# This stage retains the existing in-memory storage. It is not yet
# suitable for multiple Gunicorn workers or service restarts.
vehicle_location = {"lat": None, "lon": None, "updated": None}
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

# Stage 4: Isolated warning simulation
# /test/status.json?state=tornado&proximity=inside   (needs X-GPS-Token)
TEST_WARNINGS = {
    "normal": ("normal", ""),
    "severe": ("severe", "SEVERE THUNDERSTORM WARNING"),
    "tornado": ("tornado", "TORNADO WARNING"),
    "pds_tornado": ("pds_tornado", "PDS TORNADO WARNING"),
    "tornado_emergency": ("tornado_emergency", "TORNADO EMERGENCY"),
    "gps_unavailable": ("gps_unavailable", "GPS POSITION UNAVAILABLE"),
    "warning_data_unavailable": ("warning_data_unavailable", "WARNING DATA UNAVAILABLE"),
}


@app.route("/test/status.json", methods=["GET"])
def test_status():
    expected_token = os.environ.get("GPS_API_TOKEN")
    supplied_token = request.headers.get("X-GPS-Token", "")

    if not expected_token or not hmac.compare_digest(
        supplied_token, expected_token
    ):
        return jsonify({"error": "Unauthorized"}), 401

    selected = request.args.get("state", "normal")
    if selected not in TEST_WARNINGS:
        return jsonify({"error": "Unknown test state"}), 400
    proximity = request.args.get("proximity", "inside")
    if proximity not in ("inside", "nearby"):
        return jsonify({"error": "proximity must be inside or nearby"}), 400

    state, message = TEST_WARNINGS[selected]
    body = {
        "state": state,
        "text": message,
        "source": "simulation",
        "simulation": True,
        "alerts_checked": 0,
        **public_gps()
    }
    if state in SEVERITY_RANK:
        inside = proximity == "inside"
        body.update({
            "proximity": proximity,
            "inside_polygon": inside,
            "distance_miles": 0.0 if inside else 2.1,
            "direction": None if inside else "W",
            "warning_key": f"TEST.{state}",
            "area": "Test County, SD",
            "ends": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "detection": "RADAR INDICATED",
        })
    return jsonify(body)


def gps_status():
    timestamp = vehicle_location["updated"]
    if not timestamp or vehicle_location["lat"] is None or vehicle_location["lon"] is None:
        return "waiting", None
    try:
        updated = datetime.fromisoformat(timestamp)
        age = max(0, (datetime.now(timezone.utc) - updated).total_seconds())
    except (TypeError, ValueError):
        return "waiting", None
    if age <= 15:
        return "connected", round(age, 1)
    if age <= 60:
        return "stale", round(age, 1)
    return "disconnected", round(age, 1)


def public_gps():
    state, age = gps_status()
    return {"gps_status": state, "gps_age_seconds": age,
            "gps_last_updated": vehicle_location["updated"]}


@app.route("/")
def home():
    return "Storm warning bot is running"


@app.route("/tracker")
def tracker():
    return "GPS tracking is handled by the WeatherHolidays Windows GPSGate bridge."


@app.route("/location/update", methods=["POST"])
def location_update():
    expected_token = os.environ.get("GPS_API_TOKEN")
    supplied_token = request.headers.get("X-GPS-Token", "")
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
        if not (math.isfinite(lat) and math.isfinite(lon) and
                -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Invalid coordinates"}), 400
    vehicle_location.update(lat=lat, lon=lon, updated=datetime.now(timezone.utc).isoformat())
    return jsonify({"ok": True, **public_gps()})


@app.route("/location.json")
def location_json():
    # Public route intentionally does not expose precise coordinates.
    return jsonify(public_gps())


# ---------------------------------------------------------------------------
# Team position relay (team laptop -> backend -> home GRLevel3 bridge)
#   POST /ingest              team laptop sends fixes here  (GPS_WRITE_TOKEN)
#   GET  /position/<source>   home bridge reads from here   (GPS_READ_TOKEN)
# Fixes from the vehicle named in OVERLAY_SOURCE (default team-van-1) also
# update vehicle_location, so the same feed drives the Larix overlay.
# Any other vehicle name only goes to the GRLevel3 relay.
# ---------------------------------------------------------------------------
team_positions = {}  # source id -> latest fix


def bearer_token_ok(env_name):
    expected = os.environ.get(env_name)
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


@app.route("/ingest", methods=["POST"])
def team_ingest():
    if not bearer_token_ok("GPS_WRITE_TOKEN"):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON"}), 400
    try:
        lat, lon = float(data["lat"]), float(data["lon"])
        fix_time = datetime.fromisoformat(data["fix_time"])
        if not (math.isfinite(lat) and math.isfinite(lon) and
                -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid coordinates")
        if fix_time.tzinfo is None:
            raise ValueError("fix_time needs a timezone")
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Invalid data"}), 400

    if fix_time > datetime.now(timezone.utc) + timedelta(seconds=60):
        return jsonify({"error": "fix_time is in the future"}), 400

    source = str(data.get("source", "default"))[:40]
    previous = team_positions.get(source)
    if previous and fix_time <= previous["fix_time"]:
        return jsonify({"ok": True, "ignored": "older than current fix"})

    team_positions[source] = {
        "lat": lat,
        "lon": lon,
        "fix_time": fix_time,
        "speed_kn": data.get("speed_kn"),
        "course": data.get("course"),
        "hdop": data.get("hdop"),
        "sats": data.get("sats"),
        "alt_m": data.get("alt_m"),
    }

    # Feed the overlay too, but only from the streaming vehicle
    if source == os.environ.get("OVERLAY_SOURCE", "team-van-1"):
        vehicle_location.update(lat=lat, lon=lon, updated=fix_time.isoformat())

    return jsonify({"ok": True})


@app.route("/position/<source>", methods=["GET"])
def team_position(source):
    if not bearer_token_ok("GPS_READ_TOKEN"):
        return jsonify({"error": "Unauthorized"}), 401
    p = team_positions.get(source)
    if not p:
        resp = jsonify({"available": False})
    else:
        age = (datetime.now(timezone.utc) - p["fix_time"]).total_seconds()
        resp = jsonify({"available": True, "age_seconds": round(age, 1),
                        **p, "fix_time": p["fix_time"].isoformat()})
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Warning detection
# ---------------------------------------------------------------------------
NEARBY_MILES = float(os.environ.get("NEARBY_MILES", "5"))
NEARBY_EXIT_MILES = NEARBY_MILES + 0.5   # hysteresis: must get further away to clear "nearby"
INSIDE_EXIT_MILES = 0.3                   # hysteresis: must be this far outside to clear "inside"
NWS_CACHE_SECONDS = 30
NWS_MAX_AGE_SECONDS = 180                 # older than this = warning data unavailable
NWS_RETRY_SECONDS = 10
NWS_CONTACT = os.environ.get("NWS_CONTACT", "weatherholidays.com")
NWS_HEADERS = {
    "User-Agent": f"(WeatherHolidays storm overlay, {NWS_CONTACT})",
    "Accept": "application/geo+json",
}
WARNING_EVENTS = ("Tornado Warning", "Severe Thunderstorm Warning")
SEVERITY_RANK = {"severe": 1, "tornado": 2, "pds_tornado": 3, "tornado_emergency": 4}
STATE_TEXT = {
    "severe": "SEVERE THUNDERSTORM WARNING",
    "tornado": "TORNADO WARNING",
    "pds_tornado": "PDS TORNADO WARNING",
    "tornado_emergency": "TORNADO EMERGENCY",
}
VTEC_RE = re.compile(r"/[OTEX]\.([A-Z]{3})\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")
COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

_nws_cache = {"alerts": None, "fetched": 0.0, "last_attempt": 0.0}
_nws_lock = threading.Lock()
_last_proximity = {}  # warning key -> "inside" / "nearby" from the previous check


def get_active_warnings():
    """Returns (features, age_seconds) from a 30-second cache, or (None, None)."""
    now = time.time()
    with _nws_lock:
        cached = _nws_cache["alerts"]
        fresh = cached is not None and now - _nws_cache["fetched"] < NWS_CACHE_SECONDS
        backing_off = now - _nws_cache["last_attempt"] < NWS_RETRY_SECONDS
        if not fresh and not backing_off:
            _nws_cache["last_attempt"] = now
            try:
                features = []
                for event in WARNING_EVENTS:
                    r = requests.get(NWS_ALERTS_URL, headers=NWS_HEADERS,
                                     params={"event": event, "status": "actual"}, timeout=10)
                    r.raise_for_status()
                    features.extend(r.json().get("features") or [])
                _nws_cache.update(alerts=features, fetched=time.time())
            except Exception:
                app.logger.exception("NWS fetch failed, keeping previous data")
        if _nws_cache["alerts"] is None:
            return None, None
        return _nws_cache["alerts"], time.time() - _nws_cache["fetched"]


def parse_time(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def vtec_info(props):
    """(warning key that survives updates, still active?) from the VTEC codes."""
    codes = (props.get("parameters") or {}).get("VTEC") or []
    key, active = None, False
    for code in codes:
        m = VTEC_RE.search(code)
        if not m:
            continue
        action, office, phen, sig, etn = m.groups()
        key = key or f"{office}.{phen}.{sig}.{etn}"
        if action not in ("CAN", "EXP"):
            active = True
    if key is None:
        return props.get("id"), True
    return key, active


def classify(props):
    event = props.get("event")
    if event == "Severe Thunderstorm Warning":
        return "severe"
    if event != "Tornado Warning":
        return None
    params = props.get("parameters") or {}
    threat = " ".join(params.get("tornadoDamageThreat") or []).upper()
    text = " ".join(str(props.get(k) or "") for k in ("headline", "description")).upper()
    if "CATASTROPHIC" in threat or "TORNADO EMERGENCY" in text:
        return "tornado_emergency"
    if "CONSIDERABLE" in threat or "PARTICULARLY DANGEROUS SITUATION" in text:
        return "pds_tornado"
    return "tornado"


def compass(dx, dy):
    bearing = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return COMPASS[int((bearing + 22.5) // 45) % 8]


def find_relevant_warning(features, lat, lon):
    """Most important warning we're inside, else the most important one nearby."""
    global _last_proximity
    # Distances measured in a projection centred on the vehicle, so miles are true miles
    to_local = Transformer.from_crs(
        "EPSG:4326", f"+proj=aeqd +lat_0={lat} +lon_0={lon} +units=m +datum=WGS84",
        always_xy=True)
    vehicle = Point(0, 0)
    now = datetime.now(timezone.utc)
    candidates = {}

    for feature in features:
        props = feature.get("properties") or {}
        if props.get("status") != "Actual" or props.get("messageType") == "Cancel":
            continue
        state = classify(props)
        if not state:
            continue
        key, active = vtec_info(props)
        if not active:
            continue
        ends = parse_time(props.get("ends")) or parse_time(props.get("expires"))
        if ends and ends < now:
            continue
        geometry = feature.get("geometry")
        if not geometry:
            continue
        try:
            polygon = transform(to_local.transform, shape(geometry))
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.is_empty:
                continue
            miles = polygon.distance(vehicle) / 1609.344
        except Exception:
            app.logger.warning("Skipping warning with bad geometry: %s", props.get("id"))
            continue

        previous = _last_proximity.get(key)
        inside = miles == 0 or (previous == "inside" and miles <= INSIDE_EXIT_MILES)
        limit = NEARBY_EXIT_MILES if previous else NEARBY_MILES
        if not inside and miles > limit:
            continue

        direction = None
        if not inside:
            edge, _ = nearest_points(polygon, vehicle)
            direction = compass(edge.x, edge.y)

        candidate = {
            "state": state,
            "proximity": "inside" if inside else "nearby",
            "inside_polygon": inside,
            "distance_miles": 0.0 if inside else round(miles, 1),
            "direction": direction,
            "warning_key": key,
            "area": props.get("areaDesc"),
            "ends": ends.isoformat() if ends else None,
            "detection": " ".join(
                (props.get("parameters") or {}).get("tornadoDetection") or []) or None,
            "_sent": props.get("sent") or "",
            "_miles": miles,
        }
        # NWS sometimes lists the same warning twice; keep the newest message
        existing = candidates.get(key)
        if existing is None or candidate["_sent"] > existing["_sent"]:
            candidates[key] = candidate

    _last_proximity = {k: c["proximity"] for k, c in candidates.items()}
    if not candidates:
        return None
    best = max(candidates.values(), key=lambda c: (
        c["inside_polygon"], SEVERITY_RANK[c["state"]], -c["_miles"]))
    return {k: v for k, v in best.items() if not k.startswith("_")}


@app.route("/status.json")
def status():
    gps = public_gps()
    if gps["gps_status"] != "connected":
        # Never treat an old or unknown position as "no warning"
        return jsonify({"state": "gps_unavailable", "text": "GPS POSITION UNAVAILABLE",
                        "source": "live_gps_nws_polygon", "alerts_checked": 0, **gps})

    features, age = get_active_warnings()
    if features is None or age > NWS_MAX_AGE_SECONDS:
        return jsonify({"state": "warning_data_unavailable", "text": "WARNING DATA UNAVAILABLE",
                        "source": "live_gps_nws_polygon", "alerts_checked": 0,
                        "warnings_age_seconds": None if age is None else round(age, 1), **gps})

    base = {"source": "live_gps_nws_polygon", "alerts_checked": len(features),
            "warnings_age_seconds": round(age, 1),
            "warnings_stale": age > NWS_CACHE_SECONDS * 2, **gps}
    try:
        match = find_relevant_warning(features, vehicle_location["lat"], vehicle_location["lon"])
    except Exception:
        app.logger.exception("Warning check failed")
        return jsonify({"state": "warning_data_unavailable", "text": "WARNING DATA UNAVAILABLE",
                        **base})

    if not match:
        return jsonify({"state": "normal", "text": "", "proximity": None, **base})
    return jsonify({**match, "text": STATE_TEXT[match["state"]], **base})


if __name__ == "__main__":
    app.run(debug=True)
