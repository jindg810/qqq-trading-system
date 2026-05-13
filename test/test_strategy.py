#!/usr/bin/env python3
"""src/core/strategy.py 单元测试（工业级精简版）"""
import pytest
from datetime import datetime
from src.core.strategy import ExitReason, QQQStrategy
from src.config import CONFIG

class TestStateLifecycle:
    def test_add_bar_valid(self, strategy, make_bar):
        assert strategy.add_bar(make_bar(0)) is True
        assert strategy.add_bar(make_bar(1)) is True
        assert len(strategy.bars) == 2

    def test_add_bar_rejects_duplicate(self, strategy, make_bar):
        b = make_bar(0)
        assert strategy.add_bar(b) is True
        assert strategy.add_bar(b) is False

    def test_reset_daily(self, strategy):
        strategy.trades_today, strategy.daily_pnl, strategy.consecutive_losses, strategy.emergency_stopped = 5, -200.0, 2, True
        strategy.reset_daily()
        assert strategy.trades_today == 0 and strategy.daily_pnl == 0.0
        assert strategy.consecutive_losses == 0 and not strategy.emergency_stopped

class TestIndicators:
    def test_sma20_insufficient(self, strategy, make_bar):
        for i in range(19): strategy.add_bar(make_bar(i, close=450.0))
        assert strategy._sma20_price() is None

    def test_sma20_correct(self, strategy, make_bar):
        prices = [450.0 + i for i in range(20)]
        for i, p in enumerate(prices): strategy.add_bar(make_bar(i, close=p))
        assert abs(strategy._sma20_price() - sum(prices)/20) < 1e-6

    def test_volume_ok_dynamic(self, strategy, make_bar):
        # 1. 注入10根基准K线（均量 100,000）
        for i in range(10): 
            strategy.add_bar(make_bar(i, volume=100000))

        # 2. 第11根：量不足 (动态门槛 150k) → 预期 False
        strategy.add_bar(make_bar(10, volume=140000)) # 阈值 150k，失败
        assert not strategy._volume_ok()

        # 3. 第12根：量达标 (160k >= 150k) → 预期 True
        strategy.add_bar(make_bar(11, volume=160000)) # 成功
        assert strategy._volume_ok()
    
    def test_volume_ok_standard(self, strategy, make_bar):
        """测试满20根后使用标准阈值逻辑 (cnt >= 20)"""
        # 1. 注入恰好20根K线，建立完整基准（均量 100,000）
        for i in range(20):
            strategy.add_bar(make_bar(i, volume=100000))
        
        # ✅ 验证已进入标准分支
        assert len(strategy.sma20_vol) == 20
        
        # 2. 计算标准阈值（避免硬编码 CONFIG 值）
        avg_vol = sum(strategy.sma20_vol) / 20  # 100,000
        threshold = avg_vol * CONFIG["vol_mult"]  # 通常为 80,000
        
        # 3. 第21根：量不足 → 预期 False
        strategy.add_bar(make_bar(20, volume=int(threshold) - 1000))
        assert not strategy._volume_ok()
        
        # 4. 第22根：量达标 → 预期 True
        strategy.add_bar(make_bar(21, volume=int(threshold)))
        assert strategy._volume_ok()

class TestSignalGeneration:
    def test_breakout_call(self, strategy, inject_bars):
        inject_bars(20, "up")
        prev_high = max(b["high"] for b in list(strategy.bars)[-6:-1])
        strategy.add_bar({"ts": datetime(2026,5,8,10,0,0), "open": prev_high, "high": prev_high+0.7, "low": prev_high, "close": prev_high+0.6, "volume": 180000})
        assert strategy._breakout_signal() == "call"

    def test_breakout_put(self, strategy, inject_bars):
        inject_bars(20, "down")
        prev_low = min(b["low"] for b in list(strategy.bars)[-6:-1])
        strategy.add_bar({"ts": datetime(2026,5,8,10,0,0), "open": prev_low, "high": prev_low, "low": prev_low-0.7, "close": prev_low-0.6, "volume": 180000})
        assert strategy._breakout_signal() == "put"

    def test_breakout_filtered_by_body(self, strategy, inject_bars):
        inject_bars(20)
        prev_high = max(b["high"] for b in list(strategy.bars)[-6:-1])
        strategy.add_bar({"ts": datetime(2026,5,8,10,0,0), "open": 452.0, "close": 452.01, "high": prev_high+0.5, "low": 451.0, "volume": 200000})
        assert strategy._breakout_signal() is None

    def test_reversal_put(self, strategy, make_bar):
        for i in range(20): strategy.add_bar(make_bar(i, high=453.0+i*0.05, close=450.0, volume=90000))
        strategy.add_bar(make_bar(20, open=450.5, close=449.2, high=453.5, low=449.0, volume=100000))
        assert strategy._reversal_signal() == "put"

class TestRiskAndTime:
    def test_risk_daily_loss(self, strategy):
        strategy.daily_pnl = CONFIG.get("max_daily_loss", -500) - 100
        assert not strategy.check_risk()
        assert strategy.emergency_stopped

    def test_risk_consecutive_loss(self, strategy):
        strategy.consecutive_losses = CONFIG.get("max_consecutive_losses", 3)
        assert not strategy.check_risk()

    @pytest.mark.parametrize("ts, expected", [
        ("2026-05-08T14:00:00Z", True),  # 周五 10:00 ET
        ("2026-05-09T14:00:00Z", False), # 周六
        ("2026-05-08T21:30:00Z", False), # 周五 17:30 ET
    ])
    def test_is_trading_hours(self, strategy, ts, expected):
        assert strategy.is_trading_hours(ts) == expected

class TestPositionManagement:
    def setup_method(self):
        self.strat = QQQStrategy()
        self.strat.open_position("call", 450.0, 1.0, "QQQ260508C452000.US")

    def test_stop_loss(self): 
        assert self.strat.check_position_exit(0.74) == ExitReason.STOP_LOSS
    
    def test_take_profit(self):
        # 盈亏比峰值 1.1，盈利 1倍 时止盈
        self.strat.position["peak_pnl"] = 1.1
        assert self.strat.check_position_exit(2.05) == ExitReason.TAKE_PROFIT
    
    def test_trailing_stop(self):
        # ✅ 设置 peak_pnl = 0.95 (+95%)
        # 要求：peak < 1.0 (避开止盈) 且 (peak - 当前pnl) >= 0.3 (触发回撤)
        # 当前 pnl = (1.6-1.0)/1.0 = 0.6 → 0.95 - 0.6 = 0.35 >= 0.30 
        # 验证回撤触发条件
        self.strat.position["peak_pnl"] = 0.95
        assert self.strat.check_position_exit(1.6) == ExitReason.TRAILING_STOP

    def test_timeout(self):
        # 持仓周期达到预设 bars 数，触发时间止损 TODO: 15 ？
        self.strat.position["bars_held"] = 14
        assert self.strat.check_position_exit(1.05) == ExitReason.TIMEOUT
        assert self.strat.position["bars_held"] == 15
    
    def test_close_profit_resets(self):
        trade = self.strat.close_position(1.5)
        assert trade["pnl"] == 0.5 and self.strat.consecutive_losses == 0
    
    def test_close_loss_increments(self):
        trade = self.strat.close_position(0.8)
        assert trade["pnl"] == -0.2 and self.strat.consecutive_losses == 1
    
    def test_generate_symbol(self):
        sym = QQQStrategy.generate_option_symbol(450.12, "call")
        assert sym.startswith("QQQ") and sym.endswith(".US") and "C" in sym
        assert QQQStrategy.generate_option_symbol(0, "call") is None