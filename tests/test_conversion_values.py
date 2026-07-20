"""
Tests for generalized non-purchase action/conversion value extraction in
meta_ads_mcp.core.insights (_normalize_metrics / _extract_roas).
"""
from meta_ads_mcp.core.insights import _normalize_metrics, _extract_roas


def _row(actions=None, action_values=None, conversions=None, conversion_values=None, spend="100"):
    return {
        "spend": spend,
        "impressions": "1000",
        "actions": actions or [],
        "action_values": action_values or [],
        "conversions": conversions or [],
        "conversion_values": conversion_values or [],
    }


class TestExtractRoasBackwardCompatible:

    def test_default_still_matches_purchase(self):
        action_values = [{"action_type": "omni_purchase", "value": "250.00"}]
        assert _extract_roas(action_values) == "250.00"

    def test_no_match_returns_none(self):
        action_values = [{"action_type": "lead", "value": "10.00"}]
        assert _extract_roas(action_values) is None

    def test_explicit_action_types_overrides_default(self):
        action_values = [{"action_type": "lead", "value": "10.00"}]
        assert _extract_roas(action_values, ("lead",)) == "10.00"


class TestNormalizeMetricsPurchaseUnaffected:

    def test_purchase_extraction_unchanged_without_new_params(self):
        row = _row(
            actions=[{"action_type": "omni_purchase", "value": "3"}],
            action_values=[{"action_type": "omni_purchase", "value": "300.00"}],
        )
        normalized = _normalize_metrics(row, archetype="ecommerce")
        assert normalized["purchases"] == "3"
        assert normalized["revenue"] == "300.00"
        assert "target_conversion_count" not in normalized
        assert "target_conversion_value" not in normalized


class TestConversionActionTypesParam:

    def test_extracts_count_value_roas_cpa_for_non_purchase_type(self):
        row = _row(
            actions=[{"action_type": "lead", "value": "5"}],
            action_values=[{"action_type": "lead", "value": "500.00"}],
            spend="100",
        )
        normalized = _normalize_metrics(row, conversion_action_types=["lead"])
        assert normalized["target_conversion_count"] == "5"
        assert normalized["target_conversion_value"] == "500.00"
        assert normalized["target_conversion_roas"] == "5.00"
        assert normalized["target_conversion_cpa"] == "20.00"

    def test_multiple_action_types_first_match_wins(self):
        row = _row(
            actions=[{"action_type": "offsite_conversion.fb_pixel_lead", "value": "2"}],
            action_values=[{"action_type": "offsite_conversion.fb_pixel_lead", "value": "80.00"}],
        )
        normalized = _normalize_metrics(
            row,
            conversion_action_types=["lead", "offsite_conversion.fb_pixel_lead"],
        )
        assert normalized["target_conversion_count"] == "2"
        assert normalized["target_conversion_value"] == "80.00"

    def test_no_match_omits_target_keys(self):
        row = _row(actions=[], action_values=[])
        normalized = _normalize_metrics(row, conversion_action_types=["lead"])
        assert "target_conversion_count" not in normalized
        assert "target_conversion_value" not in normalized
        assert normalized["target_conversion_action_types"] == ["lead"]


class TestConversionsArrayFallback:
    """Regression test: found live against Happiest Baby (act_61643804) — pixel-custom
    conversions put count/value in conversions/conversion_values, not actions/action_values.
    The target_conversion_* extraction must check both pairs, like pixel_conversions does."""

    def test_extracts_from_conversions_when_absent_from_actions(self):
        row = _row(
            actions=[],
            action_values=[],
            conversions=[{"action_type": "offsite_conversion.fb_pixel_custom.Sleepea Purchase", "value": "703"}],
            conversion_values=[{"action_type": "offsite_conversion.fb_pixel_custom.Sleepea Purchase", "value": "28933.04"}],
        )
        normalized = _normalize_metrics(
            row, conversion_action_types=["offsite_conversion.fb_pixel_custom.Sleepea Purchase"],
        )
        assert normalized["target_conversion_count"] == "703"
        assert normalized["target_conversion_value"] == "28933.04"

    def test_prefers_actions_over_conversions_when_both_present(self):
        row = _row(
            actions=[{"action_type": "lead", "value": "5"}],
            conversions=[{"action_type": "lead", "value": "999"}],
        )
        normalized = _normalize_metrics(row, conversion_action_types=["lead"])
        assert normalized["target_conversion_count"] == "5"


class TestCustomConversionIdParam:

    def test_builds_custom_conversion_prefix_and_extracts_value(self):
        row = _row(
            actions=[{"action_type": "offsite_conversion.custom.123456", "value": "7"}],
            action_values=[{"action_type": "offsite_conversion.custom.123456", "value": "700.00"}],
        )
        normalized = _normalize_metrics(row, custom_conversion_id="123456")
        assert normalized["target_conversion_count"] == "7"
        assert normalized["target_conversion_value"] == "700.00"

    def test_combines_with_conversion_action_types(self):
        row = _row(
            actions=[{"action_type": "lead", "value": "4"}],
            action_values=[{"action_type": "lead", "value": "40.00"}],
        )
        normalized = _normalize_metrics(
            row, conversion_action_types=["lead"], custom_conversion_id="999",
        )
        assert normalized["target_conversion_action_types"] == [
            "lead", "offsite_conversion.custom.999",
        ]
        assert normalized["target_conversion_count"] == "4"
