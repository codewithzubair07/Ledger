from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FinancialProfile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: tuple[str, ...]
    expense_categories_to_protect: frozenset[str]
    expense_categories_user_is_willing_to_reduce: frozenset[str]
    expense_categories_user_is_willing_to_stop: frozenset[str]
    payment_methods_user_will_consider: frozenset[str]
    max_installment_months: int | None


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_decimal(value: str) -> Decimal | None:
    value = (value or "").strip()
    if not value:
        return None
    return Decimal(value)


def _parse_pipe(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in (value or "").split("|") if x.strip())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


class DataLoader:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.dataset_dir = repo_root / "dataset"

    def load_all(self) -> dict[str, Any]:
        requests = _read_csv(self.dataset_dir / "requests.csv")
        profiles_rows = _read_csv(self.dataset_dir / "financial_profiles.csv")
        events = _read_csv(self.dataset_dir / "financial_events.csv")
        rates = _read_csv(self.dataset_dir / "exchange_rates.csv")
        payment_options = _read_csv(self.dataset_dir / "request_payment_options.csv")
        messages = _read_csv(self.dataset_dir / "messages.csv")
        images = _read_csv(self.dataset_dir / "images.csv")

        profiles_by_user: dict[str, FinancialProfile] = {}
        for row in profiles_rows:
            max_inst = row["max_installment_months"].strip()
            profiles_by_user[row["user_id"]] = FinancialProfile(
                user_id=row["user_id"],
                home_currency=row["home_currency"],
                current_available_balance=_parse_decimal(row["current_available_balance"]) or Decimal("0"),
                minimum_balance_to_keep=_parse_decimal(row["minimum_balance_to_keep"]) or Decimal("0"),
                financial_priorities=_parse_pipe(row["financial_priorities"]),
                expense_categories_to_protect=frozenset(_parse_pipe(row["expense_categories_to_protect"])),
                expense_categories_user_is_willing_to_reduce=frozenset(
                    _parse_pipe(row["expense_categories_user_is_willing_to_reduce"])
                ),
                expense_categories_user_is_willing_to_stop=frozenset(
                    _parse_pipe(row["expense_categories_user_is_willing_to_stop"])
                ),
                payment_methods_user_will_consider=frozenset(_parse_pipe(row["payment_methods_user_will_consider"])),
                max_installment_months=int(max_inst) if max_inst else None,
            )

        for row in requests:
            row["request_date_obj"] = _parse_date(row["request_date"])
            row["desired_completion_date_obj"] = _parse_date(row["desired_completion_date"])
            row["requested_amount_decimal"] = _parse_decimal(row["requested_amount"]) or Decimal("0")
            row["allows_partial_payment_bool"] = row["allows_partial_payment"].strip().lower() == "yes"

        for row in events:
            row["event_date_obj"] = _parse_date(row["event_date"])
            row["settlement_date_obj"] = _parse_date(row["settlement_date"])
            row["amount_decimal"] = _parse_decimal(row["amount"])
            row["minimum_allowed_amount_decimal"] = _parse_decimal(row.get("minimum_allowed_amount", ""))

        for row in rates:
            row["rate_date_obj"] = _parse_date(row["rate_date"])
            row["rate_decimal"] = _parse_decimal(row["rate"]) or Decimal("0")

        for row in payment_options:
            row["payment_amount_decimal"] = _parse_decimal(row["payment_amount"]) or Decimal("0")
            row["number_of_payments_int"] = int(row["number_of_payments"])
            row["first_payment_date_obj"] = _parse_date(row["first_payment_date"])
            row["payment_frequency_days_int"] = int(row["payment_frequency_days"]) if row["payment_frequency_days"] else None
            row["financing_fee_decimal"] = _parse_decimal(row["financing_fee"]) or Decimal("0")
            row["total_payable_amount_decimal"] = _parse_decimal(row["total_payable_amount"]) or Decimal("0")

        for row in messages:
            row["sent_at_obj"] = _parse_datetime(row["sent_at"])

        events_by_user: dict[str, list[dict[str, Any]]] = {}
        events_by_id: dict[str, dict[str, Any]] = {}
        for row in events:
            events_by_user.setdefault(row["user_id"], []).append(row)
            events_by_id[row["event_id"]] = row

        payment_options_by_request: dict[str, list[dict[str, Any]]] = {}
        for row in payment_options:
            payment_options_by_request.setdefault(row["request_id"], []).append(row)

        messages_by_user: dict[str, list[dict[str, Any]]] = {}
        messages_by_request: dict[str, list[dict[str, Any]]] = {}
        messages_by_event: dict[str, list[dict[str, Any]]] = {}
        for row in messages:
            messages_by_user.setdefault(row["user_id"], []).append(row)
            if row["request_id"]:
                messages_by_request.setdefault(row["request_id"], []).append(row)
            if row["related_event_id"]:
                messages_by_event.setdefault(row["related_event_id"], []).append(row)

        images_by_event: dict[str, dict[str, str]] = {}
        images_by_user: dict[str, list[dict[str, str]]] = {}
        for row in images:
            images_by_user.setdefault(row["user_id"], []).append(row)
            if row["related_event_id"]:
                images_by_event[row["related_event_id"]] = row

        return {
            "requests": requests,
            "profiles_by_user": profiles_by_user,
            "events_by_user": events_by_user,
            "events_by_id": events_by_id,
            "exchange_rates": rates,
            "payment_options_by_request": payment_options_by_request,
            "messages_by_user": messages_by_user,
            "messages_by_request": messages_by_request,
            "messages_by_event": messages_by_event,
            "images_by_event": images_by_event,
            "images_by_user": images_by_user,
        }
