import builtins
from datetime import datetime
import os
import sys

# sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import longbridge
from longbridge.openapi import Order, OrderSide, OrderType, TimeInForceType

from src.config import CONFIG
'''
print(f"✅ Order 来源: {Order.__module__}")
try:
    o = longbridge.openapi.Order(
        symbol="QQQ.US", quantity=1, side=OrderSide.Buy,
        order_type=OrderType.MO, time_in_force=TimeInForceType.Day
    )
    print(f"✅ 实例化成功: {o.symbol} | {o.order_type}")
except Exception as e:
    print(f"❌ SDK 实例化失败: {e}")
'''
bar_str = "2026-05-08T14:00:00Z"
bar_date = bar_str.date() if isinstance(bar_str, datetime) else datetime.strptime(bar_str, "%Y-%m-%dT%H:%M:%SZ").date()
print(f"✅ 日期解析成功: {bar_date}")

timestamp=datetime.now(CONFIG["tz_et"])
print(f"✅ 当前时间（ET时区）: {timestamp}, type: {type(timestamp)}")