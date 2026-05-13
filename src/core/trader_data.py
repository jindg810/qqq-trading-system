#!/usr/bin/env python3
"""
数据操作独立模块
- 负责所有数据读写、计算和存储
- 与交易逻辑解耦
- 便于单元测试和维护
"""

import csv
import json
import os
import shutil
import sys
import threading
from datetime import datetime, timedelta
from typing import Any

from src.config import CONFIG, DATA_DIR
from src.logger import get_logger

# 设置日志
logger = get_logger(__name__)


class TraderDataManager:
    """交易数据管理器"""

    def __init__(self):
        """初始化数据管理器"""

        self._file_lock = threading.Lock()

        # 确保目录存在
        os.makedirs(os.path.dirname(CONFIG["csv_file"]), exist_ok=True)
        os.makedirs(CONFIG["records_dir"], exist_ok=True)

        logger.info("数据管理器初始化完成")

    # ================== CSV文件操作 ==================
    def get_today_csv(self):
        return CONFIG["csv_file"]

    def init_csv(self):
        csv_path = self.get_today_csv()
        os.makedirs(DATA_DIR, exist_ok=True)

        if not os.path.exists(csv_path):
            with open(csv_path, "w", newline="") as f:
                csv.writer(f).writerow(["ts", "open", "high", "low", "close", "volume"])
            logger.debug(f"创建CSV文件: {csv_path}")

    def write_kline_to_csv(self, bar: dict[str, Any]) -> None:
        """写入K线到当日CSV"""
        try:
            # logger.debug(f"写入K线到CSV:{self.get_today_csv()}")
            with open(self.get_today_csv(), "a", newline="") as f:
                csv.writer(f).writerow(
                    [
                        bar["ts"],
                        bar["open"],
                        bar["high"],
                        bar["low"],
                        bar["close"],
                        bar["volume"],
                    ]
                )
        except Exception as e:
            logger.error(f"写入K线失败: {e}")

    def archive_csv(self) -> None:
        """归档当日CSV（按日期存储）"""
        try:
            # 归档昨天的数据（开盘收到第1条数据时触发）
            yesterday = datetime.now(CONFIG["tz_et"]) - timedelta(days=1)
            # 当日文件：today.csv
            today_csv = self.get_today_csv()
            archive_dir = CONFIG["records_dir"]
            archive_csv = os.path.join(archive_dir, f"{yesterday.strftime('%Y-%m-%d')}.csv")

            os.makedirs(archive_dir, exist_ok=True)
            if os.path.exists(archive_csv):
                logger.info("归档已存在，跳过")
                return

            if not os.path.exists(today_csv):
                logger.warning(f"归档失败：{today_csv} 不存在")
                return

            # 移动文件
            shutil.move(today_csv, archive_csv)
            logger.info(f"📁 已归档CSV: {archive_csv}")

            # 创建新的空文件
            self.init_csv()
            logger.info("✅ 收盘归档完成")

        except Exception as e:
            logger.error(f"归档CSV失败: {e}")

    # ================== 状态文件操作 ==================
    def save_state(self, state_data: dict[str, Any]) -> bool:
        # 使用 with 语句自动处理异常释放，且保证串行执行
        with self._file_lock:
            temp_path = f"{CONFIG['state_file']}.tmp"
            try:
                with open(temp_path, "w") as f:
                    json.dump(state_data, f, indent=2)
                shutil.move(temp_path, CONFIG["state_file"])
                return True
            except Exception as e:
                logger.error(f"保存状态文件失败: {e}")
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                return False

    def load_state(self) -> dict[str, Any] | None:
        """加载状态文件"""
        try:
            if os.path.exists(CONFIG["state_file"]):
                with open(CONFIG["state_file"]) as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"加载状态文件失败: {e}")
        return None

    def init_state_file(self) -> None:
        """初始化状态文件"""
        if not os.path.exists(CONFIG["state_file"]):
            default_state = {
                "running": False,
                "position": None,
                "trades_today": 0,
                "consecutive_losses": 0,
                "daily_pnl": 0.0,
                "emergency_stopped": False,
            }
            self.save_state(default_state)
            logger.info("初始化状态文件")

    if __name__ == "__main__":
        print("this is trader_data module.")
