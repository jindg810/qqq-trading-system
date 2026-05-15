#!/usr/bin/env python3
"""
专业级分钟级期权链数据下载器（长桥 API 版）

数据字段说明（严格遵循 openapi.txt 定义）：
==========================================
标的资产字段：
- underlying_price: 标的最新价格（从 Quote 获取）
- underlying_symbol: 标的代码（如 "QQQ.US"）

期权基础字段（来自 OptionQuote）：
- symbol: 期权代码（如 "QQQ230317C160000.US"）
- last_done: 最新成交价（核心价格字段）
- prev_close: 昨收价
- open: 开盘价
- high: 最高价
- low: 最低价
- volume: 成交量
- turnover: 成交额
- trade_status: 交易状态
- implied_volatility: 隐含波动率（IV，核心希腊字母计算基础）
- open_interest: 未平仓合约数（持仓量）
- expiry_date: 到期日
- strike_price: 行权价
- contract_multiplier: 合约乘数
- contract_type: 期权类型（美式/欧式）
- contract_size: 合约大小
- direction: 期权方向（Call/Put）
- historical_volatility: 历史波动率
- underlying_symbol: 标的代码

计算字段（通过 Black-Scholes 模型计算）：
- delta: 标的价格变动对期权价格的影响
- gamma: delta 的变化率（加速度）
- theta: 时间衰减（每天损失的价值）
- vega: 波动率变动对期权价格的影响
==========================================
"""

import os
import json
import time
import csv
import threading
from datetime import datetime, date, time as dt_time
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
from pathlib import Path
import math

from longbridge.openapi import (
    Config, QuoteContext, OptionQuote, StrikePriceInfo,
    OptionDirection, OptionType, Market
)


@dataclass
class OptionDataPoint:
    """单个期权数据点（严格基于 OptionQuote 实际字段）"""
    
    # ========== 基础信息 ==========
    timestamp: datetime           # 数据采集时间戳
    symbol: str                 # 期权代码
    underlying_symbol: str       # 标的代码（如 "QQQ.US"）
    underlying_price: float      # 标的最新价格
    strike_price: float         # 行权价
    option_type: str            # 'call' 或 'put'
    expiry_date: date           # 到期日
    
    # ========== 来自 OptionQuote 的实际字段 ==========
    last_done: float           # 最新成交价（核心价格）
    prev_close: float          # 昨收价
    open: float                # 开盘价
    high: float               # 最高价
    low: float                # 最低价
    volume: int               # 成交量
    turnover: float           # 成交额
    implied_volatility: float # 隐含波动率（IV，核心参数）
    open_interest: int        # 未平仓合约数（持仓量）
    contract_multiplier: float # 合约乘数
    contract_size: float      # 合约大小
    historical_volatility: float # 历史波动率
    
    # ========== 计算字段（通过 Black-Scholes 计算） ==========
    delta: Optional[float] = None   # Δ：标的价格变动1单位，期权价格变动多少
    gamma: Optional[float] = None   # Γ：标的价格变动1单位，delta变动多少
    theta: Optional[float] = None   # Θ：每过一天，期权损失多少价值
    vega: Optional[float] = None    # ν：波动率变动1%，期权价格变动多少


@dataclass
class DownloadStats:
    """下载统计信息"""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    options_downloaded: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


