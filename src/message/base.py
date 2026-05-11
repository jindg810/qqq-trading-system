#!/usr/bin/env python3
"""消息通知抽象基类 & 平台注册表"""
from abc import ABC, abstractmethod
from typing import Dict, Type, ClassVar

# 全局注册表：key=平台名, value=实现类
_NOTIFIER_REGISTRY: Dict[str, Type["BaseNotifier"]] = {}

def register_notifier(name: str):
    """平台注册装饰器（遵循开闭原则，新增平台零侵入核心路由）"""
    def decorator(cls: Type["BaseNotifier"]):
        _NOTIFIER_REGISTRY[name] = cls
        return cls
    return decorator

class BaseNotifier(ABC):
    """所有通知平台的统一契约"""
    
    @abstractmethod
    def send(self, title: str, content: str) -> bool:
        """发送文本/Markdown消息，返回是否成功"""
        pass

    def format_message(self, title: str, content: str) -> Dict:
        """默认格式化（子类可重写以适配特定平台语法）"""
        return {"title": title, "content": content, "msgtype": "text"}