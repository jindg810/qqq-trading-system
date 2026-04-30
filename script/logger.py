#!/usr/bin/env python3
"""
简洁实用的日志工具类
- 单例模式
- 同时输出到文件和控制台
- 自动按大小轮转
- 简单易用
"""

import os
import logging
import logging.handlers
from typing import Optional

from config import CONFIG

class Logger:
    """简洁的日志记录器"""
    
    # 单例存储
    _instances = {}
    
    def __new__(cls, name: str, log_file: str, level: str = "INFO"):
        """单例模式：相同名称返回同一个实例"""
        if name not in cls._instances:
            cls._instances[name] = super().__new__(cls)
        return cls._instances[name]
    
    def __init__(self, name: str, log_file: str, level: str = "INFO"):
        """初始化日志记录器"""
        # 避免重复初始化
        if hasattr(self, '_initialized'):
            return
            
        self.name = name
        self.log_file = log_file
        self.level = getattr(logging, level.upper(), logging.INFO)
        
        # 确保日志目录存在
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        
        # 创建日志记录器
        self.logger = logging.getLogger(name)
        self.logger.setLevel(self.level)
        
        # 清除现有处理器（避免重复）
        self.logger.handlers.clear()
        
        # 设置处理器
        self._setup_handlers()
        
        self._initialized = True
    
    def _setup_handlers(self):
        """设置日志处理器"""
        # 日志格式
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 文件处理器（按大小轮转，10MB一个文件，保留5个备份）
        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(self.level)
        
        # 控制台处理器
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(self.level)
        
        # 添加处理器
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    # ================== 日志记录方法 ==================
    def debug(self, msg: str):
        """调试信息"""
        self.logger.debug(msg)
    
    def info(self, msg: str):
        """普通信息"""
        self.logger.info(msg)
    
    def warning(self, msg: str):
        """警告信息"""
        self.logger.warning(msg)
    
    def error(self, msg: str):
        """错误信息"""
        self.logger.error(msg)
    
    def critical(self, msg: str):
        """严重错误信息"""
        self.logger.critical(msg)
    
    def exception(self, msg: str):
        """异常信息（包含堆栈跟踪）"""
        self.logger.exception(msg)
    
    # ================== 便捷方法 ==================
    def trade(self, action: str, symbol: str, price: float):
        """记录交易操作"""
        self.info(f"交易: {action} | {symbol} @ ${price:.2f}")
    
    def risk(self, msg: str):
        """记录风控信息"""
        self.warning(f"风控: {msg}")
    
    def system(self, msg: str):
        """记录系统信息"""
        self.info(f"系统: {msg}")


# ===================== 工厂函数 =====================
def get_logger(name: str) -> Logger:
    """
    根据配置获取日志记录器
    
    Args:
        name: 日志记录器名称
        
    Returns:
        Logger实例
    """
    return Logger(
        name=name,
        log_file=CONFIG["log_file"],
        level=CONFIG["log_level"]
    )


if __name__ == "__main__":
    # 测试代码
    test_logger = Logger("test", "test.log", "DEBUG")
    test_logger.debug("这是调试信息")
    test_logger.info("这是普通信息")
    test_logger.warning("这是警告信息")
    test_logger.error("这是错误信息")
    test_logger.critical("这是严重错误信息")