#!/usr/bin/env python3
"""
QQQ 0DTE 核心策略模块
✅ 纯数学/状态逻辑，不依赖长桥 SDK / 文件系统 / 网络
✅ 实盘与回测 100% 共用，修改一处全局生效
"""

from collections import deque
from enum import StrEnum
from typing import Optional, Dict, Any
from datetime import datetime
from src.config import CONFIG
from src.logger import get_logger

logger = get_logger("core.strategy")

class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS" # 止损
    TAKE_PROFIT = "TAKE_PROFIT" # 止盈
    TRAILING_STOP = "TRAILING_STOP" # 移动止损
    TIMEOUT = "TIMEOUT" # 时间止损（持仓超过预设周期）
    
class QQQStrategy:
    def __init__(self):
        # K线与指标缓冲
        self.bars = deque(maxlen=200)
        self.sma20_vol = deque(maxlen=20)
        self.last_bar_ts = None
        
        # 交易状态
        self.position: Optional[Dict[str, Any]] = None
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.emergency_stopped = False

    def reset_daily(self):
        """每日状态重置（由适配器按日期切换时调用）"""
        self.trades_today = 0
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.emergency_stopped = False

    def add_bar(self, bar: Dict[str, Any]) -> bool:
        """注入新K线并更新缓冲（含时间戳防乱序校验）"""
        current_ts = bar.get("ts")
        if self.last_bar_ts and current_ts and current_ts <= self.last_bar_ts:
            logger.warning(f"⏰ 乱序或重复K线被丢弃: {current_ts} <= {self.last_bar_ts}")
            return False  # 乱序/重复K线拦截
        
        self.last_bar_ts = current_ts
        self.bars.append(bar)
        self.sma20_vol.append(bar.get("volume", 0))
        return True

    # ================= 风控检查 =================
    def check_risk(self) -> bool:
        if self.emergency_stopped: return False
        if self.daily_pnl <= CONFIG.get("max_daily_loss", -500):
            logger.critical(f"🚨 触发日亏损熔断: {self.daily_pnl:.2f}")
            self.emergency_stopped = True
            return False
        if self.consecutive_losses >= CONFIG.get("max_consecutive_losses", 3):
            logger.critical(f"🚨 触发连续止损熔断: {self.consecutive_losses}次")
            self.emergency_stopped = True
            return False
        return True
    
    # ================= 信号检测 =================
    def is_trading_hours(self, raw_ts: Optional[datetime] = None) -> bool: 
        if raw_ts is None:
            bar_ts = datetime.now(CONFIG["tz_et"])
        elif isinstance(raw_ts, (int, float)):
            # Unix timestamp（int / float）
            bar_ts = datetime.fromtimestamp(raw_ts, tz=CONFIG["tz_et"])
        elif isinstance(raw_ts, datetime):
            # Unix timestamp（int / float）
            bar_ts = raw_ts.astimezone(CONFIG["tz_et"]) if raw_ts.tzinfo else raw_ts.replace(tzinfo=CONFIG["tz_et"])
        else:
            bar_ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(CONFIG["tz_et"])
            if bar_ts.weekday() >= 5: return False

        market_open = bar_ts.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = bar_ts.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= bar_ts <= market_close
    
    def generate_signal(self) -> Optional[str]:
        if len(self.bars) < 2: return None
        
        # 取K线时间适用回测，交易窗口过滤 (09:30 - 15:50 ET)
        '''
        ts = self.bars[-1]["ts"]
        mins = ts.hour * 60 + ts.minute
        if not (9 * 60 + 30 <= mins <= 16 * 60 - 10):
            return None
        '''
        if not self.is_trading_hours(self.bars[-1]["ts"]): return None

        # 双信号引擎
        if sig := self._breakout_signal(): return sig
        if sig := self._reversal_signal(): return sig
        return None

    def _sma20_price(self) -> Optional[float]:
        """
        计算最近20根K线的收盘价简单移动平均，实际使用：
        价格在 SMA20 上方 → 多头趋势（只做多）,
        价格在 SMA20 下方 → 空头趋势（只做空）。
        """
        if len(self.bars) < 20: return None
        return sum(b["close"] for b in list(self.bars)[-20:]) / 20

    def _volume_ok(self) -> bool:
        """成交量必须满足：当前K线成交量 >= 最近20根K线平均成交量 * vol_mult"""
        cnt = len(self.sma20_vol)
        if cnt == 0: return False
        # 不足20根时动态提高门槛，上限1.5倍
        if cnt < 20:
            dynamic_mult = min(CONFIG["vol_mult"] * (20 / cnt), 1.5)
            avg_vol = sum(self.sma20_vol) / cnt
            return self.bars[-1]["volume"] >= avg_vol * dynamic_mult
        
        # 满20根后使用标准逻辑
        avg_vol = sum(self.sma20_vol) / 20
        return self.bars[-1]["volume"] >= avg_vol * CONFIG["vol_mult"]

    def _breakout_signal(self) -> Optional[str]:
        """
        突破条件：👉 四重确认，才开仓
        ✅ 价格在 SMA20 上方（看涨）或下方（看跌）
        ✅ 收阳线（看涨）或阴线（看跌）
        ✅ 成交量达标（当前K线成交量 >= 最近20根K线平均成交量 * vol_mult）
        """
        lb = CONFIG.get("lookback", 5)
        if len(self.bars) < lb + 1: return None
        
        # ✅ 严格取前 lookback 根（不含当前K线）
        prev = list(self.bars)[-(lb + 1):-1]
        if not prev: return None
        last = self.bars[-1]
        
        upper = max(b["high"] for b in prev)
        lower = min(b["low"] for b in prev)
        body = abs(last["close"] - last["open"]) / last["close"]

        # 过滤漏斗
        """
        body, K 线实体: abs(收盘价 - 开盘价) / 收盘价
        拒绝“十字星”、“针状线”等实体较小的K线，避免假突破
        实体大小占收盘价的比例必须大于 min_body_pct（0.03%）
        """
        if body < CONFIG.get("min_body_pct", 0.0003): return None
        if not self._volume_ok(): return None
        
        sma = self._sma20_price()
        if sma is None: return None
        
        """
        1. 当前价格突破最近5根K线的最高价（看涨）或最低价（看跌）
        2. 当前价格在SMA20上方（看涨）或下方（看跌）
        3. 当前K线为阳线（看涨）或阴线（看跌）
        """
        if last["close"] > upper and last["close"] > sma and last["close"] > last["open"]:
            return "call"
        if last["close"] < lower and last["close"] < sma and last["close"] < last["open"]:
            return "put"
        return None

    def _reversal_signal(self) -> Optional[str]:
        if len(self.bars) < 20: return None
        drop_thresh = CONFIG.get("reversal_drop", 0.002)
        body_thresh = CONFIG.get("min_body_pct", 0.0003)
 
        bars_list = list(self.bars)
        highs = [b["high"] for b in bars_list[-20:]]
        lows = [b["low"] for b in bars_list[-20:]]
        last = self.bars[-1]
        body = abs(last["close"] - last["open"]) / last["close"]
        if body < body_thresh: return None
        
        max_h, min_l = max(highs), min(lows)
        if max_h > 0 and last["close"] < last["open"] and (max_h - last["close"]) / max_h >= drop_thresh:
            # 看跌反转: 从高点回落 >=0.2% 且收阴
            return "put"
        if min_l > 0 and last["close"] > last["open"] and (last["close"] - min_l) / min_l >= drop_thresh:
            # 看涨反转: 从低点反弹 >=0.2% 且收阳
            return "call"
        return None

    # ================= 持仓动态管理 =================
    def check_position_exit(self, current_opt_price: float) -> Optional[str]:
        """检查持仓退出条件，返回退出原因或 None"""
        if not self.position: return None
        entry_opt = self.position["entry_opt"]
        pnl_pct = (current_opt_price - entry_opt) / entry_opt # 当前盈亏比
        peak = max(self.position.get("peak_pnl", 0), pnl_pct) # 最新盈亏比峰值
        self.position["peak_pnl"] = peak

        # 止损：亏损达到预设百分比（如25%）立即止损
        if pnl_pct <= -CONFIG.get("sl_pct", 0.25): return ExitReason.STOP_LOSS
        # 止盈：盈亏比达到1倍时止盈全部，达到0.5倍时止盈一半（可选）
        if peak >= CONFIG.get("tp_half", 1.0): return ExitReason.TAKE_PROFIT
        # 移动止损：盈亏比达到0.3倍时开始移动止损，保护盈利回撤不超过0.3倍
        if peak > 0 and (peak - pnl_pct) >= CONFIG.get("trail_pct", 0.30): return ExitReason.TRAILING_STOP
        
        self.position["bars_held"] = self.position.get("bars_held", 0) + 1
        if self.position["bars_held"] >= CONFIG.get("timeout_bars", 15): return ExitReason.TIMEOUT
        return None

    def open_position(self, side: str, entry_stock: float, entry_opt: float, symbol: str):
        """记录开仓状态"""
        self.position = {
            "side": side, "symbol": symbol,
            "entry_stock": entry_stock, "entry_opt": entry_opt,
            "peak_pnl": 0.0, # 盈亏比峰值（移动止损基准）
            "bars_held": 0 # 持仓周期计数器（用于时间止损）
        }
        self.trades_today += 1

    def close_position(self, exit_opt: float) -> Dict[str, Any]:
        """平仓并返回交易记录"""
        pnl = exit_opt - self.position["entry_opt"]
        self.daily_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        
        trade = {
            **self.position, "exit_opt": round(exit_opt, 4),
            "pnl": round(pnl, 4), "pnl_pct": round(pnl / self.position["entry_opt"], 4),
            "exit_ts": self.bars[-1].get("ts") if self.bars else None
        }
        self.position = None
        return trade

    @staticmethod
    def generate_option_symbol(price: float, side: str) -> str:
        """生成0DTE期权合约代码（纯函数）"""
        if price <= 0: return None

        # 期权行权价​根据标的价格和偏移量计算，向上取整（看涨）或向下取整（看跌），单位为0.01美元
        # 期权行权价 = 距离现货 ±2 美元的 0DTE 虚值期权行权价
        strike = round(price + CONFIG["offset"]) if side == "call" else round(price - CONFIG["offset"])
        exp = datetime.now(CONFIG["tz_et"]).strftime("%y%m%d")
        otype = "C" if side == "call" else "P"
        return f"QQQ{exp}{otype}{strike * 1000:06d}.US"
    
if __name__ == "__main__":
    # 简单测试
    strategy = QQQStrategy()
    print(strategy.is_trading_hours("2026-05-08T14:04:00Z"))
    timestamp=datetime(2026, 5, 8, 14, 0, 0)
    print(strategy.is_trading_hours(timestamp))

    from longbridge.openapi import Order, OrderSide, OrderType, TimeInForceType
    o = Order(stock_name='QQQ260509C452000.US', quantity=1, side=OrderSide.Buy, order_type=OrderType.MO, time_in_force=TimeInForceType.Day)
    print('✅ Order 实例化成功:', o.symbol, o.order_type)
    