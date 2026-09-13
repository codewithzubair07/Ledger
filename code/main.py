from __future__ import annotations

import csv
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from currency import CurrencyConverter
from data_loader import DataLoader
from evidence import EvidenceInterpreter
from events import build_resolved_ledger
from forecast import Forecaster
from plan import choose_plan, make_explanation, serialize_payment_plan, serialize_spending_changes


def _fmt_decimal(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def run(repo_root: Path) -> None:
    loader = DataLoader(repo_root)
    data = loader.load_all()

    converter = CurrencyConverter(data["exchange_rates"])
    evidence = EvidenceInterpreter()

    # Parse messages with strict event-id validation.
    interpreted_messages_by_user: dict[str, list[dict]] = {}
    for user_id, msgs in data["messages_by_user"].items():
        user_event_ids = {e["event_id"] for e in data["events_by_user"].get(user_id, [])}
        out = []
        for msg in msgs:
            out.append(evidence.interpret_message(msg, valid_event_ids=user_event_ids))
        interpreted_messages_by_user[user_id] = out

    # Resolve image amounts (LLM disabled by default per spec fallback).
    image_amounts_by_event_id: dict[str, tuple[Decimal, str]] = {}
    for event_id, row in data["images_by_event"].items():
        image_id = row["image_id"]
        path = repo_root / "dataset" / "media" / "images" / f"{image_id}.png"
        parsed = evidence.extract_image_amount(path)
        if parsed.get("amount") is not None and parsed.get("currency"):
            image_amounts_by_event_id[event_id] = (parsed["amount"], parsed["currency"])

    # Build one resolved ledger per user.
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

    output_rows = []
    for req in data["requests"]:
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

        output_rows.append(
            {
                "request_id": req["request_id"],
                "amount_safe_to_pay": _fmt_decimal(amount_safe),
                "affordability_status": choice.affordability_status,
                "recommended_payment_method": choice.recommended_payment_method,
                "payment_plan": serialize_payment_plan(choice.payment_plan),
                "earliest_date_for_full_payment": earliest,
                "spending_changes_needed": serialize_spending_changes(choice.spending_changes),
                "decision_explanation": make_explanation(choice, profile, req, forecast),
            }
        )

    fieldnames = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    ]

    output_path = repo_root / "output.csv"
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    usage_report_path = repo_root / "code" / "evaluation" / "usage_report.md"
    usage_report_path.parent.mkdir(parents=True, exist_ok=True)
    usage_report_path.write_text(evidence.usage_report(total_requests=len(output_rows)), encoding="utf-8")


if __name__ == "__main__":
    run(Path(__file__).resolve().parent.parent)
