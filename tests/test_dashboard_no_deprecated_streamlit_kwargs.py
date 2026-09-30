"""Streamlit removed `use_container_width` (replaced by `width="stretch"` /
`width="content"`); every call still passing it logs a deprecation warning
on each render. Scan the dashboard source so it cannot come back."""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
DEPRECATED_KWARGS = {"use_container_width"}


def test_dashboard_uses_no_deprecated_streamlit_kwargs():
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in DEPRECATED_KWARGS:
                        offenders.append(f"{path.relative_to(APP_DIR.parent)}:{node.lineno} {keyword.arg}")
    assert not offenders, "deprecated Streamlit kwargs: " + ", ".join(offenders)
