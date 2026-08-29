"""Typed, environment-aware application configuration."""

from pathlib import Path
import os
import math

from pydantic import BaseModel, Field, model_validator
import yaml


class StrategySettings(BaseModel):
    rebalance: str = "monthly"
    min_score: float = Field(70, ge=0, le=100)
    min_positions: int = Field(3, ge=1)
    max_positions: int = Field(10, ge=1)
    minimum_data_coverage: float = Field(0.70, ge=0, le=1)
    minimum_average_daily_volume: float = Field(0, ge=0)

    @model_validator(mode="after")
    def valid_position_range(self) -> "StrategySettings":
        if self.min_positions > self.max_positions:
            raise ValueError("min_positions cannot exceed max_positions")
        return self


class RiskSettings(BaseModel):
    max_position: float = Field(0.2, gt=0, le=1)
    max_sector: float | None = Field(0.3, gt=0, le=1)
    no_leverage: bool = True


class CoverageSettings(BaseModel):
    insufficient_below: float = Field(0.40, ge=0, le=1)
    full_confidence_at: float = Field(0.70, ge=0, le=1)

    @model_validator(mode="after")
    def valid_threshold_order(self) -> "CoverageSettings":
        if self.insufficient_below >= self.full_confidence_at:
            raise ValueError("insufficient_below must be less than full_confidence_at")
        return self


class TransactionCostSettings(BaseModel):
    fixed_commission: float = Field(0.0, ge=0)
    percentage_commission: float = Field(0.001, ge=0)
    spread: float = Field(0.001, ge=0, lt=1)
    slippage: float = Field(0.0005, ge=0, lt=1)
    minimum_trade_amount: float = Field(10.0, ge=0)


class BacktestSettings(BaseModel):
    initial_capital: float = Field(100_000, gt=0)
    base_currency: str = "USD"
    rebalance: str = "monthly"
    weighting: str = "equal"
    fractional_shares: bool = True
    risk_free_rate: float = 0.0
    execution: str = "next_available_open"
    costs: TransactionCostSettings = Field(default_factory=TransactionCostSettings)


class Settings(BaseModel):
    database_url: str = "sqlite:///data/alpha_lab.db"
    universe: dict[str, list[str]]
    strategy: StrategySettings
    weights: dict[str, float]
    coverage: CoverageSettings
    risk: RiskSettings
    paper_trading: dict[str, float]
    data_quality: dict[str, int]
    backtest: BacktestSettings
    rating_weights: dict[str, float]
    ethics_policy_path: str = "config/ethics.yaml"

    @model_validator(mode="after")
    def weights_total_one(self) -> "Settings":
        expected = {
            "earnings",
            "revisions",
            "fundamentals",
            "valuation",
            "momentum",
            "balance_sheet",
            "ai",
            "dividend",
        }
        if set(self.weights) != expected:
            raise ValueError(
                f"Composite weights must contain exactly: {sorted(expected)}"
            )
        if any(
            not math.isfinite(value) or value < 0 for value in self.weights.values()
        ):
            raise ValueError("Composite weights must be finite and non-negative")
        if abs(sum(self.weights.values()) - 1.0) > 1e-8:
            raise ValueError("Composite weights must total 1.0")
        expected_live = {
            "earnings_growth",
            "analyst_revisions",
            "business_quality",
            "valuation",
            "momentum",
            "financial_strength",
            "ai_research",
            "shareholder_return",
        }
        if set(self.rating_weights) != expected_live:
            raise ValueError(
                f"Phase 3 rating_weights must contain exactly: {sorted(expected_live)}"
            )
        if any(
            not math.isfinite(value) or value < 0
            for value in self.rating_weights.values()
        ):
            raise ValueError("Phase 3 rating_weights must be finite and non-negative")
        if abs(sum(self.rating_weights.values()) - 1.0) > 1e-8:
            raise ValueError("Phase 3 rating_weights must total 1.0")
        return self


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = Path(path or os.getenv("ALPHALAB_CONFIG", "config/default.yaml"))
    with config_path.open(encoding="utf-8") as stream:
        values = yaml.safe_load(stream)
    values["database_url"] = os.getenv("ALPHALAB_DATABASE_URL", values["database_url"])
    return Settings.model_validate(values)
