#!/usr/bin/env python3
"""
backtest/engine.py 单元测试
覆盖：K线流驱动 / 每日重置 / 指标计算 / 模拟成交
"""
import pytest
from src.backtest.engine import BacktestEngine

class TestBacktestFlow:
    @pytest.mark.skip(reason="⏸️ 临时跳过：等待逻辑重构")
    def test_run_generates_trades(self, make_bar):
        """验证引擎能正常跑通并记录交易"""
        engine = BacktestEngine()
        bars = [make_bar(offset=i, close=450.0+i*0.1, volume=100000) for i in range(30)]
        
        # 手动注入一个持仓触发平仓条件
        engine.strategy.open_position("call", 450.0, 1.0, "QQQ260508C452000.US")
        engine.strategy.position["peak_pnl"] = 1.1  # 触发止盈
        
        # 追加一根价格推高K线
        bars.append(make_bar(offset=30, close=460.0, high=461.0, low=459.0, volume=120000))
        
        trades = engine.run(bars)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "TAKE_PROFIT"
        assert engine.strategy.position is None  # 已清空

    def test_daily_reset(self, make_bar):
        """验证跨日自动重置状态"""
        engine = BacktestEngine()
        bars = []
        base = make_bar(offset=0)["ts"]
        # 第1天
        bars.append(make_bar(offset=0, close=450.0, volume=100000))
        # 第2天（时间戳跨日）
        from datetime import timedelta
        next_day = base + timedelta(hours=24, minutes=-390)  # 回到 09:30
        bars.append({"ts": next_day, "open":450, "high":450.5, "low":449.5, "close":450.2, "volume":100000})
        
        engine.run(bars)
        assert engine.strategy.trades_today == 0
        assert engine.strategy.emergency_stopped is False

    def test_metrics_calculation(self):
        """验证绩效指标计算正确性"""
        engine = BacktestEngine()
        # 伪造交易记录
        engine.trades = [
            {"pnl": 0.5, "pnl_pct": 0.5, "exit_reason": "TAKE_PROFIT"},
            {"pnl": -0.3, "pnl_pct": -0.3, "exit_reason": "STOP_LOSS"},
            {"pnl": 0.8, "pnl_pct": 0.8, "exit_reason": "TRAILING_STOP"}
        ]
        metrics = engine.get_metrics()
        assert metrics["total_trades"] == 3
        assert metrics["win_rate"] == pytest.approx(2/3, abs=0.01)
        assert metrics["profit_factor"] == pytest.approx(1.3/0.3, abs=0.01)
        assert "TAKE_PROFIT" in metrics["exit_dist"]