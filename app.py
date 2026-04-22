#!/usr/bin/env python3
"""
API Runner — Web-based Postman-like collection runner.
Run: python app.py
Open: http://localhost:5123
"""

import csv
import io
import json
import os
import re
import shlex
import time
import uuid
from datetime import datetime
from threading import Thread

try:
    from flask import Flask, request, jsonify, send_from_directory
    from flask_cors import CORS
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "flask", "flask-cors", "requests", "-q"])
    from flask import Flask, request, jsonify, send_from_directory
    from flask_cors import CORS

import requests as http_requests

app = Flask(__name__, static_folder="static")
CORS(app)

# ── In-memory store for runs ─────────────────────────────────────────────────
runs = {}


# ── Curl parser ──────────────────────────────────────────────────────────────

def parse_curl(curl_str: str) -> dict:
    curl_str = curl_str.replace("\\\n", " ")
    curl_str = curl_str.replace("\n", " ").strip()
    curl_str = curl_str.replace("“", '"').replace("”", '"')
    curl_str = curl_str.replace("‘", "'").replace("’", "'")

    # ✅ Fix for Postman: Decode URL-encoded curly braces back to {{ and }}
    curl_str = curl_str.replace("%7B", "{").replace("%7D", "}")
    curl_str = curl_str.replace("%7b", "{").replace("%7d", "}")

    tokens = shlex.split(curl_str)

    method = None
    url = ""
    headers = {}
    data = None

    i = 0
    while i < len(tokens):
        tok = tokens[i]

        if tok == "curl":
            i += 1
            continue

        if tok in ("--location", "-L", "--compressed", "-k", "--insecure", "-i", "-s", "--silent", "-v", "--verbose"):
            i += 1
            continue

        # HEADERS
        if tok in ("--header", "-H"):
            if i + 1 < len(tokens):
                raw = tokens[i + 1]

                # Remove quotes if present
                raw = raw.strip().strip('"').strip("'")

                if ":" in raw:
                    key, val = raw.split(":", 1)
                    headers[key.strip()] = val.strip()

                i += 2
                continue

        # DATA
        if tok in ("--data", "--data-raw", "--data-binary", "-d", "--data-urlencode"):
            if i + 1 < len(tokens):
                new_data = tokens[i + 1]
                # If multiple data fields exist, join them with &
                if data:
                    data = f"{data}&{new_data}"
                else:
                    data = new_data
                    
                if not method:
                    method = "POST"
                i += 2
                continue

        # METHOD
        if tok in ("-X", "--request"):
            if i + 1 < len(tokens):
                method = tokens[i + 1].upper()
                i += 2
                continue
                
        # EXPLICIT URL FLAG
        if tok in ("--url",):
            if i + 1 < len(tokens):
                url = tokens[i + 1]
                i += 2
                continue

        # ✅ POSITIONAL URL (Allow normal HTTP or URLs starting with {{variable}})
        if not tok.startswith("-") and not url:
            if tok.startswith("http") or "{{" in tok:
                url = tok
                i += 1
                continue

        i += 1

    if not method:
        method = "POST" if data else "GET"

    return {
        "method": method,
        "url": url,
        "headers": headers,
        "data": data
    }


def find_variables(parsed: dict) -> list:
    all_text = parsed["url"] + " ".join(parsed["headers"].values()) + (parsed["data"] or "")
    return sorted(set(re.findall(r"\{\{(.+?)\}\}", all_text)))


def substitute(template: str, variables: dict) -> str:
    if not template:
        return ""
    def replacer(match):
        key = match.group(1).strip()
        return str(variables.get(key, match.group(0)))
    return re.sub(r"\{\{(.+?)\}\}", replacer, template)


# ── CSV / JSON parser ────────────────────────────────────────────────────────

def clean_keys(data_list):
    """Strips {{ }} and spaces from CSV headers to ensure accurate matching"""
    if not data_list:
        return []
    cleaned = []
    for row in data_list:
        new_row = {}
        for k, v in row.items():
            if k is not None:  # Protects against empty trailing CSV columns
                clean_k = re.sub(r'[\{\}\s]', '', str(k))
                new_row[clean_k] = v
        cleaned.append(new_row)
    return cleaned

def parse_data_file(content: str, filename: str) -> list:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".json":
        data = json.loads(content)
        rows = data if isinstance(data, list) else [data]
    else:
        # CSV
        reader = csv.DictReader(io.StringIO(content))
        rows = [row for row in reader]
    
    return clean_keys(rows)


# ── Request executor ─────────────────────────────────────────────────────────

