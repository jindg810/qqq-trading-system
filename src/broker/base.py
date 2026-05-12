#!/usr/bin/env python3
"""券商适配器抽象基类（依赖倒置核心）"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Callable, List, Optional

from longbridge.openapi import AdjustType, Candlestick, Period
from .models import OrderRequest, OrderCheck, Quote, KlineData

class BrokerError(Exception): pass
class ConnectionError(BrokerError): pass
class OrderError(BrokerError): pass

class BrokerAdapter(ABC):

    @abstractmethod
    def connect(self) -> None: ...
    
    @abstractmethod
    def disconnect(self) -> None: ...
    
    @abstractmethod
    def is_connected(self) -> bool: ...
    
    # 行情订阅
    @abstractmethod
    def subscribe_klines(self, symbol: str) -> None: ...
    
    # K线数据回调接口，用户实现后会被适配器调用
    @abstractmethod
    def set_kline_callback(self, callback: Callable[[KlineData], None]) -> None: ...
    
    # 历史数据接口，适配器内部实现，用户通过回调获取数据
    @abstractmethod
    def history_candlesticks_by_date(self, symbol: str, period: Period, adjust_type: AdjustType, target_date: datetime) -> List[Candlestick]: ...

    # 订单相关接口
    @abstractmethod
    def get_quote(self, symbol: str) -> Optional[Quote]: ...
    
    # 订单提交接口
    @abstractmethod
    def submit_order(self, order: OrderRequest) -> str: ...
    
    # 订单撤销接口，返回是否成功
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...
    
    # 订单查询接口，返回订单状态等信息
    @abstractmethod
    def check_order(self, order_id: str) -> Optional[OrderCheck]: ...
    
    # 等待事件循环，适配器内部实现，用户无需关心
    @abstractmethod
    def wait_for_events(self) -> None: ...