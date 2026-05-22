#!/usr/bin/env python3
"""Futu OpenAPI 适配器（封装协议转换、重试、时区、生命周期）"""
import threading
from datetime import datetime, timezone
from typing import Callable, List, Optional
from futu import *
from futu.common.constant import KLType, SortField, OptionType, OrderType as FTOrderType, TimeInForce as FUTimeInForce

from src.logger import get_logger
from src.core.strategy import QQQStrategy
from src.broker.base import BrokerAdapter, BrokerError, ConnectionError, OrderError
from src.broker.models import OptionQuote, OrderRequest, OrderCheck, Quote, KlineData, OrderStatus, OrderSide, OrderType, TimeInForce
from src.config import CONFIG

logger = get_logger("broker.futu")

# 🔑 枚举映射表（集中管理，方便后续券商替换）
SIDE_MAP = {OrderSide.BUY: TrdSide.BUY, OrderSide.SELL: TrdSide.SELL}
TYPE_MAP = {OrderType.MARKET: FTOrderType.MARKET, OrderType.NORMAL: FTOrderType.NORMAL, OrderType.LIMIT: FTOrderType.LIMIT_IF_TOUCHED}
TIF_MAP = {TimeInForce.DAY: FUTimeInForce.DAY}
_STATUS_NAME_MAP = {
    "FILLED_ALL": OrderStatus.FILLED,
    "FILLED_PART": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED_ALL": OrderStatus.CANCELLED,
    "FAILED": OrderStatus.REJECTED,
    "DISABLED": OrderStatus.FAILED,
    "WAITING_SUBMIT": OrderStatus.PENDING,
    "UNSUBMITTED": OrderStatus.PENDING,
    "SUBMITTING": OrderStatus.PENDING,
    "PendingCancel": OrderStatus.PENDING,
    "WaitToReplace": OrderStatus.PENDING,
    "PendingReplace": OrderStatus.PENDING,
}
    
