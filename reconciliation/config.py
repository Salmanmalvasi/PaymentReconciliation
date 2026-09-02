"""
Application configuration.

All tunable parameters live here — no magic numbers in business logic.
Tolerances, windows, and thresholds are configurable via environment
variables with sensible defaults.
"""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # --- Database ---
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/payment_recon",
        description="PostgreSQL connection string",
    )

    # --- Reconciliation tolerances ---
    rounding_tolerance: float = Field(
        default=0.02,
        description="Maximum minor-unit difference considered rounding noise (e.g. $0.02)",
    )
    fee_tolerance: float = Field(
        default=0.05,
        description="Maximum acceptable difference after fee deduction before flagging",
    )
    settlement_delay_days: int = Field(
        default=5,
        description="Number of days after creation before a missing settlement is flagged",
    )
    duplicate_amount_tolerance: float = Field(
        default=0.00,
        description="Amount difference threshold for treating duplicates as conflicting",
    )

    # --- Generator defaults ---
    default_num_records: int = Field(default=10000, description="Default record count for generator")
    default_anomaly_rate: float = Field(default=0.15, description="Default anomaly injection rate")
    default_seed: int = Field(default=42, description="Default random seed")

    # --- API ---
    api_title: str = "Payment Reconciliation API"
    api_version: str = "1.0.0"
    default_page_size: int = 50
    max_page_size: int = 500

    model_config = {"env_prefix": "RECON_", "env_file": ".env", "extra": "ignore"}


settings = Settings()
