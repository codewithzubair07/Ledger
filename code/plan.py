from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import re


@dataclass
class PlanChoice:
    affordability_status: str
    recommended_payment_method: str
    payment_plan: list[tuple[date, Decimal]]
    spending_changes: list[dict]
    payment_option_id: str | None


def _format_amount(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"))
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def serialize_payment_plan(payments: list[tuple[date, Decimal]]) -> str:
    if not payments:
        return "none"
    return "|".join(f"{d.isoformat()}:{_format_amount(a)}" for d, a in sorted(payments, key=lambda x: x[0]))


def serialize_spending_changes(changes: list[dict]) -> str:
    if not changes:
        return "none"
    out = []
    for change in changes[:3]:
        if change["kind"] == "stop":
            out.append(f"stop:{change['event_id']}")
        elif change["kind"] == "reduce_to":
            out.append(f"reduce_to:{change['event_id']}:{_format_amount(change['new_amount'])}")
    return "|".join(out) if out else "none"


def _option_numeric_id(option_id: str | None) -> int:
    if not option_id:
        return 10**9
    m = re.search(r"(\d+)$", option_id)
    return int(m.group(1)) if m else 10**9


def choose_plan(request: dict, profile, payment_options: list[dict], forecast, forecaster) -> PlanChoice:
    request_date: date = request["request_date_obj"]
    desired_completion: date = request["desired_completion_date_obj"]
    requested_amount: Decimal = request["requested_amount_decimal"]

    consider = profile.payment_methods_user_will_consider
    candidates: list[tuple[tuple, PlanChoice]] = []

    # Full payment now
    if "full_payment" in consider:
        if forecast.full_payment_safe_now_without_changes:
            payments = [(request_date, requested_amount)]
            plan = PlanChoice("affordable_now", "full_payment", payments, [], None)
            rank = (
                0,
                0,
                requested_amount,
                request_date,
                len(payments),
                10**9,
            )
            candidates.append((rank, plan))
        elif forecast.full_payment_safe_now_with_changes and forecast.selected_spending_changes:
            payments = [(request_date, requested_amount)]
            plan = PlanChoice(
                "affordable_with_plan",
                "full_payment",
                payments,
                forecast.selected_spending_changes,
                None,
            )
            rank = (
                0,
                1,
                requested_amount,
                request_date,
                len(payments),
                10**9,
            )
            candidates.append((rank, plan))

    # Partial payment
    if (
        request["allows_partial_payment_bool"]
        and "partial_payment" in consider
        and forecast.amount_safe_to_pay > Decimal("0")
        and forecast.amount_safe_to_pay < requested_amount
        and forecast.earliest_date_for_full_payment is not None
        and forecast.earliest_date_for_full_payment <= desired_completion
    ):
        rem = (requested_amount - forecast.amount_safe_to_pay).quantize(Decimal("0.01"))
        payments = [
            (request_date, forecast.amount_safe_to_pay),
            (forecast.earliest_date_for_full_payment, rem),
        ]
        ok, _ = forecaster.simulate(request_date, payments, [])
        if ok:
            total_paid = sum(a for _, a in payments)
            plan = PlanChoice("affordable_with_plan", "partial_payment", payments, [], None)
            rank = (
                0 if payments[-1][0] <= desired_completion else 1,
                0,
                total_paid,
                payments[0][0],
                len(payments),
                10**9,
            )
            candidates.append((rank, plan))

    # Installments from supplied options only
    if "installments" in consider:
        for opt in payment_options:
            if opt["payment_method"] != "installments":
                continue
            if profile.max_installment_months is not None and opt["number_of_payments_int"] > profile.max_installment_months:
                continue
            first_date = opt["first_payment_date_obj"]
            freq = opt["payment_frequency_days_int"] or 30
            n = opt["number_of_payments_int"]
            amt = opt["payment_amount_decimal"]
            payments = [(first_date + timedelta(days=freq * i), amt) for i in range(n)]
            ok, _ = forecaster.simulate(request_date, payments, [])
            if not ok:
                continue
            total_paid = sum(a for _, a in payments)
            plan = PlanChoice("affordable_with_plan", "installments", payments, [], opt["payment_option_id"])
            rank = (
                0 if payments[-1][0] <= desired_completion else 1,
                0,
                total_paid,
                payments[0][0],
                len(payments),
                _option_numeric_id(opt["payment_option_id"]),
            )
            candidates.append((rank, plan))

    # Wait option
    if (
        "full_payment" in consider
        and forecast.earliest_date_for_full_payment is not None
        and forecast.earliest_date_for_full_payment > request_date
    ):
        pay_date = forecast.earliest_date_for_full_payment
        payments = [(pay_date, requested_amount)]
        ok, _ = forecaster.simulate(request_date, payments, [])
        if ok:
            plan = PlanChoice("affordable_later", "wait", payments, [], None)
            rank = (
                0 if pay_date <= desired_completion else 1,
                0,
                requested_amount,
                pay_date,
                1,
                10**9,
            )
            candidates.append((rank, plan))

    if candidates:
        return sorted(candidates, key=lambda x: x[0])[0][1]

    return PlanChoice("not_affordable", "not_recommended", [], [], None)


def make_explanation(
    choice: PlanChoice,
    profile,
    request: dict,
    forecast,
) -> str:
    currency = profile.home_currency
    req_amt = _format_amount(request["requested_amount_decimal"])
    min_bal = _format_amount(profile.minimum_balance_to_keep)

    if choice.recommended_payment_method == "full_payment" and choice.affordability_status == "affordable_now":
        return f"Pay {currency} {req_amt} today. This keeps at least {currency} {min_bal} available over the next 90 days."
    if choice.recommended_payment_method == "installments":
        p0 = choice.payment_plan[0]
        return (
            f"Use {len(choice.payment_plan)} installments of {currency} {_format_amount(p0[1])}, starting {p0[0].isoformat()}. "
            f"This keeps the {currency} {min_bal} minimum protected."
        )
    if choice.recommended_payment_method == "wait" and choice.payment_plan:
        p = choice.payment_plan[0]
        return (
            f"Wait until {p[0].isoformat()}, then pay {currency} {_format_amount(p[1])} in full. "
            f"Paying earlier risks breaching the {currency} {min_bal} minimum."
        )
    if choice.recommended_payment_method == "not_recommended":
        d = request["desired_completion_date_obj"].isoformat()
        return (
            f"Do not make this payment by {d}. Available options do not keep the {currency} {min_bal} minimum protected."
        )
    if choice.spending_changes:
        return (
            f"Apply selected spending changes and pay {currency} {req_amt} on {request['request_date_obj'].isoformat()}. "
            f"This keeps the {currency} {min_bal} minimum protected."
        )
    return (
        f"Proceed with {choice.recommended_payment_method.replace('_', ' ')} while preserving the {currency} {min_bal} minimum."
    )
