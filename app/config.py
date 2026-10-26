"""Runtime configuration for ProofPay.

Everything is read from environment variables (a local `.env` file is loaded
automatically). When API keys are missing the app falls back to MOCK mode so
anyone can run the full flow without accounts.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass
class Settings:
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-3.8-flash"))
    # Tried once if the primary model still answers 404/503 after retries. Empty = no fallback.
    gemini_fallback_model: str = field(default_factory=lambda: _env("GEMINI_FALLBACK_MODEL"))
    # Per-HTTP-call timeout and total retry budget (seconds). AI runs in the background, so these
    # can be generous without making anyone wait on a request.
    gemini_timeout: float = field(default_factory=lambda: float(_env("GEMINI_TIMEOUT", "150")))
    gemini_total_budget: float = field(default_factory=lambda: float(_env("GEMINI_TOTAL_BUDGET", "330")))
    # "thread": real AI work runs in a background thread (default).
    # "inline": run it synchronously inside the request (used by tests; handy for debugging).
    ai_background: str = field(default_factory=lambda: _env("AI_BACKGROUND", "thread").lower())
    paypal_client_id: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_ID"))
    paypal_client_secret: str = field(default_factory=lambda: _env("PAYPAL_CLIENT_SECRET"))
    paypal_base: str = field(
        default_factory=lambda: _env("PAYPAL_BASE", "https://api-m.sandbox.paypal.com").rstrip("/")
    )
    # "auto" (mock only where keys are missing), "mock" (force mock everywhere), "live"
    mode: str = field(default_factory=lambda: _env("PROOFPAY_MODE", "auto").lower())
    approval_threshold: float = field(
        default_factory=lambda: float(_env("APPROVAL_THRESHOLD", "0.75"))
    )
    database_path: str = field(
        default_factory=lambda: _env("DATABASE_PATH", str(BASE_DIR / "data" / "proofpay.db"))
    )
    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", str(BASE_DIR / "data" / "uploads")))
    max_photos: int = field(default_factory=lambda: int(_env("MAX_PHOTOS", "4")))
    max_photo_mb: float = field(default_factory=lambda: float(_env("MAX_PHOTO_MB", "8")))

    @property
    def ai_mock(self) -> bool:
        if self.mode == "mock":
            return True
        return not self.gemini_api_key

    @property
    def paypal_mock(self) -> bool:
        if self.mode == "mock":
            return True
        return not (self.paypal_client_id and self.paypal_client_secret)

    def summary(self) -> dict:
        return {
            "ai": "mock" if self.ai_mock else f"gemini:{self.gemini_model}",
            "paypal": "mock" if self.paypal_mock else ("sandbox" if "sandbox" in self.paypal_base else "live"),
            "approval_threshold": self.approval_threshold,
        }
