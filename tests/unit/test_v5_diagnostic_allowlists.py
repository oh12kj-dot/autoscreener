"""Defect 3 (2026-09-05 audit, docs/audit_followup_2026-09-05.md), diagnostic
half: the run diagnostics added by WP-B2
(docs/racr_wp_b2_risk_terms_2026-09-04.md) flagged every constant numeric
value it saw, with no distinction between "a genuine defect" and "a
declared policy constant" or "a known, deliberately-retained degenerate
field". Every real run therefore reported noise like
``objective_constant_term:risk_adjusted_compounding.tail_lambda=0.35`` and
``distribution_constant_field:expected_shortfall_10pct_log=-0.657881455``
alongside any real finding -- risking exactly the kind of miss the
diagnostic exists to prevent.

These tests cover the two explicit, per-key/per-field allowlists added to
``scoring/v5/engine.py``:

  - ``_POLICY_PARAMETER_EXPLANATION_KEYS``: declared config/module
    constants in an objective's explanation (lambda coefficients,
    ``target_moic``, ``assumed_recovery``) are excluded from
    ``_constant_explanation_terms`` entirely -- only *computed* terms can
    still raise ``objective_constant_term``.
  - ``_KNOWN_CONSTANT_DISTRIBUTION_FIELDS``: the deprecated
    ``expected_shortfall_10pct_log`` still gets reported as constant (the
    diagnostic is never silenced), but tagged
    ``distribution_constant_field_known`` with its documented reason
    instead of the plain ``distribution_constant_field`` tag that means
    "unexplained, go investigate".

Pure-Python, no DB session.
"""

from __future__ import annotations

from autoscreener.scoring.v5.engine import (
    _constant_explanation_terms,
    _distribution_field_diagnostics,
)


# -- Policy parameters excluded from objective_constant_term -----------------

def test_declared_policy_parameters_do_not_raise_objective_constant_term():
    """tail_lambda/target_moic/assumed_recovery etc. are the same for every
    ticker by construction (ObjectivesConfig coefficients, or a shared
    module constant) -- they must never appear in constant_terms or
    warnings, however many tickers share the run."""
    explanations = {
        "risk_adjusted_compounding": [
            {
                "status": "available",
                "tail_lambda": 0.35,
                "failure_lambda": 0.5,
                "drawdown_lambda": 0.2,
                "permanent_loss_lambda": 0.0,
                "uncertainty_lambda": 0.5,
                "assumed_recovery": 0.01,
                "ce_cagr": 0.05,
            },
            {
                "status": "available",
                "tail_lambda": 0.35,
                "failure_lambda": 0.5,
                "drawdown_lambda": 0.2,
                "permanent_loss_lambda": 0.0,
                "uncertainty_lambda": 0.5,
                "assumed_recovery": 0.01,
                "ce_cagr": -0.10,
            },
        ],
        "ten_bagger": [
            {"status": "available", "target_moic": 10.0, "raw_p_target": 0.2},
            {"status": "available", "target_moic": 10.0, "raw_p_target": 0.6},
        ],
        "risk_adjusted": [
            {"status": "available", "lambda": 0.5, "expected_moic_given_loss_cagr": -0.1},
            {"status": "available", "lambda": 0.5, "expected_moic_given_loss_cagr": -0.4},
        ],
    }
    constant_terms, warnings = _constant_explanation_terms(explanations)
    flat_warnings = " ".join(warnings)

    for key in (
        "tail_lambda", "failure_lambda", "drawdown_lambda",
        "permanent_loss_lambda", "uncertainty_lambda", "assumed_recovery",
    ):
        assert key not in constant_terms.get("risk_adjusted_compounding", [])
        assert key not in flat_warnings
    assert "target_moic" not in constant_terms.get("ten_bagger", [])
    assert "target_moic" not in flat_warnings
    assert "lambda" not in constant_terms.get("risk_adjusted", [])

    # None of the objectives' genuinely-varying computed values were caught
    # up in the exclusion -- only the declared-constant keys were skipped.
    assert "ce_cagr" not in constant_terms.get("risk_adjusted_compounding", [])
    assert "raw_p_target" not in constant_terms.get("ten_bagger", [])
    assert "expected_moic_given_loss_cagr" not in constant_terms.get("risk_adjusted", [])


