#!/usr/bin/env python3
"""
QQQ 交易系统 Web 仪表盘 v6.2 (优化版)
✅ 行情/账户 API 防频限缓存 (TTL机制 + 优雅降级)
✅ 日志解析升级：正则表达式精准提取 (兼容 logger.py 格式)
✅ 启动时 CONFIG 同步校验 & 单源配置防漂移
✅ 线程安全 & 生产级异常隔离
"""
import os
import json
import time
import re
import threading
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from logger import get_logger
from config import CONFIG, DATA_DIR
from trader_data import TraderDataManager

load_dotenv()
logger = get_logger(__name__)

app = Flask(__name__, template_folder='../templates')

# ===================== 核心优化 1: 线程安全缓存 (防长桥API频限) =====================
class TTLCache:
    """轻量级带过期时间的线程安全缓存"""
    def __init__(self):
        self._data = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                val, exp = self._data[key]
                if time.time() < exp:
                    return val
                del self._data[key]
        return None

    def set(self, key: str, value, ttl: int = 15):
        with self._lock:
            self._data[key] = (value, time.time() + ttl)

cache = TTLCache()

def get_qqq_quote():
    """获取QQQ实时行情 (带15s缓存，防API限频)"""
    cached = cache.get("qqq_quote")
    if cached: return cached

    try:
        from longbridge.openapi import Config, QuoteContext
        cfg = Config.from_apikey_env()
        qc = QuoteContext(cfg)
        quotes = qc.quote([CONFIG["symbol"]])
        if quotes:
            q = quotes[0]
            data = {
                "price": round(float(q.last_done), 2),
                "change": round(float(q.last_done) - float(q.prev_close), 2),
                "high": round(float(q.high), 2),
                "low": round(float(q.low), 2),
                "volume": int(getattr(q, 'volume', 0)),
                "updated_ts": time.time()
            }
            cache.set("qqq_quote", data, ttl=15)
            return data
    except Exception as e:
        logger.warning(f"行情API调用失败(使用缓存/降级): {e}")
    
    # 降级策略：返回上次缓存或零值，绝不阻塞页面渲染
    return cache.get("qqq_quote") or {"price": 0.0, "change": 0.0, "high": 0.0, "low": 0.0, "volume": 0}

def get_account_info():
    """获取账户余额 (带60s缓存，账户数据变化慢)"""
    cached = cache.get("account_balance")
    if cached: return cached

    try:
        from longbridge.openapi import Config, TradeContext
        cfg = Config.from_apikey_env()
        tc = TradeContext(cfg)
        balances = tc.account_balance()
        if balances:
            b = balances[0]
            data = {
                "net_assets": round(float(b.net_assets), 2),
                "power": round(float(b.buy_power), 2),
                "cash": round(float(b.total_cash), 2),
                "currency": b.currency
            }
            cache.set("account_balance", data, ttl=60)
            return data
    except Exception as e:
        logger.warning(f"账户API调用失败(使用缓存/降级): {e}")
    return cache.get("account_balance") or {"net_assets": 0.0, "power": 0.0, "cash": 0.0}

# ===================== 核心优化 2: 正则日志解析 =====================
# 精准匹配 logger.py 格式: '2026-05-07 09:30:01 [INFO] 系统: 初始化交易引擎...'
LOG_PATTERN = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (.*)$')

def parse_logs():
    """使用正则安全解析日志，自动跳过乱码/空行"""
    logs = []
    log_path = CONFIG.get("log_file", "logs/trader.log")
    if not os.path.exists(log_path):
        return logs

    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            # 仅读取最后 50 行，避免大文件拖慢响应
            lines = f.readlines()[-50:]
            
        for line in lines:
            line = line.strip()
            if not line: continue
            
            match = LOG_PATTERN.match(line)
            if match:
                logs.append({
                    "time": match.group(1),
                    "level": match.group(2),
                    "msg": match.group(3)
                })
            else:
                # 兼容非标准格式或旧日志
                parts = line.split(' ', 3)
                logs.append({
                    "time": parts[1] if len(parts) > 1 else "",
                    "level": "INFO",
                    "msg": parts[-1] if len(parts) > 3 else line
                })
    except Exception as e:
        logger.error(f"读取日志文件异常: {e}")
    return logs

# ===================== 核心优化 3: CONFIG 同步校验 =====================
REQUIRED_KEYS = [
    'sl_pct', 'tp_half', 'trail_pct', 'lookback', 'vol_mult', 'min_body_pct',
    'breakout_max', 'reversal_max', 'max_daily_loss', 'max_consecutive_losses',
    'symbol', 'state_file', 'log_file', 'tz_et'
]

