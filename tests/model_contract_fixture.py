"""Offline-only pricing fixture; never a runtime model default or price table."""
from datetime import date

MODEL = "offline-model"
BASE_URL = "https://offline.invalid/v1"
PRICE_DATE = date(2026, 10, 14)


def price_contract(**overrides):
    return {"model": MODEL, "base_url": BASE_URL, "input_limit": 1_000_000,
            "input_price_cny": "6", "output_price_cny": "24",
            "output_limit": "max_completion_tokens", "output_tolerance": 10,
            "response_models": [MODEL], "verified_on": PRICE_DATE.isoformat(),
            "pricing_url": "https://offline.invalid/prices",
            "contract_url": "https://offline.invalid/protocol",
            "completion_includes_reasoning": True} | overrides
