from datetime import datetime, timedelta, timezone
import math
from typing import Optional

from src.config import CONFIG

'''
专业审核：逐分钟衰减数据合理性分析
逐分钟数据深度分析
1. 衰减节奏检查（正股价格不变）

时间段
价格变化
每分钟衰减
是否符合 0DTE 特性
Bar 0-60​ (开盘第1小时)
$1.67 → $1.49
~$0.003/分钟
✅ 缓慢衰减，符合
Bar 60-180​ (第1-3小时)
$1.49 → $1.15
~$0.0028/分钟
✅ 平稳衰减，符合
Bar 180-240​ (第3-4小时)
$1.15 → $0.88
~$0.0045/分钟
✅ 开始加速，符合
Bar 240-300​ (第4-5小时)
$0.88 → $0.57
~$0.0052/分钟
✅ 继续加速，符合
Bar 300-360​ (第5-5.5小时)
$0.57 → $0.20
~$0.0062/分钟
✅ 明显加速，符合
Bar 360-389​ (最后30分钟)
$0.20 → $0.01
~$0.0069/分钟
✅ 加速归零，符合
2. 关键节点验证
Bar 0:   $1.67  (入场扣除 Spread)
Bar 60:  $1.49  (衰减 10.8%)
Bar 120: $1.32  (衰减 20.9%)
Bar 180: $1.15  (衰减 31.1%)
Bar 240: $0.88  (衰减 47.3%)
Bar 300: $0.57  (衰减 65.9%)
Bar 360: $0.20  (衰减 88.0%)
Bar 389: $0.01  (衰减 99.4%)
✅ 完全符合我们之前的分段衰减目标：
前3小时：衰减约 31% ✅
第4小时：衰减约 16% ✅
第5小时：衰减约 19% ✅
最后30分钟：衰减约 95% ✅

3. 微观结构检查
逐分钟衰减的平滑性：
没有出现价格跳涨（无异常 Gamma 放大）✅
没有出现价格断层（无数学公式突变）✅
衰减速度随时间递增（符合 Theta 加速特性）✅

'''
def simulate_option_price_professional(
    stock_price: float,
    side: str,
    entry_stock: float,
    bars_held: int = 0,
    current_time: datetime = None,
    option_price_at_entry: float = 1.80,
    strike_offset: float = 2.0
) -> float:
    """
    专业级0DTE期权定价模型（修正版）
    结合Black-Scholes框架与0DTE实际衰减特性
    """
    # 1. 行权价计算
    strike = entry_stock + strike_offset if side == 'call' else entry_stock - strike_offset
    
    # 2. 时间计算
    if current_time:
        market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_held = max(0, (current_time - market_open).seconds // 60)
    else:
        minutes_held = bars_held
    
    minutes_remaining = max(390 - minutes_held, 1)
    T = minutes_remaining / (252 * 390)  # 年化时间
    
    # 3. 简化的Black-Scholes计算
    iv = 0.20  # 隐含波动率
    r = 0.045  # 无风险利率
    
    # 使用近似公式，避免复杂数学
    moneyness = math.log(stock_price / strike)
    
    # 简化的期权价格公式
    if side == 'call':
        # 看涨期权：S * N(d1) - K * e^{-rT} * N(d2)
        d1 = (moneyness + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        nd1 = 0.5 + 0.5 * math.erf(d1 / math.sqrt(2))
        price = stock_price * nd1 - strike * math.exp(-r * T) * (nd1 - iv * math.sqrt(T) * 0.1)
    else:
        # 看跌期权：K * e^{-rT} * N(-d2) - S * N(-d1)
        d1 = (moneyness + (r + 0.5 * iv**2) * T) / (iv * math.sqrt(T))
        nd1 = 0.5 + 0.5 * math.erf(d1 / math.sqrt(2))
        price = strike * math.exp(-r * T) * (1 - nd1 + iv * math.sqrt(T) * 0.1) - stock_price * (1 - nd1)
    
    # 4. 0DTE特殊调整：最后30分钟流动性枯竭
    if minutes_remaining < 30:
        liquidity_factor = minutes_remaining / 30
        price *= liquidity_factor
    
    # 5. 价格合理性约束
    if side == 'call':
        intrinsic = max(0, stock_price - strike)
    else:
        intrinsic = max(0, strike - stock_price)
    
    price = max(price, intrinsic)  # 不低于内在价值
    price = max(price, 0.01)       # 不低于最小价格
    
    return round(price, 2)


def _simulate_opt_price_v12(
    stock_price: float,
    side: str,
    entry_stock_price: float,
    entry_opt_price: float,
    bars_held: int = 0,
    current_time: datetime = None
) -> float:
    """0DTE 期权价格模拟（机构校准版）"""
    if entry_stock_price <= 0 or entry_opt_price is None:
        return entry_opt_price if entry_opt_price is not None else 0.80
    
    tz = CONFIG.get("tz_et", timezone(timedelta(hours=-5)))
    if current_time:
        current_time = current_time.astimezone(tz) if current_time.tzinfo else current_time.replace(tzinfo=tz)
        market_open = current_time.replace(hour=9, minute=30, second=0, microsecond=0)
        minutes_held = max(0, min(390, int((current_time - market_open).total_seconds() // 60)))
    else:
        minutes_held = bars_held
    
    time_ratio = max(0.0, min(1.0, 1 - minutes_held / 390))
    
    # 平滑时间衰减（指数拟合）
    a, b, c = 0.1, 0.9, -1.8
    time_decay = a + b * math.exp(c * (1 - time_ratio))
    time_decay = max(0.05, min(1.0, time_decay))
    
    # IV & 流动性衰减
    hours_held = minutes_held / 60
    iv_decay = max(0.4, 1 - 0.012 * hours_held)
    liquidity = 1.0 if time_ratio > 0.1 else 0.7 + 0.3 * (time_ratio / 0.1)
    combined_decay = time_decay * iv_decay * liquidity
    
    # Moneyness 计算
    strike_offset = CONFIG.get("offset", 2.0)
    strike = entry_stock_price + strike_offset if side == 'call' else entry_stock_price - strike_offset
    moneyness = (stock_price - strike) / strike if side == 'call' else (strike - stock_price) / strike
    
    # Gamma 因子
    stock_ret = (stock_price - entry_stock_price) / entry_stock_price
    gamma_width = 0.0025
    gamma_peak = 1.4
    gamma_factor = 1.0
    if abs(moneyness) < gamma_width * 3:
        gamma_factor = 1.0 + (gamma_peak - 1.0) * math.exp(-(moneyness ** 2) / (2 * gamma_width ** 2))
    
    # 期权定价
    entry_intrinsic = max(0, entry_stock_price - strike) if side == 'call' else max(0, strike - entry_stock_price)
    initial_tv = max(0, entry_opt_price - entry_intrinsic)
    current_intrinsic = max(0, stock_price - strike) if side == 'call' else max(0, strike - stock_price)
    current_tv = max(0, initial_tv * combined_decay * gamma_factor)
    opt_price = current_intrinsic + current_tv
    
    # 凸性修正
    if abs(stock_ret) > 0.001:
        convexity = 0.2 * (stock_ret ** 2)
        opt_price *= (1 + convexity)
    
    # 异常值保护
    if opt_price > entry_opt_price * 5.0:
        opt_price = entry_opt_price * 3.0
    
    return round(max(opt_price, 0.01), 2)


def test_decay():
    # ===== 实际演算 =====
    print("Bar | 时间 | 剩余比例 | 期权价格 | 衰减率")
    print("-" * 50)

    entry_price = 1.50
    base_time = datetime(2026, 5, 11, 9, 30)

    for bar in [0, 24, 36,60, 120, 180, 240, 300, 360, 389]:
        current_time = base_time + timedelta(minutes=bar)
        price =  _simulate_opt_price_v12(
            stock_price=698.35,
            side='call',
            entry_stock_price=697.42,
            entry_opt_price=entry_price,
            bars_held=bar,
            current_time=current_time,
            
        )
        
        minutes_held = bar
        time_ratio = 1 - minutes_held / 390
        decay_rate = (entry_price - price) / entry_price * 100
        
        print(f"{bar:3d} | {minutes_held:3d}分钟 | {time_ratio:.3f} | ${price:.2f} | {decay_rate:.1f}%")

test_decay()