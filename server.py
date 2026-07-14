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
    eid = (body.get("eid") or "").strip().upper()

    if not site_token:
        return jsonify({"success": False, "error": "site_token is required"}), 400

    base_url = f"{IOP_BASE}/neops/{site_token}"

    # Step 1: get node details to resolve eNodeB IDs
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.get(
                f"{base_url}/node/details",
                headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                timeout=30,
                verify=False,
            )
        if r.status_code != 200:
            return jsonify({"success": False, "error": f"IOP node details failed: HTTP {r.status_code}"}), 502
        node_data = r.json()
    except Exception as e:
        return jsonify({"success": False, "error": f"Node details error: {e}"}), 502

    # Extract eNodeB IDs — try common field names
    enodeb_ids = (
        node_data.get("enodeb_ids")
        or node_data.get("enodebIds")
        or [str(e.get("id") or e.get("enodebId") or e.get("enodeb_id") or "")
            for e in (node_data.get("enodebs") or node_data.get("eNodeBs") or []) if e]
    )
    enodeb_ids = [str(i) for i in enodeb_ids if i]

    if not enodeb_ids:
        return jsonify({
            "success": False,
            "error": f"Could not find eNodeB IDs in node details. Keys returned: {list(node_data.keys())}",
        }), 502

    # Step 2: POST health check with correct IOP payload
    hc_url = f"{base_url}/enodeb/healthcheck"
    payload = {
        "enodeb_healthcheck": {
            "enodeb_ids": enodeb_ids,
            "req_type": "On-Demand",
            "email_ids": [],
            "precheck_start_time": "",
            "postcheck_start_time": "",
            "ondemandcheck_start_time": "",
            "include_pre_check_time": "no",
            "include_post_check_time": "no",
            "include_ondemand_check_time": "no",
            "include_prb_heat_map": "no",
            "timezone": "eastern",
            "command_list": "regular",
            "command_list_5g": "fast_5g",
            "attachments": [],
            "vendor": "ENM",
            "source": "SITES",
            "isTargeted": False,
            "targeted_hc_options": [],
            "ip_address": None,
            "created_by": eid or OAP_USER_EID,
        }
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                hc_url,
                json=payload,
                headers=IOP_HEADERS,
                timeout=60,
                verify=False,
            )
        if r.status_code not in (200, 201, 202):
            return jsonify({"success": False, "error": f"IOP health check POST failed: HTTP {r.status_code}: {r.text[:300]}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": f"Health check POST error: {e}"}), 502

    # Handle response — may be HTML directly or JSON with request IDs to poll
    content_type = r.headers.get("content-type", "")
    if "html" in content_type:
        return jsonify({"success": True, "html": r.text, "request_id": site_token})

    resp_data = r.json()

    # If HTML is embedded in the JSON response
    hc_html = resp_data.get("enodeb_healthcheck_result", "")
    if hc_html and hc_html != "No HCs found":
        if not isinstance(hc_html, str):
            import json as _json
            hc_html = _json.dumps(hc_html)
        return jsonify({"success": True, "html": hc_html, "request_id": site_token})

    # If we got a request ID back, poll for the result
    request_ids = (
        resp_data.get("requestIds")
        or resp_data.get("request_ids")
        or ([resp_data["requestId"]] if resp_data.get("requestId") else [])
        or ([resp_data["request_id"]] if resp_data.get("request_id") else [])
    )
    if not request_ids:
        return jsonify({
            "success": False,
            "error": f"Unexpected IOP response: {str(resp_data)[:300]}",
        }), 502

    request_id = request_ids[0]
    poll_url = f"{IOP_BASE}/neops/enodeb/healthcheck/request/{request_id}"
    for attempt in range(31):
        if attempt > 0:
            time.sleep(10)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rp = httpx.get(
                    poll_url,
                    headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                    timeout=30,
                    verify=False,
                )
            if rp.status_code != 200:
                continue
            result = rp.json()
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
        "error": f"Health check timed out waiting for results (request ID: {request_id}).",
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