def execute_single(parsed, variables, timeout=30):
    url = substitute(parsed["url"], variables)
    
    # Safely convert spaces inside substituted URL variables to %20 to prevent requests from throwing an InvalidURL exception
    url = url.replace(" ", "%20") 
    
    headers = {k: substitute(v, variables) for k, v in parsed["headers"].items()}
    body = substitute(parsed["data"], variables) if parsed["data"] else None

    start = time.time()
    try:
        resp = http_requests.request(
            method=parsed["method"], url=url, headers=headers,
            data=body.encode("utf-8") if body else None,
            timeout=timeout, verify=True,
        )
        elapsed = round((time.time() - start) * 1000)
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text[:5000]
        return {
            "status": resp.status_code,
            "time_ms": elapsed,
            "body": resp_body,
            "error": None,
            "response_headers": dict(resp.headers),
            "request": {
                "method": parsed["method"],
                "url": url,
                "headers": headers,
                "body": body
            }
        }
    except Exception as e:
        elapsed = round((time.time() - start) * 1000)
        return {
            "status": None, "time_ms": elapsed,
            "body": None, "error": str(e),
            "response_headers": {},
            "request": {"method": parsed["method"], "url": url, "headers": headers, "body": body}
        }


def run_batch(run_id, parsed, rows, delay_ms, timeout):
    run = runs[run_id]
    run["status"] = "running"
    run["started_at"] = datetime.now().isoformat()

    for i, row in enumerate(rows):
        if run.get("abort"):
            run["status"] = "aborted"
            break

        run["current_index"] = i
        result = execute_single(parsed, row, timeout)
        result["row_index"] = i
        result["variables"] = row
        run["results"].append(result)

        if result["error"] or (result["status"] and result["status"] >= 400):
            run["fail_count"] += 1
        else:
            run["success_count"] += 1

        if i < len(rows) - 1 and delay_ms > 0:
            time.sleep(delay_ms / 1000)

    if run["status"] != "aborted":
        run["status"] = "completed"
    run["finished_at"] = datetime.now().isoformat()


# ── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/parse-curl", methods=["POST"])
def api_parse_curl():
    curl_str = request.json.get("curl", "")
    parsed = parse_curl(curl_str)
    variables = find_variables(parsed)
    return jsonify({"parsed": parsed, "variables": variables})

@app.route("/api/parse-data", methods=["POST"])
def api_parse_data():
    content = request.json.get("content", "")
    filename = request.json.get("filename", "data.csv")
    try:
        rows = parse_data_file(content, filename)
        columns = list(rows[0].keys()) if rows else []
        return jsonify({"rows": rows, "columns": columns, "count": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/dry-run", methods=["POST"])
def api_dry_run():
    curl_str = request.json.get("curl", "")
    rows = request.json.get("rows", [])
    parsed = parse_curl(curl_str)

    previews = []
    for i, row in enumerate(rows):
        url = substitute(parsed["url"], row)
        url = url.replace(" ", "%20") # Safely handle spaces in URLs
        headers = {k: substitute(v, row) for k, v in parsed["headers"].items()}
        body = substitute(parsed["data"], row) if parsed["data"] else None
        previews.append({
            "index": i, "method": parsed["method"], "url": url,
            "headers": headers, "body": body, "variables": row
        })
    return jsonify({"previews": previews})

@app.route("/api/run", methods=["POST"])
def api_run():
    curl_str = request.json.get("curl", "")
    rows = request.json.get("rows", [])
    delay_ms = request.json.get("delay", 200)
    timeout = request.json.get("timeout", 30)
    parsed = parse_curl(curl_str)

    run_id = str(uuid.uuid4())[:8]
    runs[run_id] = {
        "id": run_id, "status": "pending", "total": len(rows),
        "current_index": -1, "results": [],
        "success_count": 0, "fail_count": 0,
        "abort": False,
    }

    thread = Thread(target=run_batch, args=(run_id, parsed, rows, delay_ms, timeout))
    thread.daemon = True
    thread.start()

    return jsonify({"run_id": run_id})

@app.route("/api/run/<run_id>")
def api_run_status(run_id):
    run = runs.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(run)

@app.route("/api/run/<run_id>/abort", methods=["POST"])
def api_abort_run(run_id):
    run = runs.get(run_id)
    if not run:
        return jsonify({"error": "Run not found"}), 404
    run["abort"] = True
    return jsonify({"status": "aborting"})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  🚀 API Runner is live at http://localhost:5123\n")
    app.run(host="0.0.0.0", port=5123, debug=False)
