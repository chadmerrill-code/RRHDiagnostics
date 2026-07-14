# -*- coding: utf-8 -*-
"""
Pure-Python engine: parsers, rule classifier, and LLM narration.
No Streamlit imports -- safe to import in tests or other scripts.
"""
import re
import io
import time
import warnings
from collections import defaultdict

import anthropic
import httpx
import pandas as pd
from bs4 import BeautifulSoup
from fpdf import FPDF

VEGAS_URL = "https://oa-uat.ebiz.verizon.com/vegas/apps/prompt/LLMInsight"
VEGAS_KEY = "aECNZGARg2o6dGzxQiSWsFezaw2rrMg01aDU88Appj5YUHnP"

# ─────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────

def _parse_html_tables(soup) -> list:
    """Return list of {headers, data} dicts for every table in soup."""
    result = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if not any(headers):
            continue
        data = []
        for row in rows[1:]:
            cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
            if cells:
                data.append(dict(zip(headers, cells)))
        result.append({"headers": headers, "data": data})
    return result


def _find_table(tables: list, *required_cols: str) -> list:
    """Return data rows from first table whose headers contain all required_cols."""
    for t in tables:
        if all(c in t["headers"] for c in required_cols):
            return t["data"]
    return []


def detect_vendor(html_bytes: bytes) -> str:
    """Auto-detect Ericsson vs Samsung from data-section markers (handles single- or double-quoted attrs)."""
    text = html_bytes.decode("utf-8", errors="replace")
    # Samsung uses single-quoted attrs (data-section='cpri') in its minified HC output
    if re.search(r"""data-section=['"]cpri['"]""", text) or re.search(r"""data-section=['"]vswr['"]""", text):
        return "Samsung"
    if re.search(r"""data-section=['"]active-alarms['"]""", text) or re.search(r"""data-section=['"]light-level['"]""", text):
        return "Ericsson"
    return "Unknown"


# ─────────────────────────────────────────────
# ERICSSON PARSER
# ─────────────────────────────────────────────

