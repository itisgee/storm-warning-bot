import os
import hmac
import math
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape
from shapely.ops import transform
from pyproj import Transformer

app = Flask(__name__)
CORS(app)

# This stage retains the existing in-memory storage. It is not yet
# suitable for multiple Gunicorn workers or service restarts.
vehicle_location = {"lat": None, "lon": None, "updated": None}
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

# Stage 4: Isolated warning simulation
TEST_WARNINGS = {
    "normal": ("normal", ""),
    "severe": ("severe", "SEVERE THUNDERSTORM WARNING"),
    "tornado": ("tornado", "TORNADO WARNING ISSUED"),
    "pds_tornado": ("pds_tornado", "PDS TORNADO WARNING"),
    "tornado_emergency": ("tornado_emergency", "TORNADO EMERGENCY"),
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

    state, message = TEST_WARNINGS[selected]

    return jsonify({
        "state": state,
        "text": message,
        "source": "simulation",
        "simulation": True,
        "alerts_checked": 0,
        **public_gps()
    })



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


def vehicle_inside_alert_polygon(alert, lat, lon):
    geometry = alert.get("geometry")
    return bool(geometry and shape(geometry).covers(Point(lon, lat)))


def vehicle_distance_to_polygon_miles(alert, lat, lon):
    geometry = alert.get("geometry")
    if not geometry:
        return None
    polygon = shape(geometry)
    point = Point(lon, lat)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    polygon_m = transform(transformer.transform, polygon)
    point_m = transform(transformer.transform, point)
    return polygon_m.distance(point_m) / 1609.344


def classify_tornado_alert(alert):
    props = alert.get("properties", {})
    combined_text = " ".join(str(props.get(k) or "") for k in
                             ("headline", "description", "instruction")).upper()
    if "TORNADO EMERGENCY" in combined_text:
        return "tornado_emergency", "TORNADO EMERGENCY"
    if "PARTICULARLY DANGEROUS SITUATION" in combined_text or "PDS" in combined_text:
        return "pds_tornado", "PDS TORNADO WARNING"
    return "tornado", "TORNADO WARNING ISSUED"


@app.route("/status.json")
def status():
    gps = public_gps()
    if gps["gps_status"] != "connected":
        # Do not call NWS using an outdated or unknown position.
        return jsonify({"state": "gps_unavailable", "text": "GPS POSITION UNAVAILABLE",
                        "source": "live_gps_nws_polygon", "alerts_checked": 0, **gps})

    lat, lon = vehicle_location["lat"], vehicle_location["lon"]
    total_alerts_checked = 0
    try:
        for warning_type in ("Tornado Warning", "Severe Thunderstorm Warning"):
            response = requests.get(
                NWS_ALERTS_URL,
                headers={"User-Agent": "WeatherHolidays storm bot"},
                params={"event": warning_type, "status": "actual", "message_type": "alert"},
                timeout=10,
            )
            response.raise_for_status()
            alerts = response.json().get("features", [])
            total_alerts_checked += len(alerts)
            for alert in alerts:
                inside = vehicle_inside_alert_polygon(alert, lat, lon)
                distance = vehicle_distance_to_polygon_miles(alert, lat, lon)
                if inside or (distance is not None and distance <= 3):
                    if warning_type == "Tornado Warning":
                        state, message = classify_tornado_alert(alert)
                    else:
                        state, message = "severe", "SEVERE THUNDERSTORM WARNING"
                    return jsonify({"state": state, "text": message,
                                    "source": "live_gps_nws_polygon",
                                    "alerts_checked": total_alerts_checked,
                                    "distance_miles": round(distance, 2) if distance is not None else None,
                                    "inside_polygon": inside, **gps})
        return jsonify({"state": "normal", "text": "",
                        "source": "live_gps_nws_polygon",
                        "alerts_checked": total_alerts_checked, **gps})
    except Exception:
        app.logger.exception("Warning check failed")
        return jsonify({"state": "error", "text": "Warning check failed",
                        "source": "live_gps_nws_polygon", **gps}), 503


if __name__ == "__main__":
    app.run(debug=True)
