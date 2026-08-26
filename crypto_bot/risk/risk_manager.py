"""RiskManager: the single authority on "is this trade allowed right now".

Combines capital limits (open positions, per-position cap, total exposure,
daily new capital), a consecutive-loss circuit breaker, the market-crash
pause, and the manual pause/emergency-stop switches exposed via Telegram.
All flags persist to the `settings` table so they survive a restart.

`StrategyEngine` must route every BUY/DCA through this before
`ExecutionEngine` ever sees it (project rule: Strategy -> Risk -> Execution
-> Binance) - this module never talks to Binance itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from config.settings import Settings
from database.models import BotEventLevel
from database.repository import EventRepository, PositionRepository, SettingsRepository
from database.session import session_scope
from market.market_regime import RegimeAssessment
from risk.crash_detector import apply_crash_policy
from risk.exposure_manager import add_daily_new_capital, current_exposure, get_daily_new_capital
from utils.time import utcnow

KEY_BUY_PAUSED = "risk_buy_paused"
KEY_DCA_PAUSED = "risk_dca_paused"
KEY_EMERGENCY_STOP = "risk_emergency_stop"
KEY_CONSECUTIVE_BAD_TRADES = "risk_consecutive_bad_trades"


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reasons: list[str]


@dataclass(frozen=True)
class RiskFlags:
    buy_paused: bool
    dca_paused: bool
    emergency_stop: bool
    consecutive_bad_trades: int


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # ---- persisted flags --------------------------------------------------

    def _read_flags(self, settings_repo: SettingsRepository) -> RiskFlags:
        return RiskFlags(
            buy_paused=settings_repo.get_bool(KEY_BUY_PAUSED, False),
            dca_paused=settings_repo.get_bool(KEY_DCA_PAUSED, False),
            emergency_stop=settings_repo.get_bool(KEY_EMERGENCY_STOP, False),
            consecutive_bad_trades=settings_repo.get_int(KEY_CONSECUTIVE_BAD_TRADES, 0),
        )

    def status(self) -> RiskFlags:
        with session_scope() as session:
            return self._read_flags(SettingsRepository(session))

    def pause_buys(self) -> None:
        with session_scope() as session:
            SettingsRepository(session).set_bool(KEY_BUY_PAUSED, True)

    def resume_buys(self) -> None:
        with session_scope() as session:
            SettingsRepository(session).set_bool(KEY_BUY_PAUSED, False)

    def stop_dca(self) -> None:
        with session_scope() as session:
            SettingsRepository(session).set_bool(KEY_DCA_PAUSED, True)

    def start_dca(self) -> None:
        with session_scope() as session:
            SettingsRepository(session).set_bool(KEY_DCA_PAUSED, False)

    def trigger_emergency_stop(self) -> None:
        """Kill switch: stop new BUYs and DCA immediately. Does NOT sell
        existing positions - that's a deliberate, separate, explicitly
        configured action (see `EMERGENCY_AUTO_SELL` / app-level handling),
        never an automatic side effect of the switch itself."""
        with session_scope() as session:
            repo = SettingsRepository(session)
            repo.set_bool(KEY_EMERGENCY_STOP, True)
            repo.set_bool(KEY_BUY_PAUSED, True)
            repo.set_bool(KEY_DCA_PAUSED, True)
            EventRepository(session).log(
                level=BotEventLevel.CRITICAL, category="risk", message="EMERGENCY STOP triggered"
            )

    def clear_emergency_stop(self) -> None:
        with session_scope() as session:
            SettingsRepository(session).set_bool(KEY_EMERGENCY_STOP, False)

    def register_trade_result(self, *, is_win: bool) -> int:
        """Call once per closed position. Returns the updated consecutive
        bad-trade count. Resets to 0 on a win; a losing streak reaching
        `MAX_CONSECUTIVE_BAD_TRADES` auto-pauses new BUYs (DCA on already
        open positions is left alone - that's governed separately by its own
        re-analysis gate)."""
        with session_scope() as session:
            settings_repo = SettingsRepository(session)
            flags = self._read_flags(settings_repo)
            bad_trades = 0 if is_win else flags.consecutive_bad_trades + 1
            settings_repo.set(KEY_CONSECUTIVE_BAD_TRADES, str(bad_trades))
            if not is_win and bad_trades >= self._settings.max_consecutive_bad_trades:
                settings_repo.set_bool(KEY_BUY_PAUSED, True)
                EventRepository(session).log(
                    level=BotEventLevel.WARNING, category="risk",
                    message=f"{bad_trades} consecutive losing trades - new BUYs paused",
                )
            return bad_trades

    def record_new_capital_deployed(self, amount_usdt: Decimal) -> None:
        with session_scope() as session:
            add_daily_new_capital(SettingsRepository(session), utcnow().date().isoformat(), amount_usdt)

    # ---- decisions ----------------------------------------------------------

    def can_open_new_position(
        self, *, requested_usdt: Decimal, trading_balance_usdt: Decimal, regime: RegimeAssessment
    ) -> RiskDecision:
        reasons: list[str] = []
        with session_scope() as session:
            settings_repo = SettingsRepository(session)
            position_repo = PositionRepository(session)
            flags = self._read_flags(settings_repo)

            if flags.emergency_stop:
                reasons.append("emergency stop is active (/emergency_stop)")
            if flags.buy_paused:
                reasons.append("new BUYs are paused")
            if flags.consecutive_bad_trades >= self._settings.max_consecutive_bad_trades:
                reasons.append(
                    f"{flags.consecutive_bad_trades} consecutive losing trades >= limit "
                    f"{self._settings.max_consecutive_bad_trades}"
                )

            crash_policy = apply_crash_policy(regime, self._settings)
            if crash_policy.buys_paused:
                reasons.append(crash_policy.reason or "market crash policy blocks new BUYs")

            open_count = position_repo.count_open()
            if open_count >= self._settings.max_open_positions:
                reasons.append(f"MAX_OPEN_POSITIONS reached ({open_count}/{self._settings.max_open_positions})")

            if requested_usdt > self._settings.max_position_usdt:
                reasons.append(
                    f"requested {requested_usdt} exceeds MAX_POSITION_USDT {self._settings.max_position_usdt}"
                )

            total_open_cost, _ = current_exposure(position_repo, trading_balance_usdt)
            if trading_balance_usdt > 0:
                projected_pct = (total_open_cost + requested_usdt) / trading_balance_usdt * 100
            else:
                projected_pct = Decimal("100")
            if projected_pct > self._settings.max_total_exposure_percent:
                reasons.append(
                    f"would push exposure to {projected_pct:.1f}% "
                    f"(max {self._settings.max_total_exposure_percent}%)"
                )

            today = utcnow().date().isoformat()
            daily_deployed = get_daily_new_capital(settings_repo, today)
            if daily_deployed + requested_usdt > self._settings.max_daily_new_capital_usdt:
                reasons.append(
                    f"would exceed MAX_DAILY_NEW_CAPITAL_USDT for {today} "
                    f"({daily_deployed + requested_usdt} > {self._settings.max_daily_new_capital_usdt})"
                )

        return RiskDecision(allowed=not reasons, reasons=reasons or ["risk checks passed"])

    def can_dca(self, *, regime: RegimeAssessment) -> RiskDecision:
        with session_scope() as session:
            flags = self._read_flags(SettingsRepository(session))

        reasons: list[str] = []
        if flags.emergency_stop:
            reasons.append("emergency stop is active (/emergency_stop)")
        if flags.dca_paused:
            reasons.append("DCA is paused (/stop_dca)")
        crash_policy = apply_crash_policy(regime, self._settings)
        if crash_policy.dca_paused:
            reasons.append(crash_policy.reason or "market crash policy blocks DCA")
        return RiskDecision(allowed=not reasons, reasons=reasons or ["DCA risk checks passed"])
