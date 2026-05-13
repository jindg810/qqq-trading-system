#!/usr/bin/env python3
"""
core/trader.py 单元测试
覆盖：订单提交/成交/取消 / 状态持久化 / 行情回调路由
"""
from unittest.mock import patch, MagicMock
from src.broker.models import OrderCheck, OrderStatus, Quote
import pytest

from test.conftest import base_time

class TestOrderExecution:
    @patch('core.trader.time.sleep')
    def test_wait_for_fill_success(self, mock_sleep, trader):
        """测试订单快速成交（无需等待 sleep）"""
        trader.broker.check_order.return_value = OrderCheck(
            order_id="ORD_001", 
            filled_price=1.85, 
            filled_qty=1, 
            status=OrderStatus.FILLED, 
            updated_at=base_time
        )

        assert trader._wait_for_order_fill("ORD_001", timeout=2) == 1.85
        trader.broker.check_order.assert_called()

    @patch('core.trader.time.sleep')
    def test_wait_for_fill_timeout_cancel(self, mock_sleep, trader):
        trader.broker.check_order.return_value = []
        trader.broker.cancel_order = MagicMock()
        
        price = trader._wait_for_order_fill("ORD_002", timeout=1)
        assert price is None
        trader.broker.cancel_order.assert_called_once_with("ORD_002")
    
    #@pytest.mark.skip(reason="⏸️ 临时跳过：等待Order冲突解决")
    @patch('core.trader.time.sleep')
    def test_execute_open_success(self, mock_sleep, trader, freeze_trade_time):
        trader.broker.submit_order.return_value = "ORD_123"
        trader.broker.quote_option.return_value = Quote(symbol="TEST", last_price=1.55)
        trader.broker.check_order.return_value = OrderCheck(
            order_id="ORD_123", filled_price=1.50, 
            filled_qty=1, status=OrderStatus.FILLED, 
            updated_at=freeze_trade_time
        )

        # ✅ 执行开仓（调用你实际的内部方法）
        trader.strategy.bars.append({"ts": freeze_trade_time})
        result = trader._execute_open("call", 450.0)

        # ✅ 核心断言
        assert result is True, f"开仓返回 False，请检查 mock 是否生效"
        assert trader.strategy.position is not None
        assert trader.strategy.position["side"] == "call"
        assert trader.strategy.position["entry_opt"] == 1.55  # 验证 sleep 后抓取了最新报价
        assert trader.strategy.trades_today == 1

        # ✅ 验证调用链
        trader.broker.submit_order.assert_called_once()
        trader.broker.check_order.assert_called_once_with(order_ids=["ORD_123"])
        trader.broker.quote_option.assert_called_once()
        trader.data_manager.save_state.assert_called()

    @pytest.mark.skip(reason="⏸️ 临时跳过：等待Order冲突解决")
    def test_execute_close_success(self, trader, freeze_trade_time):
        trader.strategy.open_position("call", 450.0, 1.0, "TEST")
        trader.broker.submit_order.return_value = "ORD_CLOSE"
        trader.broker.check_order.return_value = [MagicMock(filled_quantity=1, filled_avg_price=1.80, status=OrderStatus.Filled)]

        trader._execute_close("TAKE_PROFIT")
        
        assert trader.strategy.position is None
        trader.broker.submit_order.assert_called_once()
        trader.data_manager.save_state.assert_called()

class TestTraderCallbacks:
    def test_on_kline_skips_unconfirmed(self, trader):
        mock_event = MagicMock()
        mock_event.is_confirmed = False
        mock_event.candlestick = MagicMock()
        
        trader._on_kline(mock_event)
        trader.data_manager.write_kline_to_csv.assert_not_called()

    def test_on_kline_processes_confirmed(self, trader, freeze_trade_time):
        # 模拟长桥推送的K线对象
        mock_event = MagicMock(
            open=450.0, high=450.5, low=449.5, close=450.2, volume=100000,
            ts="2026-05-08T14:00:00Z"
        )
        
        trader._on_kline(mock_event)
        
        # 验证数据成功注入策略缓冲 & CSV写入被调用
        assert len(trader.strategy.bars) == 1
        assert trader.strategy.bars[-1]["close"] == 450.2
        trader.data_manager.write_kline_to_csv.assert_called_once()
        trader.data_manager.save_state.assert_called_once()

    def test_on_kline_respects_risk_fuse(self, trader, freeze_trade_time):
        trader.strategy.emergency_stopped = True  # 触发熔断
        mock_event = MagicMock(open=450.0, high=450.5, low=449.5, close=450.2, volume=100000, timestamp="2026-05-08T14:00:00Z")
        
        trader._on_kline(mock_event)
        # 熔断后不应调用开仓或状态保存（仅记录K线）
        trader.broker.submit_order.assert_not_called()
