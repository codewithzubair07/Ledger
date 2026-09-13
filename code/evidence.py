from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
AMOUNT_RE = re.compile(r"\b(?:INR|IDR|USD|EUR|ZAR)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\b", re.IGNORECASE)


@dataclass
class UsageTracker:
    provider: str
    model: str
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class EvidenceInterpreter:
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "none")
        self.model = os.getenv("LLM_MODEL", "none")
        self.api_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("LLM_API_KEY")
        )
        self.usage = UsageTracker(provider=self.provider, model=self.model)

    def interpret_message(self, row: dict, valid_event_ids: set[str] | None = None) -> dict:
        text = (row.get("message_text") or "").strip()
        lower = text.lower()
        action = "none"

        if any(k in lower for k in ["cancel", "cancelled", "dibatalkan", "ended", "end", "no off-season income"]):
            action = "cancel"
        elif any(k in lower for k in ["resumes on", "expected on", "confirmed credit date", "revised date", "delayed", "postponed"]):
            action = "delay"
        elif any(k in lower for k in ["increased to", "reduced to", "naik menjadi", "temporary monthly pay", "first salary will be", "amount is"]):
            action = "amend"
        elif any(k in lower for k in ["confirmed", "completed", "reached your account", "claim is now closed"]):
            action = "confirm"

        target_event_id = (row.get("related_event_id") or "").strip() or None
        if target_event_id and valid_event_ids is not None and target_event_id not in valid_event_ids:
            target_event_id = None

        amount_match = AMOUNT_RE.search(text)
        new_amount = None
        if amount_match:
            new_amount = Decimal(amount_match.group(1).replace(",", ""))

        date_match = DATE_RE.search(text)
        new_date = date_match.group(1) if date_match else None

        return {
            "action": action,
            "target_event_id": target_event_id,
            "new_amount": new_amount,
            "new_date": new_date,
            "note": "rule_based_extraction",
        }

    def extract_image_amount(self, path: Path) -> dict:
        if not self.api_key:
            return {
                "amount": None,
                "currency": None,
                "note": f"UNRESOLVED_NO_API_KEY:{path.name}",
            }
        return {
            "amount": None,
            "currency": None,
            "note": f"UNRESOLVED_LLM_NOT_CONFIGURED:{path.name}",
        }

    def usage_report(self, total_requests: int) -> str:
        total_requests = max(total_requests, 1)
        avg_tokens = self.usage.total_tokens / total_requests
        return (
            "# Usage Report\n\n"
            f"- Provider: {self.usage.provider}\n"
            f"- Model: {self.usage.model}\n"
            f"- Model calls: {self.usage.model_calls}\n"
            f"- Input tokens: {self.usage.input_tokens}\n"
            f"- Output tokens: {self.usage.output_tokens}\n"
            f"- Total tokens: {self.usage.total_tokens}\n"
            f"- Average tokens per request: {avg_tokens:.2f}\n"
            "- Estimated total cost: 0 (no metered calls captured)\n"
            "- Estimated cost per request: 0\n"
        )
