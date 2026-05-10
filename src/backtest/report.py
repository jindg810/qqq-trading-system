#!/usr/bin/env python3
import os
import json
import logging
import pandas as pd
from jinja2 import Environment, FileSystemLoader
from datetime import datetime

logger = logging.getLogger("backtest.report")

class ReportGenerator:
    def __init__(self, trades: list[dict], output_dir: str = "backtest/results"):
        self.trades = trades
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def calculate_metrics(self) -> dict:
        df = pd.DataFrame(self.trades)
        if df.empty:
            return {"error": "无交易记录"}
            
        wins = df[df["pnl"] > 0]
        losses = df[df["pnl"] <= 0]
        
        metrics = {
            "total_trades": len(df),
            "win_rate": len(wins) / len(df),
            "total_return": (df["pnl"].sum() / 100000) * 100,
            "profit_factor": abs(wins["pnl"].sum() / losses["pnl"].sum()) if not losses.empty else float('inf'),
            "avg_win_pct": wins["pnl_pct"].mean() * 100 if not wins.empty else 0,
            "avg_loss_pct": losses["pnl_pct"].mean() * 100 if not losses.empty else 0,
            "max_cons_losses": self._max_cons_loss(df),
            "avg_bars": df["bars_held"].mean(),
            "exit_dist": df["exit_reason"].value_counts(normalize=True).to_dict()
        }
        logger.info(f"📊 指标计算完成 | 胜率: {metrics['win_rate']*100:.1f}%")
        return metrics

    def _max_cons_loss(self, df):
        mx, cur = 0, 0
        for p in df["pnl"]:
            cur = cur + 1 if p <= 0 else 0
            mx = max(mx, cur)
        return mx

    def save_results(self, metrics: dict):
        # 保存 CSV
        csv_path = os.path.join(self.output_dir, f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        pd.DataFrame(self.trades).to_csv(csv_path, index=False)
        
        # 保存 JSON
        json_path = os.path.join(self.output_dir, "metrics.json")
        with open(json_path, "w") as f:
            json.dump(metrics, f, indent=2, default=str)
            
        # 渲染 HTML (模板分离)
        tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
        env = Environment(loader=FileSystemLoader(tpl_dir))
        template = env.get_template("../templates/backtest_report.html")
        html_path = os.path.join(self.output_dir, "report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(template.render(metrics=metrics, trades=self.trades))
            
        logger.info(f"✅ 报告已生成: {html_path}")
        return html_path