import os
import re
import math
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

def load_clean_token() -> str:
    """Безопасная загрузка токена Telegram-бота из переменных окружения или файлов."""
    for key in ["BOT_TOKEN", "TOKEN", "bot_token", "token"]:
        val = os.getenv(key)
        if val and len(val.strip()) > 20:
            return val.strip().strip('"\'')
    
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [".env", "Token.env", "token.env", "token.txt", "/home/container/.env"]
    token_pattern = re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{30,45}\b')
    for fn in candidates:
        fp = os.path.join(base_dir, fn) if not os.path.isabs(fn) else fn
        if os.path.exists(fp):
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        m = token_pattern.search(line)
                        if m:
                            return m.group(0).strip()
            except Exception:
                pass
    return os.getenv("BOT_TOKEN", "")

TOKEN = load_clean_token()

def load_env_val(key: str, default: str = "") -> str:
    """Загрузка параметров строго из того же файла/папки, где лежит токен (Token.env или .env)."""
    val = os.getenv(key)
    if val and val.strip():
        return val.strip().strip('"\'')
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        "Token.env", "token.env", ".env", "token.txt",
        "/home/container/Token.env", "/home/container/token.env", "/home/container/.env"
    ]
    for fn in candidates:
        fp = os.path.join(base_dir, fn) if not os.path.isabs(fn) else fn
        if os.path.exists(fp):
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip().upper() == key.upper():
                                return v.strip().strip('"\'')
            except Exception:
                pass
    return default

