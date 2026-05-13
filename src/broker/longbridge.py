#!/usr/bin/env python3
"""长桥 OpenAPI 适配器（封装协议转换、重试、时区、生命周期）"""
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional
from longbridge.openapi import AdjustType, Candlestick, Config, QuoteContext, TradeContext, Period, TradeSessions
from longbridge.openapi import OrderSide as LBSide, OrderType as LBType
from longbridge.openapi import TimeInForceType as LBTIF

from src.logger import get_logger
from src.core.strategy import QQQStrategy
from src.broker.base import BrokerAdapter, BrokerError, ConnectionError, OrderError
from src.broker.models import OptionQuote, OrderRequest, OrderCheck, Quote, KlineData, OrderStatus, OrderSide, OrderType, TimeInForce
from src.config import CONFIG

logger = get_logger("broker.longbridge")

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

            lb_env = "🧪 模拟盘" if CONFIG.get("longbridge_use_sandbox") else "🔴 实盘"
            logger.info(f"✅ 长桥连接成功 [{lb_env}]")
        except Exception as e:
            raise ConnectionError(f"长桥初始化失败: {e}") from e

    def disconnect(self) -> None:
        self._connected = False
        #if self.qc: self.qc.close()
        #if self.tc: self.tc.close()
        logger.info("🔌 长桥连接已关闭")

    def is_connected(self) -> bool: return self._connected

    def subscribe_klines(self, symbol: str) -> None:
        if not self.qc: raise ConnectionError("未连接")
        self.qc.subscribe_candlesticks(symbol, Period.Min_1, TradeSessions.Intraday)

    def set_kline_callback(self, callback: Callable[[KlineData], None]) -> None:
        self._kline_cb = callback
        def _wrapper(sym, event):
            if not getattr(event, "candlestick", False): return
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
                    ts = ts, open=float(cs.open), high = float(cs.high), 
                    low = float(cs.low), close = float(cs.close), 
                    volume = float(getattr(cs, "volume", 0)), 
                    is_confirmed = getattr(event, "is_confirmed", False)
                ))
        self.qc.set_on_candlestick(_wrapper)

    def history_candlesticks_by_date(self, symbol: str, period: Period, adjust_type: AdjustType, target_date: datetime) -> List[Candlestick]:
        if not self.qc: raise ConnectionError("未连接")
        try:
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
    
    # 🔑 安全转换：SDK 返回 Decimal / None，统一转为 float
    def _safe_float(self, val): return float(val) if val is not None else 0.0

    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            res = self.qc.quote([symbol])
            if not res: return None
            sq = res[0]  # SecurityQuote 实例

            return Quote(
                symbol=sq.symbol,
                last_price=self._safe_float(sq.last_done),
                open=self._safe_float(sq.open),
                high=self._safe_float(sq.high),
                low=self._safe_float(sq.low),
                prev_close=self._safe_float(sq.prev_close),
                volume=int(sq.volume) if sq.volume else 0,
                timestamp=sq.timestamp if hasattr(sq, 'timestamp') else None
            )
        except Exception as e:
            raise OrderError(f"报价查询失败: {e}") from e

    def quote_option(self, symbol: str) -> Optional[OptionQuote]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            res = self.qc.option_quote([symbol])
            if not res: return None

            oq = res[0]  # SDK OptionQuote 实例
            return OptionQuote(
                symbol=oq.symbol,
                last_price=self._safe_float(oq.last_done),
                prev_close=self._safe_float(oq.prev_close), 
                open=self._safe_float(oq.open),
                high=self._safe_float(oq.high), 
                low=self._s_safe_floatf(oq.low),
                volume=int(oq.volume) if oq.volume else 0,
                turnover=self._safe_float(oq.turnover),
                trade_status=str(oq.trade_status) if oq.trade_status else None,
                implied_volatility=self._safe_float(oq.implied_volatility),
                open_interest=int(oq.open_interest) if oq.open_interest else 0,
                expiry_date=oq.expiry_date,
                strike_price=self._safe_float(oq.strike_price),
                contract_multiplier=self._safe_float(oq.contract_multiplier),
                contract_type=str(oq.contract_type) if oq.contract_type else None,
                contract_size=self._safe_float(oq.contract_size),
                direction=str(oq.direction) if oq.direction else None,
                historical_volatility=self._safe_float(oq.historical_volatility),
                underlying_symbol=oq.underlying_symbol or "",
                timestamp=getattr(oq, 'timestamp', None)
            )
        except Exception as e:
            raise OrderError(f"期权报价查询失败: {e}") from e
        
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