class FutuAdapter(BrokerAdapter):
    def __init__(self):
        self.futu_host = "127.0.0.1"
        self.futu_port = 11111
        self.qc: Optional[OpenQuoteContext] = None
        self.tc: Optional[OpenSecTradeContext] = None
        self._kline_cb: Optional[Callable[[KlineData], None]] = None
        self._connected = False

    def connect(self) -> None:
        try:
            self.qc = OpenQuoteContext(self.futu_host, self.futu_port)
            self.tc = OpenSecTradeContext(TrdMarket.HK, self.futu_host, self.futu_port)
            self.tc_us = OpenSecTradeContext(TrdMarket.US, self.futu_host, self.futu_port)
            self._connected = True
            logger.info(f"✅ 富途连接成功")
        except Exception as e:
            raise ConnectionError(f"富途初始化失败: {e}") from e

    def _tc_(self, symbol: str) -> OpenSecTradeContext:
        # US. 为前缀的股票使用 tc_us 连接
        return self.tc_us if symbol is not None and symbol.startswith("US.") or symbol.endswith(".US") else self.tc


    def disconnect(self) -> None:
        self._connected = False
        if self.qc: self.qc.unsubscribe_all()
        if self.qc: self.qc.close()
        if self.tc: self.tc.close()
        if self.tc_us: self.tc_us.close()
        logger.info("🔌 富途连接已关闭")

    def is_connected(self) -> bool: return self._connected

    def get_trade_env(self) -> str:
        # TrdEnv：REAL，SIMULATE
        sandbox_on = CONFIG.get("broker_sandbox_on", True)
        return TrdEnv.SIMULATE if sandbox_on else TrdEnv.REAL
    
    def _subscribe(self, symbol: str) -> bool:
        # Quote 之前必须先订阅
        if not self.qc: raise ConnectionError("未连接")
        symbol = self._formater_(symbol)
        ret_sub, err_message = self.qc.subscribe(symbol, [SubType.QUOTE], session=Session.ALL)
        if ret_sub == RET_OK:
            logger.info(f"✅ K线订阅成功。symbol={symbol}")
            return True
        else:
            logger.error(f"⚠️ K线订阅失败。symbol={symbol}, ret={ret_sub}, message={err_message}")
            return False

    def subscribe_klines(self, symbol: str) -> None:
        if not self.qc: raise ConnectionError("未连接")
        symbol = self._formater_(symbol)
        ret_sub, err_message = self.qc.subscribe(symbol, [SubType.K_1M], session=Session.RTH)
        if ret_sub == RET_OK:
            logger.info(f"✅ K线订阅成功。symbol={symbol}")
        else:
            logger.error(f"⚠️ K线订阅失败。symbol={symbol}, ret={ret_sub}, message={err_message}")

    def set_kline_callback(self, callback: Callable[[KlineData], None]) -> None:
        self.qc.set_handler(KlineHandler(callback))
    
    def history_kline_by_date(self, symbol: str, kline_type: str, target_date: datetime) -> List[KlineData]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            symbol = self._formater_(symbol)
            req_date = target_date.strftime("%Y-%m-%d")
            req_fields = [KL_FIELD.DATE_TIME, KL_FIELD.OPEN, KL_FIELD.HIGH, 
                            KL_FIELD.LOW, KL_FIELD.CLOSE, KL_FIELD.TRADE_VOL]
            ret, data, page_req_key = self.qc.request_history_kline(
                symbol, start=req_date, 
                end=req_date, ktype=kline_type,
                fields=req_fields, session=Session.RTH)
            if ret != RET_OK:
                logger.error(f"💥 获取K线历史数据失败。symbol={symbol}, ret={ret}, msg={data}")
                return None
            klines = self._to_kline_data(data)

            while page_req_key != None:  # 請求後面的所有結果
                ret, data, page_req_key = self.qc.request_history_kline(
                    symbol, start=req_date, 
                end=req_date, ktype=kline_type,
                page_req_key=page_req_key, fields=req_fields, 
                session=Session.RTH)
                if ret == RET_OK: klines.append(self._to_kline_data(data))
                else: logger.error(f"💥 获取K线历史分页数据失败。symbol={symbol}, page_req_key={page_req_key}, ret={ret}, data={data}")
            return klines
        except Exception as e:
            raise BrokerError(f"💥 历史K线数据查询失败: {e}") from e

    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            symbol = self._formater_(symbol)
            # 獲取已訂閱股票的即時報價，必須要先訂閱
            if not self._subscribe(symbol): return None

            ret, data = self.qc.get_stock_quote([symbol])
            if ret != RET_OK: 
                logger.error(f"💥 获取即时报价错误。 symbol={symbol},ret={ret}, data={data}")
                return None
            
            # df dataframe 实例
            q = data.iloc[0]
            quote = Quote(
                symbol=q.code,
                last_price=self._safe_float(q.last_price),
                open=self._safe_float(q.open_price),
                high=self._safe_float(q.high_price),
                low=self._safe_float(q.low_price),
                prev_close=self._safe_float(q.prev_close_price),
                volume=int(q.volume) if q.volume else 0
            )
            if hasattr(q, "data_date") and hasattr(q, "data_time") :
                dt_str = q.data_date +" " + q.data_time
                quote.timestamp = datetime.strptime(dt_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            return quote
        except Exception as e:
            traceback.print_exc()
            raise OrderError(f"💥 报价查询失败: {e}") from e

    def quote_option(self, symbol: str) -> Optional[OptionQuote]:
        if not self.qc: raise ConnectionError("未连接")
        try:
            symbol = self._formater_(symbol)
            
            # 查询前先订阅
            if not self._subscribe(symbol): return None

            ret, data = self.qc.get_stock_quote([symbol])
            if ret != RET_OK: 
                logger.error(f"💥 获取即时期权报价错误。 symbol={symbol}, ret={ret}, data={data}")
                return None

            return self._to_optioin_quote(data.iloc[0])  # SDK OptionQuote 实例
        except Exception as e:
            traceback.print_exc()
            raise OrderError(f"💥 期权报价查询失败: {e}") from e
     
    def submit_order(self, order: OrderRequest) -> str:
        #if not self.tc: raise ConnectionError("未连接")
        try:
            order.symbol = self._formater_(order.symbol)
            if order.type == OrderType.NORMAL and not order.price:
                raise OrderError("限价单必须指定 price 参数")
            
            if not self._unlock_trade: return

            trade_env = self.get_trade_env() 
            ret, order_resp = self._tc_(order.symbol).place_order(
                code=order.symbol, 
                qty=order.quantity,
                trd_side=SIDE_MAP[order.side], 
                order_type=TYPE_MAP[order.type],
                trd_env=trade_env,
                price=order.price if order.type == OrderType.NORMAL else 0,
                time_in_force=TIF_MAP[order.time_in_force]
            )
            if(ret != RET_OK or order_resp.empty):
                logger.error(f"💥 订单提交失败: {order.symbol}-{order.client_id} -> {order_resp}")
                return None
            else:
                _order = order_resp.iloc[0]
                logger.info(f"📤 订单提交: {order.symbol}-{order.client_id} -> {_order.order_id}")
                return _order.order_id
        except Exception as e:
            raise OrderError(f"💥 订单提交失败: {e}") from e

    def cancel_order(self, order_id: str, symbol: str = None) -> bool:
        #if not self.tc: return False
        try: 
            if not self._unlock_trade: return # 解锁交易
            
            trade_env = self.get_trade_env()
            ret_code, data = self._tc_(symbol).modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=trade_env)
            if(ret_code != RET_OK or data.empty):
                logger.error(f"💥 订单取消失败。order_id={order_id}，{ret_code}-{data}")
                return False
            return True
        except: return False

    def check_order(self, order_id: str, symbol: str = None) -> Optional[OrderCheck]:
        if not self.tc: return None
        try:
            trade_env = self.get_trade_env()
            ret_code, orders = self._tc_(symbol).order_list_query(order_id=order_id, trd_env=trade_env)
            if(ret_code != RET_OK or orders.empty):
                logger.error(f"💥 订单查询失败: order_id={order_id} -> {ret_code}:{orders}")
                return None
            
            o = orders.iloc[0]
            status_name = type(o.order_status).__name__
            mapped_status = _STATUS_NAME_MAP.get(status_name, OrderStatus.PENDING)
            return OrderCheck(
                order_id = order_id,
                filled_price = float(o.dealt_avg_price) if o.dealt_avg_price is not None else None,
                filled_qty = o.dealt_qty if o.dealt_qty is not None else 0,
                status = mapped_status,
                updated_at = o.updated_time or datetime.now(CONFIG["tz_et"]) # 兜底防 None
            )
        except Exception as e:
            raise OrderError(f"💥 订单状态查询失败: {order_id} -> {e}") from e

    def wait_for_events(self) -> None:
        """阻塞主线程等待行情/订单事件，支持 Ctrl+C 优雅退出"""
        try: threading.Event().wait()
        except KeyboardInterrupt: logger.info("⏹️ 收到退出信号")
    
    #=============================================================
        
    def _to_kline_data(self, data: pd.DataFrame) -> list[KlineData]:
        return [
            KlineData(
                ts=row.time_key, open=row.open,
                high=row.high, low=row.low,
                close=row.close, volume=row.volume,
                #is_confirmed=row.is_confirmed,
            ) for row in data.itertuples()
        ]
    
    def _to_optioin_quote(self, quote: pd.DataFrame):
        # 类型转换：pd.DataFrame -> OptionQuote
        option_quote = OptionQuote(
                symbol = quote.code,
                last_price = self._safe_float(quote.last_price),
                prev_close = self._safe_float(quote.prev_close_price), 
                open = self._safe_float(quote.open_price),
                high = self._safe_float(quote.high_price), 
                low = self._safe_float(quote.low_price),
                volume = int(quote.volume) if quote.volume else 0,
                turnover = self._safe_float(quote.turnover),
                trade_status = str(quote.sec_status) if quote.sec_status else None,
                implied_volatility = self._safe_float(quote.implied_volatility),
                open_interest = int(quote.open_interest) if quote.open_interest else 0,
                strike_price = self._safe_float(quote.strike_price),
                contract_multiplier = self._safe_float(quote.contract_multiplier),
                contract_size = self._safe_float(quote.contract_size)
            )
        # 特殊字段转换
        if quote.expiry_date_distance:
            expiry_date = datetime.now().date() + timedelta(days=quote.expiry_date_distance)
            option_quote.expiry_date = expiry_date
        if hasattr(quote, "data_date") and hasattr(quote, "data_time") :
            dt_str = quote.data_date + " " + quote.data_time
            option_quote.timestamp = datetime.strptime(dt_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
        if quote.code:
            # 简单解析：代码中包含C为看涨，P为看跌
            if "C" in quote.code.split(str(quote.strike_price))[0] if quote.strike_price else "C" in quote.code:
                option_quote.contract_type = "CALL"
                option_quote.direction = "CALL"
            elif "P" in quote.code.split(str(quote.strike_price))[0] if quote.strike_price else "P" in quote.code:
                option_quote.contract_type = "PUT"
                option_quote.direction = "PUT"
            else:
                # 尝试从其他字段推断
                option_quote.contract_type = str(quote.index_option_type) if quote.index_option_type else None
                option_quote.direction = option_quote.contract_type
        else:
            option_quote.contract_type = None
            option_quote.direction = None

        return option_quote

    def _unlock_trade(self):

        # 非模拟环境下，需要解释才能提交/修改订单
        if not self.tc: raise ConnectionError("未连接")

        # 模拟环境不需要解锁交易
        if self.get_trade_env() == TrdEnv.SIMULATE:
            return True
        
        # 若使用真實賬户下單，需先對賬户進行解鎖。
        pwd_unlock = ""
        ret, data = self.tc.unlock_trade(pwd_unlock)  
        if ret == RET_OK:
            logger.error(f"💥 下单解锁失败。ret={ret}, data={data}")
            return False
        return True
   
       # 🔑 安全转换：SDK 返回 Decimal / None，统一转为 float
   
    def _safe_float(self, val): return float(val) if val is not None else 0.0

    def _formater_(self, symbol):
        # APPL.US -> US.APPL
        if not symbol or '.' not in symbol:
            return symbol
    
        symbol = symbol.upper()
        # 后缀映射
        suffix_to_prefix = { '.US': 'US.','.HK': 'HK.', }
        if symbol.startswith(('US.', 'HK.')):
            # 检查是否已为目标格式
            return symbol
        
        # 处理常见后缀
        for suffix, prefix in suffix_to_prefix.items():
            if symbol.endswith(suffix):
                return f"{prefix}{symbol[:-len(suffix)]}"
        
        return symbol

class KlineHandler(CurKlineHandlerBase):
    def __init__(self, callback: Callable[[KlineData], None]):
        self._file = None
        self._ts: datetime = None
        self._kline_callback = callback

    def on_recv_rsp(self, rsp_pb):
        ret_code, data = super(KlineHandler, self).on_recv_rsp(rsp_pb)
        if ret_code != RET_OK:
            logger.error(f"💥 KlineHandler error, code={ret_code}, msg: {data}")
            return
        
        #print("CurKlineTest ", data) # CurKlineTest 自己的處理邏輯
        cs = data.iloc[0]
        new_ts = datetime.strptime(cs.time_key, "%Y-%m-%d %H:%M:%S")
        if self._ts is not None and self._ts < new_ts:
            # 每分钟最后一条作为 is_confirmed 的k线
            print("bbbbb...")
            self.is_confirmed = True
            self._ts = new_ts
        else:
            self._ts = new_ts
            self.is_confirmed = False
        
        if self._kline_callback:
            self._kline_callback(KlineData(
                ts = cs.time_key, open=float(cs.open), 
                high = float(cs.high), low = float(cs.low), 
                close = float(cs.close), volume = float(getattr(cs, "volume", 0)), 
                is_confirmed = self.is_confirmed
            ))
            
# ================= 独立 CLI 诊断测试 =================
def run_diagnostic(broker: FutuAdapter, symbol: str = "QQQ.US", test_orders: bool = False):
    import time, sys, traceback
    from datetime import date

    print("="*55)
    print("🔍 Futu Broker 适配器 CLI 诊断工具")
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
            current_ts = datetime.now(CONFIG["tz_et"])
            op_symbol = QQQStrategy.generate_option_symbol(710.00, OrderSide.BUY, current_ts)
            op_quote = broker.quote_option(op_symbol)
            assert op_quote and op_quote.last_price > 0
            print(f"   ✅ PASS: {op_symbol} | Last: {op_quote.last_price} | volume: {op_quote.volume} | ts: {op_quote.timestamp}")
        except Exception as ex:
            print(f"   ⚠️ WARN: May be no quote access. ex: {ex}")

        # 3. 历史K线
        print("\n📈 [4/6] 测试历史K线 (history_kline_by_date)...")
        # 默认取今日，若非交易日会返回空列表或抛异常，属正常现象
        target_date = datetime.now(CONFIG["tz_et"]) + timedelta(-1)
        klines = broker.history_kline_by_date(symbol, "K_1M", target_date)

        if klines is not None and len(klines) > 0:
            print(f"   ✅ PASS: 成功获取 {len(klines)} 条 1分钟K线 | 首根时间: {klines[0].ts}")
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
            if k.is_confirmed:
                print(f" 📩 confirmed kline: {k}")
        broker.set_kline_callback(_temp_cb)
        broker.subscribe_klines(symbol)
        print("   ⏳ 监听中 (等待 5 秒接收推送)...")
        time.sleep(5)  # 替代 wait_for_events，避免永久阻塞
        if recv_count > 0:
            print(f"   ✅ PASS: 共收到 {recv_count} 条实时K线推送")
        else:
            print("   ⚠️ WARN: 未收到推送 (可能当前非交易时段或网络订阅未生效)")
        
        # 5. 订单流程 (高风险，默认关闭)
        if test_orders:
            print("\n📝 [6/6] 测试订单流程 (submit -> check -> cancel)...")
            print("   ⚠️ 严重警告：此测试将向交易所发送真实请求！")
            print("   💡 请确保 .env 中配置的是证券模拟盘(Sandbox)环境！")
            confirm = input("   确认继续? (输入 YES 并回车): ")
            if confirm.strip().upper() == "YES":
                try:
                    q = broker.quote(symbol)
                    # 设置远低于市价的限价单，100% 防误成交
                    safe_price = round(q.last_price * 0.2, 2)
                    req = OrderRequest(
                        symbol=symbol, side=OrderSide.BUY, type=OrderType.NORMAL,
                        quantity=100, price=safe_price
                    )
                    order_id = broker.submit_order(req)
                    if order_id is None:
                         print(f"   ❌ 提交失败 | OrderId: {order_id} | 限价: {safe_price}")
                    else:
                        print(f"   📤 提交成功 | OrderId: {order_id} | 限价: {safe_price}")
                        time.sleep(3)
                        check = broker.check_order(order_id, symbol)
                        if check: print(f"   🔍 状态查询: {check.status.name} | 成交数: {check.filled_qty}")
                        else: print(f"   ❌ 状态查询失败: {order_id} -> {check}")
                        
                        broker.cancel_order(order_id, symbol)
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
    '''
    # 1. 基础诊断（连接/报价/K线/推送）
    python -m src.broker.futu --symbol QQQ.US
    # 2. 完整诊断（含订单流程，需手动确认 YES）
    python -m src.broker.futu --symbol QQQ.US --test-orders
    '''
    import argparse

    # CLI 参数解析
    parser = argparse.ArgumentParser(description="Futu Broker 接口 CLI 诊断工具")
    parser.add_argument("--symbol", default="QQQ.US", help="测试标的代码 (默认: QQQ.US)")
    parser.add_argument("--test-orders", action="store_true", help="开启订单提交/撤单流程测试 (高风险)")
    args = parser.parse_args()
    
    #ss = 
    #broker = FutuAdapter()
    #print(broker._formater_("US.QQQ260521P003000"))
    run_diagnostic(broker = FutuAdapter(), symbol=args.symbol, test_orders=args.test_orders)