# ID Администрации и параметры подключения
OWNER_ID = int(load_env_val("OWNER_ID", "697487661"))
ADMIN_CHAT_ID = int(load_env_val("ADMIN_CHAT_ID", "-1002033175002"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

CHANNEL_USERNAME = load_env_val("CHANNEL_USERNAME", "@NemosTrade")
CHANNEL_URL = load_env_val("CHANNEL_URL", "https://t.me/NemosTrade")
raw_rev = load_env_val("REVIEWS_CHANNEL", "@NemosReview")
try:
    if str(raw_rev).startswith("-") and (str(raw_rev)[1:].isdigit() or str(raw_rev).startswith("-100")):
        REVIEWS_CHANNEL = int(raw_rev)
    else:
        REVIEWS_CHANNEL = raw_rev
except Exception:
    REVIEWS_CHANNEL = raw_rev

MSK = timezone(timedelta(hours=3))

def get_now() -> datetime:
    """Текущее время по Московскому часовому поясу (UTC+3)."""
    return datetime.now(MSK)

def get_now_iso() -> str:
    """Стандартный ISO формат даты (YYYY-MM-DD HH:MM:SS) для SQLite."""
    return get_now().strftime("%Y-%m-%d %H:%M:%S")

def format_datetime_display(dt_str: Optional[str]) -> str:
    """Преобразование ISO даты в читаемый формат DD.MM.YYYY HH:MM."""
    if not dt_str:
        return ""
    try:
        cleaned = dt_str.replace("T", " ")[:19]
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(dt_str)

EFFECT_CONFETTI_ID = "5046509860389126442"

# Финансовые константы
DAILY_RATE = 0.03  # +3% в день
REFERRAL_BONUS_INVITER = 100_000.0
REFERRAL_BONUS_INVITED = 250_000.0
MIN_DEPOSIT = 500_000.0
MIN_WITHDRAW = 100_000.0
MIN_REINVEST = 100_000.0
MIN_TRANSFER = 50_000.0
TRANSFER_FEE_PERCENT = 0.0222  # 2.22%
OKA_GOS_PRICE = 55_000.0

# Игровые константы (Dice PvE и PvP)
MIN_DICE_BET = 10_000.0
MAX_DICE_BET = 5_000_000.0
DICE_MULTIPLIER = 1.8
DICE_PVP_FEE_PERCENT = 0.0333  # 3.33%
MAX_ACTIVE_PVP_LOBBIES = 3  # Лимит открытых дуэлей на одного игрока
MIN_DICE_PVP_BET = 50_000.0
MAX_DICE_PVP_BET = 1_000_000_000.0

DB_FILE = os.getenv("DB_FILE", "bot_database.db")
BANNER_FILE = os.getenv("BANNER_FILE", "banner.png")

EMOJIS = {
    "check": "✅", "shield": "🛡", "plus": "➕", "diamond": "💎",
    "lock": "🔒", "mail": "✉️", "star": "⭐️", "sparkles": "✨",
    "crown": "👑", "game": "🎮", "gear": "⚙️", "clip": "📎",
    "siren": "🚨", "pencil": "✏️", "link": "🔗", "plane": "🛫",
    "bank": "🏛", "clover": "🍀", "piggy": "🐷", "briefcase": "💼",
    "chest": "📦", "stats": "📊", "calc": "🧮", "dollar": "💵",
    "clipboard": "📝", "eyes": "👀", "lightning": "⚡️", "bag": "🎁",
    "stop": "⛔️", "prohibited": "🚫", "excl": "❗️", "warn": "⚠️",
    "globe": "🌐", "chat": "💬", "clock": "⏱"
}

BOXES = {}

MENU_COMMANDS_AND_TEXTS = {
    "👤 личный кабинет", "🎮 игры", "🎁 промокод", "👥 партнерам", 
    "💬 поддержка", "👑 админ-панель", "📥 пополнить", "📤 вывести", 
    "🔄 реинвест", "💸 перевести", "🧮 калькулятор", "📜 история",
    "👤", "🎮", "🎁", "👥", "💬", "👑"
}

def is_menu_trigger(text: Optional[str]) -> bool:
    """Глобальная проверка: является ли сообщение командой или нажатием кнопки меню."""
    if not text:
        return False
    t = text.lower().strip()
    if t.startswith("/") or t in MENU_COMMANDS_AND_TEXTS:
        return True
    return any(k in t for k in ["кабинет", "профиль", "игры", "промокод", "партнер", "поддержк", "админ", "пополнить", "вывести", "реинвест", "перевести", "калькулятор", "история"])

# Фирменное форматирование чисел Nemos Trade: замена '0' на заглавную букву 'O'
def fmt_o(val) -> str:
    try:
        val_float = float(val)
        is_neg = val_float < 0
        s = f"{abs(int(round(val_float))):,}"
        res = s.replace("0", "O")
        return f"-{res}" if is_neg else res
    except Exception:
        return str(val)

def format_bold_num(val) -> str:
    return f"<b>{fmt_o(val)}$</b>"

def format_raw_num(val) -> str:
    return f"{fmt_o(val)}$"

def parse_amount(text: str, max_val: Optional[float] = None) -> float:
    """Строгий парсер сумм с поддержкой разделителей тысяч, букв О и защитой от Float-пыли."""
    t = text.lower().strip().replace(" ", "").replace("$", "").replace("₽", "").replace("вирт", "")
    if max_val is not None and t in ["all", "все", "всё", "full", "макс", "max", "весь"]:
        return round(max(0.0, float(max_val)), 2)

    t = t.replace("о", "0").replace("o", "0")
    mult = 1.0

    if any(k in t for k in ["ккк", "kkk", "млрд", "bn", "b"]):
        mult = 1_000_000_000.0
        for sub in ["ккк", "kkk", "млрд", "bn", "b"]: t = t.replace(sub, "")
    elif any(k in t for k in ["кк", "kk", "млн", "m", "м"]):
        mult = 1_000_000.0
        for sub in ["кк", "kk", "млн", "m", "м"]: t = t.replace(sub, "")
    elif any(k in t for k in ["к", "k", "т", "t", "тыс"]):
        mult = 1_000.0
        for sub in ["к", "k", "т", "t", "тыс"]: t = t.replace(sub, "")

    # Обработка разделителей тысяч (100,000 или 1,000,000)
    if t.count(",") > 1 or ("," in t and re.search(r",\d{3}(?:[^\d]|$)", t)):
        t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")

    if t.count(".") > 1:
        parts = t.split(".")
        t = "".join(parts[:-1]) + "." + parts[-1] if len(parts[-1]) <= 2 else "".join(parts)

    try:
        val = float(t) * mult
        if math.isnan(val) or math.isinf(val) or val < 1.0 or val > 1_000_000_000_000:
            raise ValueError
        return round(val, 2)
    except Exception:
        raise ValueError("Неверный формат суммы")
MIN_MINES_BET = 50_000.0
MAX_MINES_BET = 5_000_000.0

def fmt_mult(val) -> str:
    """Форматирование множителей в виде 1.хх с заменой цифры 0 на заглавную букву O."""
    try:
        s = f"{float(val):.2f}"
        return s.replace("0", "O")
    except Exception:
        return str(val)

WHALE_ALERT_THRESHOLD = 5_000_000.0
CLOUD_UPDATE_URL = load_env_val("CLOUD_UPDATE_URL", "https://drive.google.com/drive/folders/1RLcPke5FNUY-jyF90TmxW5yWAUMLHtS7")
