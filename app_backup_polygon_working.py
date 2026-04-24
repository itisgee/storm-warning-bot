from flask import Flask, jsonify
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape

app = Flask(__name__)
CORS(app)

VEHICLE_LAT = 40.73
VEHICLE_LON = -94.93

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"


@app.route("/")
def home():
    return "Storm warning bot is running"


def vehicle_inside_alert_polygon(alert, lat, lon):
    geometry = alert.get("geometry")

    if not geometry:
        return False

    polygon = shape(geometry)
    point = Point(lon, lat)

    return polygon.covers(point)


@app.route("/status.json")
def status():
    headers = {
        "User-Agent": "WeatherHolidays storm bot"
    }

    params = {
        "event": "Tornado Warning",
        "status": "actual",
        "message_type": "alert"
    }

    try:
        r = requests.get(
            NWS_ALERTS_URL,
            headers=headers,
            params=params,
            timeout=10
        )

        r.raise_for_status()
        alerts = r.json().get("features", [])

        for alert in alerts:
            if vehicle_inside_alert_polygon(alert, VEHICLE_LAT, VEHICLE_LON):
                return jsonify({
                    "state": "warning",
                    "text": "TORNADO WARNING ISSUED",
                    "source": "nws_polygon_test",
                    "alerts_checked": len(alerts),
                    "vehicle": {
                        "lat": VEHICLE_LAT,
                        "lon": VEHICLE_LON
                    }
                })

        return jsonify({
            "state": "normal",
            "text": "",
            "source": "nws_polygon_test",
            "alerts_checked": len(alerts),
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