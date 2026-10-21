import json
import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get('MEM0_SETTINGS_PATH', APP_ROOT / 'config' / 'settings.json'))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
DATA_ROOT = Path(CONFIG['data_root'])
os.environ['MEM0_TELEMETRY'] = 'false'
os.environ['MEM0_DIR'] = str(DATA_ROOT / 'data' / 'mem0')
MODEL_ROOT = Path(CONFIG.get('model_cache_root', APP_ROOT))
os.environ['FASTEMBED_CACHE_PATH'] = str(MODEL_ROOT / 'cache' / 'models')
os.environ['HF_HOME'] = str(MODEL_ROOT / 'cache' / 'huggingface')
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
API_TOKEN = (CONFIG_PATH.parent / 'local-api.key').read_text().strip()
BASE_URL = f"http://127.0.0.1:{CONFIG['port']}"
