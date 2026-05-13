#!/usr/bin/env python3
"""跨券商标准化数据模型（零业务逻辑，严格类型约束）"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import uuid

class OrderSide(Enum): BUY = "BUY"; SELL = "SELL"
class OrderType(Enum): MARKET = "MARKET"; LIMIT = "LIMIT"
class OrderStatus(Enum): 
    PENDING = "PENDING"; FILLED = "FILLED"; PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"; REJECTED = "REJECTED"; FAILED = "FAILED"
class TimeInForce(Enum): DAY = "DAY"; GTC = "GTC"; IOC = "IOC"

@dataclass(slots=True)
class OrderRequest:
    symbol: str; 
    side: OrderSide; 
    type: OrderType; 
    quantity: int;
    time_in_force: TimeInForce = TimeInForce.DAY;
    price: Optional[float] = None;  # 🔑 新增：限价单必填，市价单为 None
    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])  # 🔑 幂等防重单

@dataclass(slots=True)
class OrderCheck:
    order_id: str; filled_price: Optional[float]
    filled_qty: int; status: OrderStatus; updated_at: datetime

@dataclass(slots=True)
class Quote:
    symbol: str
    last_price: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0
    timestamp: Optional[datetime] = None

@dataclass(slots=True)
class KlineData:
    ts: datetime; open: float; high: float; low: float; close: float; volume: float; confirmed: bool = True