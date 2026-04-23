from flask import Flask, jsonify
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape

app = Flask(__name__)
CORS(app)

# TEST VEHICLE LOCATION
# Change these to place the "car" somewhere else.
VEHICLE_LAT = 35.4676
VEHICLE_LON = -97.5164

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"


@app.route("/")
def home():
    return "Storm warning bot is running"


def vehicle_inside_alert_polygon(alert, lat, lon):
    geometry = alert.get("geometry")

    if not geometry:
        return False

    polygon = shape(geometry)
    vehicle_point = Point(lon, lat)  # shapely uses lon, lat

    return polygon.contains(vehicle_point)


@app.route("/status.json")
def status():
    try:
        headers = {
            "User-Agent": "WeatherHolidays storm warning bot contact: crashlanding@hotmail.co.uk"
        }

        params = {
            "event": "Tornado Warning",
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
        alerts = response.json().get("features", [])

        for alert in alerts:
            if vehicle_inside_alert_polygon(alert, VEHICLE_LAT, VEHICLE_LON):
                props = alert.get("properties", {})
                return jsonify({
                    "state": "warning",
                    "text": "TORNADO WARNING ISSUED",
                    "source": "nws_polygon_test",
                    "headline": props.get("headline", ""),
                    "area": props.get("areaDesc", ""),
                    "expires": props.get("expires", ""),
                    "vehicle": {
                        "lat": VEHICLE_LAT,
                        "lon": VEHICLE_LON
                    }
                })

        return jsonify({
            "state": "normal",
            "text": "",
            "source": "nws_polygon_test",
            "vehicle": {
                "lat": VEHICLE_LAT,
                "lon": VEHICLE_LON
            }
        })

    except Exception as e:
        return jsonify({
            "state": "error",
            "text": "Warning check failed",
            "source": "nws_polygon_test",
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(debug=True)