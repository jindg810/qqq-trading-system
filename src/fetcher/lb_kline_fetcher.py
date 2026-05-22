#!/usr/bin/env python3
"""
长桥 K 线数据下载器（简化版）
✅ 单类实现，核心功能完整
✅ 支持断点续传、并发控制、按日/月/区间下载
✅ 长桥 API 独立封装，便于替换
"""
import os
import json
import shutil
import sys
import time
import math
import argparse
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import traceback
from typing import List, Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from src.broker.longbridge import LongbridgeAdapter
from src.broker.base import BrokerAdapter
from src.config import CONFIG
from longbridge.openapi import  Period, AdjustType, TradeSessions
import pandas as pd

@dataclass
class DownloadConfig:
    """下载配置"""
    symbol: str = "QQQ.US"
    period: str = "Min_1"
    trade_sessions: TradeSessions = field(default_factory=lambda: TradeSessions.All)
    base_dir: str = "../data/klines"
    max_workers: int = 3          # 并发数（建议 ≤ 3）
    retry_count: int = 3          # 重试次数
    retry_delay: float = 1.0      # 重试延迟（秒）
    request_delay: float = 0.5    # 请求间隔（秒）
    batch_size: int = 5           # 每批处理天数


class KlineFetcher:
    """长桥 K 线下载器（单类版）"""
    def __init__(self, config: DownloadConfig, broker:BrokerAdapter):
        self.config = config
        self.broker = broker
        self._lock = threading.Lock()
        self._stats = {
            "total": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0
        }
        
        # 初始化
        #self._init_context()
        os.makedirs(self.config.base_dir, exist_ok=True)

        if not self.broker.is_connected():
            try:
                self.broker.connect()
            except ConnectionError as e:
                print(f"❌ 长桥连接失败，无法下载数据: {e}")
                sys.exit(1)
    
    # ==================== 长桥 API 封装（独立方法，便于替换） ====================
    def _fetch_daily_klines(self, target_date: date) -> Tuple[bool, List[Dict], str]:
        """
        获取单日 K 线数据（独立封装，便于替换数据源）
        
        Returns:
            (是否成功, K线数据列表, 错误信息)
        """
        for attempt in range(1, self.config.retry_count + 1):
            try:
                candles = self.broker.history_kline_by_date(
                    symbol=self.config.symbol,
                    period=self.config.period,
                    target_date=target_date
                )
                
                # 限流保护
                time.sleep(self.config.request_delay)
                
                if not candles: return True, [], "无数据"
                
                # 转换为标准格式
                klines = []
                for c in candles:
                    raw_ts = c.ts
                    ts = raw_ts.astimezone(CONFIG["tz_et"])
                    klines.append({
                        "datetime": ts.strftime("%Y-%m-%d %H:%M:%S"),  # 统一美东标准格式，确保后续排序与可视化一致
                        "open": float(c.open),
                        "high": float(c.high),
                        "low": float(c.low),
                        "close": float(c.close),
                        "volume": int(c.volume) if c.volume else 0
                    })
                
                return True, klines, f"成功获取 {len(klines)} 条"
                
            except Exception as e:
                error_msg = f"第 {attempt}/{self.config.retry_count} 次失败: {str(e)[:50]}"
                print(f"  ⚠️ {error_msg}")
                traceback.print_exc()
                
                if attempt < self.config.retry_count:
                    # 指数退避
                    delay = self.config.retry_delay * (2 ** (attempt - 1))
                    time.sleep(delay)
        
        return False, [], f"重试 {self.config.retry_count} 次后失败"
    
    # ==================== 断点续传 ====================
    def _checkpoint_file(self) -> str:
        """断点文件路径"""
        return os.path.join(self.config.base_dir, f"{self.config.symbol}_checkpoint.json")
    
    def _load_checkpoint(self) -> Dict[str, str]:
        """加载断点"""
        checkpoint_file = self._checkpoint_file()
        if os.path.exists(checkpoint_file):
            try:
                with open(checkpoint_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    
    def _save_checkpoint(self, date_str: str, file_path: str):
        """保存断点（稳定版，移除脆弱的 os.replace）"""
        checkpoint_file = self._checkpoint_file()
        os.makedirs(os.path.dirname(checkpoint_file), exist_ok=True)  # 兜底确保目录存在
        
        data = self._load_checkpoint()
        data[date_str] = file_path
        
        try:
            with open(checkpoint_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️ 保存断点文件失败 (不影响下载): {e}")
    
    def _is_day_downloaded(self, date_str: str) -> bool:
        """检查某天是否已下载"""
        data = self._load_checkpoint()
        return date_str in data and os.path.exists(data[date_str])
    
    # ==================== 日期处理 ====================
    def _parse_dates(self, mode: str, value: str) -> List[date]:
        """解析日期参数（增加容错处理）"""
        dates = []
        
        # 清理 value，去掉可能存在的 mode= 前缀
        if '=' in value:
            # 如果格式是 "day=2026-05-11"，提取后面的日期
            parts = value.split('=', 1)
            if len(parts) == 2:
                value = parts[1].strip()
        
        try:
            if mode == 'day':
                dates.append(date.fromisoformat(value))
            
            elif mode == 'month':
                year, month = map(int, value.split('-'))
                first_day = date(year, month, 1)
                if month == 12:
                    last_day = date(year + 1, 1, 1) - timedelta(days=1)
                else:
                    last_day = date(year, month + 1, 1) - timedelta(days=1)
                
                current = first_day
                while current <= last_day:
                    dates.append(current)
                    current += timedelta(days=1)
            
            elif mode == 'range':
                start_str, end_str = value.split(':')
                start_date = date.fromisoformat(start_str)
                end_date = date.fromisoformat(end_str)
                
                current = start_date
                while current <= end_date:
                    dates.append(current)
                    current += timedelta(days=1)
    
        except ValueError as e:
            print(f"❌ 日期格式错误: {value}")
            print(f"   正确格式:")
            print(f"   day: '2026-05-11'")
            print(f"   month: '2026-05'")
            print(f"   range: '2026-01-01:2026-05-11'")
            raise
        
        return sorted(set(dates))
    
    # ==================== 单日下载 ====================
    def _download_single_day(self, target_date: date) -> Tuple[bool, str]:
        """下载单日数据"""
        date_str = target_date.strftime("%Y%m%d")
        
        # 检查是否已下载
        if self._is_day_downloaded(date_str):
            with self._lock:
                self._stats["skipped"] += 1
            return True, "已跳过（已存在）"
        
        # 调用长桥 API
        success, klines, msg = self._fetch_daily_klines(target_date)
        
        if not success:
            with self._lock:
                self._stats["failed"] += 1
            return False, msg
        
        if not klines:
            with self._lock:
                self._stats["skipped"] += 1
            return True, msg
        
        year_month = target_date.strftime("%Y%m")
        month_subdir = os.path.join(self.config.base_dir, year_month)
        os.makedirs(month_subdir, exist_ok=True)

        # 保存日文件
        daily_file = os.path.join(month_subdir, f"{self.config.symbol}_{date_str}.csv")
        df = pd.DataFrame(klines)
        df.to_csv(daily_file, index=False)
        
        # 保存断点
        self._save_checkpoint(date_str, daily_file)
        
        with self._lock:
            self._stats["downloaded"] += 1
        
        return True, f"成功下载 {len(klines)} 条"
    
    # ==================== 按月合并 ====================
    def _merge_monthly_files(self, year: int, month: int):
        """合并月度文件"""
        month_str = f"{year}{month:02d}"
        year_month_dir = os.path.join(self.config.base_dir, month_str)
        monthly_file = os.path.join(self.config.base_dir, f"{self.config.symbol}_{month_str}_1min.csv")
        if not os.path.exists(year_month_dir):
            print(f"  ⚠️ {month_str} 子目录不存在，跳过合并")
            return
        
        # 查找该月所有日文件
        daily_files = sorted([
            os.path.join(year_month_dir, f)
            for f in os.listdir(year_month_dir)
            if f.startswith(f"{self.config.symbol}_{year}{month:02d}")
        ])
        
        if not daily_files:
            print(f"  ⚠️ {month_str} 无日文件")
            return
        
        # 合并
        dfs = [pd.read_csv(f) for f in daily_files]
        monthly_df = pd.concat(dfs, ignore_index=True)
        monthly_df.sort_values("datetime", inplace=True)
        
        # 保存
        monthly_df.to_csv(monthly_file, index=False)
        print(f"  ✅ {month_str} 月度文件已生成: {len(monthly_df)} 条")
    
    # ==================== 公开下载接口 ====================
    def download(self, mode: str, value: str):
        """执行下载"""
        print("=" * 60)
        print(f"🚀 开始下载 {self.config.symbol} K 线数据")
        print(f"   模式: {mode}")
        print(f"   参数: {value}")
        print(f"   并发数: {self.config.max_workers}")
        print(f"   存储目录: {self.config.base_dir}")
        print("=" * 60)
        
        # 解析日期
        dates = self._parse_dates(mode, value)
        self._stats["total"] = len(dates)
        
        print(f"📅 共 {len(dates)} 个交易日待处理")
        
        # 分批处理
        batches = [
            dates[i:i + self.config.batch_size]
            for i in range(0, len(dates), self.config.batch_size)
        ]
        
        for batch_idx, batch_dates in enumerate(batches, 1):
            print(f"\n📦 批次 {batch_idx}/{len(batches)}: {batch_dates[0]} ~ {batch_dates[-1]}")
            
            # 并发下载
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
                future_to_date = {
                    executor.submit(self._download_single_day, d): d
                    for d in batch_dates
                }
                
                for future in as_completed(future_to_date):
                    date_obj = future_to_date[future]
                    date_str = date_obj.isoformat()
                    success, msg = future.result()
                    
                    status = "✅" if success else "❌"
                    print(f"  {status} {date_str}: {msg}")
        
        # 按月合并
        print("\n📦 按月合并文件...")
        months = set((d.year, d.month) for d in dates)
        for year, month in sorted(months):
            self._merge_monthly_files(year, month)
        
        # 打印统计
        print("\n" + "=" * 60)
        print("📊 下载统计")
        print("=" * 60)
        print(f"   总天数: {self._stats['total']}")
        print(f"   成功下载: {self._stats['downloaded']}")
        print(f"   跳过(已存在): {self._stats['skipped']}")
        print(f"   失败: {self._stats['failed']}")
        if self._stats['total'] > 0:
            success_rate = (self._stats['downloaded'] + self._stats['skipped']) / self._stats['total'] * 100
            print(f"   成功率: {success_rate:.1f}%")
        print("=" * 60)


# ==================== CLI 接口 ====================
def main():
    '''
    python -m src.fetcher.lb_kline_fetcher --mode=month --value day='2026-05'
    python -m src.fetcher.lb_kline_fetcher --mode=day --value day='2026-05-10'
    '''
    parser = argparse.ArgumentParser(description="长桥 K 线数据下载器")
    parser.add_argument("--mode", choices=['day', 'month', 'range'], required=True,
                       help="下载模式: day(单日), month(整月), range(日期区间)")
    parser.add_argument("--value", required=True,
                       help="日期值: day='2026-05-11', month='2026-05', range='2026-01-01:2026-05-11'")
    parser.add_argument("--symbol", default="QQQ.US",
                       help="股票代码 (默认: QQQ.US)")
    parser.add_argument("--workers", type=int, default=3,
                       help="并发数 (默认: 3)")
    parser.add_argument("--retries", type=int, default=3,
                       help="重试次数 (默认: 3)")
    parser.add_argument("--delay", type=float, default=0.5,
                       help="请求间隔秒 (默认: 0.5)")
    
    args = parser.parse_args()
    value = args.value
    if value.startswith(f"{args.mode}="):
        value = value[len(f"{args.mode}="):]

    # 创建配置
    config = DownloadConfig(
        symbol=args.symbol,
        base_dir=str(CONFIG["data_dir"] / "klines"),
        max_workers=args.workers,
        retry_count=args.retries,
        request_delay=args.delay
    )
    
    # 执行下载
    downloader = KlineFetcher(config, broker=LongbridgeAdapter())
    downloader.download(args.mode, args.value)


if __name__ == "__main__":
    main()