from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import combinations

from events import ForecastEvent, project_events_for_window


@dataclass
class ForecastResult:
    amount_safe_to_pay: Decimal
    earliest_date_for_full_payment: date | None
    full_payment_safe_now_without_changes: bool
    full_payment_safe_now_with_changes: bool
    selected_spending_changes: list[dict]
    projected_min_balance: Decimal


class Forecaster:
    def __init__(self, profile, ledger):
        self.profile = profile
        self.ledger = ledger

    def _apply_spending_changes(self, event: ForecastEvent, changes_by_event_id: dict[str, dict]) -> Decimal | None:
        change = changes_by_event_id.get(event.event_id)
        if not change:
            return event.amount
        if change["kind"] == "stop":
            return Decimal("0")
        if change["kind"] == "reduce_to":
            new_amt = change["new_amount"]
            if event.direction == "debit":
                return min(event.amount, max(new_amt, Decimal("0")))
            return event.amount
        return event.amount

    def simulate(
        self,
        request_date: date,
        payments: list[tuple[date, Decimal]],
        spending_changes: list[dict] | None = None,
        horizon_days: int = 90,
    ) -> tuple[bool, Decimal]:
        events = project_events_for_window(self.ledger, request_date, horizon_days=horizon_days)
        change_map = {c["event_id"]: c for c in (spending_changes or [])}

        timeline: list[tuple[date, int, Decimal]] = []
        for evt in events:
            amt = self._apply_spending_changes(evt, change_map)
            if amt is None:
                continue
            signed = -amt if evt.direction == "debit" else amt
            timeline.append((evt.event_date, 1 if signed > 0 else 0, signed))

        for dt, amt in payments:
            timeline.append((dt, 0, -amt))

        timeline.sort(key=lambda x: (x[0], x[1]))

        balance = self.profile.current_available_balance
        min_balance = balance
        floor = self.profile.minimum_balance_to_keep

        horizon_end = request_date + timedelta(days=horizon_days)
        for dt, _, signed in timeline:
            if dt < request_date or dt > horizon_end:
                continue
            balance += signed
            if balance < min_balance:
                min_balance = balance
            if balance < floor:
                return False, min_balance
        return True, min_balance

    def _candidate_changes(self, request_date: date) -> list[dict]:
        events = project_events_for_window(self.ledger, request_date, horizon_days=90)
        candidates = []
        seen = set()
        for evt in events:
            if evt.direction != "debit" or evt.flexibility != "flexible":
                continue
            if evt.category in self.profile.expense_categories_to_protect:
                continue
            if evt.event_id in seen:
                continue
            seen.add(evt.event_id)
            if evt.category in self.profile.expense_categories_user_is_willing_to_stop:
                candidates.append({"kind": "stop", "event_id": evt.event_id})
            if evt.category in self.profile.expense_categories_user_is_willing_to_reduce:
                min_allowed = evt.minimum_allowed_amount
                if min_allowed is None:
                    min_allowed = (evt.amount * Decimal("0.7")).quantize(Decimal("0.01"))
                candidates.append(
                    {
                        "kind": "reduce_to",
                        "event_id": evt.event_id,
                        "new_amount": min_allowed,
                    }
                )
        return candidates[:8]

    def _find_changes_to_enable_full_payment(self, request_date: date, requested_amount: Decimal) -> list[dict]:
        base_candidates = self._candidate_changes(request_date)
        if not base_candidates:
            return []

        payment = [(request_date, requested_amount)]
        best: list[dict] = []
        for r in range(1, min(3, len(base_candidates)) + 1):
            for combo in combinations(base_candidates, r):
                ids = [c["event_id"] for c in combo]
                if len(set(ids)) != len(ids):
                    continue
                ok, _ = self.simulate(request_date, payment, list(combo))
                if ok:
                    return list(combo)
                if not best:
                    best = list(combo)
        return []

    def _max_safe_now(self, request_date: date, requested_amount: Decimal) -> tuple[Decimal, Decimal]:
        lo = Decimal("0")
        hi = requested_amount
        best = Decimal("0")
        best_min_bal = self.profile.current_available_balance

        for _ in range(40):
            mid = (lo + hi) / 2
            mid = mid.quantize(Decimal("0.01"))
            ok, min_bal = self.simulate(request_date, [(request_date, mid)], [])
            if ok:
                best = mid
                best_min_bal = min_bal
                lo = mid
            else:
                hi = mid
            if (hi - lo) <= Decimal("0.01"):
                break
        if best > requested_amount:
            best = requested_amount
        return max(Decimal("0"), best), best_min_bal

    def _find_earliest_full_payment_date(self, request_date: date, requested_amount: Decimal) -> date | None:
        for offset in range(0, 91):
            dt = request_date + timedelta(days=offset)
            ok, _ = self.simulate(request_date, [(dt, requested_amount)], [])
            if ok:
                return dt
        return None

    def evaluate(self, request_date: date, requested_amount: Decimal) -> ForecastResult:
        amount_safe_to_pay, projected_min = self._max_safe_now(request_date, requested_amount)
        earliest = self._find_earliest_full_payment_date(request_date, requested_amount)

        safe_now_no_changes, _ = self.simulate(request_date, [(request_date, requested_amount)], [])
        changes = []
        safe_now_with_changes = safe_now_no_changes
        if not safe_now_no_changes:
            changes = self._find_changes_to_enable_full_payment(request_date, requested_amount)
            if changes:
                safe_now_with_changes, _ = self.simulate(request_date, [(request_date, requested_amount)], changes)

        return ForecastResult(
            amount_safe_to_pay=max(Decimal("0"), min(amount_safe_to_pay, requested_amount)),
            earliest_date_for_full_payment=earliest,
            full_payment_safe_now_without_changes=safe_now_no_changes,
            full_payment_safe_now_with_changes=safe_now_with_changes,
            selected_spending_changes=changes if safe_now_with_changes else [],
            projected_min_balance=projected_min,
        )
