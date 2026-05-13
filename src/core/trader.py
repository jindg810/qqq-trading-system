#!/usr/bin/env python3
"""
QQQ 0DTE 实盘交易引擎 v6.2 (策略分离版)
✅ 仅负责：长桥API交互 / 订单执行 / 状态持久化 / CSV归档 / 异常重试
✅ 策略逻辑 100% 委托至 core.strategy
"""
import sys
import time
import threading
import traceback
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

from src.broker.longbridge import LongbridgeAdapter
from src.message.notifier import Notifier
from src.config import CONFIG
from src.logger import get_logger
from src.core.trader_data import TraderDataManager
from src.core.strategy import QQQStrategy
from src.broker.base import BrokerAdapter
from src.broker.models import (
    OrderRequest, OrderCheck, Quote, KlineData,
    OrderStatus, OrderSide, OrderType, TimeInForce
)

load_dotenv()
logger = get_logger("core.trader")

class QQQTrader:
    def __init__(self, broker: BrokerAdapter):
        logger.system("初始化交易引擎 v6.2...")
        self.data_manager = TraderDataManager()
        self.strategy = QQQStrategy()  # 🔑 注入核心策略
        
        self.last_opt_poll = 0.0
        self._current_date = None
        
        # 加载历史状态 & 初始化文件
        self.data_manager.init_state_file()
        self.data_manager.init_csv()
        self._load_state()

        self.broker = broker  # 🔑 依赖注入
        self.broker.connect() # 🔑 显式接管连接生命周期
        logger.info("✅ 券商接口连接成功")

    def _load_state(self):
        state = self.data_manager.load_state()
        if state:
            # 恢复策略状态
            self.strategy.position = state.get("position")
            self.strategy.trades_today = state.get("trades_today", 0)
            self.strategy.consecutive_losses = state.get("consecutive_losses", 0)
            self.strategy.daily_pnl = float(state.get("daily_pnl", 0.0))
            self.strategy.emergency_stopped = state.get("emergency_stopped", False)
            
            # ✅ 新增：恢复上次处理的交易日期（防重启误判）
            last_date_str = state.get("last_trade_date")
            self._current_date = datetime.strptime(last_date_str, "%Y-%m-%d").date() if last_date_str else None

    def _save_state(self):
        state = {
            "updated": datetime.now(CONFIG["tz_et"]).isoformat(),
            "running": True,
            "position": self.strategy.position,
            "trades_today": self.strategy.trades_today,
            "consecutive_losses": self.strategy.consecutive_losses,
            "daily_pnl": round(self.strategy.daily_pnl, 2),
            "emergency_stopped": self.strategy.emergency_stopped,
            "last_trade_date": self._current_date.strftime("%Y-%m-%d") if self._current_date else None
        }
        self.data_manager.save_state(state)

    def _trace_state(self):
        logger.debug(f"当前状态: \
                     持仓: {self.strategy.position}, 今日交易次数: {self.strategy.trades_today}, \
                     连续亏损: {self.strategy.consecutive_losses}, 日盈亏: {self.strategy.daily_pnl:.2f}, \
                     紧急停止: {self.strategy.emergency_stopped}, 当前日期: {self._current_date}")


    # ================= 订单执行 =================
    def _wait_for_order_fill(self, order_id: str, timeout: int = CONFIG.get("order_check_timeout", 10)) -> Optional[float]:
        start = time.time()
        while time.time() - start < timeout:
            try:
                order = self.broker.check_order(order_ids=[order_id])
                if not order:
                    time.sleep(CONFIG.get("order_check_interval", 1))
                    continue
                
                if order.filled_qty > 0 and order.filled_price > 0:
                    return float(order.filled_price)
                if order.status in (OrderStatus.Canceled, OrderStatus.Rejected, OrderStatus.Failed):
                    logger.warning(f"订单 {order_id} 状态: {order.status}，终止轮询")
                    return None
            except Exception as e:
                logger.warning(f"查询订单状态失败: {e}")
            time.sleep(CONFIG.get("order_check_interval", 1))
        try: 
            self.broker.cancel_order(order_id)
            logger.warning(f"⏱️ 订单 {order_id} 超时未成交，已取消")
        except: pass
        return None

    def _execute_open(self, side: str, stock_price: float):
        try:
            symbol = QQQStrategy.generate_option_symbol(stock_price, side)
            if symbol is None:
                logger.error(f"尝试失败，无法生成期权合约代码: price={stock_price}, side={side}")
                return False

            if not CONFIG.get("auto_trade_on", False):
                logger.info(f"💧 自动交易关闭中。开仓参数: {symbol} @ {stock_price:.2f} | side: {side}")
                return False
            
            logger.info(f"📈 尝试开仓: {symbol}")
            self._trace_state() # 状态追踪日志

            order = OrderRequest(
                symbol=symbol,
                quantity=CONFIG["max_position_size"],
                side=OrderSide.BUY,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY
            )

            order_id = self.broker.submit_order(order)
            if not order_id:
                logger.error(f"订单提交失败: symbol:{symbol}, side:{side}, price:{stock_price}.")
                return False
            
            fill_price = self._wait_for_order_fill(order_id)
            if not fill_price: return False
            
            # 抓取最新报价确认成交价
            time.sleep(1)
            quote = self.broker.quote_option(symbol)
            if quote and quote.last_price > 0:
                fill_price = quote.last_price
                
            self.strategy.open_position(side, stock_price, fill_price, symbol)
            self._save_state()
            Notifier().notify_open(symbol, fill_price, side) 
            logger.info(f"✅ 开仓成功: {symbol} @ {fill_price:.2f}")
            return True
        except Exception as e:
            logger.error(f"开仓失败: {e}")
            return False

    def _execute_close(self, exit_reason: str):
        if not self.strategy.position: return
        symbol = self.strategy.position["symbol"]
        logger.info(f"📉 尝试平仓 [{exit_reason}]: {symbol}")
        try:
            order = OrderRequest(
                symbol=symbol,
                quantity=CONFIG["max_position_size"],
                side=OrderSide.Sell,
                order_type=OrderType.MO,
                time_in_force=TimeInForce.Day,
            )
            order_id = self.broker.submit_order(order)
            fill_price = self._wait_for_order_fill(order_id)
            if fill_price:
                trade = self.strategy.close_position(fill_price)
                Notifier().notify_close(symbol, fill_price, trade["pnl"], exit_reason)
                logger.info(f"✅ 平仓成功 @ {fill_price:.2f} | 盈亏: {trade['pnl']:+.2f}")
            self._save_state()
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            # 
            if self.strategy.position: self._save_state()

    # ================= 行情回调 =================
    def _on_kline(self, event: KlineData):
        try:
            ts_time = event.ts if isinstance(event.ts, datetime) else datetime.strptime(event.ts, "%Y-%m-%dT%H:%M:%SZ")
            print(f"收到K线数据: {ts_time.strftime('%Y-%m-%dT%H:%M:%SZ')}, {event} ")
            bar = {
                "open": event.open, "high": event.high,
                "low": event.low, "close": event.close,
                "volume": event.volume, "ts": event.ts,
            }
            
            # 1. 注入策略缓冲
            if not self.strategy.add_bar(bar): return

            # 2. 每日重置与CSV归档
            bar_date = ts_time.date()
            # ✅ 核心优化：严格大于才触发，彻底杜绝重启当日重复执行
            if self._current_date is None or bar_date > self._current_date:
                logger.info(f"跨日交易状态重置&数据归档, date:{self._current_date}")
                self.strategy.reset_daily()
                self._current_date = bar_date
                self.data_manager.archive_csv()
                self._save_state()  # 跨日切换后立即落盘，防断电丢失
            self.data_manager.write_kline_to_csv(bar)

            # 3. 盘前/盘后静默
            #if not (datetime(2000,1,1,9,30).time() <= bar["ts"].time() <= datetime(2000,1,1,16,0).time()):
             #   return
            if not self.strategy.is_trading_hours(bar["ts"]): return

            # 4. 风控检查：日亏损限额 & 连续止损次数
            # if not self.strategy.check_risk(): return
            if not self.strategy.check_risk():
                reason = "日亏损熔断" if self.strategy.daily_pnl <= CONFIG["max_daily_loss"] else "连续止损熔断"
                Notifier().notify_risk_fuse(reason, f"账户状态: 连亏 {self.strategy.consecutive_losses} 次 | 日盈亏 {self.strategy.daily_pnl}")
                return
            
            # 5. 交易频次控制
            total_limit = CONFIG.get("breakout_max", 8) + CONFIG.get("reversal_max", 1)
            if self.strategy.trades_today >= total_limit:
                self._check_position_exit()
                return
                
            # 6. 信号与持仓管理
            if not self.strategy.position:
                sig = self.strategy.generate_signal()
                if sig: Notifier().notify_signal(sig, bar["close"], reason="突破/反转触发")
                if sig and self.strategy.trades_today < CONFIG.get("breakout_max", 8):
                    self._execute_open(sig, bar["close"])
            else:
                self._check_position_exit()
                
            self._save_state()
        except Exception as e:
            logger.error(f"行情处理异常: {e}")
            traceback.print_exc()

    def _check_position_exit(self):
        """REST轮询期权价格并委托策略判断退出"""
        if not self.strategy.position: return
        if time.time() - self.last_opt_poll < 15: return
        self.last_opt_poll = time.time()
        
        try:
            quote = self.broker.quote_option([self.strategy.position["symbol"]])
            if quote and quote.last_price > 0:
                exit_reason = self.strategy.check_position_exit(float(quote.last_price))
                if exit_reason:
                    self._execute_close(exit_reason)
        except Exception as e:
            logger.warning(f"持仓监控异常: {e}")

    # ================= 启动入口 =================
    def start(self):
        logger.info("🚀 启动交易引擎...")
        retry, max_retries = 0, 5
        while retry < max_retries:
            try:
                self.broker.set_kline_callback(self._on_kline)
                self.broker.subscribe_klines("700.HK")
                #self.broker.subscribe_klines(CONFIG["symbol"])
                logger.info(f"✅ 已订阅 {CONFIG['symbol']} 1分钟K线行情")
                threading.Event().wait()
            except KeyboardInterrupt:
                logger.info("🛑 收到停止信号")
                break
            except Exception as e:
                retry += 1
                logger.error(f"❌ 连接异常 ({retry}/{max_retries}): {e}")
                time.sleep(5)
            finally:
                if self.strategy.position: self._execute_close("FORCE_CLOSE")
                self._save_state()
                logger.info("🛑 交易引擎已安全停止")

if __name__ == "__main__":
    try:
        trader = QQQTrader(broker=LongbridgeAdapter())
        trader.start()
    except Exception as e:
        logger.critical(f"💥 致命错误: {e}")
        traceback.print_exc()
        sys.exit(1)