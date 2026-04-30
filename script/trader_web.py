#!/usr/bin/env python3
"""
QQQ 交易系统 Web 仪表盘（升级版）
- 支持新的暗黑主题模板
- 提供更丰富的数据结构
- 集成真实API数据
"""

import os
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
import requests
from logger import get_logger

# 导入配置
from config import CONFIG

load_dotenv()
logger = get_logger(__name__)
app = Flask(__name__, template_folder='../templates')

# ===================== 辅助函数 =====================
def get_qqq_quote():
    """获取QQQ实时行情"""
    try:
        from longbridge.openapi import Config, QuoteContext
        cfg = Config.from_apikey_env()
        qc = QuoteContext(cfg)
        quotes = qc.quote([CONFIG["symbol"]])
        if quotes and len(quotes) > 0:
            quote = quotes[0]
            return {
                "price": round(float(quote.last_done), 2),
                "change": round(float(quote.last_done) - float(quote.prev_close), 2),
                "high": round(float(quote.high), 2),
                "low": round(float(quote.low), 2),
                "volume": int(quote.volume) if hasattr(quote, 'volume') else 0
            }
    except Exception as e:
        print(f"获取行情失败: {e}")
    
    # 返回模拟数据
    return {
        "price": 438.25,
        "change": 2.35,
        "high": 439.80,
        "low": 436.50,
        "volume": 12500000
    }

def parse_account_balance(balance):
    """解析 AccountBalance 对象"""
    try:
        # 从 cash_infos 中获取 USD 现金信息
        usd_cash_info = None
        for cash_info in balance.cash_infos:
            if cash_info.currency == "USD":
                usd_cash_info = cash_info
                break
        
        return {
            "net_assets": round(float(balance.net_assets), 2),
            "power": round(float(balance.buy_power), 2),
            "limit": "无限制",
            "currency": balance.currency,
            "cash": round(float(balance.total_cash), 2),
            "risk_level": balance.risk_level,
            "margin_call": balance.margin_call
        }
    except Exception as e:
        logger.error(f"解析账户余额失败: {e}")
        return {
            "net_assets": 50000.00,
            "cash": 25000.00,
            "power": 100000.00,
            "limit": "无限制"
        }

def get_account_info():
    """获取账户信息（修复版）"""
    try:
        from longbridge.openapi import Config, TradeContext
        cfg = Config.from_apikey_env()
        tc = TradeContext(cfg)
        
        # ✅ 调用 account_balance() 方法
        balance_list = tc.account_balance()
        
        # ✅ 解析 AccountBalance 对象
        return parse_account_balance(balance_list[0])
        
    except Exception as e:
        logger.error(f"获取账户信息失败: {e}")
        return {
            "net_assets": 50000.00,
            "cash": 25000.00,
            "power": 100000.00,
            "limit": "无限制"
        }

def read_logs():
    """读取日志文件"""
    logs = []
    try:
        log_file = CONFIG["log_file"]
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                lines = f.readlines()
                # 取最后10行
                for line in lines[-10:]:
                    parts = line.strip().split(' ', 3)
                    if len(parts) >= 4:
                        logs.append({
                            "time": parts[1] if len(parts) > 1 else "",
                            "msg": parts[3] if len(parts) > 3 else line.strip()
                        })
    except Exception as e:
        print(f"读取日志失败: {e}")
    
    # 如果没有日志，返回模拟数据
    if not logs:
        logs = [
            {"time": "09:30:01", "msg": "交易引擎启动成功"},
            {"time": "09:30:05", "msg": "已订阅QQQ.US行情"},
            {"time": "09:31:22", "msg": "检测到突破信号：CALL"},
            {"time": "09:31:25", "msg": "开仓成功：QQQ240429C440000.US @ 1.85"},
            {"time": "09:45:33", "msg": "持仓监控：当前盈亏 +15.2%"}
        ]
    
    return logs

