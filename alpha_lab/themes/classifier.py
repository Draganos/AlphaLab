"""Evidence-derived business themes without ticker hard-coding."""

from pydantic import BaseModel


class ThemeEvidence(BaseModel):
    theme: str
    confidence: float
    evidence: str
    source: str


_THEMES = {
    "artificial intelligence": (
        "artificial intelligence",
        "machine learning",
        "generative ai",
    ),
    "semiconductors": ("semiconductor", "chip design", "integrated circuit"),
    "data centres": ("data center", "data centre", "hyperscale"),
    "cybersecurity": ("cybersecurity", "cyber security"),
    "robotics": ("robotics", "industrial automation"),
    "cloud": ("cloud computing", "cloud platform"),
    "payments": ("payment network", "payment processing", "merchant acquiring"),
    "aviation": ("airline", "passenger aviation", "air transport"),
    "renewable energy": ("renewable energy", "solar", "wind power"),
    "healthcare": ("healthcare", "medical device"),
    "biotech": ("biotechnology", "biotech"),
    "logistics": ("logistics", "freight", "supply chain"),
}


def derive_themes(text: str | None, source: str) -> list[ThemeEvidence]:
    if not text:
        return []
    lowered = text.lower()
    return [
        ThemeEvidence(theme=theme, confidence=0.9, evidence=term, source=source)
        for theme, terms in _THEMES.items()
        for term in terms
        if term in lowered
    ][:20]
