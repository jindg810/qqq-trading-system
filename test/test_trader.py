"""
QQQTrader 核心功能测试 - 严格按照上传的 trader_data.py
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from trader import QQQTrader

class TestInitialization:
    """初始化测试"""
    
    def test_init_success(self, trader):
        """测试正常初始化"""
        assert trader is not None
        assert trader.data_manager is not None
        assert trader.qc is not None
        assert trader.tc is not None
    
    def test_load_state_file_not_exists(self, mock_longbridge, test_config):
        """测试状态文件不存在时的处理"""
        from trader import QQQTrader
        import os
        
        # 确保状态文件不存在
        if os.path.exists(test_config["state_file"]):
            os.remove(test_config["state_file"])
        
        with patch('trader.CONFIG', test_config):
            trader = QQQTrader()
            assert trader.position is None
            assert trader.trades_today == 0
            assert trader.daily_pnl == 0.0
            assert trader.emergency_stopped is False

class TestStrategySignals:
    """策略信号测试"""
    
    def test_bullish_breakout(self, trader):
        """看涨突破信号"""
        # 设置最后一根K线为强势上涨
        last_bar = trader.data_manager.bars[-1]
        last_bar["close"] = last_bar["high"] + 1
        last_bar["open"] = last_bar["low"]
        
        signal = trader.breakout_signal()
        assert signal == "call"
    
    def test_bearish_breakout(self, trader):
        """看跌突破信号"""
        last_bar = trader.data_manager.bars[-1]
        last_bar["close"] = last_bar["low"] - 1
        last_bar["open"] = last_bar["high"]
        
        signal = trader.breakout_signal()
        assert signal == "put"
    
    def test_insufficient_data(self, trader):
        """数据不足时不产生信号"""
        trader.data_manager.bars.clear()
        
        signal = trader.breakout_signal()
        assert signal is None
    
    def test_small_body_rejection(self, trader):
        """K线实体太小被拒绝"""
        last_bar = trader.data_manager.bars[-1]
        last_bar["close"] = last_bar["open"] + 0.001  # 极小实体
        
        signal = trader.breakout_signal()
        assert signal is None
    
    def test_volume_filter(self, trader):
        """成交量过滤"""
        # 设置成交量极低
        trader.data_manager.sma20_vol.clear()
        for _ in range(20):
            trader.data_manager.sma20_vol.append(1000)
        
        last_bar = trader.data_manager.bars[-1]
        last_bar["volume"] = 100  # 远低于平均
        
        signal = trader.breakout_signal()
        assert signal is None

class TestTradingHours:
    """交易时间测试"""
    
    @pytest.mark.parametrize("weekday,hour,minute,expected", [
        (0, 9, 30, True),   # 周一开盘
        (0, 12, 0, True),   # 周一盘中
        (0, 16, 0, False),  # 周一收盘
        (5, 10, 0, False),  # 周六
        (6, 10, 0, False),  # 周日
        (0, 9, 29, False),  # 开盘前
    ])
    def test_trading_hours(self, trader, weekday, hour, minute, expected):
        """测试不同时间的交易状态"""
        with patch('datetime.datetime') as mock_dt:
            mock_now = Mock()
            mock_now.weekday.return_value = weekday
            mock_now.hour = hour
            mock_now.minute = minute
            mock_dt.now.return_value = mock_now
            
            assert trader.is_trading_hours() == expected