#!/usr/bin/env python3
"""
量化回测报告生成器 (v8.0)
✅ 职责单一：接收 BacktestResult → 计算机构级指标 → 渲染专业 HTML 报告
✅ 支持动态权益曲线 Chart.js 渲染、响应式布局、自动归档
"""
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from src.config import CONFIG
from src.logger import get_logger

logger = get_logger("backtest.report")

class ReportGenerator:
    def __init__(self, result):
        self.result = result
        self.trades_df = pd.DataFrame(result.trades) if result.trades else pd.DataFrame()
        self.equity_df = pd.DataFrame(result.equity_curve) if result.equity_curve else pd.DataFrame()
        
        self.output_dir = Path(str(CONFIG["data_dir"] / "backtest_rpt"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.template_dir = str(CONFIG["base_dir"] / "templates")

    def generate(self) -> Tuple[str, Dict[str, Any]]:
        """生成完整 HTML 报告"""
        if self.trades_df.empty:
            logger.warning("⚠️ 无交易记录，生成空报告")
            metrics = {"total_trades": 0, "win_rate": 0.0, "total_return": 0.0}
        else:
            metrics = self._calculate_metrics()
            
        html_path = self._render_html(metrics)
        self._save_csv()
        logger.info(f"✅ 报告已生成: {html_path}")
        return str(html_path), metrics

    def _calculate_metrics(self) -> dict:
        df = self.trades_df
        # ✅ 修复3：空曲线兜底，防 KeyError
        equity = self.equity_df["equity"] if not self.equity_df.empty else pd.Series([100000.0])
        
        # ✅ 修复2：pandas 正确判断列是否存在
        if "pnl_pct" not in df.columns: df["pnl_pct"] = 0.0
        if "bars_held" not in df.columns: df["bars_held"] = 0
        wins, losses = df[df["pnl"] > 0], df[df["pnl"] <= 0]
        pf = wins["pnl"].sum() / abs(losses["pnl"].sum()) if not losses.empty else float('inf')
        
        # 时间序列指标
        rets = equity.pct_change().dropna()
        rf_daily = 0.04 / 252
        # ✅ 修复4：动态年化因子（适配分钟级/日终采样）
        n_periods = len(equity) - 1
        annual_factor = np.sqrt(252 / max(n_periods, 1)) if n_periods > 0 else 1.0
        sharpe = (rets.mean() - rf_daily) / rets.std() * annual_factor if rets.std() > 0 and len(rets) > 1 else 0.0

        mdd = ((equity.cummax() - equity) / equity.cummax()).min() if len(equity) > 1 else 0.0
        cagr = (equity.iloc[-1] / equity.iloc[0]) ** (252 / max(n_periods, 1)) - 1 if len(equity) > 1 else 0.0
        
        return {
            "total_trades": len(df),
            "win_rate": len(wins) / len(df) if len(df) > 0 else 0,
            "total_return": (equity.iloc[-1] / equity.iloc[0] - 1) * 100,
            "profit_factor": pf,
            "sharpe_ratio": sharpe,
            "max_drawdown": mdd * 100,
            "calmar_ratio": cagr / abs(mdd) if mdd != 0 else 0,
            "avg_win": wins["pnl"].mean() if not wins.empty else 0,
            "avg_loss": abs(losses["pnl"].mean()) if not losses.empty else 0,
            "max_cons_loss": self._max_consecutive_loss(df),
            "expectancy": df["pnl"].mean(),
            "exit_dist": df["exit_reason"].value_counts(normalize=True).to_dict()
        }

    def _render_html(self, metrics: dict) -> Path:
        env = Environment(loader=FileSystemLoader(self.template_dir))
        template = env.get_template("backtest_report.html")
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = self.output_dir / f"report_{ts}.html"
        
        # 注入 Chart.js 所需数据
        equity_json = self.equity_df.to_json(orient="records", date_format="iso") if not self.equity_df.empty else "[]"
        
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(template.render(
                metrics=metrics,
                trades=self.trades_df.to_dict(orient="records"),
                equity_json=equity_json,
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M")
            ))
        return html_path

    def _save_csv(self):
        if not self.trades_df.empty:
            self.trades_df.to_csv(self.output_dir / "trades_detail.csv", index=False, encoding="utf-8-sig")

    def _max_consecutive_loss(self, df: pd.DataFrame) -> int:
        mx, cur = 0, 0
        for p in df["pnl"]:
            cur = cur + 1 if p <= 0 else 0
            mx = max(mx, cur)
        return mx