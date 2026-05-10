# 部署手册

## 环境要求

- **操作系统**: Linux 或 WSL（Windows原生不推荐）
- **Python**: 3.10+（推荐3.12+）
- **内存**: ≥2GB
- **网络**: 需要访问长桥API（海外服务器）

---

## 第一步：安装Python依赖

```bash
# 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install longbridge flask numpy scipy
```

### 依赖说明

| 包 | 用途 |
|----|------|
| `longbridge` | 长桥SDK：行情+交易+期权 |
| `flask` | Web仪表盘 |
| `numpy` | 数值计算（SMA、均量等） |
| `scipy` | Black-Scholes期权定价 |

---

## 第二步：配置长桥API密钥

### 2.1 申请长桥API

1. 访问 open.longportapp.com 注册账号
2. 创建应用，获取 APP_KEY、APP_SECRET、ACCESS_TOKEN
3. 确保账户已开通美股期权交易权限

### 2.2 创建 .env 文件

在项目目录创建 `.env` 文件：

```bash
LONGPORT_APP_KEY=你的APP_KEY
LONGPORT_APP_SECRET=你的APP_SECRET
LONGPORT_ACCESS_TOKEN=你的ACCESS_TOKEN
```

**⚠️ 安全警告**:
- `.env` 文件绝对不能提交到Git
- 将 `.env` 加入 `.gitignore`
- 不要在日志或消息中暴露密钥

### 2.3 验证密钥

```python
import os
with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

from longbridge.openapi import Config, QuoteContext
config = Config.from_apikey_env()
ctx = QuoteContext(config)
quotes = ctx.quote(['QQQ.US'])
print(f"QQQ: ${float(quotes[0].last_done):.2f}")
```

如果输出QQQ价格，说明密钥配置成功。

---

## 第三步：配置Web仪表盘Token

在 `trader_web.py` 中设置你自己的Web API Token：

```python
API_TOKEN = '你的自定义TOKEN'  # 用于Web API鉴权
```

**注意**: 这个Token是本地Web仪表盘的访问凭证，不是长桥API密钥。

---

## 第四步：启动系统

### 方式一：直接启动

```bash
# 终端1：启动交易引擎
cd /path/to/QQQ_Live
PYTHONUNBUFFERED=1 python live_trader.py

# 终端2：启动Web仪表盘
cd /path/to/QQQ_Live
PYTHONUNBUFFERED=1 python trader_web.py
```

### 方式二：后台启动

```bash
cd /path/to/QQQ_Live

# 后台启动交易引擎
nohup PYTHONUNBUFFERED=1 python live_trader.py > trader.log 2>&1 &

# 后台启动Web仪表盘
nohup PYTHONUNBUFFERED=1 python trader_web.py > web.log 2>&1 &
```

### 方式三：watchdog守护（推荐）

```bash
cd /path/to/QQQ_Live
python watchdog.py
```

watchdog会自动管理live_trader.py的生命周期，崩溃后自动重启。

---

## 第五步：验证部署

### 5.1 检查进程

```bash
ps aux | grep -E 'live_trader|trader_web' | grep -v grep
```

应该看到两个Python进程在运行。

### 5.2 检查state.json

```bash
python -c "
import json
d = json.load(open('state.json'))
print(f'连接: {d[\"connected\"]}')
print(f'运行: {d[\"running\"]}')
print(f'K线数: {d[\"candle_count\"]}')
print(f'更新时间: {d[\"updated\"]}')
"
```

### 5.3 检查Web仪表盘

用浏览器打开 `http://127.0.0.1:8080`，输入你的Web Token。

### 5.4 检查日志

```bash
# 交易引擎日志
tail -f trader.log

# Web仪表盘日志
tail -f web.log
```

---

## 故障排除

### 问题1: ImportError: No module named 'longbridge'

```bash
pip install longbridge
```

### 问题2: ImportError: No module named 'flask'

```bash
pip install flask
```

### 问题3: 长桥API连接失败

检查：
1. `.env` 文件是否存在且格式正确
2. 环境变量名是 `LONGPORT_*` 不是 `LONGBRIDGE_*`
3. `Config.from_apikey_env()` 不是 `Config.from_env()`
4. 网络是否能访问长桥服务器

### 问题4: Web仪表盘401 Unauthorized

检查：
1. 访问URL是否带了token参数
2. token是否与trader_web.py中的API_TOKEN一致

### 问题5: 信号检测无输出

检查：
1. state.json的candle_count是否>0
2. 当前时间是否在交易窗口内（美东09:35-15:50）
3. 是否有持仓阻塞（position不为None）

### 问题6: 期权下单失败

检查：
1. 期权合约代码格式是否正确（.US后缀+整数行权价）
2. 到期日是否用美东时间生成
3. 账户是否有足够的期权交易权限

---

## 定时任务（可选）

### 每日收盘后同步Gist

```bash
# crontab -e
0 4 * * 1-5 cd /path/to/QQQ_Live && python update_gist.py
```

（美东16:00收盘 = 北京04:00）

### 每日收盘后飞书推送

在trader_web.py的stop()方法中已集成飞书通知，系统停止时自动推送。

---

## 文件说明

| 文件 | 说明 | 是否需要修改 |
|------|------|-------------|
| live_trader.py | 交易引擎 | 修改策略时 |
| trader_web.py | Web仪表盘 | 修改界面时 |
| update_gist.py | Gist同步 | 配置Gist ID时 |
| watchdog.py | 守护进程 | 一般不需要 |
| .env | 密钥配置 | **必须配置** |
| backtest_v6.py | 回测脚本 | 回测时 |
