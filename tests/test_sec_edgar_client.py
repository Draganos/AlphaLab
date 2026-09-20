"""Offline tests for SECClient.get_text -- added so SECFilingDocumentProvider
never needs its own second HTTP fetch path (see the Document Evidence
Engine's self-review fix). No test here makes a real network call."""

from unittest.mock import patch
from urllib.error import URLError

import pytest

from alpha_lab.providers.sec_edgar import SECClient


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_get_text_returns_the_real_fetched_text(tmp_path):
    client = SECClient("AlphaLab Test test@example.com", cache_dir=tmp_path)
    with patch("alpha_lab.providers.sec_edgar.urlopen", return_value=_FakeResponse(b"Real filing text.")):
        text = client.get_text("https://www.sec.gov/example.htm")
    assert text == "Real filing text."


def test_get_text_caches_to_disk_and_does_not_refetch(tmp_path):
    client = SECClient("AlphaLab Test test@example.com", cache_dir=tmp_path)
    with patch("alpha_lab.providers.sec_edgar.urlopen", return_value=_FakeResponse(b"First fetch.")) as fake:
        client.get_text("https://www.sec.gov/example.htm")
        assert fake.call_count == 1
    # A second call, even with a urlopen that would raise if invoked, must
    # be served from the disk cache -- a filing's own text is immutable
    # once filed, so re-fetching an already-ingested one is pure waste.
    with patch("alpha_lab.providers.sec_edgar.urlopen", side_effect=AssertionError("must not refetch")):
        text = client.get_text("https://www.sec.gov/example.htm")
    assert text == "First fetch."


def test_get_text_refresh_true_bypasses_the_cache(tmp_path):
    client = SECClient("AlphaLab Test test@example.com", cache_dir=tmp_path)
    with patch("alpha_lab.providers.sec_edgar.urlopen", return_value=_FakeResponse(b"Version one.")):
        client.get_text("https://www.sec.gov/example.htm")
    with patch("alpha_lab.providers.sec_edgar.urlopen", return_value=_FakeResponse(b"Version two.")):
        text = client.get_text("https://www.sec.gov/example.htm", refresh=True)
    assert text == "Version two."


def test_get_text_returns_none_never_raises_when_every_retry_fails(tmp_path):
    client = SECClient("AlphaLab Test test@example.com", cache_dir=tmp_path, retries=0)
    with patch("alpha_lab.providers.sec_edgar.urlopen", side_effect=URLError("boom")):
        text = client.get_text("https://www.sec.gov/example.htm")
    assert text is None