def parse_healthcheck(html_bytes: bytes) -> dict:
    soup = BeautifulSoup(html_bytes.decode("utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    sections = defaultdict(list)
    for tag in soup.find_all(attrs={"data-section": True}):
        sec = tag.get("data-section")
        identifier = tag.get("data-identifier", "")
        text = tag.get_text(strip=True)
        sections[sec].append({"identifier": identifier, "text": text})

    # PIM / VSWR summary table
    pim_table = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
        if "ENB_SEC_CAR" in headers or "RSSI_Rx1" in "".join(headers):
            for row in rows[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["th", "td"])]
                if cells:
                    pim_table.append(dict(zip(headers, cells)))
    sections["pim-table"] = pim_table

    # Extract site ID
    site_id = None
    for item in sections.get("cell-status", []):
        m = re.search(r"EUtranCellFDD=(\d+)_", item["identifier"])
        if m:
            site_id = m.group(1)
            break
    if not site_id:
        for item in sections.get("active-alarms", []):
            m = re.search(r"(\d{6})_", item["text"])
            if m:
                site_id = m.group(1)
                break

    sections["_site_id"] = site_id
    return dict(sections)


def parse_switch_alarms(xlsx_bytes: bytes, site_id: str) -> pd.DataFrame:
    df = pd.read_excel(io.BytesIO(xlsx_bytes), engine="calamine")
    if site_id and "Site #" in df.columns:
        df["Site #"] = df["Site #"].astype(str)
        site_df = df[df["Site #"].str.contains(str(site_id), na=False)]
        if len(site_df) > 0:
            return site_df.reset_index(drop=True)
    return df.reset_index(drop=True)


# ─────────────────────────────────────────────
# RULE ENGINE — Ericsson Archetype A / B
# ─────────────────────────────────────────────

_A_PATTERNS = [
    r"Link Failure", r"No signal detected", r"link start time-out",
    r"No hardware detected", r"HW Partial Fault",
    r"Not functional carrier HW resources",
    r"Inconsistent Configuration", r"LINEARIZATION_HW_FAULT",
    r"RRH low.gain", r"transceiver.problem",
]
_B_PATTERNS = [
    r"Resource Configuration Failure", r"Request is not unique",
    r"Resource Activation Timeout", r"Unable to allocate radio resources",
]
_HARD_FAULT_PATTERNS = [
    r"No hardware detected", r"HW Partial Fault",
    r"LINEARIZATION_HW_FAULT", r"Not functional carrier HW resources",
]


def classify(hc: dict) -> dict:
    alarms      = hc.get("active-alarms", [])
    cells       = hc.get("cell-status", [])
    radios      = hc.get("radio", [])
    light       = hc.get("light-level", [])
    noise_items = hc.get("noise", [])
    pim_items   = hc.get("4g-pim-noise", [])
    pim_table   = hc.get("pim-table", [])

    alarm_texts = " ".join(a["text"] for a in alarms)

    a_score = sum(1 for p in _A_PATTERNS if re.search(p, alarm_texts, re.I))
    b_score = sum(1 for p in _B_PATTERNS if re.search(p, alarm_texts, re.I))
    has_hard_fault = any(re.search(p, alarm_texts, re.I) for p in _HARD_FAULT_PATTERNS)

    nok_links       = [li for li in light if "NOK" in li["text"] or "Link Disabled" in li["text"]]
    disabled_cells  = [c  for c  in cells  if "0 (DISABLED)" in c["text"]]
    disabled_radios = [r  for r  in radios if "0 (DISABLED)" in r["text"]]

    hw_details = []
    for a in alarms:
        pn  = re.search(r'"PN"\s*:\s*"([^"]+)"',  a["text"])
        sn  = re.search(r'"SN"\s*:\s*"([^"]+)"',  a["text"])
        pnr = re.search(r'"PNR"\s*:\s*"([^"]+)"', a["text"])
        fru = re.search(r"FieldReplaceableUnit=([\w\-]+)", a["identifier"])
        if fru or pn or sn:
            hw_details.append({
                "fru":  fru.group(1) if fru else "",
                "pn":   pn.group(1)  if pn  else "",
                "sn":   sn.group(1)  if sn  else "",
                "pnr":  pnr.group(1) if pnr else "",
                "alarm_text": a["text"],
            })

    rilinks = []
    for a in alarms:
        m    = re.search(r"RiLink=(\w+)", a["identifier"] + a["text"])
        port = re.search(r"RiPort=(\w+)", a["text"])
        if m:
            rilinks.append({"id": m.group(1), "port": port.group(1) if port else ""})

    pim_high = []
    for row in pim_table:
        try:
            if float(row.get("PIM dB", "nan")) >= 5:
                pim_high.append({**row, "flag": f"PIM {row['PIM dB']} dB (>= 5 dB threshold)"})
        except ValueError:
            pass
        try:
            if float(row.get("Return Loss dB", "nan")) < 14:
                pim_high.append({**row, "flag": f"Return Loss {row['Return Loss dB']} dB < 14 dB"})
        except ValueError:
            pass

    noisy_branches = []
    for n in noise_items:
        m = re.search(r"([\w\d_]+):\s*SC=(\w+):.*?(Branch\s+AUG=[\w,=\-]+).*?([\-\d.]+)dBm", n["text"])
        if m and float(m.group(4)) > -105:
            noisy_branches.append({
                "cell": m.group(1), "sc": m.group(2),
                "branch": m.group(3), "level_dBm": float(m.group(4)),
            })

    if a_score >= b_score and (nok_links or has_hard_fault):
        archetype, label = "A", "Physical Fronthaul Layer Break"
    elif b_score > 0 and not has_hard_fault and not nok_links:
        archetype, label = "B", "MOM Application Stack Stall"
    elif a_score > 0:
        archetype, label = "A", "Physical Fronthaul Layer Break"
    else:
        archetype, label = "UNKNOWN", "Insufficient data — manual review required"

    dispatch_warranted = has_hard_fault or (archetype == "A" and len(nok_links) > 0)

    return {
        "archetype":          archetype,
        "archetype_label":    label,
        "dispatch_warranted": dispatch_warranted,
        "has_hard_fault":     has_hard_fault,
        "disabled_cells":     disabled_cells,
        "disabled_radios":    disabled_radios,
        "nok_links":          nok_links,
        "hw_details":         hw_details,
        "rilinks":            rilinks,
        "pim_high":           pim_high,
        "noisy_branches":     noisy_branches,
        "alarm_texts":        alarm_texts,
        "a_score":            a_score,
        "b_score":            b_score,
        "all_alarms":         alarms,
        "noise_items":        noise_items,
        "pim_items":          pim_items,
        "pim_table":          pim_table,
    }


# ─────────────────────────────────────────────
# LLM NARRATION — Ericsson
# ─────────────────────────────────────────────

def build_llm_prompt(site_id: str, findings: dict, switch_df: pd.DataFrame) -> str:
    hw_lines = "\n".join(
        f"  FRU={h['fru']} PN={h['pn']} SN={h['sn']}"
        for h in findings["hw_details"] if h["fru"] or h["sn"]
    ) or "  None"

    nok_lines = "\n".join(
        f"  {li['text'][:80]}" for li in findings["nok_links"]
    ) or "  None"

    rilink_lines = "\n".join(
        f"  RiLink={r['id']} Port={r['port']}" for r in findings["rilinks"]
    ) or "  None"

    cell_lines = "\n".join(
        f"  {c['identifier']}" for c in findings["disabled_cells"]
    ) or "  None"

    radio_lines = "\n".join(
        f"  {r['identifier']}" for r in findings["disabled_radios"]
    ) or "  None"

    pim_lines = "\n".join(
        f"  {p.get('ENB_SEC_CAR','?')}: {p.get('flag','')}"
        for p in findings["pim_high"]
    ) or "  None"

    if not switch_df.empty and "Description" in switch_df.columns:
        sw_lines = "; ".join(
            f"{r.get('Severity','')} {r.get('Description','')[:60]}"
            for _, r in switch_df.head(3).iterrows()
        )
    else:
        sw_lines = "None"

    dispatch_note = "DISPATCH WARRANTED" if findings["dispatch_warranted"] else "TRY REMOTE FIRST"

    return f"""Ericsson RAN NOC expert. Site {site_id}. Archetype {findings['archetype']} ({findings['archetype_label']}). {dispatch_note}.

KEY FACTS (use these exact IDs — do not invent):
Disabled cells: {cell_lines}
Disabled radios: {radio_lines}
NOK fronthaul links: {nok_lines}
RiLink faults: {rilink_lines}
Hardware (FRU/PN/SN): {hw_lines}
PIM flags: {pim_lines}
Switch alarms: {sw_lines}

ENM COMMANDS: st EUtranCellFDD | sdir | hget ^rilink state | acc FieldReplaceableUnit=<ID> restartunit | set EUtranCellFDD=<ID> administrativeState LOCKED/UNLOCKED | mfirt

Produce exactly:
## Section 1 — Remote Troubleshooting Steps
Numbered steps with exact ENM commands using IDs above. Pass criteria. Fail action. End: "If all remote steps fail -> Section 2."

## Section 2 — Precise Dispatch Instructions
PREREQUISITE: Exhaust Section 1 first.
TASK A (fiber/SFP if link failure): exact port IDs, fiber scope, SFP swap, OMC validation.
TASK B (RRH replacement if hard fault): exact FRU/PN/SN above, LOTO, swap, OMC validation.
Be concise."""


_OAP_URL      = "https://ns-oap.ebiz.verizon.com/agent-gateway/api/v1/agents/chat/generate"
_OAP_TOKEN    = "agw_6a51d252_lHqsjsIYBdM1iLKiYkOHv4rDj4nFBjOV"
_OAP_AGENT    = "AI Powered RRH OOS Diagnostics & Remediation"
_OAP_USER_EID = "5492597915"


def _parse_oap_response(data: dict) -> str:
    """Extract AI text from OAP gateway response (standard or LangGraph/Agentic format)."""
    raw = ""
    if isinstance(data.get("response"), str) and data["response"].strip():
        raw = data["response"]
    elif isinstance(data.get("messages"), list):
        for item in data["messages"]:
            if isinstance(item.get("messages"), list):
                for msg in item["messages"]:
                    if msg.get("type") == "ai" and isinstance(msg.get("content"), str):
                        raw = msg["content"]
    if not raw.strip():
        raise RuntimeError(f"OAP: could not locate AI content in response keys: {list(data.keys())}")
    return raw


def _call_llm(prompt: str, context: str = "") -> str:
    """Call OAP agent gateway (primary) with VEGAS UAT as fallback."""
    _oap_err_msg = None
    try:
        merged = f"{prompt}\n\nContext:\n{context}" if context else prompt
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                _OAP_URL,
                json={
                    "agent_name": _OAP_AGENT,
                    "user_eid": _OAP_USER_EID,
                    "message": merged,
                    "stream": False,
                },
                headers={
                    "Authorization": f"Bearer {_OAP_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=180,
                verify=False,
            )
        r.raise_for_status()
        return _parse_oap_response(r.json())
    except Exception as _oap_err:
        _oap_err_msg = f"{type(_oap_err).__name__}: {_oap_err}"

    # Fallback: VEGAS UAT with retry on 429
    payload = {
        "useCase": "AGENTS",
        "contextId": "AGENTS",
        "preSeed_injection_map": {
            "{ROLE}": "ADMIN",
            "{TOOLS}": "iop",
            "{CONTEXT}": context,
            "{QUERY}": prompt,
        },
        "parameters": {"temperature": 0.2, "maxOutputTokens": 2000},
    }
    delay = 30
    for attempt in range(1, 4):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = httpx.post(
                VEGAS_URL,
                json=payload,
                headers={"x-apikey": VEGAS_KEY, "Content-Type": "application/json"},
                timeout=180,
                verify=False,
            )
        if r.status_code == 429 and attempt < 3:
            time.sleep(delay)
            delay *= 2
            continue
        if r.status_code != 200:
            oap_note = f" | OAP error: {_oap_err_msg}" if _oap_err_msg else ""
            raise RuntimeError(f"VEGAS {r.status_code}: {r.text[:300]}{oap_note}")
        return r.json()["prediction"]
    oap_note = f" | OAP error: {_oap_err_msg}" if _oap_err_msg else ""
    raise RuntimeError(f"VEGAS still 429 after retries{oap_note}")


def run_diagnosis(site_id: str, findings: dict, switch_df: pd.DataFrame) -> str:
    prompt = build_llm_prompt(site_id, findings, switch_df)
    return _call_llm(prompt, context=str(findings.get("archetype_label", "")))


# ─────────────────────────────────────────────
# SAMSUNG PARSER
# ─────────────────────────────────────────────

def parse_healthcheck_samsung(html_bytes: bytes) -> dict:
    soup = BeautifulSoup(html_bytes.decode("utf-8", errors="replace"), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()

    sections = defaultdict(list)
    for tag in soup.find_all(attrs={"data-section": True}):
        sec = tag.get("data-section")
        identifier = tag.get("data-identifier", "")
        text = tag.get_text(strip=True)
        sections[sec].append({"identifier": identifier, "text": text})

    tables = _parse_html_tables(soup)

    sections["_alarms"]        = _find_table(tables, "SPECIFIC PROBLEM", "LOCATION", "SEVERITY")
    sections["_cell_detail"]   = _find_table(tables, "CELLNUM", "ADMIN STATE", "OPERATIONALSTATE")
    sections["_radio_hw"]      = _find_table(tables, "BOARD TYPE", "RADIO UNITSERIAL NUMBER", "OPERATIONALSTATE")
    sections["_ecp_optical"]   = _find_table(tables, "TxWAVELENGTH", "Tx POWER", "Rx POWER", "BIT RATE")
    sections["_rrh_optical"]   = _find_table(tables, "TX - WAVELENGTH", "TX - POWER", "RX - POWER")
    sections["_sfp_ecp"]       = _find_table(tables, "PORTTYPE", "PORTID", "HARDWARENAME", "POSITION")
    sections["_sfp_rrh"]       = _find_table(tables, "UNIT ID", "UNITID", "HARDWARENAME", "SERIALNUMBER")
    sections["_vswr_table"]    = _find_table(tables, "TX-RF-POWER", "RETURN-LOSS", "VSWR")
    sections["_rssi_summary"]  = _find_table(tables, "RSSI Average", "OCNS OFF / ON")
    sections["pim-table"]      = _find_table(tables, "PIM dB", "Return Loss dB")

    site_id = None
    raw_text = html_bytes.decode("utf-8", errors="replace")
    m = re.search(r"ENB\[(\d+)\]", raw_text)
    if m:
        site_id = m.group(1)

    sections["_site_id"] = site_id
    return dict(sections)


# ─────────────────────────────────────────────
# RULE ENGINE — Samsung Archetype A / B
# ─────────────────────────────────────────────

_SAM_A_PATTERNS = [
    r"ecp cpri-fail", r"line-interface-failure", r"optic-transceiver-rx-los",
    r"rrh vswr-fail",
]
_SAM_B_PATTERNS = [
    r"cell-disabled", r"out-of-service", r"service-off",
]
_SAM_HARD_FAULT_PATTERNS = [
    r"ecp cpri-fail", r"line-interface-failure", r"optic-transceiver-rx-los",
]

_SAM_VSWR_THRESHOLD     = 1.5
_SAM_RL_THRESHOLD_DB    = 14.0
_SAM_RSSI_HIGH_DBM      = -85.0
_SAM_RX_BAD_DBM         = -35.0


def classify_samsung(hc: dict) -> dict:
    alarms        = hc.get("_alarms", [])
    cell_detail   = hc.get("_cell_detail", [])
    radio_hw      = hc.get("_radio_hw", [])
    ecp_optical   = hc.get("_ecp_optical", [])
    rrh_optical   = hc.get("_rrh_optical", [])
    vswr_table    = hc.get("_vswr_table", [])
    rssi_summary  = hc.get("_rssi_summary", [])
    pim_table     = hc.get("pim-table", [])
    cpri_ports    = hc.get("cpri", [])
    sfp_ecp       = hc.get("_sfp_ecp", [])

    alarm_text_combined = " ".join(
        f"{a.get('SPECIFIC PROBLEM','')} {a.get('PROB CAUSE','')} {a.get('DESCRIPTION','')}"
        for a in alarms
    )

    a_score        = sum(1 for p in _SAM_A_PATTERNS if re.search(p, alarm_text_combined, re.I))
    b_score        = sum(1 for p in _SAM_B_PATTERNS if re.search(p, alarm_text_combined, re.I))
    has_hard_fault = any(re.search(p, alarm_text_combined, re.I) for p in _SAM_HARD_FAULT_PATTERNS)

    cpri_failures = [
        a for a in alarms
        if re.search(r"ecp cpri-fail|line-interface-failure", a.get("SPECIFIC PROBLEM",""), re.I)
    ]
    vswr_failures = [
        a for a in alarms
        if re.search(r"rrh vswr-fail|vswr", a.get("SPECIFIC PROBLEM",""), re.I)
    ]

    disabled_cells = [
        c for c in cell_detail
        if c.get("OPERATIONALSTATE","").lower() == "disabled"
    ]
    if not disabled_cells:
        disabled_cells = [
            {"CELLNUM": c["identifier"].replace("cellNum_",""),
             "text": c["text"], "OPERATIONALSTATE": "disabled"}
            for c in hc.get("cell-status", [])
            if "disabled" in c["text"].lower()
        ]

    disabled_radios = [
        r for r in radio_hw
        if r.get("OPERATIONALSTATE","").lower() == "disabled"
    ]

    bad_optical = []
    for item in cpri_ports:
        uid = item["identifier"]
        numbers = re.findall(r"-?\d+\.\d+", item["text"])
        if numbers:
            rx = float(numbers[-1])
            if rx < _SAM_RX_BAD_DBM:
                bad_optical.append({
                    "unit_id": uid,
                    "rx_power_dBm": rx,
                    "flag": f"CPRI Rx={rx} dBm (< {_SAM_RX_BAD_DBM} dBm threshold)",
                    "source": "CPRI data-section",
                })

    vswr_high = []
    for row in vswr_table:
        try:
            v = float(row.get("VSWR","nan"))
            if v > _SAM_VSWR_THRESHOLD:
                vswr_high.append({**row, "flag": f"VSWR={v} > {_SAM_VSWR_THRESHOLD}"})
        except ValueError:
            pass
        try:
            rl = float(row.get("RETURN-LOSS","nan"))
            if rl < _SAM_RL_THRESHOLD_DB:
                vswr_high.append({**row, "flag": f"Return Loss={rl} dB < {_SAM_RL_THRESHOLD_DB} dB"})
        except ValueError:
            pass

    rssi_high = []
    for row in rssi_summary:
        try:
            avg = float(row.get("RSSI Average","nan"))
            if avg > _SAM_RSSI_HIGH_DBM:
                rssi_high.append({**row, "flag": f"RSSI={avg} dBm > {_SAM_RSSI_HIGH_DBM} dBm"})
        except ValueError:
            pass

    pim_high = []
    for row in pim_table:
        try:
            if float(row.get("PIM dB","nan")) >= 5:
                pim_high.append({**row, "flag": f"PIM {row['PIM dB']} dB"})
        except ValueError:
            pass
        try:
            if float(row.get("Return Loss dB","nan")) < _SAM_RL_THRESHOLD_DB:
                pim_high.append({**row, "flag": f"RL {row['Return Loss dB']} dB < 14 dB"})
        except ValueError:
            pass

    hw_details = [
        {
            "unit_id":    r.get("UNIT ID",""),
            "board_type": r.get("BOARD TYPE",""),
            "serial":     r.get("RADIO UNITSERIAL NUMBER",""),
            "op_state":   r.get("OPERATIONALSTATE",""),
            "cells":      r.get("CELLNUMBER",""),
        }
        for r in radio_hw
    ]

    failing_sfps = []
    for cf in cpri_failures:
        loc = cf.get("LOCATION","")
        slot_m = re.search(r"SLOT\[(\d+)\]", loc)
        port_m = re.search(r"CPRI_PORT\[(\d+)\]", loc)
        if slot_m and port_m:
            slot_id, port_id = slot_m.group(1), port_m.group(1)
            for sfp in sfp_ecp:
                pos = sfp.get("POSITION","")
                if f"SLOT[{slot_id}]" in pos and f"CPRI_PORT[{port_id}]" in pos:
                    failing_sfps.append({**sfp, "alarm_location": loc})

    if a_score > 0 and (has_hard_fault or bad_optical or cpri_failures):
        archetype, label = "A", "Physical Fronthaul / Hardware Fault"
    elif b_score > 0 and not has_hard_fault and not cpri_failures:
        archetype, label = "B", "Logical Cell / Service Outage"
    elif a_score > 0:
        archetype, label = "A", "Physical Fronthaul / Hardware Fault"
    else:
        archetype, label = "UNKNOWN", "Insufficient data — manual review required"

    dispatch_warranted = bool(
        has_hard_fault
        or (cpri_failures and bad_optical)
        or vswr_failures
    )

    return {
        "archetype":          archetype,
        "archetype_label":    label,
        "dispatch_warranted": dispatch_warranted,
        "has_hard_fault":     has_hard_fault,
        "cpri_failures":      cpri_failures,
        "vswr_failures":      vswr_failures,
        "disabled_cells":     disabled_cells,
        "disabled_radios":    disabled_radios,
        "bad_optical":        bad_optical,
        "vswr_high":          vswr_high,
        "rssi_high":          rssi_high,
        "pim_high":           pim_high,
        "hw_details":         hw_details,
        "failing_sfps":       failing_sfps,
        "all_alarms":         alarms,
        "alarm_texts":        alarm_text_combined,
        "a_score":            a_score,
        "b_score":            b_score,
        "ecp_optical":        ecp_optical,
        "rrh_optical":        rrh_optical,
        "vswr_table":         vswr_table,
        "rssi_summary":       rssi_summary,
        "pim_table":          pim_table,
    }


# ─────────────────────────────────────────────
# LLM NARRATION — Samsung
# ─────────────────────────────────────────────

def build_llm_prompt_samsung(site_id: str, findings: dict, switch_df: pd.DataFrame) -> str:
    dc_lines = "; ".join(
        f"Cell {c.get('CELLNUM', c.get('text','?'))} {c.get('USER LABEL','')}"
        for c in findings["disabled_cells"]
    ) or "None"

    cpri_lines = "; ".join(
        a.get("LOCATION","") for a in findings["cpri_failures"]
    ) or "None"

    sfp_lines = "\n".join(
        f"  HW={s.get('HARDWARENAME','')} SN={s.get('SERIALNUMBER','')} "
        f"Vendor={s.get('VENDORNAME','')} Position={s.get('POSITION','')} "
        f"[alarm: {s.get('alarm_location','')}]"
        for s in findings["failing_sfps"]
    ) or "  None matched"

    vswr_lines = "; ".join(
        f"{a.get('LOCATION','')} Cell={a.get('CELL ID','')}"
        for a in findings["vswr_failures"]
    ) or "None"

    dr_lines = "; ".join(
        f"Unit={r.get('UNIT ID','?')} Board={r.get('BOARD TYPE','?')} SN={r.get('RADIO UNITSERIAL NUMBER','?')}"
        for r in findings["disabled_radios"]
    ) or "None"

    optical_lines = "; ".join(
        f"{o['unit_id']} Rx={o['rx_power_dBm']}dBm"
        for o in findings["bad_optical"]
    ) or "None"

    rssi_lines = "; ".join(
        f"Cell={r.get('CELL NUMBER','?')} Unit={r.get('UNIT ID','?')} RSSI={r.get('RSSI Average','?')}dBm"
        for r in findings["rssi_high"]
    ) or "None"

    if not switch_df.empty and "Description" in switch_df.columns:
        sw_lines = "; ".join(
            f"{r.get('Severity','')} {str(r.get('Description',''))[:60]}"
            for _, r in switch_df.head(3).iterrows()
        )
    else:
        sw_lines = "None"

    dispatch_note = "DISPATCH WARRANTED" if findings["dispatch_warranted"] else "TRY REMOTE FIRST"

    return f"""Samsung RAN NOC expert. Site {site_id}. Archetype {findings['archetype']} ({findings['archetype_label']}). {dispatch_note}.

KEY FACTS (use these exact IDs — do not invent):
Disabled cells: {dc_lines}
Disabled radios: {dr_lines}
CPRI failures (dis-cpri-port = FAULTY_DISCONNECTED/LOS_DETECTED): {cpri_lines}
Bad optical Rx (< -35 dBm): {optical_lines}
Failing SFP details:
{sfp_lines}
VSWR failures: {vswr_lines}
RSSI high (PIM, > -85 dBm): {rssi_lines}
Switch alarms: {sw_lines}

SAMSUNG CLI COMMANDS (do NOT use Ericsson commands):
dis-alarm: severity=all | dis-cpri-port | dis-cell: cellId=<N> | bls-cell: cellId=<N> | ubl-cell: cellId=<N> | init-pcb: rackId=0,shelfId=0,slotId=<N> (wait 8 min for POST)
Decision: FAULTY_DISCONNECTED/LOS_DETECTED on dis-cpri-port → skip to TASK A. Post init-pcb still shows fault → TASK B.

Produce exactly:
## Section 1 — Remote Troubleshooting Steps
4 steps: (1) dis-alarm + dis-cell with real cell IDs above (2) dis-cpri-port for CPRI fault (3) bls-cell/ubl-cell toggle then init-pcb if needed (4) RSSI/VSWR review.
Pass criteria and fail action per step. End: "If remote fails or FAULTY_DISCONNECTED detected → Section 2."

## Section 2 — Precise Dispatch Instructions
Open: "REMOTE TROUBLESHOOTING EXHAUSTED — TARGET NODE: {site_id}"
State fault evidence. Then:
TASK A: [SAMSUNG CPRI/eCPRI LINK DOWN WORK ORDER] (if CPRI failure)
  [ ] 1. Open DC PDU; locate breaker for ECP slot from SFP details above
  [ ] 2. Measure voltage; drop breaker OFF 60s
  [ ] 3. Scope and clean fiber jumpers at exact position from SFP details above
  [ ] 4. Replace SFP: HW/SN/Vendor/Position from FAILING SFP DETAILS above
  [ ] 5. Restore power; verify CPRI alignment; confirm Rx normalizes (-15 to -28 dBm)
TASK B: [SAMSUNG HARDWARE UNIT REPLACE WORK ORDER] (if VSWR failure or disabled radio)
  [ ] 1. LOTO on DC breaker for unit/board/SN from facts above
  [ ] 2. Verify physical position and band tags on tower
  [ ] 3. Detach RF coax, power, fiber; cap open jumpers
  [ ] 4. Swap degraded unit; torque to spec; remove LOTO; power up
  [ ] 5. dis-cell: cellId=<N> confirm STATUS=ACTIVE; dis-alarm: severity=all confirm cleared
Be concise."""


def run_diagnosis_samsung(site_id: str, findings: dict, switch_df: pd.DataFrame) -> str:
    prompt = build_llm_prompt_samsung(site_id, findings, switch_df)
    return _call_llm(prompt, context=f"Samsung {findings.get('archetype_label','')}")


# ─────────────────────────────────────────────
# RULE-BASED PROBLEM SUMMARY
# ─────────────────────────────────────────────

def build_summary(site_id: str, vendor: str, findings: dict) -> str:
    """Return a 1-2 sentence plain-English problem summary from parsed findings."""
    arch   = findings.get("archetype", "")
    label  = findings.get("archetype_label", "")
    disp   = findings.get("dispatch_warranted", False)
    dcells = findings.get("disabled_cells", [])
    dradio = findings.get("disabled_radios", [])

    # ── Ericsson ──
    if vendor == "Ericsson":
        nok    = findings.get("nok_links", [])
        hw     = findings.get("hw_details", [])
        pim    = findings.get("pim_high", [])

        parts = []

        if arch == "A":
            if nok:
                link_ids = ", ".join(n.get("id", "") for n in nok[:3])
                parts.append(f"Site {site_id} has a Physical Fronthaul Layer failure on RiLink(s) {link_ids}")
            else:
                parts.append(f"Site {site_id} has a Archetype A hardware fault ({label})")
        elif arch == "B":
            parts.append(f"Site {site_id} has a software/configuration fault ({label})")
        else:
            parts.append(f"Site {site_id} fault classification: {label or 'Unknown'}")

        cell_count  = len(dcells)
        radio_count = len(dradio)
        if cell_count or radio_count:
            parts.append(
                f"{cell_count} cell(s) and {radio_count} radio(s) are currently disabled."
            )

        if hw:
            fru = hw[0].get("fru", "")
            pn  = hw[0].get("pn", "")
            if fru:
                parts.append(f"Suspected failed unit: {fru}" + (f" (PN: {pn})" if pn else "") + ".")

        if pim:
            parts.append(f"{len(pim)} PIM/Return-Loss flag(s) detected — flag for field inspection.")

        if disp:
            parts.append("Dispatch is warranted; exhaust remote steps first.")
        else:
            parts.append("Remote recovery may be possible — attempt Section 1 steps before dispatching.")

        return " ".join(parts)

    # ── Samsung ──
    cpri  = findings.get("cpri_failures", [])
    vswr  = findings.get("vswr_failures", [])
    sfp   = findings.get("sfp_details", [])
    bad_opt = findings.get("bad_optical", [])

    parts = []

    if arch == "A":
        if cpri:
            ports = ", ".join(c.get("LOCATION", "") for c in cpri[:3])
            parts.append(f"Site {site_id} has a CPRI/fiber link failure on port(s): {ports}.")
        elif vswr:
            parts.append(f"Site {site_id} has {len(vswr)} VSWR failure(s) indicating antenna or RF path fault.")
        else:
            parts.append(f"Site {site_id} has a hard hardware fault ({label}).")
    elif arch == "B":
        parts.append(f"Site {site_id} has disabled cells/services ({label}) — likely a software or config issue.")
    else:
        parts.append(f"Site {site_id} fault: {label or 'Unknown'}.")

    cell_count  = len(dcells)
    radio_count = len(dradio)
    if cell_count or radio_count:
        parts.append(f"{cell_count} cell(s) and {radio_count} radio(s) are currently disabled.")

    if sfp:
        s = sfp[0]
        pos = s.get("POSITION", s.get("HW/SN", ""))
        parts.append(f"Suspected failing SFP at position {pos}." if pos else "Failing SFP identified.")

    if bad_opt:
        parts.append(f"{len(bad_opt)} optical port(s) below acceptable Rx power threshold.")

    if disp:
        parts.append("Dispatch is warranted; exhaust remote steps first.")
    else:
        parts.append("Remote recovery may be possible — attempt Section 1 steps before dispatching.")

    return " ".join(parts)


# ─────────────────────────────────────────────
# PDF REPORT BUILDER
# ─────────────────────────────────────────────

def build_pdf(site_id: str, vendor: str, findings: dict, llm_result: str, summary: str = "") -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    L, R = 15, 15
    pdf.set_left_margin(L)
    pdf.set_right_margin(R)
    PW = pdf.w - L - R

    def _s(text: str) -> str:
        _map = [
            ("—", "--"), ("–", "-"), ("→", "->"), ("←", "<-"),
            ("•", "*"), ("●", "*"), ("✓", "[x]"), ("☐", "[ ]"),
            ("☑", "[x]"), ("°", "deg"), ("≥", ">="), ("≤", "<="),
            ("'", "'"), ("'", "'"), (""", '"'), (""", '"'),
        ]
        for src, dst in _map:
            text = text.replace(src, dst)
        return text.encode("latin-1", errors="replace").decode("latin-1")

    def _strip_inline(text: str) -> str:
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        return text

    def _mc(text: str, w: float = None, indent: float = 0):
        pdf.set_x(L + indent)
        try:
            pdf.multi_cell(w if w is not None else PW - indent, 5, _s(text),
                           new_x="LMARGIN", new_y="NEXT")
        except Exception:
            pass

    # ── Header ──
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "AI Powered RRH OOS Diagnostic Report", ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    arch_label = _s(findings.get("archetype_label", ""))
    pdf.cell(0, 6, f"Site: {site_id}   |   Vendor: {vendor}   |   Archetype: {findings.get('archetype','')} - {arch_label}", ln=True, align="C")
    disp_txt = "YES - Hard fault confirmed" if findings.get("dispatch_warranted") else "Pending - exhaust remote steps first"
    pdf.cell(0, 6, f"Dispatch Warranted: {disp_txt}", ln=True, align="C")
    pdf.ln(4)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(L, pdf.get_y(), pdf.w - R, pdf.get_y())
    pdf.ln(6)

    body = llm_result
    if "## Section 2" in body:
        sec1_raw, sec2_raw = body.split("## Section 2", 1)
        sec1 = re.sub(r"^##\s*Section 1.*", "", sec1_raw, flags=re.MULTILINE).strip()
        sec2 = re.sub(r"^[\s\-—]*(?:Precise\s+)?Dispatch Instructions.*", "", sec2_raw, flags=re.MULTILINE).strip()
    else:
        sec1, sec2 = body.strip(), ""

    if summary:
        pdf.set_fill_color(26, 26, 46)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_x(L)
        pdf.multi_cell(PW, 7, "  Diagnostic Summary", fill=True,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.set_fill_color(240, 240, 248)
        pdf.set_text_color(30, 30, 30)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(L)
        pdf.multi_cell(PW, 5.5, _s(summary), fill=True,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        pdf.set_fill_color(255, 255, 255)
        pdf.ln(5)

    def _section_header(title: str):
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_fill_color(30, 30, 30)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 8, f"  {_s(title)}", ln=True, fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    def _render_body(body: str):
        in_code = False
        for raw in body.splitlines():
            s = raw.strip()

            if s.startswith("```"):
                in_code = not in_code
                if in_code:
                    pdf.ln(1)
                else:
                    pdf.ln(2)
                continue

            if in_code:
                if s:
                    pdf.set_font("Courier", "", 8)
                    pdf.set_fill_color(242, 242, 242)
                    pdf.set_text_color(20, 20, 100)
                    pdf.set_x(L)
                    try:
                        pdf.multi_cell(PW, 4.5, _s(s), fill=True,
                                       new_x="LMARGIN", new_y="NEXT")
                    except Exception:
                        try:
                            pdf.set_x(L)
                            pdf.multi_cell(PW, 4.5, _s(s[:120]), fill=True,
                                           new_x="LMARGIN", new_y="NEXT")
                        except Exception:
                            pass
                    pdf.set_fill_color(255, 255, 255)
                    pdf.set_text_color(0, 0, 0)
                continue

            if not s:
                pdf.ln(2)
                continue

            if re.match(r"^[-*_]{3,}$", s):
                pdf.set_draw_color(200, 200, 200)
                pdf.line(L, pdf.get_y(), pdf.w - R, pdf.get_y())
                pdf.ln(3)
                continue

            if s.startswith("###"):
                text = _strip_inline(s.lstrip("#").strip())
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(20, 60, 130)
                _mc(text)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(1)
                continue

            if s.startswith("##"):
                text = _strip_inline(s.lstrip("#").strip())
                pdf.set_font("Helvetica", "B", 11)
                pdf.set_text_color(20, 60, 130)
                _mc(text)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(1)
                continue

            if s.startswith(">"):
                text = _strip_inline(s.lstrip(">").strip())
                pdf.set_font("Helvetica", "I", 9)
                pdf.set_text_color(100, 100, 100)
                _mc(text, indent=5)
                pdf.set_text_color(0, 0, 0)
                continue

            if re.match(r"^\[([ xX])\]", s):
                checked = s[1].lower() == "x"
                rest = _strip_inline(s[3:].strip())
                marker = "[x]" if checked else "[ ]"
                pdf.set_font("Helvetica", "", 9)
                _mc(f"  {marker} {rest}", indent=4)
                continue

            m = re.match(r"^(\d+)\.\s+(.*)", s)
            if m:
                text = _strip_inline(m.group(2))
                is_bold = "**" in m.group(2)
                pdf.set_font("Helvetica", "B" if is_bold else "", 9)
                _mc(f"{m.group(1)}. {text}", indent=4)
                continue

            if re.match(r"^[-*]\s+", s):
                text = _strip_inline(re.sub(r"^[-*]\s+", "", s))
                pdf.set_font("Helvetica", "", 9)
                _mc(f"- {text}", indent=4)
                continue

            if re.match(r"^(TASK [A-Z]|STEP \d)", s):
                text = _strip_inline(s)
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(160, 50, 0)
                _mc(text)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(1)
                continue

            if s.startswith("**") and s.endswith("**") and len(s) > 4:
                pdf.set_font("Helvetica", "B", 9)
                _mc(s[2:-2])
                continue

            text = _strip_inline(s)
            pdf.set_font("Helvetica", "", 9)
            _mc(text)

        pdf.ln(4)

    _section_header("Section 1 - Remote Troubleshooting Steps")
    _render_body(sec1)

    pdf.set_draw_color(180, 180, 180)
    pdf.line(L, pdf.get_y(), pdf.w - R, pdf.get_y())
    pdf.ln(4)

    if sec2:
        _section_header("Section 2 - Dispatch Instructions")
        _render_body(sec2)

    pdf.set_y(-15)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 10, f"Verizon RAN NOC - AI Powered RRH OOS Diagnostics | Site {site_id}", align="C")

    return bytes(pdf.output())
