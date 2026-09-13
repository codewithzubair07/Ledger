from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

from currency import CurrencyConverter
from data_loader import DataLoader, _parse_date, _parse_decimal
from evidence import EvidenceInterpreter
from events import build_resolved_ledger
from forecast import Forecaster
from main import _fmt_decimal
from plan import choose_plan, make_explanation, serialize_payment_plan, serialize_spending_changes


FIELDS = [
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]


def _to_decimal(value: str) -> Decimal | None:
    try:
        return Decimal((value or "").strip())
    except (InvalidOperation, ValueError):
        return None


def _load_sample_requests(path: Path) -> tuple[list[dict], dict[str, dict]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    expected_by_request: dict[str, dict] = {}
    parsed_requests: list[dict] = []
    for row in rows:
        expected_by_request[row["request_id"]] = row
        parsed = {
            "request_id": row["request_id"],
            "user_id": row["user_id"],
            "request_date_obj": _parse_date(row["request_date"]),
            "requested_amount_decimal": _parse_decimal(row["requested_amount"]) or Decimal("0"),
            "desired_completion_date_obj": _parse_date(row["desired_completion_date"]),
            "allows_partial_payment_bool": row["allows_partial_payment"].strip().lower() == "true",
            "request_date": row["request_date"],
            "requested_amount": row["requested_amount"],
            "desired_completion_date": row["desired_completion_date"],
            "allows_partial_payment": row["allows_partial_payment"],
            "request_type": row["request_type"],
            "request_text": row["request_text"],
        }
        parsed_requests.append(parsed)
    return parsed_requests, expected_by_request


def predict_for_requests(repo_root: Path, requests: list[dict]) -> dict[str, dict]:
    loader = DataLoader(repo_root)
    data = loader.load_all()
    data["requests"] = requests

    converter = CurrencyConverter(data["exchange_rates"])
    evidence = EvidenceInterpreter()

    interpreted_messages_by_user: dict[str, list[dict]] = {}
    for user_id, msgs in data["messages_by_user"].items():
        user_event_ids = {e["event_id"] for e in data["events_by_user"].get(user_id, [])}
        interpreted_messages_by_user[user_id] = [evidence.interpret_message(msg, valid_event_ids=user_event_ids) for msg in msgs]

    image_amounts_by_event_id: dict[str, tuple[Decimal, str]] = {}
    for event_id, row in data["images_by_event"].items():
        path = repo_root / "dataset" / "media" / "images" / f"{row['image_id']}.png"
        parsed = evidence.extract_image_amount(path)
        if parsed.get("amount") is not None and parsed.get("currency"):
            image_amounts_by_event_id[event_id] = (parsed["amount"], parsed["currency"])

    ledgers = {}
    for user_id, profile in data["profiles_by_user"].items():
        ledgers[user_id] = build_resolved_ledger(
            user_id=user_id,
            events=data["events_by_user"].get(user_id, []),
            all_events_by_id=data["events_by_id"],
            profile=profile,
            converter=converter,
            evidence_messages=interpreted_messages_by_user.get(user_id, []),
            image_amounts_by_event_id=image_amounts_by_event_id,
        )

    out: dict[str, dict] = {}
    for req in requests:
        user_id = req["user_id"]
        profile = data["profiles_by_user"][user_id]
        ledger = ledgers[user_id]
        forecaster = Forecaster(profile, ledger)
        forecast = forecaster.evaluate(req["request_date_obj"], req["requested_amount_decimal"])
        payment_options = data["payment_options_by_request"].get(req["request_id"], [])
        choice = choose_plan(req, profile, payment_options, forecast, forecaster)

        amount_safe = max(Decimal("0"), min(forecast.amount_safe_to_pay, req["requested_amount_decimal"]))
        earliest = forecast.earliest_date_for_full_payment.isoformat() if forecast.earliest_date_for_full_payment else ""
        if choice.affordability_status == "affordable_now":
            earliest = req["request_date_obj"].isoformat()

        out[req["request_id"]] = {
            "amount_safe_to_pay": _fmt_decimal(amount_safe),
            "affordability_status": choice.affordability_status,
            "recommended_payment_method": choice.recommended_payment_method,
            "payment_plan": serialize_payment_plan(choice.payment_plan),
            "earliest_date_for_full_payment": earliest,
            "spending_changes_needed": serialize_spending_changes(choice.spending_changes),
            "decision_explanation": make_explanation(choice, profile, req, forecast),
        }
    return out


def score_samples(repo_root: Path) -> int:
    sample_path = repo_root / "dataset" / "sample_requests.csv"
    requests, expected = _load_sample_requests(sample_path)
    predicted = predict_for_requests(repo_root, requests)

    totals = {field: 0 for field in FIELDS}
    matches = {field: 0 for field in FIELDS}

    for req in requests:
        rid = req["request_id"]
        exp = expected[rid]
        pred = predicted[rid]
        for field in FIELDS:
            totals[field] += 1
            if field == "amount_safe_to_pay":
                exp_dec = _to_decimal(exp[field])
                pred_dec = _to_decimal(pred[field])
                if exp_dec is not None and pred_dec is not None and abs(exp_dec - pred_dec) <= Decimal("0.01"):
                    matches[field] += 1
            else:
                if (exp.get(field) or "").strip() == (pred.get(field) or "").strip():
                    matches[field] += 1

    print("Per-field accuracy on dataset/sample_requests.csv")
    for field in FIELDS:
        acc = (matches[field] / totals[field]) * 100 if totals[field] else 0.0
        print(f"- {field}: {matches[field]}/{totals[field]} ({acc:.2f}%)")
    return 0


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent.parent
    raise SystemExit(score_samples(repo_root))
