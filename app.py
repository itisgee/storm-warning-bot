
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

# Default location used until GPS updates arrive.
VEHICLE_LAT = 40.73
VEHICLE_LON = -94.93

vehicle_location = {
    "lat": VEHICLE_LAT,
    "lon": VEHICLE_LON,
    "updated": None
}

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"


@app.route("/")
def home():
    return "Storm warning bot is running"


# --------------------------------------------------
# Existing browser-based GPS tracker
# --------------------------------------------------

@app.route("/tracker")
def tracker():
    return """
<!DOCTYPE html>
<html>
<head>
<title>WeatherHolidays GPS Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {
    font-family: Arial, sans-serif;
    background: #111;
    color: #fff;
    padding: 20px;
}
button {
    font-size: 20px;
    padding: 12px 18px;
    border: none;
    border-radius: 10px;
    background: #0a84ff;
    color: white;
    font-weight: bold;
}
.data {
    font-size: 18px;
    line-height: 1.6;
}
</style>
</head>
<body>
<h2>WeatherHolidays GPS Tracker</h2>
<p>
    Browser GPS tracking has been retired.
    The system now uses the vehicle's USB GPS
    receiver through GPSGate.
</p>
<div class="data">
<p>Status: GPSGate tracking mode</p>
</div>
</body>
</html>
"""


# --------------------------------------------------
# Secure GPS location updates
# --------------------------------------------------

@app.route("/location/update", methods=["POST"])
def location_update():

    expected_token = os.environ.get("GPS_API_TOKEN")
    supplied_token = request.headers.get("X-GPS-Token", "")

    if not expected_token or not hmac.compare_digest(
        supplied_token, expected_token
    ):
        return jsonify({
            "error": "Unauthorized"
        }), 401

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "Invalid JSON"
        }), 400

    try:
        lat = float(data["lat"])
        lon = float(data["lon"])

        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            raise ValueError("Invalid coordinates")

    except (KeyError, TypeError, ValueError):
        return jsonify({
            "error": "Invalid coordinates"
        }), 400

    vehicle_location["lat"] = lat
    vehicle_location["lon"] = lon
    vehicle_location["updated"] = (
        datetime.now(timezone.utc).isoformat()
    )

    return jsonify({
        "ok": True,
        "vehicle": vehicle_location
    })


@app.route("/location.json")
def location_json():
    return jsonify(vehicle_location)


# --------------------------------------------------
# NWS warning polygon detection
# --------------------------------------------------

def vehicle_inside_alert_polygon(alert, lat, lon):

    geometry = alert.get("geometry")

    if not geometry:
        return False

    polygon = shape(geometry)
    point = Point(lon, lat)

    return polygon.covers(point)


def vehicle_distance_to_polygon_miles(alert, lat, lon):

    geometry = alert.get("geometry")

    if not geometry:
        return None

    polygon = shape(geometry)
    point = Point(lon, lat)

    transformer = Transformer.from_crs(
        "EPSG:4326",
        "EPSG:3857",
        always_xy=True
    )

    polygon_m = transform(
        transformer.transform,
        polygon
    )

    point_m = transform(
        transformer.transform,
        point
    )

    distance_meters = polygon_m.distance(point_m)
    distance_miles = distance_meters / 1609.344

    return distance_miles


def classify_tornado_alert(alert):

    props = alert.get("properties", {})

    headline = props.get("headline", "") or ""
    description = props.get("description", "") or ""
    instruction = props.get("instruction", "") or ""

    combined_text = (
        f"{headline} {description} {instruction}"
    ).upper()

    if "TORNADO EMERGENCY" in combined_text:
        return (
            "tornado_emergency",
            "TORNADO EMERGENCY"
        )

    if (
        "PARTICULARLY DANGEROUS SITUATION"
        in combined_text
        or "PDS" in combined_text
    ):
        return (
            "pds_tornado",
            "PDS TORNADO WARNING"
        )

    return (
        "tornado",
        "TORNADO WARNING ISSUED"
    )


# --------------------------------------------------
# Warning status endpoint for Larix overlay
# --------------------------------------------------

@app.route("/status.json")
def status():

    headers = {
        "User-Agent": "WeatherHolidays storm bot"
    }

    warning_priority = [
        "Tornado Warning",
        "Severe Thunderstorm Warning"
    ]

    lat = vehicle_location["lat"]
    lon = vehicle_location["lon"]

    total_alerts_checked = 0

    try:

        for warning_type in warning_priority:

            params = {
                "event": warning_type,
                "status": "actual",
                "message_type": "alert"
            }

            response = requests.get(
                NWS_ALERTS_URL,
                headers=headers,
                params=params,
                timeout=10
            )

            response.raise_for_status()

            alerts = response.json().get(
                "features", []
            )

            total_alerts_checked += len(alerts)

            for alert in alerts:

                inside_polygon = (
                    vehicle_inside_alert_polygon(
                        alert, lat, lon
                    )
                )

                distance_miles = (
                    vehicle_distance_to_polygon_miles(
                        alert, lat, lon
                    )
                )

                if (
                    inside_polygon
                    or (
                        distance_miles is not None
                        and distance_miles <= 3
                    )
                ):

                    if warning_type == "Tornado Warning":

                        state, text = (
                            classify_tornado_alert(alert)
                        )

                        return jsonify({
                            "state": state,
                            "text": text,
                            "source": "live_gps_nws_polygon",
                            "alerts_checked": total_alerts_checked,
                            "distance_miles": round(
                                distance_miles, 2
                            ),
                            "inside_polygon": inside_polygon,
                            "vehicle": vehicle_location
                        })

                    if (
                        warning_type
                        == "Severe Thunderstorm Warning"
                    ):

                        return jsonify({
                            "state": "severe",
                            "text": "SEVERE THUNDERSTORM WARNING",
                            "source": "live_gps_nws_polygon",
                            "alerts_checked": total_alerts_checked,
                            "distance_miles": round(
                                distance_miles, 2
                            ),
                            "inside_polygon": inside_polygon,
                            "vehicle": vehicle_location
                        })

        return jsonify({
            "state": "normal",
            "text": "",
            "source": "live_gps_nws_polygon",
            "alerts_checked": total_alerts_checked,
            "vehicle": vehicle_location
        })

    except Exception as error:

        return jsonify({
            "state": "error",
            "text": "Warning check failed",
            "source": "live_gps_nws_polygon",
            "error": str(error),
            "vehicle": vehicle_location
        }), 500


if __name__ == "__main__":
    app.run(debug=True)
