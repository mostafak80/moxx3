"""
Currency Converter - Flask App
"""

import os
import re
import time
import logging
from dataclasses import dataclass, field
from typing import Optional

import json
import requests
from flask import Flask, render_template, request, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

API_KEY         = os.getenv("EXCHANGE_API_KEY", "662bb25b4b70f2546b90fcfe")
API_URL         = f"https://v6.exchangerate-api.com/v6/{API_KEY}/latest/USD"
CACHE_TTL       = 3600
MARKUP_RATE     = 1.025
TARGET_CURRENCY = "EGP"

FIXED_RATES_TO_SAR = {
    "EGP": lambda a: (a / 53.00) * 3.75,
    "IQD": lambda a: (a / 3500),
    "USD": lambda a: a * 3.75,
    "MVR": lambda a: (a / 4.09) * 3.75,
    "JPY": lambda a: (a * 0.00703) * 3.76,
    "SAR": lambda a: a,
}

SAR_TO_EGP = 51.50 / 3.75


@dataclass
class RateCache:
    rates: dict = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.fetched_at) > CACHE_TTL

    def update(self, rates: dict):
        self.rates = rates
        self.fetched_at = time.time()
        logger.info("تم تحديث الاسعار — %d عملة", len(rates))


_cache = RateCache()


def get_exchange_rates() -> dict:
    if not _cache.is_stale:
        return _cache.rates

    try:
        resp = requests.get(API_URL, timeout=5)
        resp.raise_for_status()
        data = resp.json()

        if data.get("result") == "success":
            _cache.update(data["conversion_rates"])
            return _cache.rates

    except requests.RequestException as e:
        logger.error("خطأ في الاتصال: %s", e)

    if _cache.rates:
        return _cache.rates

    return {
        "EGP": 50.5,
        "SAR": 3.75,
        "IQD": 1310,
        "MVR": 15.4,
        "JPY": 149.5,
        "USD": 1.0,
    }


def convert_live(amount: float, currency: str, rates: dict) -> Optional[float]:
    """تحويل للـ EGP بالأسعار الحية + هامش 2.5%"""
    if amount <= 0:
        return None

    amount *= MARKUP_RATE

    if currency == TARGET_CURRENCY:
        return round(amount, 2)

    if currency == "IQD":
        amount /= 10

    usd_rate = rates.get(currency)
    egp_rate = rates.get(TARGET_CURRENCY)

    if not usd_rate or not egp_rate:
        return None

    return round((amount / usd_rate) * egp_rate, 2)


def convert_fixed(amount: float, currency: str) -> Optional[float]:
    """تحويل للـ EGP بالأسعار الثابتة + هامش 2.5%"""
    if amount <= 0 or currency not in FIXED_RATES_TO_SAR:
        return None

    amount_in_sar = FIXED_RATES_TO_SAR[currency](amount) * MARKUP_RATE
    return round(amount_in_sar * SAR_TO_EGP, 2)


_RE_NEW = re.compile(
    r"عملية انترنت\s+ب:\s*(\d+(?:\.\d+)?)\s+([A-Za-z]{3})"
    r"\s+من:\s*(.*?)\s+بطاقة:\s*(\*\d+)\s+في:\s*(\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2})",
    re.MULTILINE,
)

_RE_OLD_AMOUNT   = re.compile(r"المبلغ\s*[:\-]\s*([\d.]+)\s+([A-Za-z]{3})")
_RE_OLD_CARD     = re.compile(r"البطاقة\s*[:\-]\s*(\S+)")
_RE_OLD_MERCHANT = re.compile(r"\bفي\s*[:\-]\s*(.+)")
_RE_OLD_DATE     = re.compile(r"بتاريخ\s*[:\-]\s*(.+)")
_RE_OLD_ACCOUNT  = re.compile(r"من حساب رقم\s*[:\-]\s*(\S+)")


def _safe_group(match, n):
    try:
        return match.group(n).strip() if match else "—"
    except Exception:
        return "—"


def _build_entry(amount, currency, merchant, card, date, account, rates) -> dict:
    currency = currency.upper()

    return {
        "amount": amount,
        "currency": currency,
        "merchant": merchant or "—",
        "card": card or "—",
        "date": date or "—",
        "account": account or "—",
        "live_egp": convert_live(amount, currency, rates),
        "fixed_egp": convert_fixed(amount, currency),
    }


def parse_transactions(raw: str, rates: dict) -> list:
    results = []
    seen_keys = set()

    for m in _RE_NEW.finditer(raw):
        amount = float(m.group(1))
        currency = m.group(2).upper()
        card = m.group(4).strip()

        seen_keys.add((round(amount, 2), card))

        results.append(
            _build_entry(
                amount,
                currency,
                m.group(3).strip(),
                card,
                m.group(5).strip(),
                None,
                rates,
            )
        )

    for block in raw.split("مشتريات إنترنت"):
        block = block.strip()
        am = _RE_OLD_AMOUNT.search(block)

        if not am:
            continue

        amount = float(am.group(1))
        currency = am.group(2).upper()
        card = _safe_group(_RE_OLD_CARD.search(block), 1)

        if (round(amount, 2), card) in seen_keys:
            continue

        results.append(
            _build_entry(
                amount,
                currency,
                _safe_group(_RE_OLD_MERCHANT.search(block), 1),
                card,
                _safe_group(_RE_OLD_DATE.search(block), 1),
                _safe_group(_RE_OLD_ACCOUNT.search(block), 1),
                rates,
            )
        )

    return results


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/result", methods=["POST"])
def result():
    raw_data = request.form.get("data", "").strip()

    if not raw_data:
        return render_template(
            "result.html",
            results=[],
            total_live=0,
            total_fixed=0,
            error="لا توجد بيانات.",
            egp_rate=53,
            rates_json=json.dumps(get_exchange_rates()),
        )

    rates = get_exchange_rates()
    results = parse_transactions(raw_data, rates)

    valid = [r for r in results if r["live_egp"] is not None]
    failed = len(results) - len(valid)

    total_live = round(sum(r["live_egp"] for r in valid), 2)
    total_fixed = round(
        sum(r["fixed_egp"] for r in valid if r["fixed_egp"] is not None),
        2,
    )

    logger.info(
        "%d عملية | %d فاشلة | اجمالي: %.2f EGP",
        len(valid),
        failed,
        total_live,
    )

    return render_template(
        "result.html",
        results=valid,
        total_live=total_live,
        total_fixed=total_fixed,
        failed=failed,
        egp_rate=round(rates.get("EGP", 50.5), 2),
        rates_json=json.dumps(rates),
    )


@app.route("/history")
def history():
    return render_template("history.html")


@app.route("/api/rates")
def api_rates():
    rates = get_exchange_rates()

    return jsonify(
        {
            "base": "USD",
            "rates": rates,
            "cached_at": _cache.fetched_at,
        }
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    get_exchange_rates()
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 5000)),
        debug=os.getenv("FLASK_DEBUG", "false").lower() == "true",
    )