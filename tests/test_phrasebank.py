"""Tests for alpha_lab.ai.phrasebank's fetch/parse logic. No real network
call and no real dataset download here -- `parse_financial_phrasebank` is
pure, so it is tested directly against a tiny in-memory zip fixture built
to match the real dataset's own on-disk shape (member path ending in the
requested subset filename, `sentence@label` lines, Latin-1 encoding)."""

import io
import zipfile

import pytest

from alpha_lab.ai.phrasebank import (
    DEFAULT_SUBSET,
    LabeledSentence,
    fetch_financial_phrasebank,
    parse_financial_phrasebank,
)


def _zip_bytes(member_name: str, content: str, *, encoding: str = "latin-1") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member_name, content.encode(encoding))
    return buffer.getvalue()


def test_parse_splits_sentence_and_label_on_last_at_sign():
    archive = _zip_bytes(
        f"FinancialPhraseBank-v1.0/{DEFAULT_SUBSET}",
        "Revenue grew 10% year over year.@positive\n"
        "The company reported a net loss.@negative\n"
        "The meeting was held in Helsinki.@neutral\n",
    )
    sentences = parse_financial_phrasebank(archive)
    assert sentences == [
        LabeledSentence(text="Revenue grew 10% year over year.", label="positive"),
        LabeledSentence(text="The company reported a net loss.", label="negative"),
        LabeledSentence(text="The meeting was held in Helsinki.", label="neutral"),
    ]


def test_parse_handles_at_signs_inside_the_sentence_text_via_rpartition():
    """rpartition splits on the LAST '@' -- an '@' inside the sentence itself
    (e.g. an email-like token) must not be mistaken for the delimiter."""
    archive = _zip_bytes(
        f"FinancialPhraseBank-v1.0/{DEFAULT_SUBSET}",
        "Contact investor@relations for the report.@neutral\n",
    )
    sentences = parse_financial_phrasebank(archive)
    assert sentences == [
        LabeledSentence(text="Contact investor@relations for the report.", label="neutral")
    ]


def test_parse_skips_blank_lines_and_lines_without_a_valid_label():
    archive = _zip_bytes(
        f"FinancialPhraseBank-v1.0/{DEFAULT_SUBSET}",
        "\nGood results this quarter.@positive\n\nMalformed line with no delimiter\n"
        "Unknown label sentence.@bullish\n",
    )
    sentences = parse_financial_phrasebank(archive)
    assert sentences == [LabeledSentence(text="Good results this quarter.", label="positive")]


def test_parse_decodes_non_ascii_latin1_characters():
    archive = _zip_bytes(
        f"FinancialPhraseBank-v1.0/{DEFAULT_SUBSET}",
        "Nokian Renkaat announced a new plant in Vsevolozhsk.@neutral\n",
        encoding="latin-1",
    )
    sentences = parse_financial_phrasebank(archive)
    assert sentences[0].text.startswith("Nokian Renkaat")


def test_parse_selects_requested_subset_only():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "FinancialPhraseBank-v1.0/Sentences_AllAgree.txt",
            "All-agree sentence.@positive\n".encode("latin-1"),
        )
        archive.writestr(
            "FinancialPhraseBank-v1.0/Sentences_50Agree.txt",
            "Fifty-agree sentence.@negative\n".encode("latin-1"),
        )
    sentences = parse_financial_phrasebank(buffer.getvalue(), subset="Sentences_AllAgree.txt")
    assert sentences == [LabeledSentence(text="All-agree sentence.", label="positive")]


def test_parse_ignores_macosx_metadata_entries():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"__MACOSX/._{DEFAULT_SUBSET}",
            "garbage@positive\n".encode("latin-1"),
        )
        archive.writestr(
            f"FinancialPhraseBank-v1.0/{DEFAULT_SUBSET}",
            "Real sentence.@positive\n".encode("latin-1"),
        )
    sentences = parse_financial_phrasebank(buffer.getvalue())
    assert sentences == [LabeledSentence(text="Real sentence.", label="positive")]


def test_fetch_uses_cache_and_never_calls_network_when_cached(tmp_path, monkeypatch):
    cache_path = tmp_path / "cached.zip"
    cache_path.write_bytes(b"cached-content")

    def _fail_urlopen(*_args, **_kwargs):
        raise AssertionError("network should not be called when a cache file exists")

    monkeypatch.setattr("alpha_lab.ai.phrasebank.urlopen", _fail_urlopen)
    result = fetch_financial_phrasebank(cache_path=cache_path)
    assert result == b"cached-content"


def test_fetch_writes_cache_on_first_download(tmp_path, monkeypatch):
    cache_path = tmp_path / "nested" / "downloaded.zip"

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def read(self):
            return b"fresh-bytes"

    monkeypatch.setattr(
        "alpha_lab.ai.phrasebank.urlopen", lambda *_args, **_kwargs: _FakeResponse()
    )
    result = fetch_financial_phrasebank(cache_path=cache_path)
    assert result == b"fresh-bytes"
    assert cache_path.read_bytes() == b"fresh-bytes"


def test_fetch_refresh_true_bypasses_existing_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "cached.zip"
    cache_path.write_bytes(b"stale-content")

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_exc_info):
            return False

        def read(self):
            return b"refreshed-content"

    monkeypatch.setattr(
        "alpha_lab.ai.phrasebank.urlopen", lambda *_args, **_kwargs: _FakeResponse()
    )
    result = fetch_financial_phrasebank(cache_path=cache_path, refresh=True)
    assert result == b"refreshed-content"
    assert cache_path.read_bytes() == b"refreshed-content"
