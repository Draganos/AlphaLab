"""Typed, environment-aware application configuration."""

from pathlib import Path
import os

from pydantic import BaseModel, Field, model_validator
import yaml


class StrategySettings(BaseModel):
    rebalance: str = "monthly"
    min_score: float = Field(70, ge=0, le=100)
    min_positions: int = Field(3, ge=1)
    max_positions: int = Field(10, ge=1)


class RiskSettings(BaseModel):
    max_position: float = Field(0.2, gt=0, le=1)
    max_sector: float | None = Field(0.3, gt=0, le=1)
    no_leverage: bool = True


class Settings(BaseModel):
    database_url: str = "sqlite:///data/alpha_lab.db"
    universe: dict[str, list[str]]
    strategy: StrategySettings
    weights: dict[str, float]
    risk: RiskSettings
    paper_trading: dict[str, float]
    data_quality: dict[str, int]

    @model_validator(mode="after")
    def weights_total_one(self) -> "Settings":
        if abs(sum(self.weights.values()) - 1.0) > 1e-8:
            raise ValueError("Composite weights must total 1.0")
        return self


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("ALPHALAB_CONFIG", "config/default.yaml"))
    with config_path.open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    values["database_url"] = os.getenv("ALPHALAB_DATABASE_URL", values["database_url"])
    return Settings.model_validate(values)
