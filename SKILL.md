---
name: qqq-trading-system
description: QQQ 0DTE期权实盘交易系统 - v6.1全过滤突破+衰竭反转双信号+动态止盈+长桥Python SDK。包含完整的部署、监控、诊断指南。
category: quant-trading
triggers:
  - QQQ交易系统
  - live_trader
  - trader_web
  - 期权交易
  - 长桥SDK交易
  - 交易系统部署
  - 交易系统监控
---

# QQQ 0DTE 交易系统

## 1. 系统概览

**版本**: v6.1 期权版（2026-04-23）
**策略**: 全过滤双向突破 + 衰竭反转双信号路径
**标的**: QQQ 0DTE虚值期权（±$2偏移）
**回测**: 761笔/2.3年，75.8%胜率，+3111%总收益

### 架构

```
live_trader.py          trader_web.py
(信号检测+下单)          (Flask Web仪表盘)
      │                       │
      ├──写──→ state.json ←──读
      ├──写──→ today.csv  ←──cron读
      └──写──→ records/*.json ←─读
```

### 文件清单

| 文件 | 职责 |
|------|------|
| `live_trader.py` | 核心：信号检测 + 下单 + 持仓监控 + 风控 |
| `trader_web.py` | Web仪表盘：Flask + HTML/CSS卡片式界面 |
| `update_gist.py` | 同步交易记录到GitHub Gist（供小程序读取） |
| `watchdog.py` | 守护进程：自动启动 + 崩溃重启 |
| `.env` | 密钥配置（**不入库**） |
| `state.json` | 实时状态共享（自动生成） |
| `today.csv` | 当日K线数据（自动生成） |
| `records/*.json` | 每日交易记录（自动生成） |

/home/jindaguai/works/qqq-trading-system/
├── script/
│   ├── trader.py          # 原 live_trader.py
│   ├── trader_web.py
│   ├── config.py
│   └── update_gist.py
├── data/
│   ├── state.json
│   ├── today.csv
│   └── records/
├── logs/
│   └── trader.log
├── .env
└── requirements.txt

---

## 2. 策略逻辑

### 双信号路径

| 路径 | 类型 | 方向 | 过滤条件 | 每天限制 |
|------|------|------|---------|---------|
| 突破延续 | 趋势跟随 | 顺势 | SMA20+量能+动量+实体+跳空 | 8次 |
| 衰竭反转 | 均值回归 | 逆势 | 跌幅≥0.2%+收阳/阴+实体 | 1次 |

### 4层过滤（突破信号）

```
1. SMA20趋势 — 做多价格>SMA20，做空<SMA20（通过率99.6%）
2. 量能确认 — 当前量 ≥ 20均量 × 0.8（通过率~70%）
3. 动量确认 — 当前K线同向（阳线做多/阴线做空）
4. K线实体 — 实体 ≥ 0.03%（通过率73.7%）
```

### 动态止盈

```
1. 止损 25% → 全部平仓
2. 盈利 ≥100% → 平仓一半（锁定利润）
3. 继续持有，追踪最高盈利
4. 从最高盈利回撤 ≥30% → 全部平仓
5. 超时 15根K线 → 全部平仓
```

### 期权合约生成

```python
from zoneinfo import ZoneInfo
TZ_ET = ZoneInfo("America/New_York")

def get_option_symbol(stock_price, direction, offset=2.0):
    now_et = datetime.now(TZ_ET)  # 必须用美东时间！
    if direction == 'call':
        strike = round(stock_price + offset)
        option_type = 'C'
    else:
        strike = round(stock_price - offset)
        option_type = 'P'
    expiry = now_et.strftime('%y%m%d')
    return f"QQQ{expiry}{option_type}{strike * 1000:06d}.US"
```

**格式要点**:
1. 必须有 `.US` 后缀
2. 行权价用 `round()` 取整到$1
3. 到期日用美东时间 `datetime.now(TZ_ET)`

### 检测频率

- **K线回调**: 每根1分钟K线收盘时检测
- **主动轮询**: 每20秒获取正股价格构建模拟K线检测
- **lookback**: 5根1分钟K线（5分钟窗口）

---

## 3. 部署与启动

### 环境要求

- Python 3.10+（推荐3.12+）
- Linux/WSL（Windows原生不推荐）

### 依赖安装

```bash
pip install longbridge flask numpy scipy
```

### 密钥配置

在项目目录创建 `.env` 文件，填入你自己的密钥：

```bash
# 长桥API（从 open.longportapp.com 申请）
LONGPORT_APP_KEY=你的APP_KEY
LONGPORT_APP_SECRET=你的APP_SECRET
LONGPORT_ACCESS_TOKEN=你的ACCESS_TOKEN
```

**`.env` 文件绝对不能提交到Git！**

### 启动命令

```bash
cd /path/to/QQQ_Live

# 启动交易引擎（后台）
PYTHONUNBUFFERED=1 python live_trader.py &

# 启动Web仪表盘（后台）
PYTHONUNBUFFERED=1 python trader_web.py &

# 或用watchdog守护（自动重启）
python watchdog.py
```

### 验证启动

