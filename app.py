from flask import Flask, jsonify
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape

app = Flask(__name__)
CORS(app)

VEHICLE_LAT = 39.01
VEHICLE_LON = -96.1

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

    return polygon.contains(point)


@app.route("/status.json")
def status():

    headers = {
        "User-Agent":"WeatherHolidays storm bot"
    }

    params = {
        "event":"Tornado Warning",
        "status":"actual",
        "message_type":"alert"
    }

    try:
        r = requests.get(
            NWS_ALERTS_URL,
            headers=headers,
            params=params,
            timeout=10
        )

        alerts = r.json().get("features",[])

        for alert in alerts:
    if vehicle_inside_alert_polygon(
        alert,
        VEHICLE_LAT,
        VEHICLE_LON
    ):
        return jsonify({
            "state": "warning",
            "text": "TORNADO WARNING ISSUED",
            "source": "nws_polygon_test"
        })

        return jsonify({
            "state":"normal",
            "text":"",
            "source":"nws_polygon_test"
        })

    except Exception as e:
        return jsonify({
            "state":"error",
            "error":str(e)
        })


if __name__=="__main__":
    app.run(debug=True)