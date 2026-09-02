from __future__ import annotations

import pytest

from autoscreener.batch.collect_market_opportunity import (
    _TAM_RE,
    _amount,
    validate_market_opportunity,
)


def test_sec_tam_parser_supports_trillion_and_requires_an_explicit_scale():
    trillion = _TAM_RE.search("total addressable market is $1.25 trillion")
    assert trillion is not None
    assert _amount(trillion.group(1), trillion.group(2)) == pytest.approx(1.25e12)

    missing = _TAM_RE.search("total addressable market is $1.0 for this therapy")
    assert missing is not None
    with pytest.raises(ValueError, match="explicit scale"):
        _amount(missing.group(1), missing.group(2))


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        ({"tam_value": 0, "currency": "USD"}, "tam_value_nonpositive"),
        ({"tam_value": 1e9, "currency": "US"}, "currency_missing_or_invalid"),
        (
            {
                "tam_value": 100e6,
                "currency": "USD",
                "current_revenue_addressable": 120e6,
                "revenue_currency": "USD",
            },
            "tam_below_addressable_revenue",
        ),
        ({"tam_value": 1e16, "currency": "USD"}, "tam_value_absurd_magnitude"),
    ],
)
def test_tam_sanity_contract_rejects_invalid_values(item, reason):
    result = validate_market_opportunity(item)
    assert not result.valid
    assert result.reason == reason


def test_penetration_requires_matching_explicit_currencies():
    base = {"tam_value": 1e9, "currency": "USD", "current_revenue_addressable": 100e6}
    assert validate_market_opportunity(base).penetration_rate is None
    assert validate_market_opportunity({**base, "revenue_currency": "EUR"}).penetration_rate is None
    assert validate_market_opportunity({**base, "revenue_currency": "USD"}).penetration_rate == pytest.approx(0.1)


def test_machine_extracted_tam_is_low_confidence_and_rejects_dollar_scale_artifact():
    valid = validate_market_opportunity(
        {"tam_value": 5e9, "currency": "USD", "confidence": "high"}, machine_extracted=True
    )
    invalid = validate_market_opportunity(
        {"tam_value": 1.0, "currency": "USD"}, machine_extracted=True
    )
    assert valid.valid and valid.confidence == "low"
    assert not invalid.valid and invalid.reason == "machine_tam_absurdly_small"