```bash
# 检查进程
ps aux | grep -E 'live_trader|trader_web'

# 检查state.json
python -c "import json; d=json.load(open('state.json')); print(f'K线:{d[\"candle_count\"]} 更新:{d[\"updated\"]}')"

# 检查Web仪表盘（用你自己的token）
python -c "
import urllib.request, json
url = 'http://127.0.0.1:8080/api/state?token=你的WEB_TOKEN'
data = json.loads(urllib.request.urlopen(url).read())
print(f'连接:{data[\"connected\"]} 运行:{data[\"running\"]}')
"
```

---

## 4. 监控与诊断

### 全流程检查清单

当系统异常时，按以下顺序排查：

#### 1) 进程状态
```bash
ps aux | grep -E 'live_trader|trader_web' | grep -v grep
```
- 未运行 = 正常（收盘后）
- 运行中但state.json不更新 = 可能卡死

#### 2) 代码语法
```bash
python -c "import py_compile; py_compile.compile('live_trader.py', doraise=True)"
python -c "import py_compile; py_compile.compile('trader_web.py', doraise=True)"
```

#### 3) state.json状态
```bash
python -c "
import json
d = json.load(open('state.json'))
print(f'连接: {d[\"connected\"]} | 运行: {d[\"running\"]}')
print(f'K线数: {d[\"candle_count\"]} | 更新: {d[\"updated\"]}')
print(f'持仓: {d[\"position\"]}')
print(f'日盈亏: \${d[\"daily_pnl\"]:+,.2f}')
"
```

#### 4) API连接测试
```python
from longbridge.openapi import Config, QuoteContext
config = Config.from_apikey_env()
ctx = QuoteContext(config)
quotes = ctx.quote(['QQQ.US'])
print(f"QQQ: ${float(quotes[0].last_done):.2f}")
```

#### 5) CONFIG同步检查
两个文件的 `sl`/`vol_mult`/`min_body`/`lookback`/`max_trades` 必须一致。

#### 6) 数据文件
```bash
ls -la records/*.json   # 交易记录
wc -l today.csv         # K线条数
```

#### 7) Web仪表盘
用Python访问你的Web API确认数据正常。

#### 8) 输出格式
按 ✅正常项 / ⚠️注意项 / 📊交易回顾 / 🔧问题 / 📋启动命令 汇报。

---

## 5. 关键踩坑

### 时区（最致命）

长桥API返回的时间戳是 **HKT(UTC+8)**，不是美东！

```python
from zoneinfo import ZoneInfo
TZ_ET = ZoneInfo("America/New_York")  # 自动夏冬令时

now = datetime.now()  # HKT
et_now = now.astimezone(TZ_ET)
cur_min_et = et_now.hour * 60 + et_now.minute
```

### 索引边界

突破检测必须用 **前N根K线（不含当前）**：

```python
# 正确
upper = max(c['high'] for c in cs[-lb-1:-1])
lower = min(c['low'] for c in cs[-lb-1:-1])

# 错误（包含当前K线，永远不触发突破）
upper = max(c['high'] for c in cs[-lb:])
```

### 期权下单方向

```python
# 开仓：不管Call还是Put，都是Buy（买入期权付出权利金）
side = OrderSide.Buy

# 平仓：不管Call还是Put，都是Sell（卖出期权收回权利金）
side = OrderSide.Sell
```

### PushCandlestick结构

```python
# 错误：candle.open → AttributeError被SDK静默吞掉
# 正确：
cs = candle.candlestick       # OHLC在.candlestick子对象里
if not candle.is_confirmed:   # 只处理已完成K线
    return
bar = {'open': cs.open, 'high': cs.high, ...}
```

### CONFIG同步

`live_trader.py` 和 `trader_web.py` 的 CONFIG **必须保持一致**。
修改参数时**两个文件都要改**。

### entry_opt_price

下单后必须获取期权成交价，否则PnL计算永远为0：

```python
time.sleep(1)
opt_q = self.quote_ctx.quote([opt_symbol])
if opt_q and opt_q[0].last_done > 0:
    self.position['entry_opt_price'] = float(opt_q[0].last_done)
```

---

## 6. 修改清单

每次改 `live_trader.py` 后，检查 `trader_web.py` 是否同步：

| # | 检查项 |
|---|--------|
| 1 | PushCandlestick + is_confirmed |
| 2 | ZoneInfo 时区 |
| 3 | 动量/实体用传入的bar（非cs[-1]） |
| 4 | trades_today 平仓后同步win/pnl |
| 5 | 平仓失败不清空position |
| 6 | today.csv 写入 |
| 7 | 信号过滤日志逐条输出 |
| 8 | 期权合约格式（.US后缀+整数行权价） |
| 9 | get_state() 返回 filters 字段 |
| 10 | Jinja2模板三态判断（ok==True/False/None） |

### 实盘↔回测一致性

| 检查项 | 实盘 | 回测 |
|--------|------|------|
| 突破参考 | 前N根1分钟K线（不含当前） | 前N根K线（不含当前） |
| 索引 | `cs[-lb-1:-1]` | 历史bin |
| 动量 | 当前bar同向 | 最近1根同向 |
| 量能 | 1分钟volume vs 20均量 | 同 |
| 跳空 | `(entry-upper)/upper` | 同 |

---

## 7. 参考文件

- `references/config.md` — CONFIG参数完整参考（含每个参数的含义和调优建议）
- `references/backtest-results.md` — 回测数据、参数演进、版本对比
- `references/deployment.md` — 详细部署手册（含环境搭建、依赖安装、故障排除）
