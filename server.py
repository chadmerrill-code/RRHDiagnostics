# -*- coding: utf-8 -*-
"""
server.py  —  AI Powered RRH OOS Diagnostics & Remediation
Flask proxy server: serves index.html, proxies LLM calls, and generates PDF reports.
Run with: python server.py
"""
import sys
import time
import warnings
import os

_PARENT = os.path.dirname(os.path.abspath(__file__))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    from flask import Flask, request, jsonify, send_from_directory, Response
except ImportError:
    raise SystemExit("Flask not installed. Run: pip install -r requirements.txt")

try:
    import httpx
except ImportError:
    raise SystemExit("httpx not installed. Run: pip install -r requirements.txt")

try:
    from rrh_engine import build_pdf as _build_pdf
    PDF_AVAILABLE = True
except Exception as _e:
    PDF_AVAILABLE = False
    _PDF_ERR = str(_e)

app = Flask(__name__, static_folder=os.path.dirname(os.path.abspath(__file__)))

OAP_URL      = "https://ns-oap.ebiz.verizon.com/agent-gateway/api/v1/agents/chat/generate"
OAP_TOKEN    = "agw_6a51d252_lHqsjsIYBdM1iLKiYkOHv4rDj4nFBjOV"
OAP_AGENT    = "AI Powered RRH OOS Diagnostics & Remediation"
OAP_USER_EID = "5492597915"

VEGAS_URL = "https://oa-uat.ebiz.verizon.com/vegas/apps/prompt/LLMInsight"
VEGAS_KEY = "aECNZGARg2o6dGzxQiSWsFezaw2rrMg01aDU88Appj5YUHnP"


def _parse_oap_response(data: dict) -> str:
    if isinstance(data.get("response"), str) and data["response"].strip():
        return data["response"]
    if isinstance(data.get("messages"), list):
        last = ""
        for item in data["messages"]:
            if isinstance(item.get("messages"), list):
                for msg in item["messages"]:
                    if msg.get("type") == "ai" and isinstance(msg.get("content"), str):
                        last = msg["content"]
        if last:
            return last
    return ""


def _call_llm(prompt: str, context: str = "") -> str:
    oap_err = None

    # Primary: OAP Agent Gateway
    try:
        merged = f"{prompt}\n\nContext:\n{context}" if context else prompt
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                OAP_URL,
                json={"agent_name": OAP_AGENT, "user_eid": OAP_USER_EID,
                      "message": merged, "stream": False},
                headers={"Authorization": f"Bearer {OAP_TOKEN}",
                         "Content-Type": "application/json"},
                timeout=180, verify=False,
            )
        if r.status_code == 200:
            text = _parse_oap_response(r.json())
            if text:
                return text
        oap_err = f"OAP HTTP {r.status_code}"
    except Exception as e:
        oap_err = f"OAP error: {e}"

    # Fallback: VEGAS UAT with 429 retry
    payload = {
        "useCase": "AGENTS", "contextId": "AGENTS",
        "preSeed_injection_map": {
            "{ROLE}": "ADMIN", "{TOOLS}": "iop",
            "{CONTEXT}": context, "{QUERY}": prompt,
        },
        "parameters": {"temperature": 0.2, "maxOutputTokens": 2000},
    }
    delay = 30
    for attempt in range(1, 4):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                VEGAS_URL, json=payload,
                headers={"x-apikey": VEGAS_KEY, "Content-Type": "application/json"},
                timeout=180, verify=False,
            )
        if r.status_code == 429 and attempt < 3:
            time.sleep(delay); delay *= 2; continue
        if r.status_code != 200:
            raise RuntimeError(
                f"VEGAS {r.status_code}: {r.text[:300]}"
                + (f" | {oap_err}" if oap_err else "")
            )
        return r.json()["prediction"]

    raise RuntimeError("VEGAS still 429 after retries" + (f" | {oap_err}" if oap_err else ""))


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


IOP_BASE = "https://iop.vh.vzwnet.com:8080"
IOP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 VZWEDN",
}


@app.route("/fetch-healthcheck", methods=["POST"])
def fetch_healthcheck():
    body = request.get_json(force=True, silent=True) or {}
    site_token = (body.get("site_token") or "").strip()
    eid = (body.get("eid") or "").strip()

    if not site_token:
        return jsonify({"success": False, "error": "site_token is required"}), 400

    # Step 1: initiate health check
    create_url = f"{IOP_BASE}/site/{site_token}/vwrscreatehealthcheck"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                create_url,
                json={"workType": "Antenna / Tower", "createdBy": eid},
                headers=IOP_HEADERS,
                timeout=60,
                verify=False,
            )
        if r.status_code != 200:
            return jsonify({"success": False, "error": f"IOP create failed: HTTP {r.status_code}"}), 502
        data = r.json()
        request_ids = data.get("requestIds", [])
        if not request_ids:
            return jsonify({"success": False, "error": "IOP returned no request IDs"}), 502
        request_id = request_ids[0]
    except Exception as e:
        return jsonify({"success": False, "error": f"Health check create error: {e}"}), 502

    # Step 2: poll until result is available (max ~2 min)
    poll_url = f"{IOP_BASE}/neops/enodeb/healthcheck/request/{request_id}"
    for attempt in range(31):
        if attempt > 0:
            time.sleep(10)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                r = httpx.get(
                    poll_url,
                    headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                    timeout=30,
                    verify=False,
                )
            if r.status_code != 200:
                continue
            result = r.json()
            hc_html = result.get("enodeb_healthcheck_result", "")
            if not isinstance(hc_html, str):
                import json as _json
                hc_html = _json.dumps(hc_html)
            if hc_html and hc_html != "No HCs found":
                return jsonify({"success": True, "html": hc_html, "request_id": str(request_id)})
        except Exception:
            continue

    return jsonify({
        "success": False,
        "error": f"Health check timed out (request ID: {request_id}). Try again in a few minutes.",
    }), 504


@app.route("/diagnose", methods=["POST"])
def diagnose():
    body = request.get_json(force=True)
    prompt  = body.get("prompt", "")
    context = body.get("context", "")
    if not prompt:
        return jsonify({"success": False, "error": "No prompt provided"}), 400
    try:
        result = _call_llm(prompt, context)
        return jsonify({"success": True, "result": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/pdf", methods=["POST"])
def pdf():
    try:
        if not PDF_AVAILABLE:
            return jsonify({"success": False, "error": f"PDF unavailable: {_PDF_ERR}"}), 500

        body = request.get_json(force=True, silent=True) or {}
        site_id    = body.get("site_id", "UNKNOWN")
        vendor     = body.get("vendor", "Unknown")
        findings   = body.get("findings", {})
        llm_result = body.get("llm_result", "")
        summary    = body.get("summary", "")

        findings.setdefault("archetype_label",   findings.get("label", ""))
        findings.setdefault("dispatch_warranted", findings.get("dispatchWarranted", False))
        findings.setdefault("archetype",          findings.get("archetype", ""))

        pdf_bytes = _build_pdf(site_id, vendor, findings, llm_result, summary=summary)
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=RRH_OOS_Diagnostic_{site_id}.pdf"}
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8082))
    print(f"\n  AI Powered RRH OOS Diagnostics & Remediation")
    print(f"  Open via FGAToolHub: http://localhost:4000/rrh/")
    print(f"  Direct: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=port, debug=False)
