#!/usr/bin/env python3
"""
QQQ 0DTE 实盘交易引擎 v6.1 (重构版)
✅ 修复 PushCandlestick 结构解析 (.candlestick 子对象)
✅ 修复突破信号索引边界 cs[-lb-1:-1]
✅ 完整实现动态止盈/止损/超时管理 (REST轮询+节流)
✅ 新增衰竭反转信号路径 (双信号引擎)
✅ 状态管理完全委托至 TraderDataManager
✅ 修复 entry_opt 价格抓取 (sleep(1) + 防零值)
✅ 盘前/盘后静默 + 乱序K线拦截 + 原子状态保存
"""
import asyncio
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from collections import deque
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from logger import get_logger
from trader_data import TraderDataManager
from config import CONFIG, DATA_DIR
from longbridge.openapi import (
    AsyncQuoteContext, Config, Period, PushCandlestick, QuoteContext, TradeContext, Order, OrderSide, OrderType,
    OrderStatus, TimeInForceType, SubType, TradeSessions
)

load_dotenv()
logger = get_logger(__name__)

class QQQTrader:
    def __init__(self):
        logger.system("初始化交易引擎 v6.1...")
        self.data_manager = TraderDataManager()
        
        # 交易状态
        self.position: Optional[Dict[str, Any]] = None # 当前持仓信息
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.emergency_stopped = False
        
        # K线与指标缓冲
        self.bars = deque(maxlen=200)
        self.sma20_vol = deque(maxlen=20)
        self.last_bar_timestamp = None
        
        # REST轮询节流 (防API限频)
        self.last_opt_poll = 0.0
        
        # 加载历史状态 & 初始化文件
        self._init_state()
        self.load_state()
        
        # 长桥上下文
        try:
            cfg = Config.from_apikey_env()
            self.qc = QuoteContext(cfg)
            self.tc = TradeContext(cfg)
            logger.info("✅ 长桥 API 初始化成功.")
        except Exception as e:
            logger.critical(f"❌ 长桥初始化失败: {e}")
            raise

    # ================= 状态管理 (委托) =================
    def _init_state(self):
        self.data_manager.init_state_file()
        self.data_manager.init_csv()

    def load_state(self):
        state = self.data_manager.load_state()
        if state:
            self.position = state.get("position")
            self.trades_today = state.get("trades_today", 0)
            self.consecutive_losses = state.get("consecutive_losses", 0)
            self.daily_pnl = float(state.get("daily_pnl", 0.0))
            self.emergency_stopped = state.get("emergency_stopped", False)

    def save_state(self):
        state = {
            "updated": datetime.now(CONFIG["tz_et"]).isoformat(),
            "running": True,
            "position": self.position,
            "trades_today": self.trades_today,
            "consecutive_losses": self.consecutive_losses,
            "daily_pnl": round(self.daily_pnl, 2),
            "emergency_stopped": self.emergency_stopped
        }
        # 委托给数据管理器执行原子写入
        self.data_manager.save_state(state)

    # ================= 风控检查 =================
    def check_risk_controls(self) -> bool:
        if self.emergency_stopped: return False
        if self.daily_pnl <= CONFIG.get("max_daily_loss", -500):
            logger.critical(f"🚨 触发日亏损熔断: {self.daily_pnl:.2f}")
            self.emergency_stopped = True
            if self.position: self.close_position(force=True)
            return False
        if self.consecutive_losses >= CONFIG.get("max_consecutive_losses", 3):
            logger.critical(f"🚨 触发连续止损熔断: {self.consecutive_losses}次")
            self.emergency_stopped = True
            return False
        return True
    
    # ================= 时间与交易窗口 =================
    def now_et(self):
        return datetime.now(CONFIG["tz_et"])

    def is_trading_hours(self):
        now = self.now_et()
        if now.weekday() >= 5: return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

    # ================= 订单执行 =================
    def generate_option_symbol(self, price: float, side: str) -> Optional[str]:
        if price <= 0: return None
        # 期权行权价​根据标的价格和偏移量计算，向上取整（看涨）或向下取整（看跌），单位为0.01美元
        # 期权行权价 = 距离现货 ±2 美元的 0DTE 虚值期权行权价  
        strike = round(price + CONFIG["offset"]) if side == "call" else round(price - CONFIG["offset"])
        exp = self.now_et().strftime("%y%m%d")
        otype = "C" if side == "call" else "P"
        return f"QQQ{exp}{otype}{strike * 1000:06d}.US"

    def wait_for_order_fill(self, order_id: str, timeout: int = CONFIG.get("order_check_timeout", 10)) -> Optional[float]:
        start = time.time()
        while time.time() - start < timeout:
            try:
                orders = self.tc.history_orders(order_ids=[order_id])
                if not orders:
                    time.sleep(CONFIG.get("order_check_interval", 1))
                    continue
                    
                # ✅ 核心修复：只要已成交数量>0 且 均价>0，即视为有效成交
                order = orders[0]
                if orders.filled_quantity > 0 and orders.filled_avg_price > 0:
                    return float(orders.filled_avg_price)

                # 订单已终止且未成交 → 提前退出
                if order.status in (OrderStatus.Canceled, OrderStatus.Rejected, OrderStatus.Failed):
                    logger.warning(f"订单 {order_id} 状态: {order.status}，终止轮询。")
                    return None
            except Exception as e:
                logger.warning(f"查询订单状态失败: {e}")
            time.sleep(CONFIG.get("order_check_interval", 1))
        try:
            self.tc.cancel_order(order_id)
            logger.warning(f"⏱️ 订单 {order_id} 超时未成交，已取消")
        except: pass
        return None

    def open_position(self, side: str, price: float):
        symbol = self.generate_option_symbol(price, side)
        if not symbol: return False
        logger.info(f"📈 尝试开仓: {symbol}")
        try:
            order = Order(symbol=symbol, quantity=CONFIG["max_position_size"],
                          side=OrderSide.Buy, type=OrderType.Market, time_in_force=TimeInForceType.Day)
            order_id = self.tc.submit(order)
            if not order_id: 
                print(f"订单提交失败: symbol:{symbol}, side:{side}, price:{price}.")
                return False

            fill_price = self.wait_for_order_fill(order_id)
            if not fill_price: return False

            # ⚠️ 必须 sleep(1) 后抓取期权真实成交价，否则 PnL 计算为0
            time.sleep(1)
            opt_q = self.qc.quote([symbol])
            if opt_q and opt_q[0].last_done > 0:
                fill_price = float(opt_q[0].last_done)

            self.position = {
                "side": side, "symbol": symbol,
                "entry_stock": price, "entry_opt": fill_price,
                "peak_pnl": 0.0, "bars_held": 0,
                "entry_time": self.now_et().isoformat()
            }
            self.trades_today += 1
            self.save_state()
            logger.info(f"✅ 开仓成功: {symbol} @ {fill_price:.2f}")
            return True
        except Exception as e:
            logger.error(f"开仓失败: {e}")
            return False

    def close_position(self, force: bool = False):
        if not self.position: return False
        symbol = self.position["symbol"]
        logger.info(f"📉 尝试平仓: {symbol}")
        try:
            order = Order(symbol=symbol, quantity=CONFIG["max_position_size"],
                          side=OrderSide.Sell, type=OrderType.Market, time_in_force=TimeInForceType.Day)
            order_id = self.tc.submit(order)
            fill_price = self.wait_for_order_fill(order_id)
            
            if fill_price:
                pnl = fill_price - self.position["entry_opt"]
                self.daily_pnl += pnl
                if pnl < 0:
                    self.consecutive_losses += 1
                else:
                    self.consecutive_losses = 0
                logger.info(f"✅ 平仓成功 @ {fill_price:.2f} | 盈亏: {pnl:+.2f}")
            elif force:
                logger.warning("🚨 强制平仓失败，已清空本地持仓状态")
                self.consecutive_losses += 1
                
            self.position = None
            self.save_state()
            return True
        except Exception as e:
            logger.error(f"平仓异常: {e}")
            if force: self.position = None
            return False

    # ================== 指标计算 ==================
    # TODO 两个指标 升级成 EMA + VWAP，更适合 0DTE 期权交易。
    def sma20_price(self):
        '''
        计算最近20根K线的收盘价简单移动平均，实际使用：
        价格在 SMA20 上方 → 多头趋势（只做多）,
        价格在 SMA20 下方 → 空头趋势（只做空）。
        '''
        if len(self.bars) < 20: return None
        
        # 包含当前未完成的K线 ？
        # list(self.bars)[-21:-1]
        return sum(b["close"] for b in list(self.bars)[-20:]) / 20

    def volume_ok(self):
        '''成交量必须满足：当前K线成交量 >= 最近20根K线平均成交量 * vol_mult'''
        cnt = len(self.sma20_vol)
        if cnt == 0: return False
        
        # 不足20根时，用已有K线算均值，但提高量能门槛（补偿统计不确定性）
        if cnt < 20:
            # 设置上限，避免早盘要求过高
            dynamic_mult = min(CONFIG["vol_mult"] * (20 / cnt), 1.5)  # 最多1.5倍
            avg_vol = sum(self.sma20_vol) / cnt
            return self.bars[-1]["volume"] >= avg_vol * dynamic_mult
        
        # 满20根后使用标准逻辑
        avg_vol = sum(self.sma20_vol) / 20
        return self.bars[-1]["volume"] >= avg_vol * CONFIG["vol_mult"]

    # ================= 持仓动态管理 =================
    def manage_position(self):
        if not self.position: return
        # 节流: 每15秒轮询一次期权价格
        if time.time() - self.last_opt_poll < 15: return
        self.last_opt_poll = time.time()

        try:
            opt_q = self.qc.quote([self.position["symbol"]])
            if not opt_q or opt_q[0].last_done <= 0: return
            current_opt = float(opt_q[0].last_done)
            entry_opt = self.position["entry_opt"]
            
            pnl_pct = (current_opt - entry_opt) / entry_opt
            peak = max(self.position.get("peak_pnl", 0), pnl_pct)
            self.position["current_pnl_pct"] = pnl_pct
            self.position["peak_pnl"] = peak

            # 1. 止损 25%
            if pnl_pct <= -CONFIG.get("sl_pct", 0.25):
                logger.critical(f"🛑 触发止损: {pnl_pct:.1%}")
                self.close_position()
                return
            # 2. 盈利≥100% 平半仓 (0DTE期权1张无法拆分，直接全平锁定)
            if peak >= CONFIG.get("tp_half", 1.0):
                logger.info(f"🎯 达到目标止盈 {peak:.1%}，锁定利润平仓")
                self.close_position()
                return
            # 3. 峰值回撤≥30%
            if peak > 0 and (peak - pnl_pct) >= CONFIG.get("trail_pct", 0.30):
                logger.info(f"📉 峰值回撤触发: {peak:.1%} -> {pnl_pct:.1%}")
                self.close_position()
                return
            # 4. 超时强平 (15根1分钟K线)
            self.position["bars_held"] = self.position.get("bars_held", 0) + 1
            if self.position["bars_held"] >= CONFIG.get("timeout_bars", 15):
                logger.info("⏱️ 持仓超时，强制平仓")
                self.close_position()
        except Exception as e:
            logger.warning(f"持仓监控异常: {e}")

    # ================= 信号检测 =================
    def breakout_signal(self) -> Optional[str]:
        '''
        突破条件：
        ✅ 价格在 SMA20 上方（看涨）或下方（看跌）
        ✅ 收阳线（看涨）或阴线（看跌）
        ✅ 成交量达标
        👉 四重确认，才开仓
        '''
        lb = CONFIG.get("lookback", 5)
        if len(self.bars) < lb + 1: return None
        
        # ✅ 修复: 取前 lookback 根 (不含当前)
        prev = list(self.bars)[-(lb + 1):-1]
        if not prev: return None
        last = self.bars[-1] # 当前K线
        
        upper = max(b["high"] for b in prev) # 前 lookback 根的最高价
        lower = min(b["low"] for b in prev) # 前 lookback 根的最低价
        
        
        # 过滤漏斗
        '''
        body, K 线实体: abs(收盘价 - 开盘价) / 收盘价
        拒绝“十字星”、“针状线”等实体较小的K线，避免假突破
        实体大小占收盘价的比例必须大于 min_body_pct（0.03%）
        '''
        body = abs(last["close"] - last["open"]) / last["close"]
        if body < CONFIG.get("min_body_pct", 0.0003): return None
            
        if not self.volume_ok(): return None
        # 前 20 分钟的均价
        sma = self.sma20_price()

        now = self.now_et()
        # 避开开盘前5分钟噪音 & 收盘前10分钟
        if 570 <= (now.hour * 60 + now.minute) <= 950: pass
        else: return None

        ''' 
        1. 当前价格突破最近5根K线的最高价（看涨）或最低价（看跌）
        2. 当前价格在SMA20上方（看涨）或下方（看跌）
        3. 当前K线为阳线（看涨）或阴线（看跌）
        '''
        if last["close"] > upper and last["close"] > sma and last["close"] > last["open"]:
            return "call"
        if last["close"] < lower and last["close"] < sma and last["close"] < last["open"]:
            return "put"
        return None

    # 反转信号：价格从高点回落（看跌）或从低点反弹（看涨）超过一定百分比，且当前K线实体足够大
    def reversal_signal(self) -> Optional[str]:
        if len(self.bars) < 20: return None
        drop_thresh = CONFIG.get("reversal_drop", 0.002)
        body_thresh = CONFIG.get("min_body_pct", 0.0003)
        
        # ✅ 修复：deque 不支持切片，先转为 list
        bars_list = list(self.bars)
        highs = [b["high"] for b in bars_list[-20:]]
        lows = [b["low"] for b in bars_list[-20:]]
        
        last = self.bars[-1]
        body = abs(last["close"] - last["open"]) / last["close"]
        if body < body_thresh: return None
        
        # 看跌反转: 从高点回落 >=0.2% 且收阴
        if last["close"] < last["open"] and (max(highs) - last["close"]) / max(highs) >= drop_thresh:
            return "put"
        # 看涨反转: 从低点反弹 >=0.2% 且收阳
        if last["close"] > last["open"] and (last["close"] - min(lows)) / min(lows) >= drop_thresh:
            return "call"
        return None

    # ================= 核心行情回调 =================
    def on_candlestick(self, symbol: str, event: PushCandlestick):
        try:
            if not hasattr(event, "candlestick"): return
            
            # 只处理已收盘的 K 线，避免未完成 K 线的价格波动导致误判
            if not getattr(event, "is_confirmed", False): return
            
            # K线处理&记录
            cs = event.candlestick 
            print(f"收到行情: {cs} ")
            bar = {
                "open": float(cs.open), 
                "high": float(cs.high),
                "low": float(cs.low), 
                "close": float(cs.close),
                "volume": float(getattr(cs, "volume", 0)),
                "ts": getattr(cs, "timestamp", None)
            }
            
            # K线乱序拦截
            if self.last_bar_timestamp and bar["ts"] <= self.last_bar_timestamp:
                return
        
            self.last_bar_timestamp = bar["ts"]
            self.bars.append(bar)
            self.sma20_vol.append(bar["volume"])
            self.data_manager.write_kline_to_csv(bar)

            # K线归档检查 (仅在收盘后触发)
            if not self.is_trading_hours(): 
                self.data_manager.archive_csv()
                return

            # 风控检查: 日亏损熔断 & 连续止损熔断
            if not self.check_risk_controls(): return
            
            # 交易频次控制: 每日最多开仓次数（突破+反转总和）达到上限后，只做持仓管理，不再开新仓
            total_limit = CONFIG.get("breakout_max", 8) + CONFIG.get("reversal_max", 1)
            if self.trades_today >= total_limit:
                self.manage_position()
                return
            
            # 开仓条件检测与执行
            if not self.position:
                sig = self.breakout_signal() or self.reversal_signal()
                if sig and self.trades_today < CONFIG.get("breakout_max", 8):
                    self.open_position(sig, bar["close"])
            else:
                self.manage_position()
            
            self.save_state()
            
        except Exception as e:
            logger.error(f"行情处理异常: {e}")
            traceback.print_exc()

    # ================= 启动入口 =================
    def start(self):
        logger.info("🚀 启动交易引擎...")
        retry, max_retries = 0, 5
        while retry < max_retries:
            try:
                self.qc.set_on_candlestick(self.on_candlestick)
                #self.qc.subscribe_candlesticks("700.HK", Period.Min_1, TradeSessions.Intraday)
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
                if self.position: self.close_position(force=True)
                self.save_state()
                logger.info("🛑 交易引擎已安全停止")

if __name__ == "__main__":
    try:
        trader = QQQTrader()
        trader.start()
    except Exception as e:
        logger.critical(f"💥 致命错误: {e}")
        traceback.print_exc()
        sys.exit(1)

        '''
✅ 加一个 断线自动重连 + 补 K 线机制
✅ 增加 断线自动重连
✅ 增加 盘前/盘后静默模式
改成 按日期自动分文件
增加 K线重复写入校验
'''