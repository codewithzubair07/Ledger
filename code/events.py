from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from statistics import median


@dataclass
class ForecastEvent:
    event_id: str
    event_date: date
    amount: Decimal
    direction: str
    category: str
    flexibility: str
    minimum_allowed_amount: Decimal | None
    is_recurring: bool


@dataclass
class ResolvedLedger:
    user_id: str
    resolved_events: list[dict]


def _event_sort_key(row: dict) -> tuple:
    status_order = {
        "cancelled": 6,
        "failed": 6,
        "settled": 5,
        "scheduled": 4,
        "pending": 3,
        "unrealized": 2,
    }
    dt = row.get("settlement_date_obj") or row.get("event_date_obj")
    return (status_order.get(row.get("status", ""), 1), dt or date.min)


def _lifecycle_root(event: dict, by_id: dict[str, dict]) -> str:
    seen: set[str] = set()
    current = event
    while current.get("linked_event_id"):
        nxt = by_id.get(current["linked_event_id"])
        if not nxt or nxt["event_id"] in seen:
            break
        seen.add(nxt["event_id"])
        current = nxt
    return current["event_id"]


def build_resolved_ledger(
    user_id: str,
    events: list[dict],
    all_events_by_id: dict[str, dict],
    profile,
    converter,
    evidence_messages: list[dict],
    image_amounts_by_event_id: dict[str, tuple[Decimal, str]],
) -> ResolvedLedger:
    valid_event_ids = {e["event_id"] for e in events}
    adjustments: dict[str, dict] = {}
    for msg in evidence_messages:
        target = msg.get("target_event_id")
        if target and target in valid_event_ids:
            adjustments[target] = msg

    prepared: list[dict] = []
    for row in events:
        clone = dict(row)

        amt = clone.get("amount_decimal")
        if amt is None:
            extracted = image_amounts_by_event_id.get(clone["event_id"])
            if extracted:
                amt, ccy = extracted
                clone["currency"] = ccy
        clone["resolved_amount"] = amt

        adj = adjustments.get(clone["event_id"])
        if adj:
            action = adj.get("action")
            if action == "cancel":
                clone["status"] = "cancelled"
            if action in {"amend", "confirm"} and adj.get("new_amount") is not None:
                clone["resolved_amount"] = adj["new_amount"]
            if action in {"delay", "amend", "confirm"} and adj.get("new_date"):
                clone["settlement_date_obj"] = date.fromisoformat(adj["new_date"])

        clone["amount_home"] = None
        if clone.get("resolved_amount") is not None:
            fx_date = clone.get("settlement_date_obj") or clone.get("event_date_obj")
            if fx_date is not None:
                try:
                    clone["amount_home"] = converter.convert(
                        clone["resolved_amount"],
                        clone["currency"],
                        profile.home_currency,
                        fx_date,
                    )
                except Exception:
                    clone["amount_home"] = None
        prepared.append(clone)

    lifecycle_groups: dict[str, list[dict]] = {}
    for row in prepared:
        lifecycle_groups.setdefault(_lifecycle_root(row, all_events_by_id), []).append(row)

    resolved: list[dict] = []
    for group in lifecycle_groups.values():
        chosen = sorted(group, key=_event_sort_key)[-1]
        if chosen.get("status") in {"cancelled", "failed"}:
            continue
        resolved.append(chosen)

    return ResolvedLedger(user_id=user_id, resolved_events=resolved)


def project_events_for_window(ledger: ResolvedLedger, request_date: date, horizon_days: int = 90) -> list[ForecastEvent]:
    horizon_end = request_date + timedelta(days=horizon_days)
    resolved = ledger.resolved_events

    result: list[ForecastEvent] = []

    # Scheduled and pending rules
    for row in resolved:
        status = row.get("status")
        evt_date = row.get("settlement_date_obj") or row.get("event_date_obj")
        amt = row.get("amount_home")
        if not evt_date or amt is None:
            continue
        if evt_date < request_date or evt_date > horizon_end:
            continue
        if status == "scheduled":
            if row.get("event_type") == "investment_valuation":
                continue
            if row.get("event_type") in {"investment_purchase", "investment_sale"} and row.get("direction") != "credit":
                continue
            result.append(
                ForecastEvent(
                    event_id=row["event_id"],
                    event_date=evt_date,
                    amount=amt,
                    direction=row.get("direction", "debit"),
                    category=row.get("category", ""),
                    flexibility=row.get("flexibility", "fixed"),
                    minimum_allowed_amount=row.get("minimum_allowed_amount_decimal"),
                    is_recurring=False,
                )
            )
        elif status == "pending":
            if row.get("direction") == "debit":
                result.append(
                    ForecastEvent(
                        event_id=row["event_id"],
                        event_date=evt_date,
                        amount=amt,
                        direction="debit",
                        category=row.get("category", ""),
                        flexibility=row.get("flexibility", "fixed"),
                        minimum_allowed_amount=row.get("minimum_allowed_amount_decimal"),
                        is_recurring=False,
                    )
                )

    # Recurrence from settled history strictly before request date
    grouped: dict[tuple, list[dict]] = {}
    for row in resolved:
        if row.get("status") != "settled":
            continue
        if row.get("event_type") in {"investment_purchase", "investment_sale", "investment_valuation"}:
            continue
        d = row.get("settlement_date_obj") or row.get("event_date_obj")
        amt = row.get("amount_home")
        if d is None or amt is None or d >= request_date:
            continue
        key = (
            row.get("direction"),
            row.get("category"),
            row.get("event_type"),
            str(amt.quantize(Decimal("0.01"))),
        )
        grouped.setdefault(key, []).append(row)

    for rows in grouped.values():
        rows.sort(key=lambda r: r.get("settlement_date_obj") or r.get("event_date_obj"))
        if len(rows) < 2:
            continue
        dates = [(r.get("settlement_date_obj") or r.get("event_date_obj")) for r in rows]
        intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates)) if dates[i] and dates[i - 1]]
        if not intervals:
            continue
        cadence = int(round(median(intervals)))
        if cadence < 6 or cadence > 40:
            continue
        base = rows[-1]
        next_date = dates[-1] + timedelta(days=cadence)
        while next_date <= horizon_end:
            if next_date >= request_date:
                result.append(
                    ForecastEvent(
                        event_id=base["event_id"],
                        event_date=next_date,
                        amount=base["amount_home"],
                        direction=base.get("direction", "debit"),
                        category=base.get("category", ""),
                        flexibility=base.get("flexibility", "fixed"),
                        minimum_allowed_amount=base.get("minimum_allowed_amount_decimal"),
                        is_recurring=True,
                    )
                )
            next_date += timedelta(days=cadence)

    return result
