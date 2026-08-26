"""Unified application configuration.

Two sources are merged into one :class:`AppConfig`:

* :class:`Settings` - flat, per-deployment values sourced from environment
  variables / ``.env`` (secrets, sizing, thresholds). This mirrors the exact
  flat variable names from the project spec (``INITIAL_ORDER_USDT``,
  ``MIN_BUY_SCORE`` etc.) so ops can tune the bot without touching code.
* :class:`RulesConfig` - structured "strategy internals" loaded from
  ``config/config.yaml`` (indicator periods, signal point-weights, regime
  policy, universe filtering rules). These are naturally lists/dicts and
  don't fit flat env vars.

Call :func:`get_config` everywhere; it is process-wide cached (call
``get_config.cache_clear()`` in tests that need a fresh read).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_YAML_PATH = Path(__file__).resolve().parent / "config.yaml"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

SIGNAL_CATEGORIES = ("trend", "momentum", "volume", "volatility", "structure")


class TradingMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Settings(BaseSettings):
    """Flat per-deployment configuration read from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode & safety ---------------------------------------------------
    mode: TradingMode = TradingMode.PAPER
    dry_run: bool = True

    # --- Binance -----------------------------------------------------------
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    binance_testnet: bool = True

    # --- Telegram ------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_user_id: int = 0

    # --- Database ------------------------------------------------------------
    database_url: str = "sqlite:///./data/crypto_bot.db"

    # --- Position sizing -------------------------------------------------
    initial_order_usdt: Decimal = Decimal("100")
    max_position_usdt: Decimal = Decimal("300")
    max_open_positions: int = 3

    # --- Take profit -----------------------------------------------------
    target_profit_percent: Decimal = Decimal("10")
    use_trailing_after_tp: bool = False
    trailing_partial_close_fraction: Decimal = Decimal("0.6")
    trailing_distance_percent: Decimal = Decimal("2.5")

    # --- DCA (controlled averaging - NOT martingale) ----------------------
    max_dca_count: int = 3
    dca_level_1: Decimal = Decimal("-3")
    dca_level_2: Decimal = Decimal("-6")
    dca_level_3: Decimal = Decimal("-10")
    dca_size_1_usdt: Decimal = Decimal("50")
    dca_size_2_usdt: Decimal = Decimal("75")
    dca_size_3_usdt: Decimal = Decimal("75")
    min_dca_score: int = 65

    # --- Signal scoring ----------------------------------------------------
    min_buy_score: int = 75
    min_confirmed_signals: int = 5
    min_confirmation_categories: int = 4

    # --- Fees / slippage assumptions --------------------------------------
    taker_fee_rate: Decimal = Decimal("0.001")
    maker_fee_rate: Decimal = Decimal("0.001")
    expected_slippage_percent: Decimal = Decimal("0.05")

    # --- Order execution ---------------------------------------------------
    limit_order_timeout_seconds: int = 90

    # --- Risk management -------------------------------------------------
    max_total_exposure_percent: Decimal = Decimal("35")
    max_daily_new_capital_usdt: Decimal = Decimal("500")
    max_consecutive_bad_trades: int = 3
    market_crash_pause: bool = True
    emergency_auto_sell: bool = False

    # --- BTC / global market filter ---------------------------------------
    btc_market_filter: bool = True

    # --- Universe / liquidity filters --------------------------------------
    min_quote_volume_24h_usdt: Decimal = Decimal("5000000")
    max_spread_percent: Decimal = Decimal("0.5")
    min_listing_age_days: int = 60
    scanner_top_n: int = 25
    scanner_interval_minutes: int = 15

    # --- News ----------------------------------------------------------------
    news_enabled: bool = True
    news_refresh_minutes: int = 10
    news_block_score_threshold: int = -50
    cryptopanic_api_token: SecretStr = SecretStr("")

    # --- Paper trading ---------------------------------------------------
    paper_starting_balance_usdt: Decimal = Decimal("10000")

    # --- Logging / health --------------------------------------------------
    log_level: str = "INFO"
    log_dir: str = "logs"
    market_data_stale_seconds: int = 180

    # --- Daily report ------------------------------------------------------
    daily_report_hour_utc: int = 21

    @field_validator("telegram_allowed_user_id", mode="before")
    @classmethod
    def _blank_user_id_to_zero(cls, v: object) -> object:
        if v in ("", None):
            return 0
        return v

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if not (0 <= self.max_dca_count <= 3):
            raise ValueError("MAX_DCA_COUNT must be between 0 and 3 (controlled DCA, not martingale)")
        if self.dca_level_1 <= self.dca_level_2 or self.dca_level_2 <= self.dca_level_3:
            raise ValueError(
                "DCA levels must get progressively deeper: DCA_LEVEL_1 > DCA_LEVEL_2 > DCA_LEVEL_3 "
                f"(got {self.dca_level_1}, {self.dca_level_2}, {self.dca_level_3})"
            )
        sizes = [self.dca_size_1_usdt, self.dca_size_2_usdt, self.dca_size_3_usdt]
        planned_total = self.initial_order_usdt + sum(sizes[: self.max_dca_count])
        if planned_total > self.max_position_usdt:
            raise ValueError(
                f"Initial order + planned DCA sizes ({planned_total} USDT) exceed "
                f"MAX_POSITION_USDT ({self.max_position_usdt}). Adjust sizing or raise the cap."
            )
        if self.mode == TradingMode.LIVE and self.dry_run:
            # Not an error: this is the safe "shadow live" combination. Left
            # here as documentation of intent, no exception raised.
            pass
        return self

    def validate_for_live(self) -> None:
        """Extra gate called explicitly before a real LIVE run is allowed to trade.

        Kept separate from the pydantic validators above because Settings must
        stay constructible (e.g. for BACKTEST, or for `--help`) without secrets.
        """
        missing: list[str] = []
        if not self.binance_api_key.get_secret_value():
            missing.append("BINANCE_API_KEY")
        if not self.binance_api_secret.get_secret_value():
            missing.append("BINANCE_API_SECRET")
        if not self.telegram_bot_token.get_secret_value():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_allowed_user_id:
            missing.append("TELEGRAM_ALLOWED_USER_ID")
        if missing:
            raise RuntimeError(
                "Cannot start MODE=LIVE trading: missing required settings: " + ", ".join(missing)
            )

    @property
    def dca_plan(self) -> list[tuple[Decimal, Decimal]]:
        """Ordered (drop_percent, size_usdt) pairs, truncated to max_dca_count."""
        levels = [
            (self.dca_level_1, self.dca_size_1_usdt),
            (self.dca_level_2, self.dca_size_2_usdt),
            (self.dca_level_3, self.dca_size_3_usdt),
        ]
        return levels[: self.max_dca_count]