class ProfessionalOptionChainDownloader:
    """
    专业级期权链下载器
    
    设计原则：
    1. 严格遵循长桥 API 定义，不使用不存在的字段
    2. 只下载 0DTE 期权（当日到期）
    3. 只下载标的价格 ±$5 范围内的期权（流动性最好区域）
    4. 每分钟下载一次，避免 API 限流
    5. 希腊字母通过 Black-Scholes 模型从 IV 计算
    """
    
    def __init__(
        self,
        config: Config,
        underlying_symbol: str = "QQQ.US",
        output_dir: str = "./option_data",
        price_range: float = 5.0,      # ±$5 范围
        download_interval: int = 60,     # 每分钟下载一次
        max_retries: int = 3,
        risk_free_rate: float = 0.045,   # 无风险利率 4.5%
    ):
        """
        初始化下载器
        
        Args:
            config: 长桥配置
            underlying_symbol: 标的资产代码
            output_dir: 数据输出目录
            price_range: 行权价范围（标的价格 ± price_range）
            download_interval: 下载间隔（秒）
            max_retries: 最大重试次数
            risk_free_rate: 无风险利率（用于希腊字母计算）
        """
        self.underlying_symbol = underlying_symbol
        self.output_dir = Path(output_dir)
        self.price_range = price_range
        self.download_interval = download_interval
        self.max_retries = max_retries
        self.risk_free_rate = risk_free_rate
        
        # 创建输出目录
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # 初始化长桥上下文
        self.ctx = QuoteContext(config)
        
        # 统计信息
        self.stats = DownloadStats()
        
        # 线程锁（用于多线程安全）
        self._lock = threading.Lock()
        
        # 已处理的期权符号集合（避免重复下载）
        self.processed_symbols: Set[str] = set()
        
        # 当前交易日
        self.current_trading_date = date.today()
        
        # 输出文件路径
        self.current_output_file = self._get_output_filename()
        
        print(f"✅ 期权链下载器初始化完成")
        print(f"   标的: {underlying_symbol}")
        print(f"   价格范围: ±${price_range}")
        print(f"   下载间隔: {download_interval}秒")
        print(f"   输出目录: {output_dir}")
    
    def _get_output_filename(self) -> Path:
        """生成输出文件名（按日期）"""
        date_str = self.current_trading_date.isoformat()
        return self.output_dir / f"option_chain_{self.underlying_symbol}_{date_str}.csv"
    
    def _get_underlying_price(self) -> float:
        """
        获取标的最新价格
        
        Returns:
            标的最新价格（来自 Quote 的 last_done）
        """
        try:
            quotes = self.ctx.quote([self.underlying_symbol])
            if quotes:
                return float(quotes[0].last_done)
            else:
                raise ValueError(f"无法获取 {self.underlying_symbol} 的价格")
        except Exception as e:
            print(f"❌ 获取标的价格失败: {e}")
            raise
    
    def _get_today_expiry(self) -> Optional[date]:
        """
        获取今日到期的期权（0DTE）
        
        Returns:
            今日到期日，如果没有则返回 None
        """
        try:
            expiry_dates = self.ctx.option_chain_expiry_date_list(self.underlying_symbol)
            today = date.today()
            
            for expiry_date in expiry_dates:
                if expiry_date == today:
                    print(f"✅ 找到今日到期日: {expiry_date}")
                    return expiry_date
            
            print(f"⚠️ 今日 {today} 没有到期的期权")
            return None
            
        except Exception as e:
            print(f"❌ 获取到期日列表失败: {e}")
            return None
    
    def _get_strike_prices_in_range(
        self, 
        expiry_date: date, 
        current_price: float
    ) -> List[StrikePriceInfo]:
        """
        获取在价格范围内的行权价信息
        
        Args:
            expiry_date: 到期日
            current_price: 当前标的价格
            
        Returns:
            在价格范围内的行权价信息列表
        """
        try:
            all_strikes = self.ctx.option_chain_info_by_date(
                self.underlying_symbol, 
                expiry_date
            )
            
            lower_bound = current_price - self.price_range
            upper_bound = current_price + self.price_range
            
            filtered_strikes = []
            for strike_info in all_strikes:
                strike_price = float(strike_info.price)
                if lower_bound <= strike_price <= upper_bound:
                    filtered_strikes.append(strike_info)
            
            print(f"✅ 筛选出 {len(filtered_strikes)} 个行权价（范围: ${lower_bound:.2f} - ${upper_bound:.2f}）")
            return filtered_strikes
            
        except Exception as e:
            print(f"❌ 获取行权价信息失败: {e}")
            return []
    
    def _extract_option_symbols(
        self, 
        strike_infos: List[StrikePriceInfo]
    ) -> List[str]:
        """
        从行权价信息中提取期权符号
        
        Args:
            strike_infos: 行权价信息列表
            
        Returns:
            期权符号列表
        """
        symbols = []
        for strike_info in strike_infos:
            if strike_info.call_symbol:
                symbols.append(strike_info.call_symbol)
            if strike_info.put_symbol:
                symbols.append(strike_info.put_symbol)
        
        # 过滤掉已经处理过的符号
        new_symbols = [s for s in symbols if s not in self.processed_symbols]
        
        with self._lock:
            self.processed_symbols.update(new_symbols)
        
        return new_symbols
    
    def _download_option_quotes(self, symbols: List[str]) -> List[OptionQuote]:
        """
        下载期权报价（带重试机制）
        
        Args:
            symbols: 期权符号列表
            
        Returns:
            期权报价列表
        """
        if not symbols:
            return []
        
        for attempt in range(1, self.max_retries + 1):
            try:
                quotes = self.ctx.option_quote(symbols)
                self.stats.successful_requests += 1
                return quotes
                
            except Exception as e:
                print(f"⚠️ 第 {attempt}/{self.max_retries} 次下载失败: {e}")
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)  # 指数退避
                else:
                    self.stats.failed_requests += 1
                    print(f"❌ 下载期权报价失败，已达到最大重试次数")
                    return []
    
    def _calculate_time_to_expiry(self, expiry_date: date) -> float:
        """
        计算到期时间（年化）
        
        对于 0DTE 期权，这是关键参数！
        假设期权在美东时间 16:00 到期。
        
        Args:
            expiry_date: 到期日
            
        Returns:
            年化到期时间（年）
        """
        now = datetime.now()
        # 假设期权在到期日下午 4 点到期
        expiry_datetime = datetime.combine(expiry_date, dt_time(16, 0))
        
        # 计算剩余分钟数
        minutes_remaining = (expiry_datetime - now).total_seconds() / 60
        
        # 转换为年化（252个交易日 × 390分钟/天）
        # 注意：0DTE 期权可能只剩几分钟，必须确保不为负
        annualized_time = max(minutes_remaining / (252 * 390), 0.0001)  # 避免除零
        
        return annualized_time
    
    def _calculate_greeks(
        self,
        underlying_price: float,
        strike_price: float,
        time_to_expiry: float,
        implied_volatility: float,
        option_type: str
    ) -> Tuple[float, float, float, float]:
        """
        使用 Black-Scholes 模型计算希腊字母
        
        Args:
            underlying_price: 标的价格
            strike_price: 行权价
            time_to_expiry: 到期时间（年化）
            implied_volatility: 隐含波动率（来自 OptionQuote.implied_volatility）
            option_type: 'call' 或 'put'
            
        Returns:
            (delta, gamma, theta, vega)
        """
        try:
            # 避免无效输入
            if time_to_expiry <= 0 or implied_volatility <= 0:
                return 0.0, 0.0, 0.0, 0.0
            
            # d1 和 d2 计算
            d1 = (math.log(underlying_price / strike_price) + 
                   (self.risk_free_rate + 0.5 * implied_volatility ** 2) * time_to_expiry) / \
                  (implied_volatility * math.sqrt(time_to_expiry))
            
            d2 = d1 - implied_volatility * math.sqrt(time_to_expiry)
            
            # 标准正态分布的 PDF 和 CDF
            def norm_pdf(x):
                return math.exp(-0.5 * x ** 2) / math.sqrt(2 * math.pi)
            
            def norm_cdf(x):
                """标准正态分布的累积分布函数"""
                return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0
            
            # 计算希腊字母
            if option_type == 'call':
                delta = norm_cdf(d1)
                # Theta 公式（每天衰减）
                theta = (-underlying_price * norm_pdf(d1) * implied_volatility / 
                        (2 * math.sqrt(time_to_expiry)) -
                        self.risk_free_rate * strike_price * math.exp(-self.risk_free_rate * time_to_expiry) *
                        norm_cdf(d2)) / 365  # 转换为每日衰减
            else:  # put
                delta = norm_cdf(d1) - 1
                theta = (-underlying_price * norm_pdf(d1) * implied_volatility / 
                        (2 * math.sqrt(time_to_expiry)) +
                        self.risk_free_rate * strike_price * math.exp(-self.risk_free_rate * time_to_expiry) *
                        norm_cdf(-d2)) / 365  # 转换为每日衰减
            
            gamma = norm_pdf(d1) / (underlying_price * implied_volatility * math.sqrt(time_to_expiry))
            vega = underlying_price * norm_pdf(d1) * math.sqrt(time_to_expiry) / 100  # 每1%波动率变化
            
            return delta, gamma, theta, vega
            
        except Exception as e:
            print(f"⚠️ 计算希腊字母失败: {e}")
            return 0.0, 0.0, 0.0, 0.0
    
    def _convert_to_datapoint(
        self, 
        quote: OptionQuote, 
        underlying_price: float
    ) -> OptionDataPoint:
        """
        将 OptionQuote 转换为 OptionDataPoint
        
        重要：OptionQuote 中只有 implied_volatility，没有希腊字母。
        希腊字母必须通过 Black-Scholes 模型计算。
        
        Args:
            quote: 期权报价（来自长桥 API）
            underlying_price: 标的最新价格
            
        Returns:
            标准化的数据点
        """
        # 确定期权类型
        option_type = 'call' if quote.direction == OptionDirection.Call else 'put'
        
        # 计算到期时间（年化）
        time_to_expiry = self._calculate_time_to_expiry(quote.expiry_date)
        
        # 计算希腊字母
        delta, gamma, theta, vega = self._calculate_greeks(
            underlying_price=underlying_price,
            strike_price=float(quote.strike_price),
            time_to_expiry=time_to_expiry,
            implied_volatility=float(quote.implied_volatility),
            option_type=option_type
        )
        
        return OptionDataPoint(
            timestamp=datetime.now(),
            symbol=quote.symbol,
            underlying_symbol=quote.underlying_symbol,
            underlying_price=underlying_price,
            strike_price=float(quote.strike_price),
            option_type=option_type,
            expiry_date=quote.expiry_date,
            
            # 来自 OptionQuote 的实际字段
            last_done=float(quote.last_done),
            prev_close=float(quote.prev_close),
            open=float(quote.open),
            high=float(quote.high),
            low=float(quote.low),
            volume=quote.volume,
            turnover=float(quote.turnover),
            implied_volatility=float(quote.implied_volatility),
            open_interest=quote.open_interest,
            contract_multiplier=float(quote.contract_multiplier),
            contract_size=float(quote.contract_size),
            historical_volatility=float(quote.historical_volatility),
            
            # 计算字段
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega
        )
    
    def _save_to_csv(self, datapoints: List[OptionDataPoint]):
        """
        保存数据点到 CSV 文件
        
        Args:
            datapoints: 数据点列表
        """
        if not datapoints:
            return
        
        file_exists = self.current_output_file.exists()
        
        with open(self.current_output_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                # 基础信息
                'timestamp', 'symbol', 'underlying_symbol', 'underlying_price',
                'strike_price', 'option_type', 'expiry_date',
                
                # 期权报价字段
                'last_done', 'prev_close', 'open', 'high', 'low',
                'volume', 'turnover', 'implied_volatility', 'open_interest',
                'contract_multiplier', 'contract_size', 'historical_volatility',
                
                # 希腊字母
                'delta', 'gamma', 'theta', 'vega'
            ])
            
            if not file_exists:
                writer.writeheader()
            
            for dp in datapoints:
                writer.writerow({
                    # 基础信息
                    'timestamp': dp.timestamp.isoformat(),
                    'symbol': dp.symbol,
                    'underlying_symbol': dp.underlying_symbol,
                    'underlying_price': dp.underlying_price,
                    'strike_price': dp.strike_price,
                    'option_type': dp.option_type,
                    'expiry_date': dp.expiry_date.isoformat(),
                    
                    # 期权报价字段
                    'last_done': dp.last_done,
                    'prev_close': dp.prev_close,
                    'open': dp.open,
                    'high': dp.high,
                    'low': dp.low,
                    'volume': dp.volume,
                    'turnover': dp.turnover,
                    'implied_volatility': dp.implied_volatility,
                    'open_interest': dp.open_interest,
                    'contract_multiplier': dp.contract_multiplier,
                    'contract_size': dp.contract_size,
                    'historical_volatility': dp.historical_volatility,
                    
                    # 希腊字母
                    'delta': dp.delta,
                    'gamma': dp.gamma,
                    'theta': dp.theta,
                    'vega': dp.vega
                })
    
    def run_once(self) -> bool:
        """
        执行一次下载（非阻塞）
        
        Returns:
            是否成功执行
        """
        try:
            # 1. 获取标的价格
            underlying_price = self._get_underlying_price()
            print(f"📈 当前标的价格: ${underlying_price:.2f}")
            
            # 2. 获取今日到期日（0DTE）
            expiry_date = self._get_today_expiry()
            if expiry_date is None:
                print("⚠️ 今日没有到期期权，跳过本次下载")
                return False
            
            # 3. 获取价格范围内的行权价
            strike_infos = self._get_strike_prices_in_range(
                expiry_date, 
                underlying_price
            )
            
            if not strike_infos:
                print("⚠️ 没有符合条件的行权价")
                return False
            
            # 4. 提取期权符号
            option_symbols = self._extract_option_symbols(strike_infos)
            print(f"📋 准备下载 {len(option_symbols)} 个期权")
            
            if not option_symbols:
                print("⚠️ 所有期权都已下载过，跳过")
                return True
            
            # 5. 下载期权报价
            option_quotes = self._download_option_quotes(option_symbols)
            
            if not option_quotes:
                print("⚠️ 没有获取到期权报价")
                return False
            
            # 6. 转换为数据点
            datapoints = [
                self._convert_to_datapoint(quote, underlying_price)
                for quote in option_quotes
            ]
            
            # 7. 保存到 CSV
            self._save_to_csv(datapoints)
            
            # 8. 更新统计
            with self._lock:
                self.stats.options_downloaded += len(datapoints)
            
            print(f"✅ 成功下载 {len(datapoints)} 个期权数据点")
            return True
            
        except Exception as e:
            print(f"❌ 执行下载失败: {e}")
            return False
    
    def run_continuous(self, duration_hours: Optional[float] = None):
        """
        连续运行下载器
        
        Args:
            duration_hours: 运行时长（小时），None 表示无限运行
        """
        print(f"🚀 开始连续下载期权链数据")
        
        self.stats.start_time = datetime.now()
        
        end_time = None
        if duration_hours:
            end_time = datetime.now().timestamp() + duration_hours * 3600
        
        try:
            while True:
                # 检查是否到达结束时间
                if end_time and datetime.now().timestamp() >= end_time:
                    print(f"⏰ 已达到设定的运行时长 {duration_hours} 小时")
                    break
                
                # 执行一次下载
                success = self.run_once()
                self.stats.total_requests += 1
                
                # 打印统计
                self._print_stats()
                
                # 等待下次下载
                if success:
                    print(f"⏳ 等待 {self.download_interval} 秒后进行下一次下载...")
                    time.sleep(self.download_interval)
                else:
                    # 如果失败，等待较短时间后重试
                    print(f"⏳ 下载失败，30秒后重试...")
                    time.sleep(30)
                    
        except KeyboardInterrupt:
            print(f"\n🛑 用户中断，停止下载")
        except Exception as e:
            print(f"❌ 下载器异常: {e}")
        finally:
            self.stats.end_time = datetime.now()
            self._print_final_stats()
    
    def _print_stats(self):
        """打印当前统计信息"""
        elapsed = datetime.now() - self.stats.start_time if self.stats.start_time else None
        
        print(f"\n📊 下载统计:")
        print(f"   运行时长: {elapsed}")
        print(f"   总请求数: {self.stats.total_requests}")
        print(f"   成功请求: {self.stats.successful_requests}")
        print(f"   失败请求: {self.stats.failed_requests}")
        print(f"   期权数据点: {self.stats.options_downloaded}")
        if self.stats.total_requests > 0:
            success_rate = (self.stats.successful_requests / self.stats.total_requests) * 100
            print(f"   成功率: {success_rate:.1f}%")
    
    def _print_final_stats(self):
        """打印最终统计信息"""
        print(f"\n" + "="*60)
        print(f"📊 最终统计报告")
        print("="*60)
        self._print_stats()
        print(f"   开始时间: {self.stats.start_time}")
        print(f"   结束时间: {self.stats.end_time}")
        print("="*60)


