from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError

from alpha_lab.config import load_settings


def test_default_configuration_is_valid():
    assert sum(load_settings().weights.values()) == pytest.approx(1)


@pytest.mark.parametrize("mutation", [
    lambda data: data["weights"].update(earnings=-0.1, revisions=0.3),
    lambda data: data["weights"].pop("ai"),
    lambda data: data["strategy"].update(min_positions=11, max_positions=10),
    lambda data: data["coverage"].update(insufficient_below=.7, full_confidence_at=.7),
])
def test_invalid_configuration_is_rejected(tmp_path: Path, mutation):
    data = yaml.safe_load(Path("config/default.yaml").read_text())
    mutation(data)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_settings(path)