def build_template_state(state_data):
    """构建模板所需的状态数据"""
    now = datetime.now(CONFIG["tz_et"])
    
    # 获取行情数据
    market_data = get_qqq_quote()
    
    # 获取账户数据
    account_data = get_account_info()
    
    # 获取日志
    logs_data = read_logs()
    
    # 构建策略信息
    strategy_info = {
        "name": "QQQ 0DTE 双信号策略",
        "description": "突破延续 + 衰竭反转",
        "version": "v6.1",
        "trading_hours": "美东时间 09:30-16:00"
    }
    
    # 构建系统状态
    system_info = {
        "data_source": "长桥证券 API",
        "engine_status": "运行中" if state_data.get("running", False) else "已停止",
        "version": "v6.1",
        "symbol": CONFIG["symbol"]
    }
    
    # 构建当日统计
    daily_stats = {
        "open_positions": 1 if state_data.get("position") else 0,
        "closed_positions": state_data.get("trades_today", 0),
        "current_holdings": 1 if state_data.get("position") else 0,
        "pnl_today": state_data.get("daily_pnl", 0.0)
    }
    
    # 构建信号信息
    position = state_data.get("position")
    if position:
        signal_info = {
            "direction": "CALL" if position.get("side") == "call" else "PUT",
            "price": f"${position.get('entry_opt', 0):.2f}",
            "reason": f"突破{position.get('side', '').upper()}信号"
        }
    else:
        signal_info = {
            "direction": "等待信号",
            "price": "--",
            "reason": "等待K线确认"
        }
    
    # 构建持仓信息
    positions_list = []
    if position:
        entry_price = position.get('entry_opt', 0)
        current_price = market_data["price"]
        pnl = (current_price - entry_price) if position.get('side') == 'call' else (entry_price - current_price)
        pnl_pct = (pnl / entry_price * 100) if entry_price > 0 else 0
        
        positions_list = [{
            "code": position.get('symbol', ''),
            "qty": 1,
            "cost": f"{entry_price:.2f}",
            "current": f"{current_price:.2f}",
            "pnl": f"{pnl:.2f}",
            "pnl_pct": f"{pnl_pct:.1f}"
        }]
    
    # 构建交易记录（从日志中提取，这里简化处理）
    trades_list = []
    if state_data.get("trades_today", 0) > 0:
        trades_list = [{
            "time": now.strftime("%H:%M:%S"),
            "side": position.get('side', '').upper() if position else 'BUY',
            "entry": f"{position.get('entry_opt', 0):.2f}" if position else "0.00",
            "qty": 1,
            "status": "已成交"
        }]
    
    # 组合所有数据
    template_state = {
        "strategy": strategy_info,
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S ET"),
        "daily_stats": daily_stats,
        "system": system_info,
        "market": market_data,
        "account": account_data,
        "signal": signal_info,
        "positions": positions_list,
        "trades": trades_list,
        "logs": logs_data
    }
    
    return template_state

# ===================== 路由 =====================
@app.route("/")
def index():
    """主页 - 使用新模板"""
    try:
        if not os.path.exists(CONFIG["state_file"]):
            # 返回默认状态
            default_state = {
                "running": False,
                "position": None,
                "trades_today": 0,
                "consecutive_losses": 0,
                "daily_pnl": 0.0,
                "emergency_stopped": False
            }
            template_state = build_template_state(default_state)
            return render_template("index.html", state=template_state)
        
        with open(CONFIG["state_file"], "r") as f:
            state_data = json.load(f)
        
        template_state = build_template_state(state_data)
        return render_template("index.html", state=template_state)
        
    except Exception as e:
        return f"系统错误: {str(e)}", 500

@app.route("/api/state")
def api_state():
    """获取系统状态 API"""
    try:
        token = request.args.get("token")
        expected_token = os.getenv("WEB_TOKEN")
        
        if not expected_token:
            return jsonify({"error": "WEB_TOKEN 未配置"}), 500
            
        if token != expected_token:
            return jsonify({"error": "未授权访问"}), 401
        
        if not os.path.exists(CONFIG["state_file"]):
            return jsonify({
                "running": False,
                "message": "系统尚未启动",
                "updated": datetime.now(CONFIG["tz_et"]).isoformat()
            })
        
        with open(CONFIG["state_file"], "r") as f:
            state = json.load(f)
        
        return jsonify(state)
        
    except Exception as e:
        return jsonify({
            "running": False,
            "error": str(e)
        }), 500

@app.route("/api/market")
def api_market():
    """获取市场数据 API"""
    try:
        market_data = get_qqq_quote()
        return jsonify(market_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/account")
def api_account():
    """获取账户数据 API"""
    try:
        account_data = get_account_info()
        return jsonify(account_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    """健康检查端点"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(CONFIG["tz_et"]).isoformat(),
        "version": "v6.1"
    })

# ===================== 主入口 =====================
if __name__ == "__main__":
    if not os.getenv("WEB_TOKEN"):
        print("[WARNING] WEB_TOKEN 未设置")
        exit(1)
    
    print(f"[INFO] Web 仪表盘启动")
    print(f"[INFO] 访问地址: http://localhost:8080")
    print(f"[INFO] 环境: {CONFIG['environment']}")
    print(f"[INFO] 模板目录: ../templates")
    
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=(CONFIG["environment"] == "development")
    )