# ---------------------------------------------------------------------------
# Structured rules (config/config.yaml)
# ---------------------------------------------------------------------------


class RSIConfig(BaseModel):
    period: int = 14
    oversold: float = 30
    overbought: float = 70
    reversal_lookback: int = 5
    slope_lookback: int = 3


class MACDConfig(BaseModel):
    fast: int = 12
    slow: int = 26
    signal: int = 9


class EMAConfig(BaseModel):
    fast: int = 20
    mid: int = 50
    slow: int = 200


class BollingerConfig(BaseModel):
    period: int = 20
    stddev: float = 2.0
    squeeze_lookback: int = 40
    squeeze_percentile: float = 20


class ADXConfig(BaseModel):
    period: int = 14
    strong_trend: float = 25
    very_strong_trend: float = 40


class ATRConfig(BaseModel):
    period: int = 14


class VolumeConfig(BaseModel):
    ma_period: int = 20
    abnormal_multiplier: float = 2.0
    confirmation_multiplier: float = 1.2


class VWAPConfig(BaseModel):
    anchor: Literal["session", "rolling"] = "session"
    rolling_window: int = 96


class SwingStructureConfig(BaseModel):
    order: int = 3
    lookback: int = 60


class IndicatorsConfig(BaseModel):
    rsi: RSIConfig = RSIConfig()
    macd: MACDConfig = MACDConfig()
    ema: EMAConfig = EMAConfig()
    bollinger: BollingerConfig = BollingerConfig()
    adx: ADXConfig = ADXConfig()
    atr: ATRConfig = ATRConfig()
    volume: VolumeConfig = VolumeConfig()
    vwap: VWAPConfig = VWAPConfig()
    swing_structure: SwingStructureConfig = SwingStructureConfig()


