"""Data model: money, products, fetch attempts, page outcomes."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal, Optional

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

PriceStatus = Literal["ok", "unknown", "ambiguous"]
PageKind = Literal["listing", "detail"]
PageOutcomeKind = Literal[
    "parsed", "failed", "skipped_by_robots", "unattempted_limit"
]
ProductOutcome = Literal[
    "accepted", "exact_duplicate", "conflicting_id", "rejected", "template_unrecognised"
]


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.amount_minor, int) or isinstance(self.amount_minor, bool):
            raise ValueError("amount_minor must be an int")
        if self.amount_minor < 0:
            raise ValueError("amount_minor must be non-negative")
        if not _CURRENCY_RE.match(self.currency):
            raise ValueError(f"invalid currency code: {self.currency!r}")

    def same_currency(self, other: "Money") -> bool:
        return self.currency == other.currency


@dataclass
class FetchAttempt:
    attempt_no: int
    url: str
    status_code: Optional[int]
    error: Optional[str]
    elapsed_s: float
    waited_before_s: float


@dataclass
class PageOutcome:
    url: str
    canonical_url: str
    kind: PageKind
    outcome: PageOutcomeKind
    attempts: list[FetchAttempt] = field(default_factory=list)
    final_url: Optional[str] = None
    status_code: Optional[int] = None
    fetch_utc: Optional[str] = None
    content_sha256: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class RawProductRecord:
    product_id: Optional[str]
    name: Optional[str]
    price: Optional[Money]
    price_status: PriceStatus
    category: Optional[str]
    template: str  # "v1" | "v2" | "unrecognised" | "malformed"
    source_url: str
    content_hash: str
    outcome: ProductOutcome
    reason: Optional[str] = None


@dataclass
class Product:
    product_id: str
    name: str
    price: Optional[Money]
    price_status: PriceStatus
    category: Optional[str]
    template: str
    source_urls: list[str]
    content_hash: str
    parser_version: str
