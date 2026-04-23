from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
@app.route("/")
def home():
    return "Storm warning bot is running"

@app.route("/status.json")
def status():
    return jsonify({
        "state":"warning",
"text":"TORNADO WARNING ISSUED",
        "source":"prototype"
    })

if __name__ == "__main__":
    app.run(debug=True)