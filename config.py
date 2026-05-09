#!/usr/bin/env python3
"""
QQQ 交易系统配置文件
- 集中管理所有配置参数
- 自动创建所需目录
"""

import os
from zoneinfo import ZoneInfo

# ===================== 目录配置 =====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# 自动创建目录
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, "records"), exist_ok=True)

# ===================== 基础配置 =====================
SYMBOL = "QQQ.US"
TIMEZONE = "America/New_York"
TZ_ET = ZoneInfo(TIMEZONE)

# ===================== 策略参数 =====================
STRATEGY_CONFIG = {
    "lookback": 5,
    "sma_period": 20,
    "vol_mult": 0.8,
    "min_body_pct": 0.0003,
    "breakout_max": 8,  # 突破最大持仓数量
    "reversal_max": 1,  # 反转最大持仓数量
    "sl_pct": 0.25,  # 止损百分比
    "tp_half": 1.0,  # 盈亏比达到1倍时止盈一半
    "trail_pct": 0.3,  # 盈亏比达到0.3倍时开始移动止损
    "timeout_bars": 15,
    "offset": 2.0,
}

# ===================== 风控参数 =====================
RISK_CONFIG = {
    "max_daily_loss": -500.0,  # 单日最大亏损额度​
    "max_consecutive_losses": 3,  # 最大连续止损次数
    "order_check_timeout": 10,  # 订单检查超时时间（秒）
    "order_check_interval": 1,  # 订单检查间隔（秒）
    "max_position_size": 1,  # 最大持仓数量
}

# ===================== 系统配置 =====================
SYSTEM_CONFIG = {
    "log_level": "INFO",
    "log_file": os.path.join(LOGS_DIR, "trader.log"),
    "state_file": os.path.join(DATA_DIR, "state.json"),
    "csv_file": os.path.join(DATA_DIR, "today.csv"),
    "records_dir": os.path.join(DATA_DIR, "records"),
    "check_interval": 20,
}

# ===================== 环境配置 =====================
ENVIRONMENT = os.getenv("TRADING_ENV", "production")


# ===================== 完整配置合并 =====================
def get_config():
    config = {
        "symbol": SYMBOL,
        "timezone": TIMEZONE,
        "tz_et": TZ_ET,
        **STRATEGY_CONFIG,
        **RISK_CONFIG,
        **SYSTEM_CONFIG,
        "environment": ENVIRONMENT,
    }

    if ENVIRONMENT == "development":
        config.update(
            {
                "max_daily_loss": -1000.0,
                "log_level": "DEBUG",
            }
        )

    return config


# ===================== 配置验证 =====================
def validate_config(config):
    errors = []

    # 策略参数验证
    if config["lookback"] <= 0:
        errors.append("lookback 必须大于0")
    if config["sma_period"] <= 0:
        errors.append("sma_period 必须大于0")
    if config["vol_mult"] <= 0 or config["vol_mult"] > 1:
        errors.append("vol_mult 必须在 (0,1] 之间")
    if config["min_body_pct"] <= 0:
        errors.append("min_body_pct 必须大于0")
    if config["sl_pct"] <= 0 or config["sl_pct"] >= 1:
        errors.append("sl_pct 必须在0-1之间")
    if config["breakout_max"] <= 0:
        errors.append("breakout_max 必须为正整数")
    if config["reversal_max"] <= 0:
        errors.append("reversal_max 必须为正整数")
    if config["offset"] <= 0:
        errors.append("offset 必须大于0")

    # 风控参数验证
    if config["max_daily_loss"] >= 0:
        errors.append("max_daily_loss 必须为负数")
    if config["max_consecutive_losses"] <= 0:
        errors.append("max_consecutive_losses 必须为正整数")
    if config["order_check_timeout"] <= 0:
        errors.append("order_check_timeout 必须为正数")
    if config["order_check_interval"] <= 0:
        errors.append("order_check_interval 必须为正数")
    if config["max_position_size"] <= 0:
        errors.append("max_position_size 必须为正整数")

    # 系统参数验证
    if config["check_interval"] <= 0:
        errors.append("check_interval 必须为正数")

    return errors


# ===================== 导出配置 =====================
CONFIG = get_config()

# 验证配置
validation_errors = validate_config(CONFIG)
if validation_errors:
    raise ValueError(f"配置验证失败: {validation_errors}")

# 打印配置摘要
if __name__ == "__main__":
    print("=" * 50)
    print("QQQ 交易系统配置")
    print("=" * 50)
    for key, value in CONFIG.items():
        if key != "tz_et":
            print(f"{key:20}: {value}")
    print("=" * 50)
