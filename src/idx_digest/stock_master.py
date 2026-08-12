from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class StockMaster:
    tickers: frozenset[str]
    source: str
    retrieved_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tickers": sorted(self.tickers),
            "source": self.source,
            "retrieved_at": self.retrieved_at,
        }


def _ticker_from_row(row: Any) -> str | None:
    if isinstance(row, str):
        value = row
    elif isinstance(row, dict):
        value = (
            row.get("KodeEmiten") or row.get("Kode_Emiten") or row.get("kodeEmiten")
            or row.get("Ticker") or row.get("ticker") or row.get("Code") or row.get("code")
            or row.get("Symbol") or row.get("symbol")
        )
    else:
        return None
    value = str(value or "").strip().upper()
    if 2 <= len(value) <= 8 and value.replace("-", "").isalnum():
        return value
    return None


def parse_stock_master_payload(payload: Any) -> frozenset[str]:
    """Parse several IDX profile-list response shapes without guessing securities."""
    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("Results", "results", "Profiles", "profiles", "Data", "data", "Replies", "replies"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, dict):
                for nested in ("Results", "results", "Items", "items", "Data", "data"):
                    nested_value = value.get(nested)
                    if isinstance(nested_value, list):
                        candidates.extend(nested_value)
        # Some endpoints return a dictionary keyed by ticker.
        if not candidates:
            for key, value in payload.items():
                if isinstance(value, dict):
                    row = dict(value)
                    row.setdefault("ticker", key)
                    candidates.append(row)
    tickers = {ticker for row in candidates if (ticker := _ticker_from_row(row))}
    return frozenset(sorted(tickers))


class StockMasterCache:
    def __init__(self, path: Path):
        self.path = path

    def load(self, *, max_age_hours: float | None = None) -> StockMaster | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            tickers = frozenset(str(x).strip().upper() for x in payload.get("tickers") or [] if str(x).strip())
            if not tickers:
                return None
            master = StockMaster(tickers=tickers, source=str(payload.get("source") or "cache"), retrieved_at=str(payload.get("retrieved_at") or ""))
            if max_age_hours is not None and master.retrieved_at:
                stamp = datetime.fromisoformat(master.retrieved_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - stamp.astimezone(timezone.utc) > timedelta(hours=float(max_age_hours)):
                    return None
            return master
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def save(self, master: StockMaster) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(master.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)

    def refresh(self, fetcher: Callable[[], Any], *, source: str) -> StockMaster:
        payload = fetcher()
        tickers = parse_stock_master_payload(payload)
        if not tickers:
            raise ValueError("stock-master endpoint returned no recognizable stock tickers")
        master = StockMaster(
            tickers=tickers,
            source=source,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
        )
        self.save(master)
        return master
