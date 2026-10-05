import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))

# Внутри Docker-контейнера нет дисплея — /login (видимое окно браузера для
# входа в аккаунт площадки) там открыть некуда. Флаг ставится в Dockerfile;
# на обычном локальном запуске (Windows/Mac напрямую) остаётся False.
RUNNING_IN_DOCKER = os.getenv("RUNNING_IN_DOCKER") == "1"

# ИИ читает описания объявлений (matching/ai_reader.py). По умолчанию —
# Ollama на том же Маке (brew services start ollama): из контейнера она
# видна как host.docker.internal. Любой OpenAI-совместимый сервер подойдёт.
AI_ENABLED = os.getenv("AI_ENABLED", "true").lower() == "true"
AI_BASE_URL = os.getenv("AI_BASE_URL") or (
    "http://host.docker.internal:11434/v1" if RUNNING_IN_DOCKER else "http://localhost:11434/v1"
)
AI_API_KEY = os.getenv("AI_API_KEY", "not-needed")
AI_MODEL = os.getenv("AI_MODEL", "")  # пусто — первая модель, найденная в Ollama
AI_MAX_PER_CYCLE = int(os.getenv("AI_MAX_PER_CYCLE", "200"))  # описаний за полный скан

DB_PATH = DATA_DIR / "cardeal.db"
FEATURES_PATH = DATA_DIR / "features.yaml"

# Дефолты настроек (хранятся в БД, это начальные значения)
DEFAULT_CHECK_INTERVAL_MIN = 60
DEFAULT_PRICE_TOLERANCE_PCT = 10

# Курс для нормализации цен lalafo (объявления бывают в сомах)
KGS_PER_USD = 87.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
