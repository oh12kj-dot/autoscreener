"""Point-in-time input builder for Model v5."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy.orm import Session

from autoscreener.db.models import PriceSnapshot, RawSnapshot, Ticker, UniverseSnapshot
from autoscreener.scoring.engine import build_inputs_for_ticker
from autoscreener.scoring.moic import MoicInputs
from autoscreener.validation.rules import sanitize_info


@dataclass(frozen=True)
class V5PitInput:
    ticker_id: int
    symbol: str
    as_of: datetime.date
    moic_inputs: MoicInputs | None
    raw_snapshot_id: int | None
    raw_available_from: datetime.date | None
    price_as_of: datetime.date | None
    input_status: str

    def evidence(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "raw_snapshot_id": self.raw_snapshot_id,
            "raw_available_from": self.raw_available_from.isoformat() if self.raw_available_from else None,
            "price_as_of": self.price_as_of.isoformat() if self.price_as_of else None,
            "input_status": self.input_status,
            "pit_rules": {
                "raw_snapshot": "available_from <= as_of",
                "price": "trade_date <= as_of",
            },
        }


def build_v5_pit_inputs(session: Session, *, as_of: datetime.date) -> list[V5PitInput]:
    """Build exactly the universe visible on ``as_of`` without current-value fallback."""

    tickers = (
        session.query(Ticker)
        .join(UniverseSnapshot, UniverseSnapshot.ticker_id == Ticker.id)
        .filter(UniverseSnapshot.snapshot_date == as_of, UniverseSnapshot.included.is_(True))
        .order_by(Ticker.symbol)
        .all()
    )
    built: list[V5PitInput] = []
    for ticker in tickers:
        raw = (
            session.query(RawSnapshot)
            .filter(RawSnapshot.ticker_id == ticker.id, RawSnapshot.available_from <= as_of)
            .order_by(RawSnapshot.available_from.desc(), RawSnapshot.snapshot_date.desc(), RawSnapshot.id.desc())
            .first()
        )
        prices = (
            session.query(PriceSnapshot)
            .filter(PriceSnapshot.ticker_id == ticker.id, PriceSnapshot.trade_date <= as_of)
            .order_by(PriceSnapshot.trade_date.asc())
            .all()
        )
        price_as_of = prices[-1].trade_date if prices else None
        if raw is None:
            built.append(V5PitInput(ticker.id, ticker.symbol, as_of, None, None, None,
                                    price_as_of, "not_collected"))
            continue
        info = sanitize_info(raw.payload.get("info") or {})
        sector = info.get("sector") or ticker.sector
        inputs = build_inputs_for_ticker(raw.payload, prices, as_of, sector)
        built.append(V5PitInput(
            ticker.id,
            ticker.symbol,
            as_of,
            inputs,
            raw.id,
            raw.available_from,
            price_as_of,
            "collected_with_data" if inputs is not None else "collected_no_finding",
        ))
    return built