def main():
    """主函数 - 演示如何使用下载器"""
    import argparse
    
    parser = argparse.ArgumentParser(description="专业级期权链数据下载器")
    parser.add_argument("--symbol", default="QQQ.US", help="标的资产代码")
    parser.add_argument("--dir", default="./option_data", help="输出目录")
    parser.add_argument("--range", type=float, default=5.0, help="价格范围（±$）")
    parser.add_argument("--interval", type=int, default=60, help="下载间隔（秒）")
    parser.add_argument("--hours", type=float, default=6.5, help="运行时长（小时，默认6.5小时交易时段）")
    
    args = parser.parse_args()
    
    # 从环境变量加载配置
    config = Config.from_apikey_env()
    
    # 创建下载器
    downloader = ProfessionalOptionChainDownloader(
        config=config,
        underlying_symbol=args.symbol,
        output_dir=args.dir,
        price_range=args.range,
        download_interval=args.interval
    )
    
    # 运行下载器（默认运行整个交易时段）
    downloader.run_continuous(duration_hours=args.hours)


if __name__ == "__main__":
    main()
    '''
    # 基本使用（下载 QQQ 期权，±$5 范围，每分钟一次，运行6.5小时）
    python option_downloader.py

    # 自定义参数
    python option_downloader.py \
        --symbol "QQQ.US" \
        --dir "./data/options" \
        --range 3.0 \  # 只下载 ±$3 范围内的期权
        --interval 30 \  # 每30秒下载一次
        --hours 3.5      # 只运行3.5小时
    '''