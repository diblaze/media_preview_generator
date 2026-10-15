import sqlite3
import sys

from flask import Flask, jsonify

app = Flask(__name__)


@app.get("/v1/info")
def info():
    return jsonify(ok=True, sqlite=sqlite3.sqlite_version, python=sys.version.split()[0])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765)
