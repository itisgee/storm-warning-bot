from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "Storm warning bot is running"

@app.route("/status.json")
def status():
    return jsonify({
        "state":"normal",
        "text":"",
        "source":"prototype"
    })

if __name__ == "__main__":
    app.run(debug=True)