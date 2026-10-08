"""
Tests for account-currency budget labelling.

Budget display strings used to be hardcoded "EUR", so USD (and any other
currency) accounts were mislabelled. The label must come from the account.
"""
import pytest

from meta_ads_mcp.core import utils
from meta_ads_mcp.core.api import MetaAPIError, api_client


@pytest.fixture(autouse=True)
def _clear_currency_cache():
    utils._ACCOUNT_CURRENCY_CACHE.clear()
    yield
    utils._ACCOUNT_CURRENCY_CACHE.clear()


class TestFormatBudget:
    def test_uses_given_currency(self):
        assert utils.format_budget_cents_to_currency("5000", "USD") == "USD 50.00"

    def test_no_currency_is_unlabelled_not_eur(self):
        out = utils.format_budget_cents_to_currency(5000)
        assert out == "50.00"
        assert "EUR" not in out


class TestGetAccountCurrency:
    def test_resolves_and_caches(self, monkeypatch):
        calls = []

        def fake_get(endpoint, params=None, fields=None):
            calls.append(endpoint)
            return {"currency": "USD", "id": "act_1"}

        monkeypatch.setattr(api_client, "graph_get", fake_get)
        assert utils.get_account_currency("123") == "USD"
        assert utils.get_account_currency("act_123") == "USD"
        assert calls == ["/act_123"]  # second call served from cache

    def test_lookup_failure_returns_none_and_is_not_cached(self, monkeypatch):
        def boom(endpoint, params=None, fields=None):
            raise MetaAPIError("nope", error_code=190)

        monkeypatch.setattr(api_client, "graph_get", boom)
        assert utils.get_account_currency("123") is None

        monkeypatch.setattr(api_client, "graph_get", lambda *a, **k: {"currency": "GBP"})
        assert utils.get_account_currency("123") == "GBP"

    def test_empty_account_id(self):
        assert utils.get_account_currency(None) is None
        assert utils.get_account_currency("") is None


class TestReadToolsLabelWithAccountCurrency:
    def _patch(self, monkeypatch, payload_by_suffix):
        def fake_get(endpoint, params=None, fields=None):
            for suffix, payload in payload_by_suffix.items():
                if endpoint.endswith(suffix):
                    return payload
            raise AssertionError(f"unexpected endpoint {endpoint}")

        monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
        monkeypatch.setattr(api_client, "graph_get", fake_get)

    def test_get_campaigns_labels_usd(self, monkeypatch):
        from meta_ads_mcp.core.campaigns import get_campaigns

        self._patch(monkeypatch, {
            "/act_9": {"currency": "USD"},
            "/act_9/campaigns": {"data": [
                {"id": "1", "daily_budget": "2500", "effective_status": "ACTIVE"},
                {"id": "2", "lifetime_budget": "100000", "effective_status": "PAUSED"},
            ]},
        })
        result = get_campaigns("act_9")
        assert result["currency"] == "USD"
        assert result["campaigns"][0]["daily_budget_display"] == "USD 25.00"
        assert result["campaigns"][1]["lifetime_budget_display"] == "USD 1000.00"

    def test_get_campaign_details_resolves_currency_from_account_id(self, monkeypatch):
        from meta_ads_mcp.core.campaigns import get_campaign_details

        self._patch(monkeypatch, {
            "/act_9": {"currency": "GBP"},
            "/77/adsets": {"data": []},
            "/77": {"id": "77", "account_id": "9", "daily_budget": "1000"},
        })
        result = get_campaign_details("77")
        assert result["currency"] == "GBP"
        assert result["daily_budget_display"] == "GBP 10.00"

    def test_get_adsets_labels_account_currency(self, monkeypatch):
        from meta_ads_mcp.core.adsets import get_adsets

        self._patch(monkeypatch, {
            "/act_9": {"currency": "USD"},
            "/act_9/adsets": {"data": [{"id": "5", "daily_budget": "1500"}]},
        })
        result = get_adsets("act_9")
        assert result["currency"] == "USD"
        assert result["adsets"][0]["daily_budget_display"] == "USD 15.00"

    def test_get_adset_details_labels_account_currency(self, monkeypatch):
        from meta_ads_mcp.core.adsets import get_adset_details

        self._patch(monkeypatch, {
            "/act_9": {"currency": "USD"},
            "/55/ads": {"data": []},
            "/55": {"id": "55", "account_id": "9", "lifetime_budget": "20000"},
        })
        result = get_adset_details("55")
        assert result["lifetime_budget_display"] == "USD 200.00"

    def test_unknown_currency_never_falls_back_to_eur(self, monkeypatch):
        from meta_ads_mcp.core.campaigns import get_campaigns

        def fake_get(endpoint, params=None, fields=None):
            if endpoint == "/act_9":
                raise MetaAPIError("no access", error_code=200)
            return {"data": [{"id": "1", "daily_budget": "2500", "effective_status": "ACTIVE"}]}

        monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
        monkeypatch.setattr(api_client, "graph_get", fake_get)
        result = get_campaigns("act_9")
        assert result["currency"] is None
        assert result["campaigns"][0]["daily_budget_display"] == "25.00"
