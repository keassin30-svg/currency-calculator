#!/usr/bin/env python3
"""Fetch official CFETS RMB central parity and Bank of Russia reference rates."""
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

CFETS_LATEST_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
CFETS_HISTORY_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew"
CBR_LATEST_URL = "https://www.cbr.ru/scripts/XML_daily_eng.asp"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CurrencyCalculator/2.0; +https://keassin30-svg.github.io/currency-calculator/)",
    "Accept": "application/json, text/xml, application/xml, */*",
    "Referer": "https://www.chinamoney.com.cn/english/bmkcpr/",
    "Origin": "https://www.chinamoney.com.cn",
    "X-Requested-With": "XMLHttpRequest",
}
PAIRS = ("USD/CNY", "EUR/CNY", "GBP/CNY", "CNY/RUB")
CURRENCIES = ("CNY", "RUB", "GBP", "USD", "EUR")


def fetch_bytes(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None) -> bytes:
    req_headers = dict(HEADERS)
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    with urllib.request.urlopen(req, timeout=35) as response:
        return response.read()


def number(value) -> float:
    text = str(value).strip().replace(",", "")
    if not text or text in {"---", "-", "None", "null"}:
        raise ValueError("missing numeric rate")
    return float(text)


def fetch_cfets_latest():
    payload = json.loads(fetch_bytes(CFETS_LATEST_URL).decode("utf-8-sig"))
    head = payload.get("head", {})
    if str(head.get("rep_code", "200")) != "200":
        raise RuntimeError("CFETS returned an error: " + str(head.get("rep_message", "")))
    records = payload.get("records", [])
    pairs = {}
    for item in records:
        pair = str(item.get("vrtEName", "")).upper()
        if pair in PAIRS:
            pairs[pair] = number(item.get("price"))
    missing = [pair for pair in PAIRS if pair not in pairs]
    if missing:
        raise RuntimeError("CFETS latest data missing: " + ", ".join(missing))
    raw_date = payload.get("data", {}).get("lastDate", "")
    rate_date = str(raw_date)[:10]
    if len(rate_date) != 10:
        raise RuntimeError("CFETS date missing or invalid")
    return rate_date, raw_date, pairs


def fetch_cbr_latest():
    try:
        raw = fetch_bytes(CBR_LATEST_URL)
        root = ET.fromstring(raw)
        raw_date = root.attrib.get("Date", "")
        rate_date = datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat()
        rub_per_unit = {}
        for item in root.findall("Valute"):
            code = item.findtext("CharCode", "").upper()
            if code not in {"CNY", "USD", "EUR", "GBP"}:
                continue
            nominal = number(item.findtext("Nominal", "1"))
            value = number(item.findtext("Value", ""))
            if nominal <= 0:
                continue
            rub_per_unit[code] = value / nominal
        if len(rub_per_unit) != 4:
            raise RuntimeError("CBR did not return all four required currencies")
        return {
            "date": rate_date,
            "rub_per_unit": rub_per_unit,
            "source": "Bank of Russia official daily exchange rates",
            "url": "https://www.cbr.ru/eng/currency_base/daily/",
        }
    except Exception as exc:
        print("WARNING: Bank of Russia cross-check unavailable:", exc)
        return None


def fetch_history_pair(pair: str, start: date, end: date):
    params = {
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "currency": pair,
        "pageNum": "1",
        "pageSize": "100",
        "t": str(int(datetime.now(timezone.utc).timestamp())),
    }
    url = CFETS_HISTORY_URL + "?" + urllib.parse.urlencode(params)
    try:
        raw = fetch_bytes(url)
        payload = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        # Some CFETS deployments accept the same query with a JSON POST body.
        body = json.dumps({"pageNum": 1, "pageSize": 100}).encode("utf-8")
        raw = fetch_bytes(
            url,
            method="POST",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        payload = json.loads(raw.decode("utf-8-sig"))

    if str(payload.get("head", {}).get("rep_code", "200")) != "200":
        raise RuntimeError("CFETS history API returned an error")
    rows = {}
    for item in payload.get("records", []):
        day = str(item.get("date", ""))[:10]
        values = item.get("values") or []
        if len(day) == 10 and values:
            try:
                rows[day] = number(values[0])
            except (TypeError, ValueError):
                pass
    return rows


def make_rate_table(pairs: dict[str, float]):
    # CFETS quotes USD/CNY, EUR/CNY and GBP/CNY as CNY per 1 foreign unit;
    # CNY/RUB is RUB per 1 CNY. Convert all currencies to a common CNY pivot.
    cny_per_unit = {
        "CNY": 1.0,
        "USD": pairs["USD/CNY"],
        "EUR": pairs["EUR/CNY"],
        "GBP": pairs["GBP/CNY"],
        "RUB": 1.0 / pairs["CNY/RUB"],
    }
    table = {}
    for base in CURRENCIES:
        table[base] = {}
        for target in CURRENCIES:
            if target != base:
                table[base][target.lower()] = cny_per_unit[base] / cny_per_unit[target]
    return cny_per_unit, table


def read_history():
    path = DATA_DIR / "history.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            row["date"]: dict(row.get("pairs", {}))
            for row in payload.get("records", [])
            if row.get("date")
        }
    except Exception:
        return {}


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    cfets_date, cfets_timestamp, latest_pairs = fetch_cfets_latest()
    cbr = fetch_cbr_latest()

    cny_per_unit, rates = make_rate_table(latest_pairs)
    latest = {
        "date": cfets_date,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": "China Foreign Exchange Trade System (CFETS), authorized by the People's Bank of China",
        "source_url": "https://www.chinamoney.com.cn/english/bmkcpr/",
        "source_api": CFETS_LATEST_URL,
        "source_timestamp": cfets_timestamp,
        "quote_method": "Official CNY central parity; other currency pairs are cross-rates derived from the same-day CNY parity.",
        "pairs": latest_pairs,
        "cny_per_unit": cny_per_unit,
        "rates": rates,
        "cbr_reference": cbr,
        "notes": [
            "CFETS is authorized by the People's Bank of China to publish RMB central parity rates.",
            "Bank of Russia official rates are shown as an independent RUB cross-check when available.",
            "The Bank of England states that its database exchange rates are not official; GBP uses the CFETS CNY/GBP central parity.",
            "Reference rates are not bank, payment-provider, or marketplace settlement quotes."
        ],
    }
    write_json(DATA_DIR / "latest.json", latest)

    history = read_history()
    for pair in PAIRS:
        try:
            rows = fetch_history_pair(pair, date.fromisoformat(cfets_date) - timedelta(days=45), date.fromisoformat(cfets_date))
            print("History", pair, "received", len(rows), "rows")
            for day, value in rows.items():
                history.setdefault(day, {})[pair] = value
        except Exception as exc:
            print("WARNING: CFETS history fetch failed for", pair, ":", exc)

    # Always keep the latest published fixing in the local history file.
    history.setdefault(cfets_date, {}).update(latest_pairs)
    cutoff = date.fromisoformat(cfets_date) - timedelta(days=400)
    history = {
        day: pairs
        for day, pairs in history.items()
        if len(day) == 10 and date.fromisoformat(day) >= cutoff
    }
    history_rows = [
        {"date": day, "pairs": history[day]}
        for day in sorted(history)
    ]
    write_json(DATA_DIR / "history.json", {
        "source_name": "CFETS historical CNY central parity",
        "source_url": "https://www.chinamoney.com.cn/english/bmkcpr/",
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records": history_rows,
    })
    print("Saved latest CFETS date:", cfets_date, "history records:", len(history_rows))


if __name__ == "__main__":
    main()
