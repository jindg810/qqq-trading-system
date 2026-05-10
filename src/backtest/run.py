#!/usr/bin/env python3
import sys
import os
import logging
from config import CONFIG
from logger import get_logger
from backtest.data_loader import DataLoader
from backtest.engine import BacktestEngine
from backtest.report import ReportGenerator

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
logger = get_logger("backtest.main")

def main():
    csv_path = "data/qqq_1min.csv"  # 替换为你的历史数据路径
    if not os.path.exists(csv_path):
        logger.error(f"❌ 未找到数据文件: {csv_path}")
        sys.exit(1)

    try:
        # 1. 加载清洗数据
        bars = DataLoader.load_and_clean(csv_path)
        
        # 2. 运行回测
        engine = BacktestEngine(initial_capital=100000.0)
        trades = engine.run(bars)
        
        # 3. 生成报告
        reporter = ReportGenerator(trades)
        metrics = reporter.calculate_metrics()
        reporter.save_results(metrics)
        
        print("\n🎉 回测完成！请查看 backtest/results/report.html")
    except Exception as e:
        logger.critical(f"💥 回测执行失败: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()