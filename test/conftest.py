#!/usr/bin/env python3
"""
pytest 全局配置与共享 Fixtures
✅ 自动处理项目路径 / 隔离文件系统 / 模拟时间 / 共享测试数据
"""

import sys
import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch



@pytest.fixture(scope="session", autouse=True)
def setup_env():
    """会话级：确保测试环境隔离，不污染真实 data/ 目录"""
    os.environ["TRADING_ENV"] = "test"
    yield


@pytest.fixture(scope="function")
def base_time():
    """固定测试基准时间（美东 09:40，避开静默期）"""
    return datetime(2026, 5, 8, 9, 40, 0)


@pytest.fixture(scope="function")
def make_bar(base_time):
    """工厂函数：快速生成标准化K线字典"""

    def _make(offset=0, open=450.0, high=450.5, low=449.5, close=450.2, volume=100000):
        return {
            "ts": base_time + timedelta(minutes=offset),
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }

    return _make


@pytest.fixture(scope="function")
def inject_bars(strategy):
    """🔑 抽象至 conftest：批量注入平稳K线，跨测试文件复用"""

    def _inject(count=20, direction="up", base_price=450.0, volume=100000):
        step = 0.1 if direction == "up" else -0.1
        for i in range(count):
            close = base_price + i * step
            strategy.add_bar(
                {
                    "ts": datetime(2026, 5, 8, 9, 40 + i, 0),
                    "open": close,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": volume,
                }
            )

    return _inject


@pytest.fixture(scope="function")
def strategy():
    """提供干净的 QQQStrategy 实例，测试后自动清理"""
    from src.core.strategy import QQQStrategy

    strat = QQQStrategy()
    yield strat
    # Teardown: 清理状态防污染
    strat.bars.clear()
    strat.sma20_vol.clear()
    strat.position = None
    strat.reset_daily()


@pytest.fixture(scope="function")
def mock_broker():
    """Mock BrokerAdapter 接口，零 SDK 依赖"""
    from unittest.mock import MagicMock
    from src.broker.base import BrokerAdapter
    mb = MagicMock(spec=BrokerAdapter)
    mb.connect.return_value = None
    mb.is_connected.return_value = True
    mb.submit_order.return_value = "MOCK_ORD_001"
    mb.check_order.return_value = None  # 默认未成交
    mb.quote.return_value = None
    mb.quote.return_value = None
    mb.quote_option.return_value = None
    mb.cancel_order.return_value = True
    yield mb

@pytest.fixture(scope="function")
def mock_data_manager():
    """Mock 数据管理器，隔离 state.json / today.csv 真实读写"""
    with patch("src.core.trader.TraderDataManager") as MockDM:
        dm = MockDM.return_value
        dm.load_state.return_value = None
        dm.save_state.return_value = True
        dm.init_state_file.return_value = None
        dm.init_csv.return_value = None
        yield dm

@pytest.fixture(scope="function")
def mock_notifier():
    with patch("src.core.trader.Notifier") as MockNotifier:
        yield MockNotifier.return_value

@pytest.fixture(scope="function")
def trader(mock_broker, mock_data_manager, mock_notifier):
    """
    提供已初始化、已注入 Mock 的 QQQTrader 实例
    ✅ 每个用例获得独立干净的状态
    ✅ 自动绕过风控初始化
    """
    from src.config import CONFIG
    from src.core.trader import QQQTrader
    
    # 1. 测试环境强制开启自动交易（绕过拦截）
    CONFIG["auto_trade_on"] = True

    # 2. 依赖注入实例化（Mock 已生效，不会真连API）
    trader_inst = QQQTrader(broker=mock_broker)
    trader_inst.data_manager = mock_data_manager

    # 3.强制重置策略状态
    trader_inst.strategy.position = None
    trader_inst.strategy.trades_today = 0
    trader_inst.strategy.consecutive_losses = 0
    trader_inst.strategy.daily_pnl = 0.0
    trader_inst.strategy.emergency_stopped = False

    yield trader_inst


@pytest.fixture
def freeze_trade_time():
    """上下文管理器：冻结系统时间为美东 10:00（确保在交易窗口内）"""
    from freezegun import freeze_time
    with freeze_time("2026-05-08 10:00:00"):
        yield datetime(2026, 5, 8, 10, 0, 0)
