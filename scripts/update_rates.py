#!/usr/bin/env python3
"""Fetch official Bank of Russia daily rates and CFETS CNY parity cross-check.\n\nThis script also backfills the recent official daily series for the 7-day and 30-day charts.\n"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

CBR_LATEST_URL = "https://www.cbr.ru/scripts/XML_daily_eng.asp"
CBR_HISTORY_URL = "https://www.cbr.ru/scripts/XML_dynamic.asp"
CFETS_LATEST_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"

CURRENCIES = ("CNY", "RUB", "GBP", "USD", "EUR")
CBR_CODES = {
    "USD": "R01235",
    "EUR": "R01239",
    "GBP": "R01035",
    "CNY": "R01375",
}
CFETS_PAIRS = ("USD/CNY", "EUR/CNY", "GBP/CNY", "CNY/RUB")

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "application/json, text/xml, application/xml, */*",
}


def fetch_bytes(url: str, headers: dict | None = None) -> bytes:
    req_headers = dict(BASE_HEADERS)
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.read()


def number(value) -> float:
    text = str(value).strip().replace(" ", "").replace(",", ".")
    if not text or text in {"---", "-", "None", "null"}:
        raise ValueError("missing numeric rate")
    return float(text)


def parse_cbr_xml(raw: bytes):
    root = ET.fromstring(raw)
    raw_date = root.attrib.get("Date", "")
    rate_date = datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat()
    rub_per_unit = {}
    for item in root.findall("Valute"):
        code = item.findtext("CharCode", "").upper()
        if code not in CBR_CODES:
            continue
        nominal = number(item.findtext("Nominal", "1"))
        value = number(item.findtext("Value", ""))
        if nominal > 0:
            rub_per_unit[code] = value / nominal
    missing = [code for code in CBR_CODES if code not in rub_per_unit]
    if missing:
        raise RuntimeError("CBR missing rates: " + ", ".join(missing))
    return rate_date, rub_per_unit


def fetch_cbr_latest():
    raw = fetch_bytes(CBR_LATEST_URL, {"Referer": "https://www.cbr.ru/eng/currency_base/daily/"})
    return parse_cbr_xml(raw)


def fetch_cfets_latest():
    payload = json.loads(fetch_bytes(
        CFETS_LATEST_URL,
        {"Referer": "https://www.chinamoney.com.cn/english/bmkcpr/"}
    ).decode("utf-8-sig"))
    if str(payload.get("head", {}).get("rep_code", "200")) != "200":
        raise RuntimeError("CFETS returned an error: " + str(payload.get("head", {}).get("rep_message", "")))
    pairs = {}
    for item in payload.get("records", []):
        pair = str(item.get("vrtEName", "")).upper()
        if pair in CFETS_PAIRS:
            pairs[pair] = number(item.get("price"))
    missing = [pair for pair in CFETS_PAIRS if pair not in pairs]
    if missing:
        raise RuntimeError("CFETS missing pairs: " + ", ".join(missing))
    raw_date = str(payload.get("data", {}).get("lastDate", ""))
    rate_date = raw_date[:10]
    if len(rate_date) != 10:
        raise RuntimeError("CFETS date missing or invalid")
    return rate_date, raw_date, pairs


def fetch_cbr_history(code: str, start: date, end: date):
    params = {
        "date_req1": start.strftime("%d/%m/%Y"),
        "date_req2": end.strftime("%d/%m/%Y"),
        "VAL_NM_RQ": CBR_CODES[code],
    }
    url = CBR_HISTORY_URL + "?" + urllib.parse.urlencode(params)
    raw = fetch_bytes(url, {"Referer": "https://www.cbr.ru/eng/currency_base/daily/"})
    root = ET.fromstring(raw)
    rows = {}
    for item in root.findall("Record"):
        raw_date = item.attrib.get("Date", "")
        if not raw_date:
            continue
        day = datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat()
        nominal = number(item.findtext("Nominal", "1"))
        value = number(item.findtext("Value", ""))
        if nominal > 0:
            rows[day] = value / nominal
    return rows


def make_rate_table(rub_per_unit: dict[str, float]):
    rub_per_unit = dict(rub_per_unit)
    rub_per_unit["RUB"] = 1.0
    table = {}
    for base in CURRENCIES:
        table[base] = {}
        for target in CURRENCIES:
            if target != base:
                table[base][target.lower()] = rub_per_unit[base] / rub_per_unit[target]
    return rub_per_unit, table


def read_history():
    path = DATA_DIR / "history.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = {}
        for row in payload.get("records", []):
            day = row.get("date")
            if not day:
                continue
            if row.get("rub_per_unit"):
                result[day] = dict(row["rub_per_unit"])
        return result
    except Exception:
        return {}


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    cbr_date, cbr_rub_rates = fetch_cbr_latest()
    cfets_date, cfets_timestamp, cfets_pairs = fetch_cfets_latest()
    rub_per_unit, rates = make_rate_table(cbr_rub_rates)

    latest = {
        "date": cbr_date,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": "Bank of Russia official daily exchange rates",
        "source_url": "https://www.cbr.ru/eng/currency_base/daily/",
        "source_api": CBR_LATEST_URL,
        "quote_method": "Official daily rates of CNY, GBP, USD and EUR against RUB; all other pairs are cross-rates derived from the same-day RUB rates.",
        "rub_per_unit": rub_per_unit,
        "rates": rates,
        "cfets_reference": {
            "date": cfets_date,
            "timestamp": cfets_timestamp,
            "source_name": "China Foreign Exchange Trade System (CFETS), authorized by the People's Bank of China",
            "source_url": "https://www.chinamoney.com.cn/english/bmkcpr/",
            "pairs": cfets_pairs,
        },
        "notes": [
            "Bank of Russia official daily exchange rates are the primary calculation source.",
            "CFETS RMB central parity is displayed as an independent PBOC-authorized reference for CNY pairs.",
            "The Bank of England states that its database exchange rates are not official; this page does not label GBP as a Bank of England official rate.",
            "Reference rates are not bank, payment-provider, or marketplace settlement quotes."
        ],
    }
    write_json(DATA_DIR / "latest.json", latest)

    history = read_history()
    end = date.fromisoformat(cbr_date)
    start = end - timedelta(days=45)
    for code in CBR_CODES:
        try:
            rows = fetch_cbr_history(code, start, end)
            print("CBR history", code, "received", len(rows), "rows")
            for day, value in rows.items():
                history.setdefault(day, {})[code] = value
        except Exception as exc:
            print("WARNING: CBR history fetch failed for", code, ":", exc)

    history.setdefault(cbr_date, {}).update(cbr_rub_rates)
    cutoff = end - timedelta(days=400)
    history = {
        day: rates_for_day
        for day, rates_for_day in history.items()
        if len(day) == 10 and date.fromisoformat(day) >= cutoff
    }
    rows = [
        {"date": day, "rub_per_unit": dict(history[day])}
        for day in sorted(history)
    ]
    write_json(DATA_DIR / "history.json", {
        "source_name": "Bank of Russia historical official daily exchange rates",
        "source_url": "https://www.cbr.ru/eng/currency_base/daily/",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records": rows,
    })
    print("Saved CBR date:", cbr_date, "CFETS date:", cfets_date, "history records:", len(rows))


if __name__ == "__main__":
    main()
