"""
Tests for upload_video_from_url.

Covers input validation, title derivation, and the request sent to Meta
(file_url passthrough) using a mocked HTTP client - no real API calls.
"""
from unittest.mock import MagicMock, patch

import pytest


class TestUploadVideoFromUrlValidation:

    def test_function_exists(self):
        from meta_ads_mcp.core.video import upload_video_from_url
        assert callable(upload_video_from_url)

    @pytest.mark.parametrize("bad_url", ["", "   ", "example.com/v.mp4", "ftp://example.com/v.mp4"])
    def test_invalid_urls_blocked(self, bad_url):
        from meta_ads_mcp.core.video import upload_video_from_url
        result = upload_video_from_url(account_id="act_123", video_url=bad_url)
        assert "error" in result
        assert result["blocked_at"] == "input_validation"


class TestDeriveTitle:

    def test_strips_query_and_extension(self):
        from meta_ads_mcp.core.video import _derive_title_from_url
        url = "https://air-prod.imgix.net/abc/My%20Spot_15s.mp4?X-Amz-Signature=xyz&expires=1"
        assert _derive_title_from_url(url) == "My Spot_15s"

    def test_fallback_when_no_filename(self):
        from meta_ads_mcp.core.video import _derive_title_from_url
        assert _derive_title_from_url("https://example.com/") == "Uploaded video"


def _mock_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    resp.json.return_value = json_body or {}
    resp.text = str(json_body)
    return resp


class TestUploadVideoFromUrlRequest:

    def _run(self, response, **kwargs):
        from meta_ads_mcp.core import video

        client = MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = response

        with patch.object(video.api_client, "_ensure_initialized"), \
             patch.object(video.api_client, "_access_token", "TOKEN", create=True), \
             patch.object(video.api_client, "graph_get",
                          return_value={"status": {"processing_phase": {"status": "in_progress"}}}), \
             patch.object(video.httpx, "Client", return_value=client):
            result = video.upload_video_from_url(**kwargs)
        return result, client

    def test_sends_file_url_and_returns_video_id(self):
        result, client = self._run(
            _mock_response(200, {"id": "987"}),
            account_id="123",
            video_url="https://cdn.example.com/ad.mp4?sig=1",
            description="desc",
        )
        url = client.post.call_args.args[0]
        data = client.post.call_args.kwargs["data"]
        assert url.endswith("/act_123/advideos")
        assert data["file_url"] == "https://cdn.example.com/ad.mp4?sig=1"
        assert data["title"] == "ad"
        assert data["description"] == "desc"
        assert "files" not in client.post.call_args.kwargs
        assert result["video_id"] == "987"
        assert result["processing_state"] == "processing"
        assert result["upload_status"] == "success"

    def test_explicit_title_used(self):
        _, client = self._run(
            _mock_response(200, {"id": "1"}),
            account_id="act_1",
            video_url="https://cdn.example.com/ad.mp4",
            title="Custom",
        )
        assert client.post.call_args.kwargs["data"]["title"] == "Custom"

    def test_meta_error_surfaced(self):
        result, _ = self._run(
            _mock_response(400, {"error": {"message": "Could not download file"}}),
            account_id="act_1",
            video_url="https://cdn.example.com/expired.mp4",
        )
        assert result["blocked_at"] == "api_upload"
        assert "Could not download file" in result["error"]

    def test_missing_id_reported(self):
        result, _ = self._run(
            _mock_response(200, {}),
            account_id="act_1",
            video_url="https://cdn.example.com/ad.mp4",
        )
        assert result["blocked_at"] == "api_response"
