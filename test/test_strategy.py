"""
策略逻辑专项测试
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../script'))

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from trader import QQQTrader
from trader_data import TraderDataManager

class TestBreakoutSignal:
    """突破信号测试"""
    
    def setup_method(self):
        """每个测试前的设置"""
        self.data_manager = TraderDataManager()
    
    def create_bar(self, open_price, high, low, close, volume=1000):
        """创建K线数据"""
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume
        }
    
    def test_bullish_breakout_complete(self):
        """完整看涨突破测试"""
        # 创建5根前期K线
        for i in range(5):
            bar = self.create_bar(100, 101, 99, 100.5)
            self.data_manager.add_bar(bar)
        
        # 创建突破K线
        breakout_bar = self.create_bar(100.5, 102, 100, 101.5, 2000)
        self.data_manager.add_bar(breakout_bar)
        
        with patch('trader.Config'), \
             patch('trader.QuoteContext'), \
             patch('trader.TradeContext'), \
             patch.object(QQQTrader, 'load_state'):
            
            trader = QQQTrader()
            trader.data_manager = self.data_manager
            
            signal = trader.breakout_signal()
            assert signal == "call"
    
    def test_bearish_breakout_complete(self):
        """完整看跌突破测试"""
        # 创建5根前期K线
        for i in range(5):
            bar = self.create_bar(100, 101, 99, 100.5)
            self.data_manager.add_bar(bar)
        
        # 创建跌破K线
        breakdown_bar = self.create_bar(100.5, 101, 98, 98.5, 2000)
        self.data_manager.add_bar(breakdown_bar)
        
        with patch('trader.Config'), \
             patch('trader.QuoteContext'), \
             patch('trader.TradeContext'), \
             patch.object(QQQTrader, 'load_state'):
            
            trader = QQQTrader()
            trader.data_manager = self.data_manager
            
            signal = trader.breakout_signal()
            assert signal == "put"
    
    def test_no_signal_insufficient_bars(self):
        """数据不足时不产生信号"""
        # 只有3根K线
        for i in range(3):
            bar = self.create_bar(100, 101, 99, 100.5)
            self.data_manager.add_bar(bar)
        
        with patch('trader.Config'), \
             patch('trader.QuoteContext'), \
             patch('trader.TradeContext'), \
             patch.object(QQQTrader, 'load_state'):
            
            trader = QQQTrader()
            trader.data_manager = self.data_manager
            
            signal = trader.breakout_signal()
            assert signal is None
    
    def test_no_signal_small_body(self):
        """K线实体太小不产生信号"""
        # 创建5根前期K线
        for i in range(5):
            bar = self.create_bar(100, 101, 99, 100.5)
            self.data_manager.add_bar(bar)
        
        # 创建十字星K线（实体很小）
        doji_bar = self.create_bar(100.5, 101, 100, 100.6, 2000)
        self.data_manager.add_bar(doji_bar)
        
        with patch('trader.Config'), \
             patch('trader.QuoteContext'), \
             patch('trader.TradeContext'), \
             patch.object(QQQTrader, 'load_state'):
            
            trader = QQQTrader()
            trader.data_manager = self.data_manager
            
            signal = trader.breakout_signal()
            assert signal is None
    
    def test_no_signal_low_volume(self):
        """成交量不足不产生信号"""
        # 创建5根前期K线
        for i in range(5):
            bar = self.create_bar(100, 101, 99, 100.5, 1000)
            self.data_manager.add_bar(bar)
        
        # 创建低成交量K线
        low_vol_bar = self.create_bar(100.5, 102, 100, 101.5, 500)
        self.data_manager.add_bar(low_vol_bar)
        
        with patch('trader.Config'), \
             patch('trader.QuoteContext'), \
             patch('trader.TradeContext'), \
             patch.object(QQQTrader, 'load_state'):
            
            trader = QQQTrader()
            trader.data_manager = self.data_manager
            
            signal = trader.breakout_signal()
            assert signal is None