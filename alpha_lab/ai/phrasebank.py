"""Fetch and parse the real Financial PhraseBank dataset (Malo, Sinha,
Takala, Korhonen & Wallenius, 2014, "Good debt or bad debt: Detecting
semantic orientations in economic texts") -- ~4,800 sentences from real
financial news, each hand-labeled positive/negative/neutral by 16 people
with financial-markets backgrounds (researchers and Aalto University
finance/accounting/economics master's students). The real, published,
human-annotated dataset `alpha_lab.ai.sklearn_sentiment`'s classifier is
actually trained and evaluated on -- never fabricated or self-labeled.

CC-BY-NC-SA-3.0 licensed (non-commercial, share-alike, attribution
required). Fetched fresh from the dataset's own Hugging Face mirror and
cached to disk here, exactly like every other external evidence source in
this codebase (SECClient's own get_json/get_text) -- never committed to
this repo, matching data/cache/'s existing gitignored precedent.
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen
import io
import zipfile

DATASET_URL = (
    "https://huggingface.co/datasets/takala/financial_phrasebank/"
    "resolve/main/data/FinancialPhraseBank-v1.0.zip"
)
DEFAULT_CACHE_PATH = Path("data/cache/FinancialPhraseBank-v1.0.zip")

# The dataset ships four reference subsets, one per annotator-agreement
# threshold -- more agreement means fewer but cleaner-labeled sentences.
# 75Agree balances real label volume against real label quality (AllAgree
# is the cleanest but smallest at 2,264 sentences; 50Agree is the largest
# at 4,846 but includes sentences only a bare majority of annotators
# agreed on). A deliberately documented choice, not tuned against
# AlphaLab's own downstream calibration.
DEFAULT_SUBSET = "Sentences_75Agree.txt"

_VALID_LABELS = {"positive", "negative", "neutral"}


@dataclass(frozen=True)
class LabeledSentence:
    text: str
    label: str  # "positive" | "negative" | "neutral"


def fetch_financial_phrasebank(
    *, cache_path: Path | str = DEFAULT_CACHE_PATH, refresh: bool = False
) -> bytes:
    """The real dataset zip's raw bytes -- fetched fresh only when not
    already cached (or `refresh=True`); the dataset is static and
    versioned, so a cached copy is never stale the way a live filing
    index would be."""
    cache_path = Path(cache_path)
    if cache_path.exists() and not refresh:
        return cache_path.read_bytes()
    request = Request(DATASET_URL, headers={"User-Agent": "AlphaLab Research (financial_phrasebank fetch)"})
    with urlopen(request, timeout=60) as response:  # noqa: S310 - fixed HTTPS dataset URL
        payload = response.read()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(payload)
    return payload


def parse_financial_phrasebank(archive_bytes: bytes, *, subset: str = DEFAULT_SUBSET) -> list[LabeledSentence]:
    """Real (sentence, label) pairs from the dataset's own `@`-separated
    format (`sentence@sentiment`), decoded as the source files themselves
    are encoded (Latin-1, per the dataset's own README/known encoding --
    the financial news text includes non-ASCII characters, e.g. Finnish
    company names, that are not valid UTF-8 in the original files)."""
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        member = next(
            name for name in archive.namelist()
            if name.endswith(subset) and "__MACOSX" not in name
        )
        raw = archive.read(member).decode("latin-1")
    sentences: list[LabeledSentence] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "@" not in line:
            continue
        text, _, label = line.rpartition("@")
        label = label.strip().lower()
        if label not in _VALID_LABELS:
            continue
        sentences.append(LabeledSentence(text=text.strip(), label=label))
    return sentences
