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

    # Extract eNodeB IDs — response is {"nodes": [...]}
    node_list = (
        node_data.get("nodes")
        or node_data.get("enodebs")
        or node_data.get("eNodeBs")
        or []
    )
    _id_fields = ("node", "enodeb_id", "enodebId", "eNodeBId", "nodeId", "id", "enodeb", "eNBId")
    enodeb_ids = []
    for n in node_list:
        if isinstance(n, (str, int)):
            enodeb_ids.append(str(n))
        elif isinstance(n, dict):
            for f in _id_fields:
                if n.get(f):
                    enodeb_ids.append(str(n[f]))
                    break
    enodeb_ids = [i for i in enodeb_ids if i]

    if not enodeb_ids:
        first_node_keys = list(node_list[0].keys()) if node_list and isinstance(node_list[0], dict) else []
        return jsonify({
            "success": False,
            "error": f"Could not find eNodeB IDs in nodes list. First node keys: {first_node_keys}",
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

    resp_data = r.json() if "html" not in r.headers.get("content-type", "") else {}

    if "html" in r.headers.get("content-type", ""):
        return jsonify({"success": True, "html": r.text, "request_id": site_token})

    # Extract request_id from POST response to begin polling
    unwrapped = resp_data.get("data") or resp_data
    hc_meta = unwrapped.get("enodeb_healthcheck_result") or {}
    request_id = (
        hc_meta.get("request_id")
        or unwrapped.get("requestId")
        or unwrapped.get("request_id")
        or unwrapped.get("id")
        or unwrapped.get("healthcheckId")
    )
    if not request_id and isinstance(unwrapped.get("requestIds"), list):
        request_id = unwrapped["requestIds"][0]

    if not request_id:
        return jsonify({
            "success": False,
            "error": f"Could not extract request ID from IOP response. Raw response: {str(resp_data)[:500]}",
        }), 502

    request_id = str(request_id)
    poll_url = f"{IOP_BASE}/neops/enodeb/healthcheck/request/{request_id}"

    for attempt in range(40):
        if attempt > 0:
            time.sleep(10)
        elapsed = attempt * 10
        print(f"[Poll {attempt+1}/40 | {elapsed}s] GET {poll_url}", flush=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                rp = httpx.get(
                    poll_url,
                    headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                    timeout=30,
                    verify=False,
                )
            print(f"[Poll {attempt+1}/40 | {elapsed}s] HTTP {rp.status_code}", flush=True)
            if rp.status_code != 200:
                continue
            result = rp.json()
            hc_data = result.get("enodeb_healthcheck_result", {})
            if not isinstance(hc_data, dict):
                print(f"[Poll {attempt+1}/40] enodeb_healthcheck_result not a dict: {type(hc_data)}", flush=True)
                continue
            status = hc_data.get("status", "(missing)")
            print(f"[Poll {attempt+1}/40 | {elapsed}s] status={status}", flush=True)
            if status != "Completed":
                continue
            hc_html = ""
            for info_key in ("ondemand_info", "precheck_info", "postcheck_info"):
                outputs = (hc_data.get(info_key) or {}).get("result") or []
                if outputs:
                    out = (outputs[0].get("output") or [])
                    if out and out[0]:
                        hc_html = out[0]
                        print(f"[Poll {attempt+1}/40] HTML found under {info_key}, len={len(hc_html)}", flush=True)
                        break
            if hc_html:
                return jsonify({"success": True, "html": hc_html, "request_id": request_id})
            print(f"[Poll {attempt+1}/40] Completed but no HTML found in ondemand/precheck/postcheck_info", flush=True)
        except Exception as ex:
            print(f"[Poll {attempt+1}/40 | {elapsed}s] Exception: {ex}", flush=True)
            continue

    return jsonify({
        "success": False,
        "error": f"Health check timed out waiting for results (request ID: {request_id}).",
    }), 504


@app.route("/get-enodeb-ids", methods=["POST"])
def get_enodeb_ids():
    body = request.get_json(force=True, silent=True) or {}
    site_token = (body.get("site_token") or "").strip()
    if not site_token:
        return jsonify({"success": False, "error": "site_token is required"}), 400
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.get(
                f"{IOP_BASE}/neops/{site_token}/node/details",
                headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                timeout=30, verify=False,
            )
        if r.status_code != 200:
            return jsonify({"success": False, "error": f"IOP node details: HTTP {r.status_code}"}), 502
        node_data = r.json()
    except Exception as e:
        return jsonify({"success": False, "error": f"Node details error: {e}"}), 502
    node_list = node_data.get("nodes") or node_data.get("enodebs") or node_data.get("eNodeBs") or []
    _id_fields = ("node","enodeb_id","enodebId","eNodeBId","nodeId","id","enodeb","eNBId")
    enodeb_ids = []
    for n in node_list:
        if isinstance(n, (str, int)):
            enodeb_ids.append(str(n))
        elif isinstance(n, dict):
            for f in _id_fields:
                if n.get(f):
                    enodeb_ids.append(str(n[f])); break
    enodeb_ids = [i for i in enodeb_ids if i]
    if not enodeb_ids:
        first_keys = list(node_list[0].keys()) if node_list and isinstance(node_list[0], dict) else []
        return jsonify({"success": False, "error": f"No eNodeB IDs found. Node keys: {first_keys}"}), 502
    return jsonify({"success": True, "enodeb_ids": enodeb_ids})


@app.route("/create-healthcheck", methods=["POST"])
def create_healthcheck():
    body = request.get_json(force=True, silent=True) or {}
    site_token = (body.get("site_token") or "").strip()
    enodeb_ids = body.get("enodeb_ids") or []
    eid = (body.get("eid") or "").strip().upper()
    if not site_token or not enodeb_ids:
        return jsonify({"success": False, "error": "site_token and enodeb_ids required"}), 400
    hc_url = f"{IOP_BASE}/neops/{site_token}/enodeb/healthcheck"
    payload = {
        "enodeb_healthcheck": {
            "enodeb_ids": enodeb_ids, "req_type": "On-Demand", "email_ids": [],
            "precheck_start_time": "", "postcheck_start_time": "", "ondemandcheck_start_time": "",
            "include_pre_check_time": "no", "include_post_check_time": "no",
            "include_ondemand_check_time": "no", "include_prb_heat_map": "no",
            "timezone": "eastern", "command_list": "regular", "command_list_5g": "fast_5g",
            "attachments": [], "vendor": "ENM", "source": "SITES",
            "isTargeted": False, "targeted_hc_options": [],
            "ip_address": None, "created_by": eid or OAP_USER_EID,
        }
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(hc_url, json=payload, headers=IOP_HEADERS, timeout=60, verify=False)
        if r.status_code not in (200, 201, 202):
            return jsonify({"success": False, "error": f"IOP HC POST: HTTP {r.status_code}: {r.text[:300]}"}), 502
    except Exception as e:
        return jsonify({"success": False, "error": f"HC create error: {e}"}), 502
    if "html" in r.headers.get("content-type", ""):
        return jsonify({"success": True, "html": r.text, "request_id": site_token})
    resp_data = r.json()
    unwrapped = resp_data.get("data") or resp_data
    hc_meta = unwrapped.get("enodeb_healthcheck_result") or {}
    request_id = (hc_meta.get("request_id") or unwrapped.get("requestId")
                  or unwrapped.get("request_id") or unwrapped.get("id") or unwrapped.get("healthcheckId"))
    if not request_id and isinstance(unwrapped.get("requestIds"), list):
        request_id = unwrapped["requestIds"][0]
    if not request_id:
        return jsonify({"success": False, "error": f"No request ID in response: {str(resp_data)[:500]}"}), 502
    return jsonify({"success": True, "request_id": str(request_id)})


@app.route("/poll-healthcheck", methods=["POST"])
def poll_healthcheck():
    body = request.get_json(force=True, silent=True) or {}
    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        return jsonify({"success": False, "error": "request_id required"}), 400
    poll_url = f"{IOP_BASE}/neops/enodeb/healthcheck/request/{request_id}"
    print(f"[Poll] GET {poll_url}", flush=True)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rp = httpx.get(
                poll_url,
                headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                timeout=30, verify=False,
            )
        print(f"[Poll] HTTP {rp.status_code}", flush=True)
        if rp.status_code != 200:
            return jsonify({"success": False, "error": f"IOP poll: HTTP {rp.status_code}"}), 502
        result = rp.json()
        hc_data = result.get("enodeb_healthcheck_result", {})
        if not isinstance(hc_data, dict):
            return jsonify({"success": True, "done": False, "status": "unknown"})
        status = hc_data.get("status", "unknown")
        print(f"[Poll] status={status}", flush=True)
        if status != "Completed":
            return jsonify({"success": True, "done": False, "status": status})
        hc_html = ""
        for info_key in ("ondemand_info", "precheck_info", "postcheck_info"):
            outputs = (hc_data.get(info_key) or {}).get("result") or []
            if outputs:
                out = (outputs[0].get("output") or [])
                if out and out[0]:
                    hc_html = out[0]
                    print(f"[Poll] HTML found under {info_key}, len={len(hc_html)}", flush=True)
                    break
        if not hc_html:
            return jsonify({"success": True, "done": False, "status": "Completed (extracting HTML...)"})
        return jsonify({"success": True, "done": True, "status": status, "html": hc_html})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/list-healthchecks", methods=["POST"])
def list_healthchecks():
    body = request.get_json(force=True, silent=True) or {}
    site_token = (body.get("site_token") or "").strip()
    start_date = (body.get("start_date") or "").strip()
    end_date   = (body.get("end_date") or "").strip()
    if not site_token:
        return jsonify({"success": False, "error": "site_token is required"}), 400
    params = {}
    if start_date: params["start_date"] = start_date
    if end_date:   params["end_date"]   = end_date
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.get(
                f"{IOP_BASE}/neops/{site_token}/enodeb/healthcheck/",
                params=params,
                headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                timeout=30, verify=False,
            )
        if r.status_code != 200:
            return jsonify({"success": False, "error": f"IOP list failed: HTTP {r.status_code}"}), 502
        return jsonify({"success": True, "data": r.json()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


@app.route("/fetch-existing-hc", methods=["POST"])
def fetch_existing_hc():
    body = request.get_json(force=True, silent=True) or {}
    request_id = (body.get("request_id") or "").strip()
    if not request_id:
        return jsonify({"success": False, "error": "request_id is required"}), 400
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rp = httpx.get(
                f"{IOP_BASE}/neops/enodeb/healthcheck/request/{request_id}",
                headers={k: v for k, v in IOP_HEADERS.items() if k != "Content-Type"},
                timeout=30, verify=False,
            )
        if rp.status_code != 200:
            return jsonify({"success": False, "error": f"IOP fetch failed: HTTP {rp.status_code}"}), 502
        result = rp.json()
        hc_data = result.get("enodeb_healthcheck_result", {})
        if not isinstance(hc_data, dict):
            return jsonify({"success": False, "error": "Unexpected response format"}), 502
        hc_html = ""
        for info_key in ("ondemand_info", "precheck_info", "postcheck_info"):
            outputs = (hc_data.get(info_key) or {}).get("result") or []
            if outputs:
                out = (outputs[0].get("output") or [])
                if out and out[0]:
                    hc_html = out[0]
                    break
        if not hc_html:
            return jsonify({"success": False,
                            "error": f"No HTML in HC {request_id} (status: {hc_data.get('status')})"}), 502
        return jsonify({"success": True, "html": hc_html, "request_id": request_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 502


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


# ── AMOS / ENM direct connection ─────────────────────────────
# Reuses the same RSA key and direct host as TicketTracker
AMOS_DIRECT_HOST = "10.216.230.154"
AMOS_DIRECT_PORT = 22
AMOS_DIRECT_USER = "ctchgrp"
AMOS_KEY_FILE    = r"C:\Users\merch79\Documents\TicketTracker\data\amos_identity"
AMOS_KEY_PASS    = ""    # key passphrase if set (check amos_key_passphrase in TicketTracker settings)
AMOS_CMD_TIMEOUT = 90    # seconds to wait for command output

try:
    import paramiko as _paramiko
    _PARAMIKO_OK = True
except ImportError:
    _PARAMIKO_OK = False

import uuid as _uuid
import re as _re

_amos_sessions: dict = {}   # session_id → {ssh, channel, node, prompt_re}

_ANSI_RE = _re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[()][A-Z0-9]'
    r'|\x1b[^[\]()]'
)
_AMOS_PROMPT_RE = _re.compile(r'^[\w][\w._-]*>\s*$', _re.MULTILINE)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s).replace('\r', '')


def _amos_read_until(channel, pattern, timeout: float = AMOS_CMD_TIMEOUT) -> str:
    if isinstance(pattern, str):
        pattern = _re.compile(_re.escape(pattern), _re.MULTILINE)
    buf = ""
    end = time.time() + timeout
    while time.time() < end:
        if channel.recv_ready():
            buf += _strip_ansi(channel.recv(8192).decode("utf-8", errors="replace"))
            if pattern.search(buf):
                return buf
        elif channel.closed or channel.exit_status_ready():
            break
        else:
            time.sleep(0.1)
    return buf


@app.route("/amos-connect", methods=["POST"])
def amos_connect():
    if not _PARAMIKO_OK:
        return jsonify({"success": False, "error": "paramiko not installed — run: pip install paramiko"}), 500

    import os
    body = request.get_json(force=True, silent=True) or {}
    node = (body.get("node") or "").strip()
    if not node:
        return jsonify({"success": False, "error": "node name required"}), 400
    if not os.path.isfile(AMOS_KEY_FILE):
        return jsonify({"success": False, "error": f"RSA key not found: {AMOS_KEY_FILE}"}), 500

    try:
        pkey = _paramiko.RSAKey.from_private_key_file(AMOS_KEY_FILE, password=AMOS_KEY_PASS or None)
        ssh = _paramiko.SSHClient()
        ssh.set_missing_host_key_policy(_paramiko.AutoAddPolicy())
        ssh.connect(
            AMOS_DIRECT_HOST, port=AMOS_DIRECT_PORT, username=AMOS_DIRECT_USER,
            pkey=pkey, timeout=30, allow_agent=False, look_for_keys=False,
        )
        channel = ssh.invoke_shell(term='vt100', width=220, height=50)
        time.sleep(1.5)
        while channel.recv_ready():
            channel.recv(8192)   # drain banner

        channel.send(f"amos {node}\r\n")
        banner = _amos_read_until(channel, _AMOS_PROMPT_RE, timeout=60)

        if not _AMOS_PROMPT_RE.search(banner):
            ssh.close()
            return jsonify({"success": False,
                            "error": f"No AMOS prompt for node '{node}'. Got: {banner[-300:]}"}), 500

        # Derive exact prompt for precise matching (avoids false positives on cell IDs)
        exact = next(
            (l.strip() for l in reversed(banner.splitlines()) if _AMOS_PROMPT_RE.match(l.strip())),
            ""
        )
        prompt_re = (
            _re.compile(f'^{_re.escape(exact)}\\s*$', _re.MULTILINE) if exact else _AMOS_PROMPT_RE
        )

        # Drain late startup noise
        time.sleep(1.5)
        while channel.recv_ready():
            channel.recv(8192)

        # Setup: ul — enables user labels (no output collected)
        channel.send("ul\r\n")
        _amos_read_until(channel, prompt_re, timeout=30)

        session_id = str(_uuid.uuid4())[:8]
        _amos_sessions[session_id] = {"ssh": ssh, "channel": channel, "node": node, "prompt_re": prompt_re}
        print(f"[AMOS] Session {session_id} opened — node={node} prompt={exact!r}", flush=True)
        return jsonify({"success": True, "session_id": session_id, "banner": banner, "prompt": exact})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/amos-run-cmd", methods=["POST"])
def amos_run_cmd():
    body = request.get_json(force=True, silent=True) or {}
    session_id = (body.get("session_id") or "").strip()
    command    = (body.get("command") or "").strip()
    if session_id not in _amos_sessions:
        return jsonify({"success": False, "error": "Session not found — connect first"}), 400
    if not command:
        return jsonify({"success": False, "error": "command required"}), 400

    sess = _amos_sessions[session_id]
    channel, prompt_re, node = sess["channel"], sess["prompt_re"], sess["node"]
    try:
        channel.send(command + "\r\n")
        raw = _amos_read_until(channel, prompt_re, timeout=AMOS_CMD_TIMEOUT)
        output = prompt_re.sub('', raw).strip()
        print(f"[AMOS] {session_id}/{node}: {command!r} → {len(output)}c", flush=True)
        return jsonify({"success": True, "output": output})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/amos-close", methods=["POST"])
def amos_close():
    body = request.get_json(force=True, silent=True) or {}
    session_id = (body.get("session_id") or "").strip()
    if session_id in _amos_sessions:
        sess = _amos_sessions.pop(session_id)
        try:
            sess["channel"].send("q\r\n")
            time.sleep(0.5)
            sess["ssh"].close()
        except Exception:
            pass
        print(f"[AMOS] Session {session_id} closed", flush=True)
    return jsonify({"success": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8082))
    print(f"\n  AI Powered RRH OOS Diagnostics & Remediation")
    print(f"  Open via FGAToolHub: http://localhost:4000/rrh/")
    print(f"  Direct: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=port, debug=False)
