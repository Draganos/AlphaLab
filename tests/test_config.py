from pathlib import Path
import pytest
import yaml
from pydantic import ValidationError

from alpha_lab.config import load_settings


def test_default_configuration_is_valid():
    assert sum(load_settings().weights.values()) == pytest.approx(1)


def test_default_systematic_minimum_coverage_remains_seventy_percent():
    """Lowering the evidence gate requires an explicit documented model change."""
    assert load_settings().strategy.minimum_data_coverage == pytest.approx(0.70)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), -0.01],
)
def test_invalid_phase3_rating_weight_values_are_rejected(tmp_path: Path, value):
    data = yaml.safe_load(Path("config/default.yaml").read_text())
    data["rating_weights"]["valuation"] = value
    path = tmp_path / "invalid-live-weights.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_settings(path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda values: values.pop("valuation"),
        lambda values: values.update(extra_category=0.0),
        lambda values: values.update(valuation=0.14),
    ],
)
def test_phase3_rating_weights_require_exact_categories_and_total(
    tmp_path: Path, mutation
):
    data = yaml.safe_load(Path("config/default.yaml").read_text())
    mutation(data["rating_weights"])
    path = tmp_path / "invalid-live-weight-shape.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_settings(path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["weights"].update(earnings=-0.1, revisions=0.3),
        lambda data: data["weights"].pop("ai"),
        lambda data: data["strategy"].update(min_positions=11, max_positions=10),
        lambda data: data["coverage"].update(
            insufficient_below=0.7, full_confidence_at=0.7
        ),
    ],
)
def test_invalid_configuration_is_rejected(tmp_path: Path, mutation):
    data = yaml.safe_load(Path("config/default.yaml").read_text())
    mutation(data)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValidationError):
        load_settings(path)
