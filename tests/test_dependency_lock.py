"""The committed lock must cover the validated Python 3.12 dependency graph."""

from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


DIRECT = {
    "numpy", "pandas", "pydantic", "pyyaml", "sqlalchemy", "streamlit",
    "plotly", "yfinance", "pytest", "scipy", "scikit-learn", "python-dotenv",
}


def test_transitive_dependency_lock_covers_installed_runtime_graph():
    locked = {
        canonicalize_name(line.split("==", 1)[0]): line.split("==", 1)[1].split(";", 1)[0].strip()
        for line in Path("requirements.lock").read_text().splitlines()
        if line and not line.startswith("#")
    }
    assert {canonicalize_name(name) for name in DIRECT} <= set(locked)
    queue = list(DIRECT)
    visited: set[str] = set()
    while queue:
        name = queue.pop()
        canonical = canonicalize_name(name)
        if canonical in visited:
            continue
        visited.add(canonical)
        assert canonical in locked
        assert distribution(name).version == locked[canonical]
        for raw in distribution(name).requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                queue.append(requirement.name)
