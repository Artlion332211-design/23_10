"""SignalEngine: turns multi-timeframe indicator snapshots into the discrete
per-signal confirmations that `strategy.scoring` aggregates into a
BUY_SCORE.

Purely technical - market regime policy, news, and universe/liquidity
filters are layered on afterwards by `StrategyEngine`. Two hard vetoes live
here rather than there because they are direct readings of the same
technical snapshots this engine already computes: the ADX bearish-trend
veto (item 8 in the spec: don't mean-revert into a strong downtrend even on
an oversold RSI) and the per-symbol 4h trend-context veto (item 11: a
bullish 15m/5m blip must never override a bearish 4h trend on this symbol -
distinct from, and in addition to, the separate BTC-wide market filter).
"""

from __future__ import annotations

from dataclasses import dataclass

from config.settings import RulesConfig, Settings
from market.market_data import IndicatorSnapshot
from market.market_regime import TrendRegime, classify_symbol_trend
from strategy.scoring import ScoreBreakdown, score_signals


@dataclass(frozen=True)
class MultiTimeframeSnapshot:
    m15: IndicatorSnapshot
    h1: IndicatorSnapshot
    h4: IndicatorSnapshot


class SignalEngine:
    def __init__(self, settings: Settings, rules: RulesConfig) -> None:
        self._settings = settings
        self._rules = rules

    def _adx_veto(self, mtf: MultiTimeframeSnapshot) -> str | None:
        cfg = self._rules.adx_veto
        snap = mtf.h1
        if snap.adx >= cfg.adx_threshold and (snap.minus_di - snap.plus_di) >= cfg.di_diff_threshold:
            return (
                f"ADX veto: 1h ADX {snap.adx:.0f} with -DI dominant "
                f"({snap.minus_di:.0f} vs {snap.plus_di:.0f}) - strong downtrend, not a dip"
            )
        return None

    def _trend_context_veto(self, mtf: MultiTimeframeSnapshot) -> str | None:
        trend_4h = classify_symbol_trend(mtf.h4)
        if trend_4h == TrendRegime.CRASH:
            return "4h trend on this symbol is crash-like - a bullish 15m/1h reading cannot override it"
        return None

    def evaluate(
        self,
        symbol: str,
        mtf: MultiTimeframeSnapshot,
        *,
        news_adjustment: float = 0.0,
        regime_adjustment: float = 0.0,
        extra_vetoes: list[str] | None = None,
    ) -> ScoreBreakdown:
        h1, m15 = mtf.h1, mtf.m15

        rsi_reversal = h1.rsi_reversal or (m15.rsi_reversal and h1.rsi < 55)
        vwap_recovery = h1.vwap_recovery or m15.vwap_recovery
        market_structure = h1.market_structure_bullish or h1.rsi_bullish_divergence

        raw_signals = {
            "rsi_reversal": rsi_reversal,
            "macd_bullish": h1.macd_bullish,
            "ema_trend": h1.ema_trend_ok,
            "bollinger_recovery": h1.bb_recovery,
            "volume_confirmation": h1.volume_confirmation,
            "vwap_recovery": vwap_recovery,
            "market_structure": market_structure,
        }
        details = {
            "rsi_reversal": f"1h RSI {h1.rsi:.0f} (slope {h1.rsi_slope:+.1f})",
            "macd_bullish": f"1h MACD hist {h1.macd_hist:+.4f} (prev {h1.macd_hist_prev:+.4f})",
            "ema_trend": f"close {h1.close:.4f} vs EMA20 {h1.ema_fast:.4f} / EMA50 {h1.ema_mid:.4f}",
            "bollinger_recovery": f"1h close {h1.close:.4f} vs lower band {h1.bb_lower:.4f}",
            "volume_confirmation": f"1h volume {h1.volume:.0f} vs MA {h1.volume_ma:.0f}",
            "vwap_recovery": f"1h VWAP {h1.vwap:.4f}",
            "market_structure": "higher-low / candle pattern / divergence / near support",
        }

        vetoes = [v for v in (self._adx_veto(mtf), self._trend_context_veto(mtf)) if v]
        vetoes.extend(extra_vetoes or [])

        return score_signals(
            symbol,
            raw_signals,
            self._rules.signal_weights,
            min_confirmed_signals=self._settings.min_confirmed_signals,
            min_confirmation_categories=self._settings.min_confirmation_categories,
            news_adjustment=news_adjustment,
            regime_adjustment=regime_adjustment,
            vetoes=vetoes,
            details=details,
        )
