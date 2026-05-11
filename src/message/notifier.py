#!/usr/bin/env python3
"""统一通知管理器（后台线程 + 注册路由 + 业务模板）"""
import queue, threading, logging
from typing import Optional
from ..config import CONFIG
from .base import BaseNotifier, _NOTIFIER_REGISTRY
from . import dingtalk

logger = logging.getLogger("message.notifier")

class Notifier:
    _instance: Optional["Notifier"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._queue: queue.Queue = queue.Queue(maxsize=100)
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()
        self._provider = self._resolve_provider()

    def _resolve_provider(self) -> BaseNotifier:
         # 1. 全局开关拦截
        if not CONFIG.get("enable_notifications", True):
            logger.debug("📵 通知模块已禁用")
            return BaseNotifier()
            
        # 2. 路由到对应平台
        n_type = CONFIG.get("notifier_type", "dingtalk")
        provider_cls = _NOTIFIER_REGISTRY.get(n_type)
        if not provider_cls:
            logger.warning(f"⚠️ 未注册通知类型: {n_type}，降级为无操作")
            return BaseNotifier()
            
        try:
            return provider_cls(
                webhook=CONFIG.get("dingtalk_webhook", ""),
                secret=CONFIG.get("dingtalk_secret")
            )
        except Exception as e:
            logger.error(f"❌ 通知平台初始化失败: {e}")
            return BaseNotifier()

    def _worker(self):
        """后台消费线程：非阻塞处理消息队列"""
        while True:
            try:
                title, content = self._queue.get(timeout=5)
                self._provider.send(title, content)
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"消息消费异常: {e}")

    def send(self, title: str, content: str) -> bool:
        """入队发送（不阻塞主线程）"""
        if not CONFIG.get("enable_notifications", True): return False
        try:
            self._queue.put_nowait((title, content))
            return True
        except queue.Full:
            logger.warning("📦 通知队列已满，丢弃当前消息")
            return False
        except Exception as e:
            logger.error(f"消息发送异常: {e}")
            return False

    # ================= 业务便捷模板 =================
    def notify_signal(self, signal: str, price: float, reason: str = ""):
        self.send(
            f"📡 交易信号: {signal.upper()}",
            f"**标的**: QQQ\n**方向**: {signal}\n**标的价**: {price}\n**依据**: {reason or '策略触发'}"
        )

    def notify_open(self, symbol: str, price: float, side: str):
        self.send(
            f"✅ 开仓成功: {side.upper()}",
            f"**合约**: `{symbol}`\n**成交价**: {price}\n**状态**: 已成交"
        )

    def notify_close(self, symbol: str, price: float, pnl: float, reason: str):
        self.send(
            f"📉 平仓: {reason}",
            f"**合约**: `{symbol}`\n**成交价**: {price}\n**盈亏**: {pnl:+.2f}"
        )

    def notify_risk_fuse(self, reason: str, detail: str):
        self.send(f"🚨 风控熔断: {reason}", f"**原因**: {reason}\n**详情**: {detail}")