class SignalWeight(BaseModel):
    points: float
    category: str

    @field_validator("category")
    @classmethod
    def _known_category(cls, v: str) -> str:
        if v not in SIGNAL_CATEGORIES:
            raise ValueError(f"Unknown signal category {v!r}, expected one of {SIGNAL_CATEGORIES}")
        return v


class AdxVetoConfig(BaseModel):
    adx_threshold: float = 35
    di_diff_threshold: float = 10


class RegimePolicyEntry(BaseModel):
    allow_buy: bool
    min_score_delta: float = 0


class CrashDetectorConfig(BaseModel):
    window_minutes: int = 60
    drop_percent: float = 4.0
    volume_multiplier: float = 2.5
    strong_bear_drop_percent: float = 2.5
    severe_drop_percent: float = 8.0


class AntiFomoConfig(BaseModel):
    max_price_change_1h_percent: float = 8.0
    max_price_change_4h_percent: float = 15.0
    max_distance_from_ema20_atr: float = 3.0
    rsi_overbought: float = 80
    volume_spike_multiplier: float = 4.0


class CorrelationConfig(BaseModel):
    lookback_bars: int = 90
    timeframe: str = "1h"
    correlation_threshold: float = 0.75
    max_correlated_positions: int = 2


class UniverseConfig(BaseModel):
    quote_asset: str = "USDT"
    stablecoin_assets: list[str] = Field(default_factory=list)
    leveraged_token_suffixes: list[str] = Field(default_factory=lambda: ["UP", "DOWN", "BULL", "BEAR"])
    blacklist_symbols: list[str] = Field(default_factory=list)
    min_trades_24h: int = 10000
    max_orderbook_spread_depth_usdt: float = 20000


class WatchdogConfig(BaseModel):
    check_interval_seconds: int = 30
    task_restart_backoff_seconds: list[int] = Field(default_factory=lambda: [5, 15, 60])
    max_restart_attempts: int = 3


class SchedulerConfig(BaseModel):
    position_monitor_interval_seconds: int = 60
    news_refresh_minutes: int = 10


class BacktestDefaultsConfig(BaseModel):
    default_symbols: list[str] = Field(default_factory=list)
    train_fraction: float = 0.6
    validation_fraction: float = 0.2
    test_fraction: float = 0.2


class RulesConfig(BaseModel):
    indicators: IndicatorsConfig = IndicatorsConfig()
    signal_weights: dict[str, SignalWeight]
    adx_veto: AdxVetoConfig = AdxVetoConfig()
    regime_policy: dict[str, RegimePolicyEntry]
    crash_detector: CrashDetectorConfig = CrashDetectorConfig()
    anti_fomo: AntiFomoConfig = AntiFomoConfig()
    correlation: CorrelationConfig = CorrelationConfig()
    universe: UniverseConfig = UniverseConfig()
    watchdog: WatchdogConfig = WatchdogConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    backtest: BacktestDefaultsConfig = BacktestDefaultsConfig()

    @model_validator(mode="after")
    def _weights_cover_all_categories(self) -> RulesConfig:
        used = {w.category for w in self.signal_weights.values()}
        missing = set(SIGNAL_CATEGORIES) - used
        if missing:
            raise ValueError(f"signal_weights does not cover all categories, missing: {missing}")
        return self

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_YAML_PATH) -> RulesConfig:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return cls.model_validate(raw)


@dataclass(frozen=True)
class AppConfig:
    env: Settings
    rules: RulesConfig


@functools.lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig(env=Settings(), rules=RulesConfig.load())
