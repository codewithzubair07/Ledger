from __future__ import annotations

from bisect import bisect_right
from datetime import date
from decimal import Decimal


class CurrencyConverter:
    def __init__(self, exchange_rate_rows: list[dict]):
        self._rates: dict[tuple[str, str], list[tuple[date, Decimal]]] = {}
        for row in exchange_rate_rows:
            key = (row["from_currency"], row["to_currency"])
            self._rates.setdefault(key, []).append((row["rate_date_obj"], row["rate_decimal"]))
        for key in self._rates:
            self._rates[key].sort(key=lambda x: x[0])

    def convert(self, amount: Decimal, from_currency: str, to_currency: str, on_date: date) -> Decimal:
        if from_currency == to_currency:
            return amount
        rates = self._rates.get((from_currency, to_currency))
        if not rates:
            raise ValueError(f"No rate table for {from_currency}->{to_currency}")

        dates = [d for d, _ in rates]
        idx = bisect_right(dates, on_date) - 1
        if idx < 0:
            raise ValueError(
                f"No prior conversion rate for {from_currency}->{to_currency} on/before {on_date.isoformat()}"
            )
        rate = rates[idx][1]
        return amount * rate
