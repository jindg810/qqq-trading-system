#!/usr/bin/env python3
"""
QQQ 1分钟K线数据获取与清洗流水线
✅ 自动处理时区、交易时段、乱序、缺失K线、OHLC异常
✅ 输出标准化CSV: ts,open,high,low,close,volume
"""
import os
import pandas as pd
import numpy as np
from datetime import datetime, time
from zoneinfo import ZoneInfo
from src.logger import get_logger
from src.config import CONFIG

logger = get_logger("data_pipeline")
TZ_ET = ZoneInfo("America/New_York")

def load_raw_data(csv_path: str) -> pd.DataFrame:
    """加载原始CSV（支持多种时间戳格式）"""
    df = pd.read_csv(csv_path)
    # 统一列名
    col_map = {
        "datetime": "ts", "timestamp": "ts", "Date": "ts",
        "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    
    required = ["ts", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"CSV缺少必需列: {missing}")
        
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ_ET)
    df = df.sort_values("ts").reset_index(drop=True)
    logger.info(f"✅ 加载原始数据: {len(df)} 行")
    return df

def clean_and_validate(df: pd.DataFrame) -> pd.DataFrame:
    """清洗流水线：时区对齐 → 交易时段过滤 → OHLC校验 → 补全缺失 → 去重"""
    # 1. 过滤非交易时段 (09:30-16:00 ET, 周一~周五)
    mask_hours = df["ts"].dt.time.between(time(9, 30), time(16, 0))
    mask_weekday = df["ts"].dt.weekday < 5
    df = df[mask_hours & mask_weekday].copy()
    
    # 2. OHLC逻辑校验
    invalid = df[(df["high"] < df["low"]) | (df["open"] > df["high"]) | (df["close"] > df["high"])]
    if len(invalid) > 0:
        logger.warning(f"⚠️ 丢弃 {len(invalid)} 行OHLC异常数据")
        df = df.drop(index=invalid.index)
        
    # 3. 重采样补全缺失1分钟K线（前向填充）
    df = df.set_index("ts").resample("1min").last()  # 取最后一根
    df["volume"] = df["volume"].fillna(0)
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].ffill()
    df = df.dropna(subset=["open"])  # 丢弃无开盘价的行
    df = df.reset_index()
    
    # 4. 去重与最终校验
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    logger.info(f"✅ 清洗完成: {len(df)} 根有效K线 | 覆盖 {df['ts'].dt.date.nunique()} 个交易日")
    return df

def fetch_polygon_data(api_key: str, start: str, end: str) -> pd.DataFrame:
    """Polygon.io 1分钟数据下载（需付费账户）"""
    import requests
    url = f"https://api.polygon.io/v2/aggs/ticker/QQQ/range/1/minute/{start}/{end}"
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    
    all_bars = []
    while url:
        r = requests.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("results"):
            all_bars.extend(data["results"])
        url = data.get("next_url")
        if url and "&apiKey=" not in url: url += f"&apiKey={api_key}"
            
    df = pd.DataFrame(all_bars)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.tz_convert(TZ_ET)
    return df

def run_pipeline(input_path: str, output_path: str, use_polygon: bool = False, api_key: str = ""):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    if use_polygon and api_key:
        logger.info("🌐 从 Polygon 下载数据...")
        df = fetch_polygon_data(api_key, "2023-01-01", "2025-04-30")
    else:
        logger.info("📂 从本地CSV加载...")
        df = load_raw_data(input_path)
        
    df_clean = clean_and_validate(df)
    df_clean.to_csv(output_path, index=False)
    logger.info(f"💾 清洗后数据已保存: {output_path}")
    return df_clean

if __name__ == "__main__":
    # 示例运行
    run_pipeline(
        input_path="data/qqq_raw.csv",
        output_path="data/qqq_1min_clean.csv",
        use_polygon=False
    )