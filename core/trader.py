#!/usr/bin/env python3
"""
QQQ 0DTE 实盘交易引擎 v6.2 (策略分离版)
✅ 仅负责：长桥API交互 / 订单执行 / 状态持久化 / CSV归档 / 异常重试
✅ 策略逻辑 100% 委托至 core.strategy
"""
import os
import sys
import time
import threading
import traceback
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

# 路径设置
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from config import CONFIG
from logger import get_logger
from trader_data import TraderDataManager
from core.strategy import QQQStrategy
from longbridge.openapi import (
    Config, Period, PushCandlestick, QuoteContext, TradeContext, 
    Order, OrderSide, OrderType, OrderStatus, TimeInForceType, TradeSessions
)

load_dotenv()
logger = get_logger("core.trader")

class QQQTrader:
    def __init__(self):
        logger.system("初始化交易引擎 v6.2...")
        self.data_manager = TraderDataManager()
        self.strategy = QQQStrategy()  # 🔑 注入核心策略
        
        self.last_opt_poll = 0.0
        self._current_date = None
        
        # 加载历史状态 & 初始化文件
        self.data_manager.init_state_file()
        self.data_manager.init_csv()
        self._load_state()
        
        # 长桥上下文
        try:
            cfg = Config.from_apikey_env()
            self.qc = QuoteContext(cfg)
            self.tc = TradeContext(cfg)
            logger.info("✅ 长桥 API 初始化成功.")
        except Exception as e:
            logger.critical(f"❌ 长桥初始化失败: {e}")
            raise

    def _load_state(self):
        state = self.data_manager.load_state()
        if state:
            # 恢复策略状态
            self.strategy.position = state.get("position")
            self.strategy.trades_today = state.get("trades_today", 0)
            self.strategy.consecutive_losses = state.get("consecutive_losses", 0)
            self.strategy.daily_pnl = float(state.get("daily_pnl", 0.0))
            self.strategy.emergency_stopped = state.get("emergency_stopped", False)

    def _save_state(self):
        state = {
            "updated": datetime.now(CONFIG["tz_et"]).isoformat(),
            "running": True,
            "position": self.strategy.position,
            "trades_today": self.strategy.trades_today,
            "consecutive_losses": self.strategy.consecutive_losses,
            "daily_pnl": round(self.strategy.daily_pnl, 2),
            "emergency_stopped": self.strategy.emergency_stopped
        }
        self.data_manager.save_state(state)


    # ================= 订单执行 =================
    def _wait_for_order_fill(self, order_id: str, timeout: int = CONFIG.get("order_check_timeout", 10)) -> Optional[float]:
        start = time.time()
        while time.time() - start < timeout:
            try:
                orders = self.tc.history_orders(order_ids=[order_id])
                if not orders:
                    time.sleep(CONFIG.get("order_check_interval", 1))
                    continue
                order = orders[0]
                # ✅ 修复：正确访问 order 对象属性
                if order.filled_quantity > 0 and order.filled_avg_price > 0:
                    return float(order.filled_avg_price)
                if order.status in (OrderStatus.Canceled, OrderStatus.Rejected, OrderStatus.Failed):
                    logger.warning(f"订单 {order_id} 状态: {order.status}，终止轮询")
                    return None
            except Exception as e:
                logger.warning(f"查询订单状态失败: {e}")
            time.sleep(CONFIG.get("order_check_interval", 1))
        try: 
            self.tc.cancel_order(order_id)
            logger.warning(f"⏱️ 订单 {order_id} 超时未成交，已取消")
        except: pass
        return None

    def _execute_open(self, side: str, stock_price: float):
        try:
            symbol = QQQStrategy.generate_option_symbol(stock_price, side)
            if symbol is None:
                logger.error(f"尝试失败，无法生成期权合约代码: price={stock_price}, side={side}")
                return False
        
            logger.info(f"📈 尝试开仓: {symbol}")
            order = Order(
                symbol=symbol,
                quantity=CONFIG["max_position_size"],
                side=OrderSide.Buy,
                type=OrderType.Market,
                time_in_force=TimeInForceType.Day
            )
            order_id = self.tc.submit(order)
            if not order_id:
                print(f"订单提交失败: symbol:{symbol}, side:{side}, price:{price}.")
                return False
            
            fill_price = self._wait_for_order_fill(order_id)
            if not fill_price: return False
            
            # 抓取最新报价确认成交价
            time.sleep(1)
            opt_q = self.qc.quote([symbol])
            if opt_q and opt_q[0].last_done > 0:
                fill_price = float(opt_q[0].last_done)
                
            self.strategy.open_position(side, stock_price, fill_price, symbol)
            self._save_state()
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
            order = Order(
                symbol=symbol,
                quantity=CONFIG["max_position_size"],
                side=OrderSide.Sell,
                type=OrderType.Market,
                time_in_force=TimeInForceType.Day,
            )
            order_id = self.tc.submit(order)
            fill_price = self._wait_for_order_fill(order_id)
            if fill_price:
                trade = self.strategy.close_position(fill_price)
                logger.info(f"✅ 平仓成功 @ {fill_price:.2f} | 盈亏: {trade['pnl']:+.2f}")
            self._save_state()
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            # 
            if self.strategy.position: self._save_state()

    # ================= 行情回调 =================
    def is_trading_hours(self):
        now = datetime.now(CONFIG["tz_et"])
        if now.weekday() >= 5: return False

        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close
    
    def on_candlestick(self, symbol: str, event: PushCandlestick):
        try:
            # 只处理已收盘的 K 线，避免未完成 K 线的价格波动导致误判
            if not hasattr(event, "candlestick") or not getattr(event, "is_confirmed", False):
                return
                
            cs = event.candlestick
            print(f"收到行情: {cs} ")
            bar = {
                "open": float(cs.open),
                "high": float(cs.high),
                "low": float(cs.low),
                "close": float(cs.close),
                "volume": float(getattr(cs, "volume", 0)),
                "ts": getattr(cs, "timestamp", None),
            }
            
            # 1. 注入策略缓冲
            if not self.strategy.add_bar(bar): return
            
            # 2. 每日重置与CSV归档
            bar_date = bar["ts"].date()
            if self._current_date != bar_date:
                self.strategy.reset_daily()
                self._current_date = bar_date
                self.data_manager.archive_csv()
                
            self.data_manager.write_kline_to_csv(bar)
            
            # 3. 盘前/盘后静默
            if not self.is_trading_hours(): return

            # 4. 风控检查：日亏损限额 & 连续止损次数
            if not self.strategy.check_risk(): return
            
            # 5. 交易频次控制
            total_limit = CONFIG.get("breakout_max", 8) + CONFIG.get("reversal_max", 1)
            if self.strategy.trades_today >= total_limit:
                self._check_position_exit()
                return
                
            # 6. 信号与持仓管理
            if not self.strategy.position:
                sig = self.strategy.generate_signal()
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
            opt_q = self.qc.quote([self.strategy.position["symbol"]])
            if opt_q and opt_q[0].last_done > 0:
                exit_reason = self.strategy.check_position_exit(float(opt_q[0].last_done))
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
                self.qc.set_on_candlestick(self.on_candlestick)
                # self.qc.subscribe_candlesticks("700.HK", Period.Min_1, TradeSessions.Intraday)
                self.qc.subscribe_candlesticks(CONFIG["symbol"], Period.Min_1, TradeSessions.Intraday)
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
        trader = QQQTrader()
        trader.start()
    except Exception as e:
        logger.critical(f"💥 致命错误: {e}")
        traceback.print_exc()
        sys.exit(1)