from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from shapely.geometry import Point, shape
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app)

# Starting/default vehicle location
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


@app.route("/tracker")
def tracker():
    return """
<!DOCTYPE html>
<html>
<head>
<title>WeatherHolidays GPS Tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body{
font-family:Arial,sans-serif;
background:#111;
color:#fff;
padding:20px;
}
button{
font-size:20px;
padding:12px 18px;
border:none;
border-radius:10px;
background:#0a84ff;
color:white;
font-weight:bold;
}
.data{
font-size:18px;
line-height:1.6;
}
</style>
</head>
<body>

<h2>WeatherHolidays GPS Tracker</h2>

<button onclick="startTracking()">Start GPS Tracking</button>

<div class="data">
<p>Status: <span id="status">Waiting</span></p>
<p>Latitude: <span id="lat">-</span></p>
<p>Longitude: <span id="lon">-</span></p>
<p>Last sent: <span id="sent">-</span></p>
</div>

<script>
function startTracking(){
  if(!navigator.geolocation){
    document.getElementById("status").innerText = "GPS not supported";
    return;
  }

  document.getElementById("status").innerText = "Requesting GPS...";

  navigator.geolocation.watchPosition(
    sendPosition,
    showError,
    {
      enableHighAccuracy:true,
      maximumAge:0,
      timeout:10000
    }
  );
}

async function sendPosition(position){
  const lat = position.coords.latitude;
  const lon = position.coords.longitude;

  document.getElementById("lat").innerText = lat.toFixed(6);
  document.getElementById("lon").innerText = lon.toFixed(6);
  document.getElementById("status").innerText = "Tracking";

  try{
    await fetch("/location/update", {
      method:"POST",
      headers:{
        "Content-Type":"application/json"
      },
      body:JSON.stringify({
        lat:lat,
        lon:lon
      })
    });

    document.getElementById("sent").innerText = new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("status").innerText = "Send failed";
  }
}

function showError(error){
  document.getElementById("status").innerText = "GPS error: " + error.message;
}
</script>

</body>
</html>
"""


@app.route("/location/update", methods=["POST"])
def location_update():
    data = request.get_json()

    vehicle_location["lat"] = float(data["lat"])
    vehicle_location["lon"] = float(data["lon"])
    vehicle_location["updated"] = datetime.now(timezone.utc).isoformat()

    return jsonify({
        "ok": True,
        "vehicle": vehicle_location
    })


@app.route("/location.json")
def location_json():
    return jsonify(vehicle_location)


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

            r = requests.get(
                NWS_ALERTS_URL,
                headers=headers,
                params=params,
                timeout=10
            )

            r.raise_for_status()
            alerts = r.json().get("features", [])
            total_alerts_checked += len(alerts)

            for alert in alerts:
                if vehicle_inside_alert_polygon(alert, lat, lon):

                    if warning_type == "Tornado Warning":
                        return jsonify({
                            "state": "tornado",
                            "text": "TORNADO WARNING ISSUED",
                            "source": "live_gps_nws_polygon",
                            "alerts_checked": total_alerts_checked,
                            "vehicle": vehicle_location
                        })

                    if warning_type == "Severe Thunderstorm Warning":
                        return jsonify({
                            "state": "severe",
                            "text": "SEVERE THUNDERSTORM WARNING",
                            "source": "live_gps_nws_polygon",
                            "alerts_checked": total_alerts_checked,
                            "vehicle": vehicle_location
                        })

        return jsonify({
            "state": "normal",
            "text": "",
            "source": "live_gps_nws_polygon",
            "alerts_checked": total_alerts_checked,
            "vehicle": vehicle_location
        })

    except Exception as e:
        return jsonify({
            "state": "error",
            "text": "Warning check failed",
            "source": "live_gps_nws_polygon",
            "error": str(e),
            "vehicle": vehicle_location
        }), 500


if __name__ == "__main__":
    app.run(debug=True)