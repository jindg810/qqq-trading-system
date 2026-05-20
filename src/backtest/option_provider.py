#!/usr/bin/env python3
"""
期权历史价格提供者 (OptionPriceProvider)
✅ 按日加载缓存，防止内存溢出
✅ 自动处理 US.QQQ... 与 QQQ...US 的格式转换
✅ 支持向前回溯 (最多5分钟)，解决期权分钟级流动性缺失问题
"""
import re

import pandas as pd
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Tuple
from src.config import CONFIG
from src.logger import get_logger

logger = get_logger("backtest.option_provider")

class OptionPriceProvider:
    def __init__(self):
        self.base_dir = Path(str(CONFIG["data_dir"] / "opt_klines"))  #base_dir
        self._cache: Dict[date, Dict[Tuple[str, str], float]] = {}
        self._current_date: Optional[date] = None
        self._miss_count = 0  # 统计未命中次数，用于回测后分析

    def _to_csv_symbol(self, engine_symbol: str) -> str:
        """
        将引擎/API格式统一转换为 CSV 中的标准 OCC 格式
        ✅ 处理前后缀：QQQ...US -> US.QQQ...
        ✅ 处理年份位数：20260420 -> 260420 (4位转2位)
        """
        # 1. 移除尾部的 .US
        sym = engine_symbol.replace(".US", "")
        
        # 2. 确保头部有 US. 前缀
        if not sym.startswith("US."):
            sym = f"US.{sym}"
        
        # 3. 🔑 核心转换：将 QQQ 后面的 4位年份 (如 2026) 替换为 2位年份 (26)
        # 正则解释：匹配 "QQQ20" 后面的两位数字，替换为 "QQQ" + 这两位数字
        sym = re.sub(r'(QQQ)20(\d{2})', r'\1\2', sym)
        return sym

    def _load_day(self, target_date: date):
        """加载单日期权数据到内存缓存"""
        self._cache.clear()  # 释放前一天内存
        self._current_date = target_date
        
        ym = target_date.strftime("%Y%m")
        ymd = target_date.strftime("%Y%m%d")
        file_path = self.base_dir / ym / f"US.QQQ_opt_{ymd}.csv"
        
        if not file_path.exists():
            logger.warning(f"⚠️ 期权历史文件缺失: {file_path}")
            self._cache[target_date] = {}
            return

        try:
            df = pd.read_csv(file_path)
            if df.empty:
                self._cache[target_date] = {}
                return
            
            # 构建快查字典: {(symbol, datetime_str): close_price}
            day_data = {}
            for _, row in df.iterrows():
                price = row['close']
                if pd.notna(price) and price > 0:
                    day_data[(row['symbol'], row['datetime'])] = float(price)
            
            self._cache[target_date] = day_data
            logger.debug(f"✅ 已加载 {ymd} 期权数据: {len(day_data)} 条记录")
        except Exception as e:
            logger.error(f"❌ 读取期权文件失败 {file_path}: {e}")
            self._cache[target_date] = {}

    def get_price(self, ts: datetime, engine_symbol: str) -> Optional[float]:
        """
        获取指定时间的期权价格
        :param ts: 当前K线时间 (datetime)
        :param engine_symbol: 引擎生成的合约代码 (如 QQQ260420C638000.US)
        :return: 期权价格 (float) 或 None
        """
        target_date = ts.date()
        
        # 跨日时重新加载缓存
        if target_date != self._current_date:
            self._load_day(target_date)
        
        day_data = self._cache.get(target_date)
        if not day_data:
            return None
        
        csv_symbol = self._to_csv_symbol(engine_symbol)
        
        # 🔑 向前回溯机制：期权可能某分钟无交易，最多往前找 5 分钟
        for offset in range(6):
            lookup_ts = ts - timedelta(minutes=offset)
            dt_str = lookup_ts.strftime("%Y-%m-%d %H:%M:%S")
            
            price = day_data.get((csv_symbol, dt_str))
            if price is not None:
                return price
        
        # 若回溯 5 分钟仍无数据，记录缺失
        self._miss_count += 1
        return None

    def get_miss_stats(self) -> int:
        return self._miss_count
    
if __name__ == "__main__":
    # python -m src.backtest.option_provider
    provider = OptionPriceProvider()
    ts = datetime.strptime("2026-04-20 09:39:00", "%Y-%m-%d %H:%M:%S")
    symbol = "US.QQQ260420C638000"
    symbol = "QQQ20260420C638000.US"
    p = provider.get_price(ts, symbol)
    print(f"ts={ts}, symbol={symbol}, price={p}")