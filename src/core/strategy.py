#!/usr/bin/env python3
"""
QQQ 0DTE 核心策略模块 v8.5
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
    GAMMA_RISK = "GAMMA_RISK"
    EOD_FORCE = "EOD_FORCE"
    
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
        ''' 开盘 1 分钟，收盘前 30 分钟不交易 '''
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

        market_open = bar_ts.replace(hour=9, minute=31, second=0, microsecond=0)
        market_close = bar_ts.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= bar_ts < market_close
    
    def generate_signal(self) -> Optional[str]:
        if len(self.bars) < 2: return None
        
        # 1. 先检查 ATR 过滤器(若 ATR 低于阈值，说明市场死水一潭，放弃开仓)
        if not self._check_atr_filter(): return None
        
        # 2. 检查交易时间
        if not self.is_trading_hours(self.bars[-1]["ts"]): return None

        # 3. 双信号引擎：突破和反转
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
        
        if cnt < 20:
            # 不足20根时动态提高门槛，上限1倍
            base_volume = 50000
            curr_volume = self.bars[-1]["volume"]
            dynamic_mult = min(CONFIG["vol_mult"] * (20 / cnt), 1)
            avg_vol = sum(self.sma20_vol) / cnt
            is_ok = curr_volume >= base_volume and curr_volume >= avg_vol * dynamic_mult
            #if is_ok: logger.info(f"valume_ok: {self.bars[-1]['ts']} -> {self.bars[-1]['volume']} >= {avg_vol * dynamic_mult}")
            return is_ok
        
        # 满20根后使用标准逻辑
        avg_vol = sum(self.sma20_vol) / 20
        is_ok = self.bars[-1]["volume"] >= avg_vol * CONFIG["vol_mult"]
        #if is_ok: logger.info(f"valume_ok: {self.bars[-1]['ts']} -> {self.bars[-1]['volume']} >= {avg_vol * CONFIG['vol_mult']}")
        return is_ok

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
            #logger.info(f"✅ breakout Call({last['ts']}) -> {last['close']} > {upper}, {last['close']} > {sma}, {last['close']} > {last['open']}")
            return "call"
        if last["close"] < lower and last["close"] < sma and last["close"] < last["open"]:
            #logger.info(f"✅ breakout Put({last['ts']}) -> {last['close']} < {lower}, {last['close']} < {sma}, {last['close']} < {last['open']}")
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
            #logger.info(f"✅ reversal Put({last['ts']}) -> {last['close']} < {last['open']}, {(max_h - last['close']) / max_h} >= {drop_thresh}")
            return "put"
        if min_l > 0 and last["close"] > last["open"] and (last["close"] - min_l) / min_l >= drop_thresh:
            # 看涨反转: 从低点反弹 >=0.2% 且收阳
            #logger.info(f"✅ reversal Call({last['ts']}) -> {last['close']} > {last['open']}, {(last['close'] - min_l) / min_l} >= {drop_thresh}")
            return "call"
        return None

    def _check_atr_filter(self) -> bool:
        # 过滤低波动行情，降低交易频率
        # 最少需要 10 根 K 线
        if len(self.bars) < 10: return True
        
        # 计算最近 10 根 K 线的平均波幅
        atr_avg = sum(abs(bar["high"] - bar["low"]) for bar in list(self.bars)[-10:]) / 10
        atr_pct = atr_avg / self.bars[-1]["close"]
        
        # ✅ 使用百分比阈值，动态适应价格水平
        # 0DTE 期权在 1 分钟级别，0.03% 是一个合理的起点
        THRESHOLD_PCT = 0.0003
        if atr_pct < THRESHOLD_PCT:
            # logger.info(f"⏸️ ATR过滤: 波动率{atr_pct:.4%} < 阈值{THRESHOLD_PCT:.4%}")
            return False
        
        return True

    # ================= 持仓动态管理 =================
    def check_position_exit(self, current_opt_price: float, current_stock: float) -> Optional[ExitReason]:
        if not self.position: return None
        
        entry_opt = self.position["entry_opt"]
        side = self.position["side"]
        entry_stock = self.position["entry_stock"]
        
        # 1. 标的硬止损 (最高优先级，无视期权报价，期权价格止损容易被 IV 欺骗)
        # 最大允许标的反向波动 0.4%
        underlying_sl_pct = 0.004 
        if side == "call" and current_stock <= entry_stock * (1 - underlying_sl_pct):
            logger.critical(f"🚨 触发标的硬止损 (Call): 标的跌至 {current_stock:.2f}, 入场 {entry_stock:.2f}")
            return ExitReason.STOP_LOSS
        if side == "put" and current_stock >= entry_stock * (1 + underlying_sl_pct):
            logger.critical(f"🚨 触发标的硬止损 (Put): 标的涨至 {current_stock:.2f}, 入场 {entry_stock:.2f}")
            return ExitReason.STOP_LOSS
        
        # 如果期权无报价
        if current_opt_price is None or current_opt_price <= 0:
            return None
        
        # 1. 计算当前盈亏比例
        current_pnl_pct = (current_opt_price - entry_opt) / entry_opt

        # 2. 时间止损(无视期权报价)
        self.position["bars_held"] = self.position.get("bars_held", 0) + 1
        bars_held = self.position["bars_held"]
        max_bars = CONFIG.get("timeout_bars", 15)  # 最多持仓15分钟
        timeout_pnl_pct = CONFIG.get("timeout_pnl_pct", 0.10) # 持仓超时盈利要求
        if bars_held >= max_bars and current_pnl_pct < timeout_pnl_pct: # 15分钟后若盈利不足10%，直接走人
            logger.info(f"超时平仓触发：bars_held={bars_held}, 最多持仓={max_bars}, 盈利：{current_pnl_pct}")
            return ExitReason.TIMEOUT

        # 3. 更新峰值盈亏（记录最高盈利）
        peak_pnl = self.position.get("peak_pnl", 0)
        if current_pnl_pct > peak_pnl:
            self.position["peak_pnl"] = current_pnl_pct
            peak_pnl = current_pnl_pct
        
        # 4. 固定止损（期权价格层面）
        sl_threshold = CONFIG.get("sl_pct", 0.15)  # 例如 0.15 = 15%
        if current_pnl_pct <= -sl_threshold:
            logger.info(f"📉 固定止损触发: 止损阈值={sl_threshold:.1%}, 当前={current_pnl_pct:.1%}")
            return ExitReason.STOP_LOSS
        
        # 5. 固定止盈
        tp_threshold = CONFIG.get("tp_half", 1.0)  # 例如 1.0, 即 1 倍
        if current_pnl_pct >= tp_threshold:
            return ExitReason.TAKE_PROFIT
        
        # 6. 移动止损（核心修复）
        trail_activate = CONFIG.get("trail_activate", 0.05)  # 激活阈值 5%
        trail_drop = CONFIG.get("trail_pct", 0.15)  # 回撤比例 15%
        
        if peak_pnl > trail_activate:
            # ✅ 正确计算回撤比例
            drawdown = (peak_pnl - current_pnl_pct) / peak_pnl
            if drawdown >= trail_drop:
                logger.info(f"📉 移动止损触发: 峰值={peak_pnl:.1%}, 当前={current_pnl_pct:.1%}, 回撤={drawdown:.1%}")
                return ExitReason.TRAILING_STOP
        
        # 7. Gamma 风险检查
        #if self.check_gamma_risk(current_stock):
        #    return ExitReason.GAMMA_RISK
        
        return None

    def check_gamma_risk(self, current_stock: float) -> bool:
        if not self.position: return False
        strike_offset = CONFIG.get("offset", 2.0)
        entry_stock = self.position["entry_stock"]
        side = self.position["side"]
        
        # 计算开仓时的实际行权价
        strike = round(entry_stock + strike_offset) if side == "call" else round(entry_stock - strike_offset)
        
        # 计算当前价与行权价的距离百分比
        distance_pct = abs(current_stock - strike) / strike
        # 仅当距离非常近（例如0.1%）时，进一步判断方向
        if distance_pct < 0.001:  # 进入极危险区
            if side == "call":
                # 做多Call的风险：正股价格跌到行权价附近
                if current_stock <= strike:  return True
            elif side == "put":
                # 做多Put的风险：正股价格涨到行权价附近
                if current_stock >= strike: return True
        return False

    def open_position(self, side: str, entry_stock: float, entry_opt: float, symbol: str, entry_ts: datetime=None):
        """记录开仓状态"""
        self.position = {
            "side": side, "symbol": symbol,
            "entry_stock": entry_stock, "entry_opt": entry_opt,
            "entry_ts": entry_ts,
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
            "exit_stock": self.bars[-1].get("close") if self.bars else None,
            "exit_ts": self.bars[-1].get("ts") if self.bars else None
        }
        self.position = None
        return trade

    @staticmethod
    def generate_option_symbol(price: float, side: str, current_ts: datetime) -> str:
        """生成0DTE期权合约代码（纯函数）"""
        if price <= 0: return None

        # 期权行权价​根据标的价格和偏移量计算，向上取整（看涨）或向下取整（看跌），单位为0.01美元
        # 期权行权价 = 距离现货 ±2 美元的 0DTE 虚值期权行权价
        strike = round(price + CONFIG["offset"]) if side == "call" else round(price - CONFIG["offset"])
        exp = current_ts.strftime("%y%m%d")
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
    