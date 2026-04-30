#!/usr/bin/env python3
"""
QQQ 0DTE 实盘交易引擎 v6.1 (重构版)
- 修复 import * 语法错误
- 使用新目录结构
"""

import os
import sys
import json
import time
import csv
import traceback
import shutil
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv
from logger import get_logger
from trader_data import TraderDataManager

# ===================== 导入配置 =====================
from config import CONFIG, DATA_DIR

# ===================== 导入长桥 SDK（模块级别）=====================
from longbridge.openapi import (
    Config,
    QuoteContext,
    TradeContext,
    Order,
    OrderSide,
    OrderType,
    OrderStatus,
    TimeInForceType,
    SubType
)

# ===================== 加载环境变量 =====================
load_dotenv()

logger = get_logger(__name__)

# ===================== 交易引擎 =====================
class QQQTrader:
    def __init__(self):
        logger.system("初始化交易引擎...")
        
        # 使用数据管理器
        self.data_manager = TraderDataManager()
        
        # 交易状态
        self.position = None
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.emergency_stopped = False
        self.state_lock = False

        self.bars = deque(maxlen=200)          # K线数据
        self.sma20_vol = deque(maxlen=20)     # 成交量数据
        self.trades_log = []                   # 交易记录
        self.bar_counter = 0                   # K线计数器
        
        # 初始化状态文件
        self.init_state_file()
        self.load_state()
        
        try:
            cfg = Config.from_apikey_env()
            self.qc = QuoteContext(cfg)
            self.tc = TradeContext(cfg)
            logger.info("✅ 长桥 API 初始化成功")
        except Exception as e:
            logger.error(f"❌ 长桥初始化失败: {e}")
            raise

    # ================== 状态管理 ==================
    def load_state(self):
        """加载状态"""
        state = self.data_manager.load_state()
        if state:
            self.position = state.get("position")
            self.trades_today = state.get("trades_today", 0)
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.daily_pnl = state.get("daily_pnl", 0.0)
            self.emergency_stopped = state.get("emergency_stopped", False)

    def init_state_file(self):
        if not os.path.exists(CONFIG["state_file"]):
            self.data_manager.save_state({
                "running": False,
                "position": None,
                "trades_today": 0,
                "consecutive_losses": 0,
                "daily_pnl": 0.0
            })

    def save_state(self):
        """保存状态"""
        if self.state_lock:
            return
            
        self.state_lock = True
        try:
            state = {
                "updated": datetime.now(CONFIG["tz_et"]).isoformat(),
                "running": True,
                "position": self.position,
                "trades_today": self.trades_today,
                "consecutive_losses": self.consecutive_losses,
                "daily_pnl": round(self.daily_pnl, 2),
                "emergency_stopped": self.emergency_stopped
            }
            self.data_manager.save_state(state)
        finally:
            self.state_lock = False

    # ================== K线数据管理 ==================
    def validate_bar(self, bar: Dict[str, Any]) -> bool:
        """验证K线数据有效性"""
        required_fields = ["open", "high", "low", "close"]
        for field in required_fields:
            if field not in bar:
                logger.error(f"K线缺少字段: {field}")
                return False
            
            if not isinstance(bar[field], (int, float)):
                logger.error(f"K线字段 {field} 不是数字")
                return False
        
        # 验证OHLC关系
        if bar["high"] < bar["low"]:
            logger.error("最高价低于最低价")
            return False
        
        if bar["open"] > bar["high"] or bar["open"] < bar["low"]:
            logger.error("开盘价超出高低范围")
            return False
        
        if bar["close"] > bar["high"] or bar["close"] < bar["low"]:
            logger.error("收盘价超出高低范围")
            return False
        
        return True
    
    def write_kline_to_csv(self, bar: Dict[str, Any]) -> None:  
        """写入K线到当日CSV"""
        try:
            if not self.validate_bar(bar):
                logger.warning(f"无效K线数据，已忽略: {bar}")
                return;
    
            """添加K线数据"""
            self.bars.append(bar)
            self.sma20_vol.append(bar.get("volume", 0))
            self.bar_counter += 1
            self.data_manager.write_kline_to_csv(bar)

        except Exception as e:
            logger.error(f"写入K线失败: {e}")
    
    # ================== 风控检查 ==================
    def check_risk_controls(self):
        if self.emergency_stopped:
            return False
            
        # 当日盈亏金额​ <= 触发熔断的亏损额度（负数）
        if self.daily_pnl <= CONFIG["max_daily_loss"]:
            logger.critical(f"🚨 触发熔断：单日亏损 {self.daily_pnl:.2f} USD")
            self.emergency_stop()
            return False
            
        if self.consecutive_losses >= CONFIG["max_consecutive_losses"]:
            logger.critical(f"🚨 触发熔断：连续止损 {self.consecutive_losses} 次")
            self.emergency_stop()
            return False
            
        return True

    def emergency_stop(self):
        logger.critical("🚨 执行紧急交易停止")
        self.emergency_stopped = True
        
        if self.position:
            logger.warning("🚨 执行紧急平仓")
            self.close_position(force=True)
        
        self.save_state()

    # ================== 工具函数 ==================
    def now_et(self):
        return datetime.now(CONFIG["tz_et"])

    def is_trading_hours(self):
        now = self.now_et()
        
        if now.weekday() >= 5:
            return False
            
        market_open = datetime.strptime("09:30", "%H:%M").time()
        market_close = datetime.strptime("16:00", "%H:%M").time()
        current_time = now.time()
        
        return market_open <= current_time <= market_close

    def generate_option_symbol(self, price, side):
        try:
            if price <= 0:
                logger.error("无效的标的价格")
                return None

            # 期权行权价​根据标的价格和偏移量计算，向上取整（看涨）或向下取整（看跌），单位为0.01美元
            # 期权行权价 = 距离现货 ±2 美元的 0DTE 虚值期权行权价    
            strike = round(price + CONFIG["offset"]) if side == "call" else round(price - CONFIG["offset"])
            exp = self.now_et().strftime("%y%m%d")
            otype = "C" if side == "call" else "P"
            return f"QQQ{exp}{otype}{strike*1000:06d}.US"
        except Exception as e:
            logger.error(f"生成期权代码失败: {e}")
            return None

    # ================== 订单管理 ==================
    def wait_for_order_fill(self, order_id):
        start_time = time.time()
        
        while time.time() - start_time < CONFIG["order_check_timeout"]:
            try:
                orders = self.tc.history_orders(order_ids=[order_id])
                if orders and (orders[0].status == OrderStatus.Filled or orders[0].filled_quantity > 0):
                    filled_price = float(orders[0].filled_avg_price)
                    if filled_price > 0:
                        return filled_price
            except Exception as e:
                logger.warning(f"检查订单状态失败: {e}")
            
            time.sleep(CONFIG["order_check_interval"])
        
        try:
            self.tc.cancel_order(order_id)
            logger.warning(f"订单 {order_id} 超时未成交，已取消")
        except:
            pass
            
        return None

    def open_position(self, side, price):
        if not self.check_risk_controls():
            return False
            
        symbol = self.generate_option_symbol(price, side)
        if not symbol:
            return False
            
        try:
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
                logger.error("订单提交失败")
                return False
                
            filled_price = self.wait_for_order_fill(order_id)
            if not filled_price:
                logger.error("订单未成交")
                return False
                
            self.position = {
                "side": side,
                "symbol": symbol,
                "entry_stock": price,
                "entry_opt": filled_price,
                "peak_pnl": 0.0,
                "bars": 0,
                "entry_time": self.now_et().isoformat()
            }
            
            self.trades_today += 1
            logger.info(f"✅ 开仓成功: {symbol} @ {filled_price}")
            return True
            
        except Exception as e:
            logger.error(f"开仓失败: {e}")
            traceback.print_exc()
            return False

    def close_position(self, force=False):
        if not self.position:
            return False
            
        symbol = self.position["symbol"]
        
        try:
            logger.info(f"📉 尝试平仓: {symbol}")
            
            order = Order(
                symbol=symbol,
                quantity=CONFIG["max_position_size"],
                side=OrderSide.Sell,
                type=OrderType.Market,
                time_in_force=TimeInForceType.Day
            )
            
            order_id = self.tc.submit(order)
            filled_price = self.wait_for_order_fill(order_id)
            
            if filled_price:
                self.log_trade(filled_price)
                logger.info(f"✅ 平仓成功 @ {filled_price}")
                
                trade_pnl = filled_price - self.position["entry_opt"]
                if trade_pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0
                    
                self.daily_pnl += trade_pnl
                self.position = None
                return True
            else:
                logger.error("平仓订单未成交")
                if force:
                    self.position = None
                return False
                
        except Exception as e:
            logger.error(f"平仓失败: {e}")
            if force:
                self.position = None
            return False

    # ================== 指标计算 ==================
    # TODO 两个指标 升级成 EMA + VWAP，更适合 0DTE 期权交易。
    def sma20_price(self):
        '''
        计算最近20根K线的收盘价简单移动平均，实际使用：
        价格在 SMA20 上方 → 多头趋势（只做多）,
        价格在 SMA20 下方 → 空头趋势（只做空）。
        '''
        if len(self.bars) < 20:
            return None
        
        # 包含当前未完成的K线 ？
        # list(self.bars)[-21:-1]
        return sum(b["close"] for b in list(self.bars)[-20:]) / 20

    def volume_ok(self):
        '''成交量必须满足：当前K线成交量 >= 最近20根K线平均成交量 * vol_mult'''
        if len(self.sma20_vol) < 20:
            return True
        avg_vol = sum(self.sma20_vol) / 20
        return self.bars[-1]["volume"] >= avg_vol * CONFIG["vol_mult"]

    # ================== 信号检测 ==================
    def breakout_signal(self):
        '''
        突破条件：
        ✅ 价格在 SMA20 上方（看涨）或下方（看跌）
        ✅ 收阳线（看涨）或阴线（看跌）
        ✅ 成交量达标
        👉 四重确认，才开仓
        '''
        if len(self.bars) < CONFIG["lookback"] + 1:
            return None
            
        try:
            # 前 5 根 K 线（不含当前）
            prev = list(self.bars)[-(CONFIG["lookback"]):-1]
            if not prev:
                return None
                
            last = self.bars[-1] # 当前 K 线
            upper = max(b["high"] for b in prev) # 最近 5 根 K 线的最高价
            lower = min(b["low"] for b in prev) # 最近 5 根 K 线的最低价
            
            '''
            body, K 线实体: abs(收盘价 - 开盘价) / 收盘价
            拒绝“十字星”、“针状线”等实体较小的K线，避免假突破
            实体大小占收盘价的比例必须大于 min_body_pct（0.03%）
            '''
            body = abs(last["close"] - last["open"]) / last["close"]
            if body < CONFIG["min_body_pct"]:
                return None
                
            if not self.volume_ok():
                return None
                
            sma = self.sma20_price()
            if sma is None:
                return None
            
            # 忽略美东时间 09:30 - 09:35 不交易，防止开盘毛刺
            if 9 * 60+30 <= self.now_et().hour * 60 + self.now_et().minute < 9 * 60 + 35:
                return None
            
            ''' 
            1. 当前价格突破最近5根K线的最高价（看涨）或最低价（看跌）
            2. 当前价格在SMA20上方（看涨）或下方（看跌）
            3. 当前K线为阳线（看涨）或阴线（看跌）
            '''
            if last["close"] > upper and last["close"] > sma and last["close"] > last["open"]:
                return "call"
            if last["close"] < lower and last["close"] < sma and last["close"] < last["open"]:
                return "put"
                
        except ZeroDivisionError:
            logger.error("除零错误（价格为零）")
        except Exception as e:
            logger.error(f"突破信号检测失败: {e}")
            
        return None

    # ================== 行情接收回调方法 ==================
    def on_quote(self, ctx, quote):
        try:
            if not self.is_trading_hours():
                return
                
            if not hasattr(quote, "candlesticks") or not quote.candlesticks:
                return
                
            for cs in quote.candlesticks:
                if not getattr(cs, "is_confirmed", False):
                    continue
                    
                bar = {
                    "open": cs.open,
                    "high": cs.high,
                    "low": cs.low,
                    "close": cs.close,
                    "volume": getattr(cs, "volume", 0)
                }
                
                self.bars.append(bar)
                self.sma20_vol.append(bar["volume"])
                self.write_kline(bar)
                # 验证K线数据
                #self.data_manager.add_bar(bar)
                self.write_kline_to_csv(bar)
                
                if not self.check_risk_controls():
                    return
                    
                total_allowed = CONFIG["breakout_max"] + CONFIG["reversal_max"]
                if self.trades_today >= total_allowed:
                    self.manage_position()
                    continue
                    
                if not self.position:
                    sig = self.breakout_signal()
                    if sig and self.trades_today < CONFIG["breakout_max"]:
                        self.open_position(sig, bar["close"])
                else:
                    self.manage_position()

            # 定期保存状态
            if self.data_manager.bar_counter % 5 == 0:
                self.save_state()

            # ✅ 收盘检查（> 16:00整）
            self.data_manager.archive_csv()
            
        except Exception as e:
            logger.error(f"行情处理异常: {e}")
            traceback.print_exc()

    # ================== 启动 ==================
    def start(self):
        logger.info("🚀 启动交易引擎...")
        
        retry_count = 0
        max_retries = 5
        
        while retry_count < max_retries:
            try:
                self.qc.set_on_quote(self.on_quote)
                self.qc.subscribe([CONFIG["symbol"]], [SubType.Quote])
                logger.info(f"✅ 已订阅 {CONFIG['symbol']} 行情")
                
                import threading
                threading.Event().wait()
                
            except KeyboardInterrupt:
                logger.info("🛑 收到停止信号")
                break
            except Exception as e:
                retry_count += 1
                logger.error(f"❌ 连接异常 (尝试 {retry_count}/{max_retries}): {e}")
                time.sleep(5)
            finally:
                self.save_state()
                
        logger.info("🛑 交易引擎已停止")

# ===================== 主入口 =====================
if __name__ == "__main__":
    try:
        trader = QQQTrader()
        trader.start()
    except Exception as e:
        logger.critical(f"💥 致命错误: {e}")
        traceback.print_exc()
        sys.exit(1)

'''
加一个 K 线到达时间戳校验（防乱序）​
✅ 加一个 断线自动重连 + 补 K 线机制
✅ 增加 K 线乱序校验
✅ 增加 断线自动重连
✅ 增加 盘前/盘后静默模式
改成 按日期自动分文件
增加 K线重复写入校验
'''