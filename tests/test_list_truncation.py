"""
List tools stop at 200 results. They used to do so silently, so `total` and
`status_counts` looked complete (an ACTIVE campaign could be missing from an
`ALL` list with no sign anything was cut). They must now say so.
"""
import pytest

from meta_ads_mcp.core import utils
from meta_ads_mcp.core.api import api_client

PAGE = 100  # list tools request 100 per page


def _tool(name):
    """Return (callable, list endpoint, key holding the items) for each list tool."""
    if name == "campaigns":
        from meta_ads_mcp.core.campaigns import get_campaigns
        return (lambda after=None: get_campaigns("act_1", after=after)), "/act_1/campaigns", "campaigns"
    if name == "adsets":
        from meta_ads_mcp.core.adsets import get_adsets
        return (lambda after=None: get_adsets("act_1", after=after)), "/act_1/adsets", "adsets"
    if name == "ads":
        from meta_ads_mcp.core.ads import get_ads
        return (lambda after=None: get_ads("act_1", after=after)), "/act_1/ads", "ads"
    if name == "audiences":
        from meta_ads_mcp.core.audiences import list_custom_audiences
        return (lambda after=None: list_custom_audiences("act_1", limit=100, after=after)), "/act_1/customaudiences", "audiences"
    if name == "creatives":
        from meta_ads_mcp.core.creatives import get_ad_creatives
        return (lambda after=None: get_ad_creatives("act_1", limit=100, after=after)), "/act_1/adcreatives", "creatives"
    raise ValueError(name)


TOOLS = ["campaigns", "adsets", "ads", "audiences", "creatives"]


def install_pages(monkeypatch, endpoint, pages):
    """pages: list of (item_count, has_next). Page i is served when `after` == f'c{i}'."""
    fetched = []

    def fake(ep, params=None, fields=None):
        if ep == "/act_1":
            return {"currency": "USD"}
        assert ep == endpoint, ep
        after = (params or {}).get("after")
        idx = 0 if after is None else int(after[1:])
        fetched.append(idx)
        count, has_next = pages[idx]
        out = {"data": [{"id": f"{idx}-{n}", "effective_status": "ACTIVE"} for n in range(count)]}
        if has_next:
            out["paging"] = {"next": "https://graph.facebook.com/next", "cursors": {"after": f"c{idx + 1}"}}
        return out

    monkeypatch.setattr(api_client, "_ensure_initialized", lambda: None)
    monkeypatch.setattr(api_client, "graph_get", fake)
    return fetched


@pytest.mark.parametrize("name", TOOLS)
class TestTruncationFlag:
    def test_capped_list_is_flagged(self, monkeypatch, name):
        call, endpoint, key = _tool(name)
        fetched = install_pages(monkeypatch, endpoint, [(PAGE, True), (PAGE, True), (PAGE, False)])
        out = call()
        assert out["total"] == 2 * PAGE
        assert len(out[key]) == 2 * PAGE
        assert fetched == [0, 1]  # the cap is unchanged: the third page is still not fetched
        assert out["truncated"] is True
        assert "more exist" in out["truncation_note"]
        assert str(2 * PAGE) in out["truncation_note"]

    def test_truncated_response_gives_a_cursor_that_continues_the_list(self, monkeypatch, name):
        call, endpoint, key = _tool(name)
        fetched = install_pages(monkeypatch, endpoint, [(PAGE, True), (PAGE, True), (PAGE, True), (40, False)])
        first = call()
        assert first["next_cursor"] == "c2"
        assert "next_cursor" in first["truncation_note"] and "`after`" in first["truncation_note"]
        assert fetched == [0, 1]

        rest = call(after=first["next_cursor"])
        assert fetched == [0, 1, 2, 3]  # resumed at page index 2, not from the start
        assert rest["total"] == PAGE + 40
        assert rest["truncated"] is False and "next_cursor" not in rest
        ids = {i["id"] for i in first[key]} | {i["id"] for i in rest[key]}
        assert len(ids) == 2 * PAGE + PAGE + 40  # nothing lost, nothing repeated

    def test_exhausted_list_is_not_flagged(self, monkeypatch, name):
        call, endpoint, key = _tool(name)
        install_pages(monkeypatch, endpoint, [(PAGE, True), (50, False)])
        out = call()
        assert out["total"] == PAGE + 50
        assert out["truncated"] is False
        assert "truncation_note" not in out

    def test_single_short_page_is_not_flagged(self, monkeypatch, name):
        call, endpoint, key = _tool(name)
        install_pages(monkeypatch, endpoint, [(7, False)])
        out = call()
        assert out["total"] == 7 and out["truncated"] is False

    def test_empty_trailing_page_with_stale_next_link_is_not_flagged(self, monkeypatch, name):
        call, endpoint, key = _tool(name)
        install_pages(monkeypatch, endpoint, [(PAGE, True), (0, True)])
        out = call()
        assert out["total"] == PAGE
        assert out["truncated"] is False


class TestNarrowingAdvice:
    """The note must only suggest parameters the tool really has (get_campaigns once pointed at
    'a parent campaign/ad set ID', which it cannot take)."""

    def note(self, monkeypatch, name):
        call, endpoint, _ = _tool(name)
        install_pages(monkeypatch, endpoint, [(PAGE, True), (PAGE, True), (PAGE, False)])
        return call()["truncation_note"]

    def test_campaigns_only_suggests_status_filter(self, monkeypatch):
        note = self.note(monkeypatch, "campaigns")
        assert "status_filter" in note
        assert "campaign_id" not in note and "ad set" not in note

    def test_adsets_and_ads_suggest_their_real_filters(self, monkeypatch):
        assert "campaign_id" in self.note(monkeypatch, "adsets")
        assert "adset_id" in self.note(monkeypatch, "ads")

    def test_audiences_and_creatives_have_nothing_to_narrow_with(self, monkeypatch):
        for name in ("audiences", "creatives"):
            note = self.note(monkeypatch, name)
            assert "status_filter" not in note and "campaign_id" not in note, name


class TestTruncationFields:
    def test_no_next_link(self):
        assert utils.truncation_fields({}, 10) == {"truncated": False}
        assert utils.truncation_fields(None, 10) == {"truncated": False}
        assert utils.truncation_fields({"cursors": {"after": "x"}}, 10) == {"truncated": False}

    def test_next_link_means_truncated(self):
        out = utils.truncation_fields({"next": "https://x"}, 200)
        assert out["truncated"] is True
        assert "200" in out["truncation_note"]
        assert "next_cursor" not in out  # no cursor from Meta, so none offered

    def test_cursor_is_returned_when_meta_gives_one(self):
        out = utils.truncation_fields({"next": "https://x", "cursors": {"after": "abc"}}, 200, "Try a filter.")
        assert out["next_cursor"] == "abc"
        assert "Pass next_cursor as `after`" in out["truncation_note"]
        assert out["truncation_note"].endswith("Try a filter.")
