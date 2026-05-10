# CONFIG 参数完整参考

## 当前版本参数（v6.1 期权版 2026-04-23）

```python
CONFIG = {
    'symbol': 'QQQ.US',
    # 策略参数
    'sl': 0.25,               # 止损 25%（期权价格）
    'tp': 0.30,               # 止盈 30%（旧逻辑，保留兼容）
    'lookback': 5,            # 突破检测窗口（1分钟K线数，5根=5分钟）
    # 动态止盈参数
    'tp_partial_pct': 1.00,   # 盈利100%平仓一半
    'tp_trail_drop': 0.30,    # 最高盈利回撤30%全部平仓
    # 期权参数
    'option_offset': 2.0,     # 期权行权价偏移（±$2，虚值期权）
    'min_contracts': 10,      # 最小张数（1张=100股）
    'contract_multiplier': 100, # 每张期权对应股数
    # 资金管理
    'pos_pct': 2,             # 单笔仓位百分比（保守起步）
    'max_trades': 8,          # 日最大交易次数
    'daily_limit': 5,         # 日亏损熔断百分比
    # 交易窗口
    'start_time': '09:35',    # 允许入场开始（美东）
    'end_time': '15:50',      # 允许入场结束
    # 跟踪止损（备用，动态止盈优先）
    'trail_activate': 0.10,   # 跟踪止损激活 10%
    'trail_drop': 0.05,       # 跟踪止损回撤 5%
    # 过滤参数
    'max_gap': 0.0020,        # 最大跳空 0.20%
    'vol_mult': 0.8,          # 成交量倍数（相对20均量）
    'min_body': 0.0003,       # 最小K线实体比例
    # 衰竭反转参数
    'reversal_drop': 0.002,   # 从高点跌幅≥0.2%触发反转条件
    'reversal_bounce': 0.001, # 反弹K线实体≥0.1%确认反转
    # 检测频率
    'check_interval': 20,     # 检测间隔（秒）
    # 账户
    'capital': 100000,
}
```

---

## 参数说明

### 策略核心

| 参数 | 值 | 说明 | 调优建议 |
|------|-----|------|---------|
| `symbol` | QQQ.US | 交易标的 | 不要改 |
| `sl` | 0.25 | 止损25%（期权价格） | 期权波动大，不建议低于0.20 |
| `tp` | 0.30 | 止盈30%（旧逻辑兼容） | 实际用动态止盈，此参数仅备用 |
| `lookback` | 5 | 突破窗口5根1分钟K线 | 3太灵敏假信号多，10太慢错过行情 |

### 动态止盈

| 参数 | 值 | 说明 |
|------|-----|------|
| `tp_partial_pct` | 1.00 | 盈利100%时平仓一半 |
| `tp_trail_drop` | 0.30 | 从峰值回撤30%时全部平仓 |

### 期权参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `option_offset` | 2.0 | 行权价偏移±$2（OTM虚值） |
| `min_contracts` | 10 | 每笔最少10张 |
| `contract_multiplier` | 100 | 1张=100股 |

### 资金管理

| 参数 | 值 | 说明 |
|------|-----|------|
| `pos_pct` | 2 | 单笔仓位占总资金2% |
| `max_trades` | 8 | 日最大交易次数 |
| `daily_limit` | 5 | 日亏损达5%停止交易 |

### 交易窗口

| 参数 | 值 | 说明 |
|------|-----|------|
| `start_time` | 09:35 | 美东时间，避开开盘前15分钟噪音 |
| `end_time` | 15:50 | 美东时间，收盘前10分钟停止入场 |

### 过滤参数

| 参数 | 值 | 说明 | 调优建议 |
|------|-----|------|---------|
| `max_gap` | 0.0020 | 跳空>0.20%不入场 | 防止追涨杀跌 |
| `vol_mult` | 0.8 | 量能≥20均量×0.8 | 1.2太严→1.0→0.8逐步放宽 |
| `min_body` | 0.0003 | K线实体≥0.03% | 过滤十字星假突破 |

### 衰竭反转

| 参数 | 值 | 说明 |
|------|-----|------|
| `reversal_drop` | 0.002 | 从日内高低点回落≥0.2% |
| `reversal_bounce` | 0.001 | 反转K线实体≥0.1% |

---

## trader_web.py 注意事项

`trader_web.py` 的 CONFIG 仅用于 Web 界面显示，不需要期权参数（option_offset/min_contracts/contract_multiplier/check_interval）。

但以下参数**必须与 live_trader.py 一致**：
- `sl`, `tp`, `lookback`
- `tp_partial_pct`, `tp_trail_drop`
- `vol_mult`, `min_body`, `max_gap`
- `max_trades`, `daily_limit`
- `start_time`, `end_time`
- `reversal_drop`, `reversal_bounce`
