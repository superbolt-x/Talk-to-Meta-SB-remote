"""
Tests for get_ad_previews: /{ad_id}/previews?ad_format=... returns {"data": [{"body": "<iframe src=... >"}]}.
"""
import pytest

from meta_ads_mcp.core.api import MetaAPIError, api_client
from meta_ads_mcp.core.previews import _unpack_iframe

IFRAME = ('<iframe src="https://www.facebook.com/ads/api/preview_iframe.php?d=AQxyz&amp;t=AQabc" '
          'width="335" height="591" scrolling="yes" style="border: none;"></iframe>')


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)


def install(monkeypatch, handler):
    calls = []

    def fake(endpoint, params=None, fields=None):
        calls.append({"endpoint": endpoint, "params": dict(params or {})})
        out = handler(endpoint, dict(params or {}))
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(api_client, "graph_get", fake)
    return calls


def run(**kw):
    from meta_ads_mcp.core.previews import get_ad_previews
    kw.setdefault("ad_id", "120000000000001")
    return get_ad_previews(**kw)


class TestUnpack:
    def test_the_link_and_size_come_out_of_the_iframe_with_entities_decoded(self):
        assert _unpack_iframe(IFRAME) == {
            "url": "https://www.facebook.com/ads/api/preview_iframe.php?d=AQxyz&t=AQabc", "width": 335, "height": 591}

    def test_single_quotes_and_attribute_order_do_not_matter(self):
        body = "<IFRAME height='400' style='x' SRC='https://x.test/p?a=1&amp;b=2' width='300'></IFRAME>"
        assert _unpack_iframe(body) == {"url": "https://x.test/p?a=1&b=2", "width": 300, "height": 400}

    @pytest.mark.parametrize("body", [None, "", 12, "<div>no frame</div>", "<iframe width='3'></iframe>"])
    def test_anything_without_a_link_is_none(self, body):
        assert _unpack_iframe(body) is None

    def test_a_missing_or_non_numeric_size_is_none_not_an_error(self):
        assert _unpack_iframe('<iframe src="https://x.test/p" width="auto"></iframe>') == {"url": "https://x.test/p", "width": None, "height": None}


class TestTool:
    def test_default_formats_are_asked_one_by_one_on_the_ads_previews_edge(self, monkeypatch):
        calls = install(monkeypatch, lambda e, p: {"data": [{"body": IFRAME}]})
        out = run()
        assert [(c["endpoint"], c["params"]["ad_format"]) for c in calls] == [
            ("/120000000000001/previews", "DESKTOP_FEED_STANDARD"), ("/120000000000001/previews", "MOBILE_FEED_STANDARD"),
            ("/120000000000001/previews", "INSTAGRAM_STANDARD")]
        assert [p["format"] for p in out["previews"]] == ["DESKTOP_FEED_STANDARD", "MOBILE_FEED_STANDARD", "INSTAGRAM_STANDARD"]
        assert out["previews"][0]["url"].endswith("&t=AQabc") and "errors" not in out and out["ad_id"] == "120000000000001"

    def test_a_creative_id_uses_its_own_edge(self, monkeypatch):
        calls = install(monkeypatch, lambda e, p: {"data": [{"body": IFRAME}]})
        out = run(ad_id=None, creative_id="999000111222", ad_formats="instagram_story")
        assert calls[0]["endpoint"] == "/999000111222/previews" and calls[0]["params"]["ad_format"] == "INSTAGRAM_STORY"
        assert out["creative_id"] == "999000111222" and "ad_id" not in out

    def test_formats_are_trimmed_uppercased_and_deduplicated(self, monkeypatch):
        calls = install(monkeypatch, lambda e, p: {"data": [{"body": IFRAME}]})
        run(ad_formats=" instagram_story , INSTAGRAM_STORY,instagram_reels ")
        assert [c["params"]["ad_format"] for c in calls] == ["INSTAGRAM_STORY", "INSTAGRAM_REELS"]

    def test_locale_is_passed_when_given(self, monkeypatch):
        calls = install(monkeypatch, lambda e, p: {"data": [{"body": IFRAME}]})
        run(ad_formats="INSTAGRAM_STORY", locale="es_LA")
        assert calls[0]["params"]["locale"] == "es_LA"
        calls = install(monkeypatch, lambda e, p: {"data": [{"body": IFRAME}]})
        run(ad_formats="INSTAGRAM_STORY")
        assert "locale" not in calls[0]["params"]

    def test_a_format_that_fails_is_reported_and_does_not_hide_the_others(self, monkeypatch):
        def handler(e, p):
            return MetaAPIError("(#100) Invalid format for this ad", error_code=100) if p["ad_format"] == "INSTAGRAM_REELS" else {"data": [{"body": IFRAME}]}

        install(monkeypatch, handler)
        out = run(ad_formats="DESKTOP_FEED_STANDARD,INSTAGRAM_REELS")
        assert [p["format"] for p in out["previews"]] == ["DESKTOP_FEED_STANDARD"]
        assert "INSTAGRAM_REELS" in out["errors"] and "did not accept this format" in out["hint"]

    def test_an_empty_or_frameless_answer_is_an_error_for_that_format_not_a_silent_gap(self, monkeypatch):
        install(monkeypatch, lambda e, p: {"data": []} if p["ad_format"] == "A_FORMAT" else {"data": [{"body": "<div>x</div>"}]})
        out = run(ad_formats="A_FORMAT,OTHER_FORMAT")
        assert out["previews"] == [] and set(out["errors"]) == {"A_FORMAT", "OTHER_FORMAT"}
        assert out["note"] == "No preview could be built."

    def test_a_permission_failure_mentions_permissions(self, monkeypatch):
        install(monkeypatch, lambda e, p: MetaAPIError("(#200) denied", error_code=200))
        assert "ads_read" in run()["hint"]

    @pytest.mark.parametrize("kw,fragment", [
        ({"ad_id": None}, "exactly one"),
        ({"creative_id": "999000111222"}, "exactly one"),
        ({"ad_id": "abc"}, "numeric"),
        ({"ad_formats": " , "}, "1 to 6"),
        ({"ad_formats": "A_B,C_D,E_F,G_H,I_J,K_L,M_N"}, "1 to 6"),
        ({"ad_formats": "bad format!"}, "not a placement name"),
    ])
    def test_input_validation(self, kw, fragment):
        out = run(**kw)
        assert out["blocked_at"] == "input_validation" and fragment in out["error"]

    def test_registered_as_read_only_tool(self):
        from meta_ads_mcp.server import mcp
        tools = {t.name: t for t in mcp._tool_manager.list_tools()}
        assert tools["get_ad_previews"].annotations.readOnlyHint is True
