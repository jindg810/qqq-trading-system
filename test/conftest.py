"""
pytest 配置文件 - 严格按照上传的 trader_data.py
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

# 添加项目路径
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "script"))

# ✅ 从 trader_data 导入 TraderDataManager
from trader_data import TraderDataManager
from trader import QQQTrader

# ===================== Fixtures =====================
@pytest.fixture(scope="session")
def real_config():
    """使用真实配置（只读）"""
    from config import CONFIG
    return CONFIG.copy()

@pytest.fixture
def test_config(tmp_path):
    """可修改的测试配置"""
    from config import get_config
    config = get_config()
    config.update({
        "log_file": str(tmp_path / "test.log"),
        "state_file": str(tmp_path / "state.json"),
        "csv_file": str(tmp_path / "test.csv"),
        "records_dir": str(tmp_path / "records"),
        "environment": "test"
    })
    return config

@pytest.fixture
def mock_longbridge():
    """统一 Mock 长桥 API"""
    with patch('trader.Config') as mock_config, \
         patch('trader.QuoteContext') as mock_qc, \
         patch('trader.TradeContext') as mock_tc, \
         patch.object(QQQTrader, 'load_state') as mock_load:
        
        # 配置 Mock 对象
        mock_config.from_apikey_env.return_value = Mock()
        mock_qc.return_value = Mock()
        mock_tc.return_value = Mock()
        
        yield {
            'config': mock_config,
            'qc': mock_qc.return_value,
            'tc': mock_tc.return_value,
            'load_state': mock_load
        }

@pytest.fixture
def sample_bars():
    """生成测试K线数据"""
    base_time = datetime(2024, 1, 1, 9, 30, tzinfo=timezone.utc)
    bars = []
    
    for i in range(30):
        bar = {
            "ts": (base_time.replace(minute=30+i)).isoformat(),
            "open": 100.0 + i * 0.1,
            "high": 100.5 + i * 0.1,
            "low": 99.5 + i * 0.1,
            "close": 100.2 + i * 0.1,
            "volume": 1000 + i * 10
        }
        bars.append(bar)
    
    return bars

@pytest.fixture
def data_manager(sample_bars):
    """预加载数据的 DataManager - 使用正确的 add_bar 方法"""
    dm = TraderDataManager()
    for bar in sample_bars:
        dm.add_bar(bar)   # ✅ 正确使用 add_bar（不是 add_bar）
    return dm

@pytest.fixture
def trader(mock_longbridge, data_manager, test_config):
    """创建测试用的 Trader 实例"""
    with patch('trader.CONFIG', test_config):
        t = QQQTrader()
        t.data_manager = data_manager
        return t