from app.services.accounting_cogs import (
    cogs_basis_bucket,
    cogs_basis_review_fields,
    cogs_evidence_split,
    format_cogs_evidence_split,
    net_before_cogs,
    profit_after_returns,
    profit_before_returns,
    return_refund_total,
    returns_profit_impact,
    shipping_delta,
)


def test_cogs_basis_bucket_classifies_review_estimate_and_verified_sources() -> None:
    assert cogs_basis_bucket("lot_equal_quantity_fallback") == "review"
    assert cogs_basis_bucket("missing_cost_basis") == "review"
    assert cogs_basis_bucket("mixed_fifo_cost") == "review"
    assert cogs_basis_bucket("lot_expected_quantity_fallback") == "estimate"
    assert cogs_basis_bucket("mixed_estimate_fifo_cost") == "estimate"
    assert cogs_basis_bucket("mixed_verified_fifo_cost") == "ok"
    assert cogs_basis_bucket("assignment_unit_landed_cost") == "ok"


def test_cogs_basis_review_fields_include_export_bucket_and_reason() -> None:
    review = cogs_basis_review_fields("lot_equal_quantity_fallback")
    assert review["cogs_basis_bucket"] == "review"
    assert review["basis_review_required"] is True
    assert review["basis_is_estimate"] is False
    assert "equal-quantity fallback" in str(review["basis_review_reason"])

    estimate = cogs_basis_review_fields("mixed_estimate_fifo_cost")
    assert estimate["cogs_basis_bucket"] == "estimate"
    assert estimate["basis_review_required"] is False
    assert estimate["basis_is_estimate"] is True

    verified = cogs_basis_review_fields("mixed_verified_fifo_cost")
    assert verified["cogs_basis_bucket"] == "ok"
    assert verified["basis_review_required"] is False
    assert verified["basis_is_estimate"] is False


def test_cogs_evidence_split_groups_amounts_and_rows() -> None:
    split = cogs_evidence_split(
        {
            "assignment_unit_landed_cost": 12.345,
            "mixed_verified_fifo_cost": 7.655,
            "lot_expected_quantity_fallback": 3,
            "mixed_estimate_fifo_cost": 4,
            "lot_equal_quantity_fallback": 5,
            "mixed_fifo_cost": 6,
            "missing_cost_basis": 0,
        },
        {
            "assignment_unit_landed_cost": 1,
            "mixed_verified_fifo_cost": 1,
            "lot_expected_quantity_fallback": 2,
            "mixed_estimate_fifo_cost": 1,
            "lot_equal_quantity_fallback": 3,
            "mixed_fifo_cost": 1,
            "missing_cost_basis": 1,
        },
    )

    assert split == {
        "verified_amount": 20.0,
        "estimated_amount": 7.0,
        "review_needed_amount": 11.0,
        "verified_sale_rows": 2,
        "estimated_sale_rows": 3,
        "review_needed_sale_rows": 5,
    }
    assert format_cogs_evidence_split(split) == "verified $20.00; estimated $7.00; review-needed $11.00"


def test_accounting_formula_helpers_apply_canonical_profit_conventions() -> None:
    net = net_before_cogs(gross=100, shipping_charged=8, fees=12.25, label_spend=5.75)
    assert net == 90.0
    assert shipping_delta(shipping_charged=8, label_spend=5.75) == 2.25

    before_returns = profit_before_returns(net_before_cogs_amount=net, cogs=40.5)
    assert before_returns == 49.5

    refund = return_refund_total(refund_amount=20, refund_fees=2.5, refund_shipping=4)
    assert refund == 26.5
    impact = returns_profit_impact(refund_total=refund, cogs_reversal=10)
    assert impact == -16.5
    assert profit_after_returns(
        profit_before_returns_amount=before_returns,
        returns_profit_impact_amount=impact,
    ) == 33.0