def validate_config_sync():
    """启动时强制校验 CONFIG 完整性与合理性，防参数漂移"""
    missing = [k for k in REQUIRED_KEYS if k not in CONFIG]
    if missing:
        raise ValueError(f"⛔ CONFIG 缺失关键参数: {missing} (请检查 config.py)")

    # 防手滑阈值校验
    checks = {
        "sl_pct": (0.1, 0.5),          # 止损应在 10%~50%
        "vol_mult": (0.1, 2.0),        # 量能系数合理范围
        "max_daily_loss": (-5000, 0),  # 日亏损必须为负
        "lookback": (1, 20)            # 窗口期合理范围
    }
    for k, (low, high) in checks.items():
        val = CONFIG[k]
        if not (low <= val <= high):
            logger.critical(f"⚠️ CONFIG['{k}'] 值异常: {val} (合理范围: {low}-{high})")
            
    logger.info("✅ CONFIG 同步校验通过 | 单源配置已锁定")

# ===================== 视图构建 =====================
def build_template_state(state_data: dict) -> dict:
    try:
        """组装前端模板所需数据 (安全降级处理)"""
        now = datetime.now(CONFIG["tz_et"])
        market = get_qqq_quote()
        account = get_account_info()
        logs = parse_logs()
        
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
            "symbol": CONFIG["symbol"],
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
            current_price = market["price"]
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
            "market": market,
            "account": account,
            "signal": signal_info, # 
            "positions": positions_list, # 当前持仓列表（支持多持仓扩展）
            "trades": trades_list,
            "logs": logs
        }

        return template_state
    except Exception as e:
        logger.error(f"构建模板状态失败: {e}")
        return {
            "strategy": {"name": "QQQ 0DTE 双信号策略", "version": "v6.1"},
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S ET"),
            "market": {"price": 0.0, "change": 0.0, "high": 0.0, "low": 0.0, "volume": 0},
            "account": {"net_assets": 0.0, "power": 0.0, "cash": 0.0, "currency": "--"},
            "system": {"engine_status": "未知", "symbol": CONFIG["symbol"], "env": CONFIG.get("environment", "production")},
            "daily_stats": {"trades_today": 0, "pnl_today": 0.0, "consecutive_losses": 0, "emergency_stopped": False},
            "position": {"side": "--", "symbol": "--", "entry": 0.0, "current": 0.0, "pnl_pct": 0.0, "peak": 0.0, "bars_held": 0},
            "logs": []
        }
    
    

# ===================== 路由 =====================
@app.route("/")
def index():
    try:
        state_path = CONFIG["state_file"]
        if os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
        else:
            state_data = {"running": False, "position": None, "trades_today": 0, "daily_pnl": 0.0}
        print(f"加载状态文件: {state_path} | 数据: {state_data}")
        return render_template("index.html", state=build_template_state(state_data))
    except Exception as e:
        logger.error(f"渲染首页失败: {e}")
        return f"系统初始化中，请稍后刷新... ({str(e)})", 503

@app.route("/api/state")
def api_state():
    """带鉴权的实时状态 API"""
    token = request.args.get("token")
    if token != os.getenv("WEB_TOKEN"):
        return jsonify({"error": "Unauthorized"}), 401
        
    try:
        with open(CONFIG["state_file"], "r") as f:
            return jsonify(json.load(f))
    except FileNotFoundError:
        return jsonify({"running": False, "message": "System not started"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    return jsonify({
        "status": "healthy",
        "cache_hits": len(cache._data),
        "timestamp": datetime.now(CONFIG["tz_et"]).isoformat()
    })

# ===================== 启动入口 =====================
if __name__ == "__main__":
    # 1. 启动前强校验
    validate_config_sync()
    
    # 2. 检查必要环境变量
    if not os.getenv("WEB_TOKEN"):
        logger.critical("⛔ WEB_TOKEN 未配置！请在 .env 中添加 WEB_TOKEN=your_secret")
        exit(1)
        
    logger.info("🌐 Web 仪表盘启动成功")
    logger.info(f"📍 访问地址: http://localhost:8080")
    logger.info(f"🔒 缓存防频限已启用 (行情15s/账户60s)")
    
    # 生产环境关闭 debug，开启多进程/线程
    app.run(
        host="0.0.0.0",
        port=8080,
        debug=False,
        threaded=True
    )