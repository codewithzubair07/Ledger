from __future__ import annotations

import csv
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


ALLOWED_STATUS = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
ALLOWED_METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
PAYMENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}):([0-9]+(?:\.[0-9]+)?)$")
STOP_RE = re.compile(r"^stop:([^:|]+)$")
REDUCE_RE = re.compile(r"^reduce_to:([^:|]+):([0-9]+(?:\.[0-9]+)?)$")


def _to_decimal(value: str) -> Decimal | None:
    try:
        return Decimal((value or "").strip())
    except (InvalidOperation, ValueError):
        return None


def _parse_payments(value: str) -> tuple[list[tuple[date, Decimal]], str | None]:
    if (value or "").strip() == "none":
        return [], None
    parts = (value or "").strip().split("|")
    out: list[tuple[date, Decimal]] = []
    for part in parts:
        m = PAYMENT_RE.match(part.strip())
        if not m:
            return [], f"invalid payment entry '{part}'"
        try:
            out.append((date.fromisoformat(m.group(1)), Decimal(m.group(2))))
        except Exception:
            return [], f"invalid payment entry '{part}'"
    if out != sorted(out, key=lambda x: x[0]):
        return [], "payment_plan must be chronological"
    return out, None


def _parse_requests(path: Path) -> dict[str, dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for row in rows:
        out[row["request_id"]] = row
    return out


def verify(repo_root: Path, output_path: Path | None = None) -> int:
    dataset_requests = _parse_requests(repo_root / "dataset" / "requests.csv")
    if output_path is None:
        output_path = repo_root / "output.csv"

    expected_columns = [
        "request_id",
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
        "decision_explanation",
    ]
    failures: list[str] = []

    with output_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != expected_columns:
            failures.append(f"invalid columns: got {reader.fieldnames}, expected {expected_columns}")
        rows = list(reader)

    if len(rows) != len(dataset_requests):
        failures.append(f"row count mismatch: output={len(rows)} requests={len(dataset_requests)}")

    seen_ids: set[str] = set()
    for idx, row in enumerate(rows, start=2):
        rid = (row.get("request_id") or "").strip()
        if rid not in dataset_requests:
            failures.append(f"line {idx}: unknown request_id '{rid}'")
            continue
        if rid in seen_ids:
            failures.append(f"line {idx}: duplicate request_id '{rid}'")
        seen_ids.add(rid)

        req = dataset_requests[rid]
        requested_amount = Decimal(req["requested_amount"])
        request_date = date.fromisoformat(req["request_date"])
        desired_completion = date.fromisoformat(req["desired_completion_date"])
        allows_partial = req["allows_partial_payment"].strip().lower() == "true"

        amount_safe = _to_decimal(row.get("amount_safe_to_pay", ""))
        if amount_safe is None:
            failures.append(f"line {idx}: invalid amount_safe_to_pay")
            continue
        if amount_safe < 0 or amount_safe > requested_amount:
            failures.append(f"line {idx}: amount_safe_to_pay out of range")

        status = (row.get("affordability_status") or "").strip()
        method = (row.get("recommended_payment_method") or "").strip()
        if status not in ALLOWED_STATUS:
            failures.append(f"line {idx}: invalid affordability_status '{status}'")
        if method not in ALLOWED_METHODS:
            failures.append(f"line {idx}: invalid recommended_payment_method '{method}'")

        payments, payment_error = _parse_payments(row.get("payment_plan", ""))
        if payment_error:
            failures.append(f"line {idx}: {payment_error}")

        earliest_txt = (row.get("earliest_date_for_full_payment") or "").strip()
        earliest_date = None
        if earliest_txt:
            try:
                earliest_date = date.fromisoformat(earliest_txt)
            except ValueError:
                failures.append(f"line {idx}: invalid earliest_date_for_full_payment")

        if status == "affordable_now" and earliest_date != request_date:
            failures.append(f"line {idx}: affordable_now requires earliest_date_for_full_payment=request_date")

        if method == "not_recommended" and payments:
            failures.append(f"line {idx}: not_recommended must use payment_plan=none")

        if method == "partial_payment":
            if not allows_partial:
                failures.append(f"line {idx}: partial_payment used when request disallows partial payments")
            if len(payments) != 2:
                failures.append(f"line {idx}: partial_payment requires exactly two payments")
            else:
                first_date, first_amt = payments[0]
                second_date, second_amt = payments[1]
                if first_date != request_date:
                    failures.append(f"line {idx}: first partial payment must occur on request_date")
                if first_amt != amount_safe:
                    failures.append(f"line {idx}: first partial payment must equal amount_safe_to_pay")
                if earliest_date and second_date != earliest_date:
                    failures.append(f"line {idx}: second partial payment date must equal earliest_date_for_full_payment")
                if second_date > desired_completion:
                    failures.append(f"line {idx}: partial_payment second payment must be by desired_completion_date")
                if (first_amt + second_amt).quantize(Decimal("0.01")) != requested_amount.quantize(Decimal("0.01")):
                    failures.append(f"line {idx}: partial payments must sum to requested_amount")
            if not (Decimal("0") < amount_safe < requested_amount):
                failures.append(f"line {idx}: partial_payment requires 0 < amount_safe_to_pay < requested_amount")

        changes = (row.get("spending_changes_needed") or "").strip()
        if changes and changes != "none":
            parts = changes.split("|")
            if len(parts) > 3:
                failures.append(f"line {idx}: spending_changes_needed allows at most three entries")
            seen_change_events: set[str] = set()
            for part in parts:
                stop_match = STOP_RE.match(part)
                reduce_match = REDUCE_RE.match(part)
                if not stop_match and not reduce_match:
                    failures.append(f"line {idx}: invalid spending change '{part}'")
                    continue
                evt_id = stop_match.group(1) if stop_match else reduce_match.group(1)
                if evt_id in seen_change_events:
                    failures.append(f"line {idx}: duplicate spending change for event_id '{evt_id}'")
                seen_change_events.add(evt_id)

        if not (row.get("decision_explanation") or "").strip():
            failures.append(f"line {idx}: decision_explanation must be non-empty")

    missing = sorted(set(dataset_requests) - seen_ids)
    if missing:
        failures.append(f"missing request_ids: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")

    if failures:
        print(f"VERIFY FAILED ({len(failures)} issues)")
        for msg in failures:
            print(f"- {msg}")
        return 1

    print(f"VERIFY PASSED (rows={len(rows)}, failures=0)")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    output_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    return verify(repo_root, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
