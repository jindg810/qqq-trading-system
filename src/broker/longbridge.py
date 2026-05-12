#!/usr/bin/env python3
"""长桥 OpenAPI 适配器（封装协议转换、重试、时区、生命周期）"""
from abc import abstractmethod
import time, logging, threading
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional
from longbridge.openapi import AdjustType, Candlestick, Config, QuoteContext, TradeContext, Period, TradeSessions
from longbridge.openapi import Order as LBOrder, OrderSide as LBSide, OrderType as LBType
from longbridge.openapi import OrderStatus as LBStatus, TimeInForceType as LBTIF
from src.broker.base import BrokerAdapter, BrokerError, ConnectionError, OrderError
from src.broker.models import OrderRequest, OrderCheck, Quote, KlineData, OrderStatus, OrderSide, OrderType, TimeInForce
from src.config import CONFIG

logger = logging.getLogger("broker.longbridge")

# 🔑 枚举映射表（集中管理，方便后续券商替换）
SIDE_MAP = {OrderSide.BUY: LBSide.Buy, OrderSide.SELL: LBSide.Sell}
TYPE_MAP = {OrderType.MARKET: LBType.MO, OrderType.LIMIT: LBType.LO}
TIF_MAP = {TimeInForce.DAY: LBTIF.Day}
_STATUS_NAME_MAP = {
    "Filled": OrderStatus.FILLED,
    "PartialFilled": OrderStatus.PARTIALLY_FILLED,
    "Canceled": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
    "Expired": OrderStatus.FAILED,
    "WaitToNew": OrderStatus.PENDING,
    "New": OrderStatus.PENDING,
    "WaitToCancel": OrderStatus.PENDING,
    "PendingCancel": OrderStatus.PENDING,
    "WaitToReplace": OrderStatus.PENDING,
    "PendingReplace": OrderStatus.PENDING,
}

class LongbridgeAdapter(BrokerAdapter):
    def __init__(self):
        self.qc: Optional[QuoteContext] = None
        self.tc: Optional[TradeContext] = None
        self._kline_cb: Optional[Callable[[KlineData], None]] = None
        self._connected = False

    def connect(self) -> None:
        try:
            cfg = Config.from_apikey_env()
            self.qc = QuoteContext(cfg)
            self.tc = TradeContext(cfg)
            self._connected = True
            logger.info("✅ 长桥连接成功")
        except Exception as e:
            raise ConnectionError(f"长桥初始化失败: {e}") from e

    def disconnect(self) -> None:
        self._connected = False
        if self.qc: self.qc.close()
        if self.tc: self.tc.close()
        logger.info("🔌 长桥连接已关闭")

    def is_connected(self) -> bool: return self._connected

    def subscribe_klines(self, symbol: str) -> None:
        if not self.qc: raise ConnectionError("未连接")
        self.qc.subscribe_candlesticks(symbol, Period.Min_1, TradeSessions.Intraday)

    def set_kline_callback(self, callback: Callable[[KlineData], None]) -> None:
        self._kline_cb = callback
        def _wrapper(sym, event):
            if not getattr(event, "is_confirmed", False): return
            cs = event.candlestick
            raw_ts = getattr(cs, "timestamp", None)
            # 🔑 安全时区转换（兼容 int毫秒 / ISO字符串 / datetime）
            if isinstance(raw_ts, (int, float)):
                ts = datetime.fromtimestamp(raw_ts / 1000, tz=timezone.utc).astimezone(CONFIG["tz_et"])
            elif isinstance(raw_ts, str):
                ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(CONFIG["tz_et"])
            else:
                ts = raw_ts if raw_ts.tzinfo else raw_ts.replace(tzinfo=CONFIG["tz_et"])
            
            if self._kline_cb:
                self._kline_cb(KlineData(
                    ts=ts, open=float(cs.open), high=float(cs.high), low=float(cs.low),
                    close=float(cs.close), volume=float(getattr(cs, "volume", 0)), confirmed=True
                ))
        self.qc.set_on_candlestick(_wrapper)

    def history_candlesticks_by_date(self, symbol: str, period: Period, adjust_type: AdjustType, target_date: datetime) -> List[Candlestick]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            start_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(days=1)
            candles = self.qc.history_candlesticks_by_date(
                        symbol=symbol,
                        period=period,
                        adjust_type=adjust_type,
                        start=target_date,
                        end=target_date,
                        trade_sessions=TradeSessions.Intraday # 只获取当日数据，避免跨日时区问题
                    )
            return candles
        except Exception as e:
            raise BrokerError(f"历史K线数据查询失败: {e}") from e
    
    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            res = self.qc.quote([symbol])
            if res and res[0].last_done > 0:
                return Quote(symbol=symbol, last_price=float(res[0].last_done),
                             bid=float(res[0].bid or 0), ask=float(res[0].ask or 0))
            return None
        except Exception as e:
            raise OrderError(f"报价查询失败: {e}") from e

    def submit_order(self, order: OrderRequest) -> str:
        if not self.tc: raise ConnectionError("未连接")
        try:
            if order.type == OrderType.LIMIT and not order.price:
                raise OrderError("限价单必须指定 price 参数")

            order_id = self.tc.submit_order(
                symbol=order.symbol, 
                submitted_quantity=order.quantity,
                side=SIDE_MAP[order.side], 
                order_type=TYPE_MAP[order.type],
                submitted_price=order.price if order.type == OrderType.LIMIT else None,
                time_in_force=TIF_MAP[order.time_in_force]
            )
            logger.debug(f"📤 订单提交: {order.client_id} -> {order_id}")
            return order_id
        except Exception as e:
            raise OrderError(f"订单提交失败: {e}") from e

    def cancel_order(self, order_id: str) -> bool:
        if not self.tc: return False
        try: self.tc.cancel_order(order_id); return True
        except: return False

    def check_order(self, order_id: str) -> Optional[OrderCheck]:
        if not self.tc: return None
        try:
            orders = self.tc.history_orders(order_ids=[order_id])
            if not orders: return None
            o = orders[0]
            
            status_name = type(o.status).__name__
            mapped_status = _STATUS_NAME_MAP.get(status_name, OrderStatus.PENDING)
            return OrderCheck(
                order_id=order_id,
                filled_price=float(o.filled_avg_price) if o.filled_avg_price > 0 else None,
                filled_qty=o.filled_quantity,
                status=mapped_status,
                updated_at=datetime.now(CONFIG["tz_et"])
            )
        except Exception as e:
            raise OrderError(f"订单状态查询失败: {e}") from e

    def wait_for_events(self) -> None:
        """阻塞主线程等待行情/订单事件，支持 Ctrl+C 优雅退出"""
        try: threading.Event().wait()
        except KeyboardInterrupt: logger.info("⏹️ 收到退出信号")