#!/usr/bin/env python3
"""
富途期权分钟级历史数据下载器（Futu OpenAPI 标准版）
✅ 专注期权下载：仅实现富途期权链查询 + 历史 K 线获取
✅ 正确筛选：spot ± $5 范围内的虚值（OTM）call/put 合约
✅ 存储规范：按日独立存档 + 按月合并，断点续传
✅ 输出格式：datetime,open,high,low,close,volume,symbol
"""
import os
import json
import time
import argparse
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Literal
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# 🔑 富途 OpenAPI 标准导入（严格按官方文档）
from futu import *
from futu.common.constant import KLType, SortField, OptionType
import pandas as pd

from src.config import CONFIG
from src.logger import get_logger

logger = get_logger("data.futu_option")

@dataclass
class DownloaderConfig:
    """下载配置（富途期权专用）"""
    # 标的参数
    underlying: str = "US.QQQ"           # 富途标的代码格式：市场.代码
    symbol_file: str = "QQQ.US"
    option_offset: float = 5.0            # 行权价偏移量（±$5）
    option_side: Literal["call", "put", "both"] = "both"
    
    # 时间参数
    start_date: date = date(2024, 5, 1)
    end_date: date = date(2026, 4, 30)
    kl_type: KLType = field(default_factory=lambda: KLType.K_1M)
    
    # 存储与并发
    output_dir: str = str(Path(CONFIG.get("data_dir", Path.cwd() / "data")) / "opt_klines")
    max_workers: int = 2                  # 富途限流严格，建议 ≤2
    retry_count: int = 3
    retry_delay: float = 1.0
    request_delay: float = 1            # 请求间隔，防频率限制
    
    # 富途连接参数
    futu_host: str = "127.0.0.1"
    futu_port: int = 11111