# ================= 独立 CLI 诊断测试 =================
'''
# 1. 基础诊断（连接/报价/K线/推送）
python -m src.broker.longbridge --symbol QQQ.US
# 2. 完整诊断（含订单流程，需手动确认 YES）
python -m src.broker.longbridge --symbol QQQ.US --test-orders
'''
def run_diagnostic(broker: LongbridgeAdapter, symbol: str = "QQQ.US", test_orders: bool = False):
    import time, sys, traceback
    from datetime import date
    from longbridge.openapi import Period, AdjustType

    print("="*55)
    print("🔍 长桥 Broker 适配器 CLI 诊断工具")
    print("="*55)

    try:
        # 1. 连接与鉴权
        print("\n🔗 [1/6] 测试连接与鉴权 (connect)...")
        broker.connect()
        assert broker.is_connected()
        print("   ✅ PASS: 连接成功 & API Key 鉴权通过")

        # 2. 实时报价
        print("\n📊 [2/6] 测试实时报价 (get_quote)...")
        quote = broker.quote(symbol)
        assert quote and quote.last_price > 0
        print(f"   ✅ PASS: {symbol} | Last: {quote.last_price} | volume: {quote.volume} | ts: {quote.timestamp}")

        # 3. 实时期权报价
        try:
            print("\n📊 [3/6] 测试期权报价 (get_quote)...")
            op_symbol = QQQStrategy.generate_option_symbol(1.00, OrderSide.BUY)
            op_quote = broker.quote_option(op_symbol)
            assert op_quote and op_quote.last_price > 0
            print(f"   ✅ PASS: {op_symbol} | Last: {op_quote.last_price} | volume: {op_quote.volume} | ts: {op_quote.timestamp}")
        except Exception as ex:
            print(f"   ⚠️ WARN: May be no quote access. ex: {ex}")

        # 3. 历史K线
        print("\n📈 [4/6] 测试历史K线 (history_candlesticks_by_date)...")
        # 默认取今日，若非交易日会返回空列表或抛异常，属正常现象
        target_date = datetime(2026, 5, 12, tzinfo=CONFIG["tz_et"])
        candles = broker.history_candlesticks_by_date(symbol, Period.Min_1, AdjustType.ForwardAdjust, target_date)

        if len(candles) > 0:
            print(f"   ✅ PASS: 成功获取 {len(candles)} 条 1分钟K线 | 首根时间: {candles[0].timestamp}")
        else:
            print("   ⚠️ WARN: 返回0条 (可能当前非交易日或该日无数据)")

        # 4. 实时K线推送
        print("\n🔔 [5/6] 测试实时K线回调 (subscribe_klines + callback)...")
        recv_count = 0
        def _temp_cb(k: KlineData):
            nonlocal recv_count
            recv_count += 1
            if recv_count == 1:
                print(f"   📩 收到: {k.ts.strftime('%H:%M:%S')} | O:{k.open} C:{k.close}")

        broker.set_kline_callback(_temp_cb)
        broker.subscribe_klines(symbol)
        print("   ⏳ 监听中 (等待 15 秒接收推送)...")
        time.sleep(15)  # 替代 wait_for_events，避免永久阻塞
        if recv_count > 0:
            print(f"   ✅ PASS: 共收到 {recv_count} 条实时K线推送")
        else:
            print("   ⚠️ WARN: 未收到推送 (可能当前非交易时段或网络订阅未生效)")

        # 5. 订单流程 (高风险，默认关闭)
        if test_orders:
            print("\n📝 [6/6] 测试订单流程 (submit -> check -> cancel)...")
            print("   ⚠️ 严重警告：此测试将向交易所发送真实请求！")
            print("   💡 请确保 .env 中配置的是长桥模拟盘(Sandbox)环境！")
            confirm = input("   确认继续? (输入 YES 并回车): ")
            if confirm.strip().upper() == "YES":
                try:
                    q = broker.quote(symbol)
                    # 设置远低于市价的限价单，100% 防误成交
                    safe_price = round(q.last_price * 0.2, 2)
                    req = OrderRequest(
                        symbol=symbol, side=OrderSide.BUY, type=OrderType.LIMIT,
                        quantity=1, price=safe_price
                    )
                    oid = broker.submit_order(req)
                    print(f"   📤 提交成功 | OrderID: {oid} | 限价: {safe_price}")
                    
                    time.sleep(3)
                    check = broker.check_order(oid)
                    print(f"   🔍 状态查询: {check.status.name} | 成交数: {check.filled_qty}")
                    
                    broker.cancel_order(oid)
                    print("   ✅ 撤单指令已发送 (流程测试完成)")
                except Exception as e:
                    print(f"   ❌ FAIL: {e}")
                    traceback.print_exc()
            else:
                print("   ⏭️ 已取消订单测试")
        else:
            print("\n📝 [5/5] 跳过订单测试 (使用 --test-orders 开启)")

    except Exception as e:
        print(f"\n💥 诊断中断: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        print("\n🔌 正在断开连接...")
        broker.disconnect()
        print("🏁 诊断结束")

if __name__ == "__main__":
    import argparse

    # CLI 参数解析
    parser = argparse.ArgumentParser(description="长桥 Broker 接口 CLI 诊断工具")
    parser.add_argument("--symbol", default="QQQ.US", help="测试标的代码 (默认: QQQ.US)")
    parser.add_argument("--test-orders", action="store_true", help="开启订单提交/撤单流程测试 (高风险)")
    args = parser.parse_args()
    
    run_diagnostic(broker = LongbridgeAdapter(), symbol=args.symbol, test_orders=args.test_orders)
