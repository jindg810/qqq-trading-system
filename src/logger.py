#!/usr/bin/env python3
"""
简洁实用的日志工具类 (生产优化版)
✅ 修复日志重复问题 (propagate=False + 移除冗余 basicConfig)
✅ 统一日志格式 (含模块名称，便于多文件追踪)
✅ 线程安全 & 自动按大小轮转
"""
import logging
import logging.handlers
import os
import sys
from src.config import CONFIG

class Logger:
    """自定义日志记录器（单例模式）"""
    _instances = {}

    def __new__(cls, name: str, log_file: str, level: str = "INFO"):
        if name not in cls._instances:
            cls._instances[name] = super().__new__(cls)
        return cls._instances[name]

    def __init__(self, name: str, log_file: str, level: str = "INFO"):
        # 避免重复初始化
        if hasattr(self, "_initialized"):
            return
            
        self.name = name
        self.log_file = log_file
        self.level = getattr(logging, level.upper(), logging.INFO)
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(self.level)
        self.logger.handlers.clear()  # 清理旧处理器防重复添加
        
        # 🔑 核心修复：禁止向 Root Logger 传播，彻底解决重复打印
        self.logger.propagate = False

        self._setup_handlers()
        self._initialized = True

    def _setup_handlers(self):
        # 统一格式：增加 %(name)s 便于定位日志来源模块
        fmt_str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        formatter = logging.Formatter(fmt_str, datefmt="%Y-%m-%d %H:%M:%S")

        # 1. 文件处理器（按大小轮转，10MB/文件，保留5个备份）
        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(self.level)

        # 2. 控制台处理器（显式绑定 stdout 防 IDE 输出错乱）
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(self.level)

        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    # ================== 日志记录方法 ==================
    def debug(self, msg: str): self.logger.debug(msg)
    def info(self, msg: str): self.logger.info(msg)
    def warning(self, msg: str): self.logger.warning(msg)
    def error(self, msg: str): self.logger.error(msg)
    def critical(self, msg: str): self.logger.critical(msg)
    def exception(self, msg: str): self.logger.exception(msg)

    # ================== 业务便捷方法 ==================
    def trade(self, action: str, symbol: str, price: float):
        self.info(f"交易: {action} | {symbol} @ ${price:.2f}")
    def risk(self, msg: str):
        self.warning(f"风控: {msg}")
    def system(self, msg: str):
        self.info(f"系统: {msg}")

# ===================== 工厂函数 =====================
def get_logger(name: str) -> Logger:
    # ✅ 移除 basicConfig，完全由 Logger 类接管输出，避免 Root Logger 干扰
    return Logger(name=name, log_file=CONFIG["log_file"], level=CONFIG["log_level"])

if __name__ == "__main__":
    # 测试代码
    test_logger = get_logger("test")
    test_logger.debug("这是调试信息")
    test_logger.info("这是普通信息")
    test_logger.warning("这是警告信息")
    test_logger.error("这是错误信息")
    test_logger.critical("这是严重错误信息")