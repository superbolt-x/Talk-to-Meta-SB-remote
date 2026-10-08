"""
Tests for list_catalogs: finding a catalog ID without already knowing it.

Endpoints: /{business}/owned_product_catalogs and /{business}/client_product_catalogs
https://developers.facebook.com/docs/marketing-api/reference/business/owned_product_catalogs/
"""
import pytest

from meta_ads_mcp.core.api import MetaAPIError, api_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)


def cat(cid, name, count=100, business=None):
    out = {"id": cid, "name": name, "product_count": count, "vertical": "commerce", "feed_count": 1}
    if business:
        out["business"] = business
    return out


def install(monkeypatch, routes):
    """routes: endpoint -> payload or Exception. Records calls."""
    calls = []

    def fake(endpoint, params=None, fields=None):
        calls.append((endpoint, params, fields))
        payload = routes[endpoint]
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.catalogs import list_catalogs
    return list_catalogs(**kw)


class TestWhichBusiness:
    def test_account_id_resolves_owning_business(self, monkeypatch):
        calls = install(monkeypatch, {
            "/act_36922696": {"business": {"id": "b1", "name": "Ettika"}},
            "/b1/owned_product_catalogs": {"data": [cat("937978439946237", "Ettika Products", 5000)]},
            "/b1/client_product_catalogs": {"data": []},
        })
        out = run(account_id="36922696")
        assert out["account_id"] == "act_36922696"
        assert [c["id"] for c in out["catalogs"]] == ["937978439946237"]
        assert out["businesses_checked"] == [{"id": "b1", "name": "Ettika"}]
        assert out["catalogs"][0]["found_via"] == [{"business_id": "b1", "relation": "owned"}]
        assert "/me/businesses" not in [c[0] for c in calls]

    def test_explicit_business_id_makes_no_discovery_calls(self, monkeypatch):
        calls = install(monkeypatch, {
            "/b9/owned_product_catalogs": {"data": [cat("1", "A")]},
            "/b9/client_product_catalogs": {"data": []},
        })
        out = run(business_id="b9")
        assert out["total"] == 1
        assert [c[0] for c in calls] == ["/b9/owned_product_catalogs", "/b9/client_product_catalogs"]

    def test_no_arguments_checks_every_business_the_token_belongs_to(self, monkeypatch):
        install(monkeypatch, {
            "/me/businesses": {"data": [{"id": "b1", "name": "One"}, {"id": "b2", "name": "Two"}]},
            "/b1/owned_product_catalogs": {"data": [cat("1", "Zeta")]},
            "/b1/client_product_catalogs": {"data": []},
            "/b2/owned_product_catalogs": {"data": [cat("2", "Alpha")]},
            "/b2/client_product_catalogs": {"data": []},
        })
        out = run()
        assert [c["name"] for c in out["catalogs"]] == ["Alpha", "Zeta"]  # sorted by name
        assert len(out["businesses_checked"]) == 2

    def test_account_without_owning_business_explains_what_to_do(self, monkeypatch):
        install(monkeypatch, {"/act_5": {}})
        out = run(account_id="act_5")
        assert "no owning business" in out["error"]
        assert "business_id" in out["hint"]

    def test_business_lookup_failure_returns_error_not_exception(self, monkeypatch):
        install(monkeypatch, {"/me/businesses": MetaAPIError("(#200) denied", error_code=200)})
        out = run()
        assert "Could not determine" in out["error"]


class TestCatalogEntries:
    def test_shared_catalog_deduplicated_against_owned(self, monkeypatch):
        install(monkeypatch, {
            "/b1/owned_product_catalogs": {"data": [cat("1", "Shared too", business={"id": "b1", "name": "One"})]},
            "/b1/client_product_catalogs": {"data": [cat("1", "Shared too"), cat("2", "Client catalog")]},
        })
        out = run(business_id="b1")
        assert out["total"] == 2
        both = next(c for c in out["catalogs"] if c["id"] == "1")
        assert both["found_via"] == [{"business_id": "b1", "relation": "owned"},
                                     {"business_id": "b1", "relation": "shared"}]
        assert both["owner_business"] == {"id": "b1", "name": "One"}

    def test_include_shared_false_skips_the_shared_edge(self, monkeypatch):
        calls = install(monkeypatch, {"/b1/owned_product_catalogs": {"data": []}})
        run(business_id="b1", include_shared=False)
        assert [c[0] for c in calls] == ["/b1/owned_product_catalogs"]

    def test_limit_is_clamped(self, monkeypatch):
        calls = install(monkeypatch, {"/b1/owned_product_catalogs": {"data": []}})
        run(business_id="b1", include_shared=False, limit=9999)
        assert calls[0][1] == {"limit": "100"}

    def test_empty_result_explains_cross_business_ownership(self, monkeypatch):
        install(monkeypatch, {"/b1/owned_product_catalogs": {"data": []},
                              "/b1/client_product_catalogs": {"data": []}})
        out = run(business_id="b1")
        assert out["total"] == 0
        assert "another business" in out["note"]


class TestFailuresAndFallbacks:
    def test_one_edge_failing_does_not_hide_the_other(self, monkeypatch):
        install(monkeypatch, {
            "/b1/owned_product_catalogs": MetaAPIError("(#200) no access", error_code=200),
            "/b1/client_product_catalogs": {"data": [cat("7", "Shared")]},
        })
        out = run(business_id="b1")
        assert [c["id"] for c in out["catalogs"]] == ["7"]
        assert "b1:owned" in out["errors"]
        assert "catalog_management" in out["hint"]

    def test_falls_back_to_basic_fields_when_one_is_rejected(self, monkeypatch):
        from meta_ads_mcp.core import catalogs
        seen = []

        def fake(endpoint, params=None, fields=None):
            seen.append(fields)
            if "feed_count" in (fields or []):
                raise MetaAPIError("(#100) nonexisting field (feed_count)", error_code=100)
            return {"data": [{"id": "1", "name": "A", "product_count": 3, "vertical": "commerce"}]}

        monkeypatch.setattr(api_client, "graph_get", fake)
        out = run(business_id="b1", include_shared=False)
        assert seen == [catalogs.CATALOG_LIST_FIELDS, catalogs.CATALOG_LIST_FIELDS_BASIC]
        assert out["total"] == 1 and "errors" not in out

    def test_more_pages_than_the_limit_is_flagged(self, monkeypatch):
        install(monkeypatch, {"/b1/owned_product_catalogs": {
            "data": [cat("1", "A")], "paging": {"next": "https://graph.facebook.com/next"}}})
        out = run(business_id="b1", include_shared=False)
        assert out["truncated"] is True
        assert "business_id" in out["truncation_note"]

    def test_complete_list_is_not_flagged(self, monkeypatch):
        install(monkeypatch, {"/b1/owned_product_catalogs": {"data": [cat("1", "A")]}})
        assert run(business_id="b1", include_shared=False)["truncated"] is False


def test_registered_as_read_only_tool():
    from meta_ads_mcp.server import mcp
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert tools["list_catalogs"].annotations.readOnlyHint is True
