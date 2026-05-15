#!/usr/bin/env python3
"""回测主入口（串联 Engine 与 Report）"""
import argparse
from datetime import date
from pathlib import Path
from src.backtest.engine import BacktestEngine, BacktestConfig
from src.backtest.report import ReportGenerator


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="QQQ 0DTE 专业回测引擎")
    parser.add_argument("--start", default="2024-05-01", help="回测起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-05-31", help="回测结束日期 (YYYY-MM-DD)")
    parser.add_argument("--data-dir", default=str(Path.cwd()/"data/klines"), help="K线数据目录")
    parser.add_argument("--capital", type=float, default=100000.0, help="初始资金")
    parser.add_argument("--slippage", type=float, default=0.02, help="期权滑点比例")
    args = parser.parse_args()

    # 构建配置
    cfg = BacktestConfig(
        data_dir = Path(args.data_dir),
        start_date = date.fromisoformat(args.start),
        end_date = date.fromisoformat(args.end),
        initial_capital = args.capital,
        slippage_pct = args.slippage
    )

    # 执行回测与报告生成
    engine = BacktestEngine(cfg)
    #engine.backtest_strategy() 

    results = engine.run()
    reporter = ReportGenerator(results)
    report_path, report_metrics = reporter.generate()
    print(f"📄 报告已保存至: {report_path}")
    
    # 控制台输出核心指标
    print("\n" + "="*50)
    print("📊 回测核心指标")
    print("="*50)
    for k, v in report_metrics.items():
        print(f"{k:<20}: {v}")
    print("="*50)
    '''''' 