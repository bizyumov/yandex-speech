"""Stable, secret-safe speech errors."""
from __future__ import annotations

import re


class SpeechError(RuntimeError):
    def __init__(self, category: str, message: str, *, code: str | None = None,
                 request_id: str | None = None, receipt_path: str | None = None) -> None:
        self.category = category
        self.receipt_path = receipt_path
        self.code = code if code and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(code)) else None
        self.request_id = request_id if request_id and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", str(request_id)) else None
        super().__init__(f"{category}: {message}")

    def as_dict(self) -> dict:
        result = {"category": self.category, "message": str(self),
                  "code": self.code, "request_id": self.request_id}
        if self.receipt_path is not None:
            result["receipt_path"] = self.receipt_path
        return result
