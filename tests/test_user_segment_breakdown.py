import meta_ads_mcp.server  # noqa: F401  (loads core in the supported order)
from meta_ads_mcp.core.insights import (
    VALID_BREAKDOWNS,
    _normalize_metrics,
    _validate_breakdowns,
)


def test_user_segment_key_is_accepted():
    assert "user_segment_key" in VALID_BREAKDOWNS
    assert _validate_breakdowns(["user_segment_key"]) == (True, "")


def test_unknown_breakdown_still_rejected():
    ok, msg = _validate_breakdowns(["not_a_breakdown"])
    assert not ok and "user_segment_key" in msg


def test_segment_value_passes_through_normalization():
    row = {"user_segment_key": "new_customers", "spend": "10"}
    assert _normalize_metrics(row)["user_segment_key"] == "new_customers"


def test_link_click_fields_are_exposed():
    row = {
        "spend": "10",
        "clicks": None,
        "inline_link_clicks": "786",
        "inline_link_click_ctr": "3.34",
        "cost_per_inline_link_click": "4.00",
    }
    n = _normalize_metrics(row, "ecommerce")
    assert (n["link_clicks"], n["link_ctr"], n["cpc_link"]) == ("786", "3.34", "4.00")


def test_zero_purchases_reported_for_ecommerce_segments():
    row = {"spend": "0.36", "actions": [{"action_type": "video_view", "value": "1"}]}
    assert _normalize_metrics(row, "ecommerce")["purchases"] == "0"
    assert "purchases" not in _normalize_metrics(row, "awareness")


def test_link_click_metrics_requested():
    from meta_ads_mcp.core.insights import LINK_CLICK_METRICS
    assert "inline_link_clicks" in LINK_CLICK_METRICS