def test_a_genuinely_constant_computed_term_still_raises_the_warning():
    """The exclusion is narrow and per-key -- a *computed* risk term that
    happens to be constant (the actual RACR defect this diagnostic exists
    to catch) must still be flagged, policy keys or not."""
    explanations = {
        "risk_adjusted_compounding": [
            {
                "status": "available",
                "tail_lambda": 0.35,
                "cond_tail_loss_10": 0.6578814551411558,
                "ce_cagr": 0.05,
            },
            {
                "status": "available",
                "tail_lambda": 0.35,
                "cond_tail_loss_10": 0.6578814551411558,
                "ce_cagr": -0.10,
            },
        ],
    }
    constant_terms, warnings = _constant_explanation_terms(explanations)
    assert "cond_tail_loss_10" in constant_terms["risk_adjusted_compounding"]
    assert any("cond_tail_loss_10" in w for w in warnings)
    # And the co-occurring policy constant is still excluded in the same call.
    assert "tail_lambda" not in constant_terms["risk_adjusted_compounding"]


def test_policy_parameter_exclusion_is_scoped_to_its_own_objective_name():
    """`lambda` is a policy key for `risk_adjusted` but has no special
    meaning for an objective the allowlist doesn't name -- exclusion must
    not leak across objectives that happen to reuse a key name."""
    explanations = {
        "some_other_objective": [
            {"status": "available", "lambda": 0.5, "x": 1.0},
            {"status": "available", "lambda": 0.5, "x": 2.0},
        ],
    }
    constant_terms, warnings = _constant_explanation_terms(explanations)
    assert "lambda" in constant_terms.get("some_other_objective", [])
    assert any("lambda" in w for w in warnings)


# -- Known-constant distribution fields ---------------------------------------

def test_expected_shortfall_10pct_log_is_tagged_known_not_unexplained():
    """The deprecated, degenerate field must still be reported as constant
    (never silenced) but distinctly tagged so the run diagnostic reads it
    as explained, not as a fresh unexplained finding."""
    values = {
        "ce_cagr": {0.05, 0.10, -0.02, 0.30},
        "expected_shortfall_10pct_log": {-0.657881455},
    }
    counts = {"ce_cagr": 4, "expected_shortfall_10pct_log": 4}
    distinct_counts, constant_fields, warnings = _distribution_field_diagnostics(values, counts)

    # Still recorded as constant -- the mechanism is not defeated.
    assert "expected_shortfall_10pct_log" in constant_fields
    assert distinct_counts["expected_shortfall_10pct_log"] == 1

    known_warnings = [w for w in warnings if w.startswith("distribution_constant_field_known:")]
    plain_warnings = [
        w for w in warnings
        if w.startswith("distribution_constant_field:") and "known" not in w
    ]
    assert any("expected_shortfall_10pct_log" in w for w in known_warnings)
    assert not any("expected_shortfall_10pct_log" in w for w in plain_warnings)
    # The known-constant warning carries a reason, not just the bare value.
    esf_warning = next(w for w in known_warnings if "expected_shortfall_10pct_log" in w)
    assert "deprecated" in esf_warning.lower()


def test_an_unknown_constant_field_still_gets_the_plain_unexplained_tag():
    """A field NOT on the explicit allowlist -- e.g. a fresh regression
    reproducing the original model_confidence defect -- must still raise
    the plain, undifferentiated warning that means "go investigate"."""
    values = {
        "ce_cagr": {0.05, 0.10, -0.02, 0.30},
        "model_confidence": {0.5},
    }
    counts = {"ce_cagr": 4, "model_confidence": 4}
    _, constant_fields, warnings = _distribution_field_diagnostics(values, counts)
    assert "model_confidence" in constant_fields
    assert any(
        w.startswith("distribution_constant_field:model_confidence=") for w in warnings
    )
    assert not any(w.startswith("distribution_constant_field_known:") for w in warnings)
