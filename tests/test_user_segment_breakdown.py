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