class FutuOptionDownloader:
    """富途期权数据下载器（纯净版）"""
    
    def __init__(self, config: DownloaderConfig):
        self.config = config
        self._lock = threading.Lock()
        self._stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0}
        
        # 🔑 初始化富途连接（需先启动 FutuOpenD）
        self.quote_ctx = None
        self._init_futu_connection()
        
        # 初始化输出目录
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)
    
    def _init_futu_connection(self):
        """初始化富途 QuoteCtx 连接"""
        try:
            self.quote_ctx = OpenQuoteContext(
                host=self.config.futu_host,
                port=self.config.futu_port
            )
            # 测试连接
            ret, data = self.quote_ctx.get_market_snapshot(["US.QQQ"])
            if ret != RET_OK:
                raise ConnectionError(f"富途连接失败: {data}")
            logger.info("✅ 富途连接成功")
        except Exception as e:
            logger.error(f"❌ 富途初始化失败: {e}")
            logger.info("💡 请确保：1) FutuOpenD 已启动 2) 端口配置正确 3) 网络通畅")
            raise
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    def close(self):
        """优雅关闭富途连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
            logger.info("🔌 富途连接已关闭")
    
    # ==================== 期权合约筛选（核心修正） ====================
    def _get_spot_price(self, target_date: datetime) -> float:
        """
        获取标的在目标日期/时间的价格（分钟级精度）
        ✅ 仅从历史文件读取，不回退实时报价，保证回测可重复性
        ✅ 支持按分钟粒度获取价格（用于期权理论价计算）
        ✅ 数据缺失时抛出异常，避免静默兜底产生垃圾数据
        """
        try:
            if not target_date: 
                return
            
            # 1. 构建文件路径：data/klines/yyyymm/QQQ.US_yyyymmdd.csv
            year_month = target_date.strftime("%Y%m")
            date_str = target_date.strftime("%Y%m%d")
            base_klines_dir = Path(CONFIG.get("data_dir", Path.cwd() / "data") / "klines")
            
            # 优先查找月度子目录下的日文件
            month_dir = base_klines_dir / year_month
            daily_file = month_dir / f"{self.config.symbol_file}_{date_str}.csv"
            
            if daily_file.exists():
                df = pd.read_csv(daily_file, parse_dates=["datetime"])
            else:
                # 次选：查找月度合并文件 data/klines/QQQ.US_yyyymm_1min.csv
                monthly_file = base_klines_dir / f"{self.config.symbol_file}_{year_month}_1min.csv"
                if not monthly_file.exists():
                    raise FileNotFoundError(
                        f"标的历史数据缺失: {daily_file} 或 {monthly_file}"
                    )
                df = pd.read_csv(monthly_file, parse_dates=["datetime"])
            
            if df.empty:
                raise ValueError(f"标的数据文件为空: {daily_file}")
            
            # 2. 按时间筛选价格
            # 🔑 分钟级精确匹配：对齐期权定价的每分钟需求
            #target_ts = target_date.replace(second=0, microsecond=0)
            #mask = df["datetime"].dt.floor("T") == target_ts
            target_ts_str = target_date.strftime("%Y-%m-%d %H:%M:%S")
            mask = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S") == target_ts_str
            row = df[mask]
            
            if row.empty:
                raise ValueError(
                    f"标的价格缺失: {target_date} {target_date.strftime('%H:%M') if target_date else 'N/A'}"
                )
            
            return float(row.iloc[0]["close"])
            
        except (FileNotFoundError, ValueError, KeyError, pd.errors.EmptyDataError) as e:
            # 🔑 明确报错，禁止静默兜底，确保回测数据纯净
            logger.error(f"❌ 获取标的价格失败 [{target_date}]: {e}")
            raise
    
    def _generate_option_symbols(self, target_date: date) -> List[str]:
        # 查询指定日期日K，根据 最低价-offset ~ 最高价+offset 生成期权名列表
        symbols = []
        
        try:
            result, klines, msg = self._fetch_daily_klines(self.config.underlying, target_date, KLType.K_DAY)
            if not result or len(klines) <= 0:
                logger.error(f"获取日K失败:{msg},{target_date}")
                raise RuntimeError
            
            day_kline = klines[0]
            price_low:int = day_kline.get("low", 0)
            price_high:int = day_kline.get("high", 0) 
            
            if price_low == 0 or price_high == 0:
                logger.error(f"获取日K最高/低价格错误，date={target_date}")
                raise RuntimeError
            
            exp_str = target_date.strftime("%y%m%d")
            price_low, price_high = round(price_low - self.config.option_offset), round(price_high + self.config.option_offset)
            #print(f"low ~ high: {price_low}~{price_high}")
            for strike in range(price_low, price_high):
                # 根据配置决定生成方向
                sides = ["call", "put"] if self.config.option_side == "both" else [self.config.option_side]
                
                for side in sides:
                    otype = "C" if side == "call" else "P"
                    # 富途期权代码规则：行权价去掉小数点直接拼接（如 709.0 -> 709000）
                    symbol = f"US.QQQ{exp_str}{otype}{int(strike * 1000):06d}"
                    symbols.append(symbol)

            logger.info(f"🔍 筛选出 {len(symbols)} 个有效期权合约 (target_date={target_date}, offset=${self.config.option_offset})")
            return symbols
        except Exception as e:
            logger.warning(f"⚠️ 期权名获取查询异常: {e}")
            raise e
    
    def _fallback_generate(self, spot_price: float, target_date: date) -> List[str]:
        """
        降级方案：无期权链时，生成 spot ±$5 范围内的所有整数行权价合约
        ✅ 严格遍历 -5 ~ +5 共 11 个行权价，根据配置生成 Call/Put
        ✅ 富途代码格式: US.QQQ260514C709000
        """
        symbols = []
        exp_str = target_date.strftime("%y%m%d")
        base_strike = round(spot_price)  # 基准行权价（四舍五入取整）

        # 🔑 遍历 -5 到 +5 的偏移量，共 11 个行权价
        for offset in range(-5, 6):
            strike = round(base_strike + offset)
            if strike <= 0:
                continue

            # 根据配置决定生成方向
            sides = ["call", "put"] if self.config.option_side == "both" else [self.config.option_side]
            
            for side in sides:
                otype = "C" if side == "call" else "P"
                # 富途期权代码规则：行权价去掉小数点直接拼接（如 709.0 -> 709000）
                symbol = f"US.QQQ{exp_str}{otype}{int(strike * 1000):06d}"
                symbols.append(symbol)

        return symbols
    
    # ==================== 富途 API 封装（严格按官方文档） ====================
    def _fetch_daily_klines(self, symbol: str, target_date: date, kline_type: str) -> Tuple[bool, List[Dict], str]:
        """
        获取单日分钟级 K 线（严格对齐富途官方签名）
        ✅ 签名: request_history_kline(code, start, end, ktype, autype, fields, max_count, page_req_key, extended_time, session)
        """
        for attempt in range(1, self.config.retry_count + 1):
            try:
                # 🔑 严格按官方参数顺序与命名传参
                ret, data, page_req_key = self.quote_ctx.request_history_kline(
                    code=symbol,                                  # 如 "US.QQQ260514C709000"
                    start=target_date.strftime("%Y-%m-%d"),       # 开始日期 (含)
                    end=target_date.strftime("%Y-%m-%d"),         # 结束日期 (含)
                    ktype=kline_type,                             # KLType.K_1M
                    autype=AuType.NONE,                           # 期权无复权概念，必须 NONE
                    fields=[KL_FIELD.DATE_TIME, KL_FIELD.OPEN, KL_FIELD.HIGH, 
                            KL_FIELD.LOW, KL_FIELD.CLOSE, KL_FIELD.TRADE_VOL],
                    max_count=1000,
                    page_req_key=None,
                    extended_time=False,
                    session=Session.NONE
                )
                if 0 != ret:
                    logger.warning(f"request_history_kline {symbol},{target_date} fail. data rows:{len(data)}, data:{data}")
                else:
                    logger.info(f"request_history_kline {symbol} ok. ret={ret}, data rows: {len(data)}")
                # 限流保护（富途接口严格限频）
                time.sleep(self.config.request_delay)
                
                # 官方标准返回校验
                if ret != RET_OK or data is None or data.empty:
                    return True, [], f"无数据 (可能非交易日或该合约未上市)"
                
                # 转换数据格式（对齐回测引擎 CSV 规范）
                klines = []
                for _, row in data.iterrows():
                    klines.append({
                        "datetime": row['time_key'],              # 官方固定字段名
                        "open": float(row['open']),
                        "high": float(row['high']),
                        "low": float(row['low']),
                        "close": float(row['close']),
                        "volume": int(row['volume']) if pd.notna(row['volume']) else 0,
                        "symbol": symbol
                    })
                
                return True, klines, f"成功获取 {len(klines)} 条"
                
            except Exception as e:
                logger.warning(f"⚠️ 第 {attempt}/{self.config.retry_count} 次请求失败: {str(e)}")
                if attempt < self.config.retry_count:
                    time.sleep(self.config.retry_delay * (2 ** (attempt - 1)))
        
        return False, [], f"重试 {self.config.retry_count} 次后仍失败"
    
    # ==================== 断点续传 ====================
    def _checkpoint_file(self) -> str:
        return os.path.join(self.config.output_dir, "futu_option_checkpoint.json")
    
    def _load_checkpoint(self) -> Dict[str, str]:
        checkpoint_file = self._checkpoint_file()
        if os.path.exists(checkpoint_file):
            try:
                with open(checkpoint_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    
    def _save_checkpoint(self, date_str: str, file_path: str):
        checkpoint_file = self._checkpoint_file()
        os.makedirs(os.path.dirname(checkpoint_file), exist_ok=True)
        data = self._load_checkpoint()
        data[date_str] = file_path
        try:
            with open(checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"⚠️ 保存断点文件失败 (不影响下载): {e}")
    
    def _is_day_downloaded(self, date_str: str) -> bool:
        data = self._load_checkpoint()
        return date_str in data and os.path.exists(data[date_str])
    
    # ==================== 日期解析 ====================
    def _parse_date_range(self) -> List[date]:
        """解析下载日期范围"""
        dates = []
        current = self.config.start_date
        while current <= self.config.end_date:
            if current.weekday() < 5:  # 仅工作日
                dates.append(current)
            current += timedelta(days=1)
        return dates
    
    # ==================== 单日下载（核心） ====================
    def _download_single_day(self, target_date: date) -> Tuple[bool, str]:
        """下载单日期权数据"""
        date_str = target_date.strftime("%Y%m%d")
        
        # 检查断点
        if self._is_day_downloaded(date_str):
            with self._lock:
                self._stats["skipped"] += 1
            return True, "已跳过（已存在）"
        
        # 获取标的价格 + 生成期权合约列表
        #spot = self._get_spot_price(target_date)
        symbols = self._generate_option_symbols(target_date)
        if not symbols:
            with self._lock:
                self._stats["skipped"] += 1
            return True, f"无有效期权合约 (target_date={target_date})"
        
        # 下载并合并多合约数据
        all_klines = []
        for symbol in symbols:
            success, klines, msg = self._fetch_daily_klines(symbol, target_date, KLType.K_1M)
            if not success:
                with self._lock:
                    self._stats["failed"] += 1
                return False, f"{symbol}: {msg}"
            if klines:
                all_klines.extend(klines)
        
        if not all_klines:
            with self._lock:
                self._stats["skipped"] += 1
            return True, "无有效数据"
        
        # 按日保存
        year_month = target_date.strftime("%Y%m")
        month_subdir = Path(self.config.output_dir) / year_month
        month_subdir.mkdir(parents=True, exist_ok=True)
        
        daily_file = month_subdir / f"US.QQQ_opt_{date_str}.csv"
        df = pd.DataFrame(all_klines)
        df = df[["datetime", "open", "high", "low", "close", "volume", "symbol"]]
        df.to_csv(daily_file, index=False, encoding='utf-8-sig')
        
        # 保存断点
        self._save_checkpoint(date_str, str(daily_file))
        
        with self._lock:
            self._stats["downloaded"] += 1
        
        return True, f"成功下载 {len(all_klines)} 条 (合约: {len(symbols)} 个)"
    
    # ==================== 按月合并 ====================
    def _merge_monthly_files(self, year: int, month: int):
        """合并月度文件"""
        month_str = f"{year}{month:02d}"
        year_month_dir = Path(self.config.output_dir) / month_str
        monthly_file = Path(self.config.output_dir) / f"US.QQQ_opt_{month_str}_1min.csv"
        
        if not year_month_dir.exists():
            logger.warning(f"  ⚠️ {month_str} 子目录不存在，跳过合并")
            return
        
        # 查找该月所有日文件
        daily_files = sorted([
            f for f in year_month_dir.glob(f"US.QQQ_opt_{year}{month:02d}*.csv")
            if f.is_file() and "_1min.csv" not in f.name
        ])
        
        if not daily_files:
            logger.warning(f"  ⚠️ {month_str} 无日文件")
            return
        
        # 合并
        dfs = [pd.read_csv(f) for f in daily_files]
        monthly_df = pd.concat(dfs, ignore_index=True)
        monthly_df.sort_values("datetime", inplace=True)
        
        # 保存
        monthly_df.to_csv(monthly_file, index=False, encoding='utf-8-sig')
        logger.info(f"  ✅ {month_str} 月度文件已生成: {len(monthly_df)} 条 @ {monthly_file}")
    
    # ==================== 公开下载接口 ====================
    def download(self):
        """执行下载"""
        logger.info(f"🚀 开始下载富途期权数据 | 标的: {self.config.underlying}")
        logger.info(f"   日期范围: {self.config.start_date} ~ {self.config.end_date}")
        logger.info(f"   存储目录: {self.config.output_dir} | 并发数: {self.config.max_workers}")
        
        dates = self._parse_date_range()
        self._stats["total"] = len(dates)
        logger.info(f"📅 共 {len(dates)} 个交易日待处理")
        
        # 分批处理（防内存溢出）
        batches = [dates[i:i + self.config.max_workers * 2] for i in range(0, len(dates), self.config.max_workers * 2)]
        
        for batch_idx, batch_dates in enumerate(batches, 1):
            logger.info(f"📦 批次 {batch_idx}/{len(batches)}: {batch_dates[0]} ~ {batch_dates[-1]}")
            
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
                    logger.info(f"  {status} {date_str}: {msg}")
            
            # 按月合并（每批结束后尝试合并已完成的月份）
            months = set((d.year, d.month) for d in batch_dates)
            for year, month in sorted(months):
                self._merge_monthly_files(year, month)
        
        # 打印统计
        logger.info("📊 下载统计")
        logger.info(f"   总天数: {self._stats['total']}")
        logger.info(f"   成功下载: {self._stats['downloaded']}")
        logger.info(f"   跳过 (已存在): {self._stats['skipped']}")
        logger.info(f"   失败: {self._stats['failed']}")
        if self._stats['total'] > 0:
            rate = (self._stats['downloaded'] + self._stats['skipped']) / self._stats['total'] * 100
            logger.info(f"   成功率: {rate:.1f}%")

# ==================== CLI 入口 ====================
def main():
    """
    使用示例：
    # 下载默认范围（2024.5~2026.4）
    python -m src.data.futu_option_downloader
    
    # 自定义日期范围 + 参数
    python -m src.data.futu_option_downloader \
      --start 2025-01-01 \
      --end 2025-12-31 \
      --underlying US.QQQ \
      --offset 5.0 \
      --side both \
      --workers 2
    """
    parser = argparse.ArgumentParser(description="富途期权历史数据下载器")
    parser.add_argument("--start", default="2024-05-01", help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-30", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--underlying", default="US.QQQ", help="富途标的代码格式")
    parser.add_argument("--offset", type=float, default=5.0, help="期权行权价偏移量 (默认: ±$5)")
    parser.add_argument("--side", choices=['call', 'put', 'both'], default='both', help="期权方向")
    parser.add_argument("--workers", type=int, default=2, help="并发数 (建议 ≤2)")
    parser.add_argument("--output", default=None, help="输出目录 (默认: data/opt_klines)")
    parser.add_argument("--host", default="127.0.0.1", help="FutuOpenD 主机地址")
    parser.add_argument("--port", type=int, default=11111, help="FutuOpenD 端口")
    
    args = parser.parse_args()
    
    # 构建配置
    config = DownloaderConfig(
        underlying=args.underlying,
        option_offset=args.offset,
        option_side=args.side,
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        max_workers=args.workers,
        output_dir=args.output or str(Path(CONFIG.get("data_dir", Path.cwd() / "data")) / "opt_klines"),
        futu_host=args.host,
        futu_port=args.port
    )
    
    # 执行下载
    with FutuOptionDownloader(config) as downloader:
        downloader.download()

def tester():
    parser = argparse.ArgumentParser(description="富途期权历史数据下载器")
    parser.add_argument("--start", default="2024-05-01", help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-30", help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--underlying", default="US.QQQ", help="富途标的代码格式")
    parser.add_argument("--offset", type=float, default=5.0, help="期权行权价偏移量 (默认: ±$5)")
    parser.add_argument("--side", choices=['call', 'put', 'both'], default='both', help="期权方向")
    parser.add_argument("--workers", type=int, default=2, help="并发数 (建议 ≤2)")
    parser.add_argument("--output", default=None, help="输出目录 (默认: data/opt_klines)")
    parser.add_argument("--host", default="127.0.0.1", help="FutuOpenD 主机地址")
    parser.add_argument("--port", type=int, default=11111, help="FutuOpenD 端口")
    
    args = parser.parse_args()
    
    # 构建配置
    config = DownloaderConfig(
        underlying=args.underlying,
        option_offset=args.offset,
        option_side=args.side,
        start_date=date.fromisoformat(args.start),
        end_date=date.fromisoformat(args.end),
        max_workers=args.workers,
        output_dir=args.output or str(Path(CONFIG.get("data_dir", Path.cwd() / "data")) / "opt_klines"),
        futu_host=args.host,
        futu_port=args.port
    )

    fetcher = FutuOptionDownloader(config)
    target_date = datetime.strptime("2026-05-12 09:47:00","%Y-%m-%d %H:%M:%S")

    #price = fetcher._get_spot_price(target_date)
    #print(f"price : {price}")
    #option_list = fetcher._generate_option_symbols(target_date)
    #print(f"option list :{option_list}")

    # query option. option_list[0] #
    symbol = "US.QQQ260512P706000" #US.TSLA210716C600000
    result, klines, msg = fetcher._fetch_daily_klines(symbol, target_date, KLType.K_1M)
    print(f"result: {result},msg: {msg}, symbole={symbol}, klines: {klines}")
    fetcher.close()
        

if __name__ == "__main__":
    # python -m src.fetcher.futu_option_fetcher --start 2026-05-12 --end 2026-05-12
    main()
    #tester()