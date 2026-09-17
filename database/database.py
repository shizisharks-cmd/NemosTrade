from __future__ import annotations
import json
import random
import time
import aiosqlite
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional, List
from aiogram import Bot
from config.config import (
    DB_FILE, OWNER_ID, DAILY_RATE, MIN_WITHDRAW, MIN_REINVEST, EFFECT_CONFETTI_ID,
    REFERRAL_BONUS_INVITER, REFERRAL_BONUS_INVITED, MIN_MINES_BET, MAX_MINES_BET, MIN_DEPOSIT,
    get_now, get_now_iso, format_bold_num, format_raw_num, EMOJIS
)

_admin_cache: Dict[int, Tuple[bool, float]] = {}
ADMIN_CACHE_TTL = 300.0

# --- ОПТИМИЗИРОВАННЫЙ ПУЛ СОЕДИНЕНИЙ SQLITE (CONNECTION POOLING) ---
class SQLiteConnectionPool:
    """
    Высокопроизводительный пул подключений SQLite с поддержкой WAL-режима,
    автоматической установкой PRAGMA (busy_timeout, synchronous, cache_size, foreign_keys)
    и защитой от утечек соединений и незавершенных транзакций.
    """
    def __init__(self, db_path: str, max_size: int = 15, timeout: float = 30.0):
        self.db_path = db_path
        self.max_size = max_size
        self.timeout = timeout
        self._pool: Optional[asyncio.Queue] = None
        self._lock: Optional[asyncio.Lock] = None
        self._current_size = 0

    def _ensure_initialized(self):
        if self._pool is None:
            self._pool = asyncio.Queue()
        if self._lock is None:
            self._lock = asyncio.Lock()

    async def _create_connection(self):
        conn = await aiosqlite.connect(self.db_path, timeout=self.timeout)
        # Установка критически важных PRAGMA для максимальной надежности и скорости
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 30000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA cache_size = -64000")
        await conn.execute("PRAGMA temp_store = MEMORY")
        return conn

    @asynccontextmanager
    async def acquire(self):
        self._ensure_initialized()
        conn = None
        try:
            conn = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            async with self._lock:
                if self._current_size < self.max_size:
                    self._current_size += 1
                    create_new = True
                else:
                    create_new = False
            if create_new:
                try:
                    conn = await self._create_connection()
                except Exception:
                    async with self._lock:
                        self._current_size -= 1
                    raise
            else:
                conn = await self._pool.get()

        try:
            yield conn
        finally:
            if conn is not None:
                # Очистка незафиксированных транзакций перед возвратом в пул
                try:
                    await conn.rollback()
                except Exception:
                    pass
                try:
                    self._pool.put_nowait(conn)
                except Exception:
                    async with self._lock:
                        self._current_size -= 1
                    try:
                        await conn.close()
                    except Exception:
                        pass

    async def close_all(self):
        if self._lock:
            async with self._lock:
                if self._pool:
                    while not self._pool.empty():
                        try:
                            conn = self._pool.get_nowait()
                            await conn.close()
                        except Exception:
                            pass
                self._current_size = 0

_db_pool: Optional[SQLiteConnectionPool] = None

def get_db_pool() -> SQLiteConnectionPool:
    global _db_pool
    if _db_pool is None:
        _db_pool = SQLiteConnectionPool(DB_FILE, max_size=15, timeout=30.0)
    return _db_pool

def get_db_connection():
    """Создает защищенное соединение с базой данных SQLite из оптимизированного пула."""
    return get_db_pool().acquire()

async def close_db_pool():
    """Закрытие всех соединений в пуле при остановке бота."""
    global _db_pool
    if _db_pool is not None:
        await _db_pool.close_all()
        _db_pool = None

async def init_db():
    """Инициализация схемы таблиц и оптимизирующих индексов."""
    async with get_db_connection() as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=30000")

        # Таблица пользователей
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            invested REAL DEFAULT 0.0,
            earned REAL DEFAULT 0.0,
            last_accrual TEXT,
            is_admin INTEGER DEFAULT 0,
            referrer_id INTEGER DEFAULT NULL, 
            referral_rewarded INTEGER DEFAULT 0,
            is_banned INTEGER DEFAULT 0,
            last_active TEXT,
            username TEXT DEFAULT '',
            last_reminder TEXT DEFAULT NULL,
            reminder_step INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            notified_min INTEGER DEFAULT 0,
            registered_at TEXT DEFAULT ''
        )""")

        # Миграция: добавление поля registered_at
        try:
            await db.execute("ALTER TABLE users ADD COLUMN registered_at TEXT DEFAULT ''")
        except Exception:
            pass

        # Миграция: добавление поля last_daily_bonus
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_daily_bonus TEXT DEFAULT ''")
        except Exception:
            pass

        # Таблица транзакций
        await db.execute("""CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            date TEXT,
            status TEXT,
            admin_chat_id INTEGER DEFAULT NULL,
            admin_msg_id INTEGER DEFAULT NULL,
            proof_text TEXT DEFAULT '',
            proof_photo TEXT DEFAULT ''
        )""")

        for col_def in ["proof_text TEXT DEFAULT ''", "proof_photo TEXT DEFAULT ''", "curator_id INTEGER DEFAULT NULL"]:
            try:
                await db.execute(f"ALTER TABLE transactions ADD COLUMN {col_def}")
            except Exception:
                pass

        # Архив транзакций
        await db.execute("""CREATE TABLE IF NOT EXISTS transactions_archive (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            type TEXT,
            amount REAL,
            date TEXT,
            status TEXT,
            admin_chat_id INTEGER,
            admin_msg_id INTEGER
        )""")

        for col_def in ["proof_text TEXT DEFAULT ''", "proof_photo TEXT DEFAULT ''"]:
            try:
                await db.execute(f"ALTER TABLE transactions_archive ADD COLUMN {col_def}")
            except Exception:
                pass
        # Таблица настроек
        await db.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")

        # Таблица промокодов
        await db.execute("""CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            amount REAL,
            max_uses INTEGER,
            current_uses INTEGER DEFAULT 0,
            created_at TEXT
        )""")

        # Активации промокодов
        await db.execute("""CREATE TABLE IF NOT EXISTS promo_activations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            code TEXT,
            activated_at TEXT,
            UNIQUE(user_id, code)
        )""")

        # Логи действий администрации
        await db.execute("""CREATE TABLE IF NOT EXISTS admin_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            admin_tag TEXT,
            action TEXT,
            target_id INTEGER,
            details TEXT,
            date TEXT
        )""")

        # Логи кейсов
        await db.execute("""CREATE TABLE IF NOT EXISTS case_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            box_id TEXT,
            box_name TEXT,
            price REAL,
            drop_amount REAL,
            profit_user REAL,
            date TEXT
        )""")

        # Логи костей (PvE)
        await db.execute("""CREATE TABLE IF NOT EXISTS dice_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bet REAL,
            player_val INTEGER,
            bot_val INTEGER,
            result TEXT,
            win_amount REAL,
            profit_user REAL,
            date TEXT
        )""")

        # Таблица партий игры Минёр (Mines 5x5)
        await db.execute("""CREATE TABLE IF NOT EXISTS mines_games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            bet REAL,
            mines_count INTEGER,
            mines_positions TEXT,
            opened_cells TEXT,
            current_multiplier REAL DEFAULT 1.0,
            status TEXT DEFAULT 'ACTIVE',
            win_amount REAL DEFAULT 0.0,
            profit_user REAL DEFAULT 0.0,
            created_at TEXT,
            finished_at TEXT DEFAULT ''
        )""")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_mines_user_status ON mines_games (user_id, status)")

        # Лобби дуэлей в кости (PvP)
        await db.execute("""CREATE TABLE IF NOT EXISTS dice_pvp_lobbies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER,
            creator_name TEXT,
            bet REAL,
            status TEXT DEFAULT 'OPEN',
            opponent_id INTEGER DEFAULT NULL,
            creator_val INTEGER DEFAULT NULL,
            opponent_val INTEGER DEFAULT NULL,
            winner_id INTEGER DEFAULT NULL,
            created_at TEXT
        )""")

        # Логи P2P переводов
        await db.execute("""CREATE TABLE IF NOT EXISTS transfer_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            recipient_id INTEGER,
            amount REAL,
            fee REAL,
            net_amount REAL,
            is_anonymous INTEGER DEFAULT 0,
            date TEXT
        )""")

        # Индексы
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_user_id ON transactions (user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_type_status ON transactions (type, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions (date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_admin_logs_date ON admin_logs (date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users (is_banned, is_blocked)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_uname ON users (username)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_dice_pvp_status ON dice_pvp_lobbies (status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_dice_pvp_creator ON dice_pvp_lobbies (creator_id, status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_reg ON users (registered_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_transfer_sender ON transfer_logs (sender_id)")
        
        await db.commit()

# --- Системные настройки ---
async def get_setting(key: str, default: str = "0") -> str:
    async with get_db_connection() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

async def set_setting(key: str, value: str):
    async with get_db_connection() as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def is_maintenance_active() -> bool:
    return (await get_setting("maintenance", "0")) == "1"

async def is_withdrawals_frozen() -> bool:
    return (await get_setting("frozen", "0")) == "1"

# --- Права администратора ---
async def is_admin(uid: int) -> bool:
    if uid == OWNER_ID:
        return True
    
    now_ts = time.time()
    if uid in _admin_cache:
        cached_val, exp = _admin_cache[uid]
        if now_ts < exp:
            return cached_val

    async with get_db_connection() as db:
        async with db.execute("SELECT is_admin FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            res = bool(row and row[0] == 1)
            _admin_cache[uid] = (res, now_ts + ADMIN_CACHE_TTL)
            return res

async def set_user_admin_status(uid: int, is_adm: int):
    _admin_cache.pop(uid, None)
    async with get_db_connection() as db:
        await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (is_adm, uid))
        await db.commit()

async def is_user_banned(uid: int) -> bool:
    async with get_db_connection() as db:
        async with db.execute("SELECT is_banned FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
            return bool(row and row[0] == 1)

async def set_user_ban_status(uid: int, banned: int):
    async with get_db_connection() as db:
        await db.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (banned, uid))
        await db.commit()

# --- АТОМАРНОЕ НАЧИСЛЕНИЕ ПРИБЫЛИ И ПОЛУЧЕНИЕ ДАННЫХ ПОЛЬЗОВАТЕЛЯ ---
async def get_and_update_user(uid: int, ref_id: Optional[int] = None, uname: str = "") -> Dict[str, Any]:
    now = get_now()
    now_iso = get_now_iso()
    
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            
            async with db.execute(
                "SELECT invested, earned, last_accrual, is_admin, referrer_id, referral_rewarded, is_banned, is_blocked, notified_min, username, registered_at FROM users WHERE user_id = ?",
                (uid,)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                is_adm = 1 if uid == OWNER_ID else 0
                v_ref = ref_id if (ref_id and ref_id != uid) else None
                start_inv = REFERRAL_BONUS_INVITED if v_ref else 0.0
                await db.execute(
                    "INSERT INTO users (user_id, invested, earned, last_accrual, is_admin, referrer_id, referral_rewarded, is_banned, last_active, username, is_blocked, notified_min, registered_at) VALUES (?, ?, 0.0, ?, ?, ?, 0, 0, ?, ?, 0, 0, ?)",
                    (uid, start_inv, now_iso, is_adm, v_ref, now_iso, uname, now_iso)
                )
                if start_inv > 0:
                    await db.execute(
                        "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, ?, ?, ?, 'Зачислено')",
                        (uid, f"{EMOJIS['bag']} Стартовый бонус реферала", start_inv, now_iso)
                    )
                await db.commit()
                return {
                    "user_id": uid,
                    "invested": start_inv, "earned": 0.0, "is_admin": bool(is_adm or uid == OWNER_ID),
                    "referrer_id": v_ref, "referral_rewarded": 0, "is_banned": 0,
                    "is_blocked": 0, "username": uname, "notify_min": False, "is_new": True,
                    "registered_at": now_iso
                }

            inv, earn, l_acc, db_adm, r_id, r_rew, bnd, blk, notif_m, db_uname, reg_at = row
            actual_uname = uname or db_uname or ""

            try:
                cleaned_lacc = l_acc.replace("T", " ")[:19]
                last_dt = datetime.strptime(cleaned_lacc, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
                seconds = max(0.0, (now - last_dt).total_seconds())
                seconds = min(seconds, 31536000.0)
            except Exception:
                seconds = 0.0

            accrued = round(seconds * ((inv * DAILY_RATE) / 86400.0), 2) if (seconds > 0 and inv > 0) else 0.0
            new_earn = round(earn + accrued, 2)

            notify_min = False
            if new_earn >= MIN_WITHDRAW and notif_m == 0:
                notify_min = True
                notif_m = 1

            u_clause = ", username = ?" if uname else ""
            params = [new_earn, now_iso, now_iso, notif_m]
            if uname:
                params.append(uname)
            params.append(uid)

            await db.execute(
                f"UPDATE users SET earned = ?, last_accrual = ?, last_active = ?, is_blocked = 0, notified_min = ?{u_clause} WHERE user_id = ?",
                params
            )
            await db.commit()

            return {
                "user_id": uid,
                "invested": inv, "earned": new_earn, "is_admin": bool(db_adm or uid == OWNER_ID),
                "referrer_id": r_id, "referral_rewarded": r_rew, "is_banned": bnd,
                "is_blocked": 0, "username": actual_uname, "notify_min": notify_min, "is_new": False,
                "registered_at": reg_at or now_iso
            }
        except Exception:
            await db.execute("ROLLBACK")
            raise

# --- ЗАЩИТА ОТ РЕТРОАКТИВНОГО ПЕРЕСЧЕТА (MATH ACCRUAL PROTECTION) ---
async def update_user_manual(uid: int, invested: Optional[float] = None, earned: Optional[float] = None):
    await get_and_update_user(uid)
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            if invested is not None and earned is not None:
                await db.execute(
                    "UPDATE users SET invested = ROUND(MAX(0.0, ?), 2), earned = ROUND(MAX(0.0, ?), 2), last_accrual = ? WHERE user_id = ?",
                    (invested, earned, now_iso, uid)
                )
            elif invested is not None:
                await db.execute(
                    "UPDATE users SET invested = ROUND(MAX(0.0, ?), 2), last_accrual = ? WHERE user_id = ?",
                    (invested, now_iso, uid)
                )
            elif earned is not None:
                await db.execute(
                    "UPDATE users SET earned = ROUND(MAX(0.0, ?), 2), last_accrual = ? WHERE user_id = ?",
                    (earned, now_iso, uid)
                )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

# --- АТОМАРНЫЙ РЕИНВЕСТ ---
async def adjust_user_balance_atomic(uid: int, inv_delta: float = 0.0, earn_delta: float = 0.0):
    """Атомарное относительное изменение баланса без затирания параллельных начислений."""
    await get_and_update_user(uid)
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """UPDATE users SET
                   invested = ROUND(MAX(0.0, invested + ?), 2),
                   earned = ROUND(MAX(0.0, earned + ?), 2),
                   last_accrual = ?
                   WHERE user_id = ?""",
                (inv_delta, earn_delta, now_iso, uid)
            )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            raise

async def execute_reinvest(uid: int, amt: float) -> Tuple[bool, str]:
    if amt < MIN_REINVEST:
        return False, f"Минимальная сумма для реинвеста: {format_bold_num(MIN_REINVEST)}!"
    await get_and_update_user(uid)
    now_iso = get_now_iso()
    amt = round(amt, 2)
    
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE users SET invested = ROUND(invested + ?, 2), earned = ROUND(earned - ?, 2), last_accrual = ? WHERE user_id = ? AND earned >= ?",
                (amt, amt, now_iso, uid, amt)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Недостаточно средств на балансе чистой прибыли!"

            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🔄 Реинвест', ?, ?, 'Зачислено')",
                (uid, amt, now_iso)
            )
            await db.commit()
            return True, "Реинвест успешно выполнен!"
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка реинвеста: {e}"


async def has_active_withdrawal(user_id: int) -> bool:
    """Проверка наличия незавершенной заявки на вывод."""
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND type LIKE '%Вывод%' AND status IN ('В обработке', 'В работе')",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return bool(row and row[0] > 0)

async def get_user_transfer_hold_amount(user_id: int) -> float:
    """Сумма переводов, полученных пользователем за последние 12 часов (защитный холд)."""
    cutoff_iso = (get_now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT SUM(net_amount) FROM transfer_logs WHERE recipient_id = ? AND date >= ?",
            (user_id, cutoff_iso)
        ) as cur:
            row = await cur.fetchone()
            return round(row[0], 2) if row and row[0] else 0.0

# --- АТОМАРНОЕ СОЗДАНИЕ ЗАЯВКИ НА ВЫВОД ---
async def execute_withdrawal_creation(uid: int, amt: float, proof_text: str = "", proof_photo: str = "") -> Tuple[bool, int, str]:
    now_iso = get_now_iso()
    amt = round(amt, 2)
    cutoff_iso = (get_now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")

    await get_and_update_user(uid)

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            # 1. Атомарная проверка активной заявки внутри транзакции
            async with db.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND type LIKE '%Вывод%' AND status IN ('В обработке', 'В работе')",
                (uid,)
            ) as check_cur:
                row_act = await check_cur.fetchone()
                if row_act and row_act[0] > 0:
                    await db.execute("ROLLBACK")
                    return False, 0, "У вас уже есть активная заявка на вывод в обработке! Дождитесь её выполнения куратором."

            # 2. Атомарная проверка баланса и 12-часового защитного холда строго внутри транзакции
            async with db.execute("SELECT earned FROM users WHERE user_id = ?", (uid,)) as cur_u:
                u_row = await cur_u.fetchone()
            if not u_row:
                await db.execute("ROLLBACK")
                return False, 0, "Пользователь не найден!"
            cur_earned = u_row[0]

            async with db.execute(
                "SELECT SUM(net_amount) FROM transfer_logs WHERE recipient_id = ? AND date >= ?",
                (uid, cutoff_iso)
            ) as cur_h:
                h_row = await cur_h.fetchone()
                hold_amt = round(h_row[0], 2) if h_row and h_row[0] else 0.0

            avail = max(0.0, round(cur_earned - hold_amt, 2))
            if avail < amt:
                await db.execute("ROLLBACK")
                hold_msg = f" Часть средств ({format_raw_num(hold_amt)}) находится на защитном холде (12ч) после перевода." if hold_amt > 0 else ""
                return False, 0, f"Недостаточно доступных средств!{hold_msg} Доступно: {format_bold_num(avail)}"

            cur = await db.execute(
                "UPDATE users SET earned = ROUND(earned - ?, 2), last_accrual = ? WHERE user_id = ? AND earned >= ?",
                (amt, now_iso, uid, amt)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, 0, "Недостаточно средств на балансе прибыли!"

            tx_cur = await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status, proof_text, proof_photo) VALUES (?, '📤 Вывод', ?, ?, 'В обработке', ?, ?)",
                (uid, amt, now_iso, proof_text, proof_photo)
            )
            tx_id = tx_cur.lastrowid
            await db.commit()
            return True, tx_id, "Заявка создана!"
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, 0, f"Ошибка создания заявки: {e}"

async def update_tx_admin_message(tx_id: int, admin_chat_id: int, admin_msg_id: int):
    async with get_db_connection() as db:
        await db.execute(
            "UPDATE transactions SET admin_chat_id = ?, admin_msg_id = ? WHERE id = ?",
            (admin_chat_id, admin_msg_id, tx_id)
        )
        await db.commit()

async def get_tx_by_id(tx_id: int) -> Optional[Tuple]:
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, user_id, type, amount, date, status, admin_chat_id, admin_msg_id, proof_text, proof_photo, curator_id FROM transactions WHERE id = ?",
            (tx_id,)
        ) as cur:
            return await cur.fetchone()

# --- РЕФЕРАЛЬНЫЕ НАЧИСЛЕНИЯ ---
async def process_referral_reward(uid: int, bot: Optional[Bot] = None) -> Optional[int]:
    """
    Начисление реферального бонуса после подтверждения обязательной подписки на канал:
    Пригласителю начисляется 100k на баланс, приглашенному — 250k в инвестиции.
    """
    now_iso = get_now_iso()
    
    async with get_db_connection() as db:
        async with db.execute("SELECT referrer_id, referral_rewarded FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()

    if not row or not row[0] or row[1] == 1:
        return None

    ref_id, rewarded = row

    await get_and_update_user(ref_id)
    await get_and_update_user(uid)

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT referral_rewarded FROM users WHERE user_id = ?", (uid,)) as cur:
                r_check = await cur.fetchone()
            if not r_check or r_check[0] == 1:
                await db.execute("ROLLBACK")
                return None

            await db.execute("UPDATE users SET referral_rewarded = 1 WHERE user_id = ?", (uid,))
            
            # Пригласившему: 100k на баланс за приглашение (подтверждена подписка на канал)
            await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2), last_accrual = ? WHERE user_id = ?", (REFERRAL_BONUS_INVITER, now_iso, ref_id))
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, ?, ?, ?, 'Зачислено')",
                (ref_id, f"{EMOJIS['bag']} Реферальный бонус (новый партнер)", REFERRAL_BONUS_INVITER, now_iso)
            )
            await db.commit()

            if bot:
                try:
                    text_ref = (
                        f"{EMOJIS['sparkles']} <b>Новый партнер в команде!</b>\n━━━━━━━━━━━━━━━━━━\n"
                        f"Пользователь ID <code>{uid}</code> присоединился по вашей ссылке и подписался на канал!\n"
                        f"💰 <b>Вам начислен партнерский бонус:</b> <b>+{format_bold_num(REFERRAL_BONUS_INVITER)}</b> на баланс прибыли!"
                    )
                    await bot.send_message(ref_id, text_ref, parse_mode="HTML")
                except Exception:
                    pass

            return ref_id
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            return None


# --- БЫСТРЫЙ ВЫБОР ПОСЛЕДНИХ ПОЛУЧАТЕЛЕЙ ПЕРЕВОДОВ ---

async def get_user_transfer_history(sender_id: int, limit: int = 5) -> List[Tuple]:
    """Получение истории исходящих переводов пользователя."""
    async with get_db_connection() as db:
        async with db.execute(
            """SELECT t.recipient_id, u.username, t.amount, t.net_amount, t.date, t.is_anonymous 
               FROM transfer_logs t 
               LEFT JOIN users u ON t.recipient_id = u.user_id 
               WHERE t.sender_id = ? 
               ORDER BY t.id DESC LIMIT ?""",
            (sender_id, limit)
        ) as cur:
            return await cur.fetchall()

async def get_recent_transfer_recipients(sender_id: int, limit: int = 3) -> List[Tuple[int, str]]:
    """Получение списка уникальных последних получателей перевода для быстрых кнопок."""
    async with get_db_connection() as db:
        async with db.execute(
            """SELECT t.recipient_id, u.username 
               FROM transfer_logs t 
               LEFT JOIN users u ON t.recipient_id = u.user_id 
               WHERE t.sender_id = ? 
               GROUP BY t.recipient_id 
               ORDER BY MAX(t.id) DESC LIMIT ?""",
            (sender_id, limit)
        ) as cur:
            return await cur.fetchall()

# --- КАТЕГОРИЙНЫЕ ФИЛЬТРЫ ИСТОРИИ ОПЕРАЦИЙ ---
async def get_user_transactions_filtered(uid: int, category: str = "all", limit: int = 10, offset: int = 0) -> Tuple[List[Tuple], int]:
    """Выборка транзакций по категориям с пагинацией."""
    cat_clauses = {
        "deposit": "AND (type LIKE '%Депозит%' OR type LIKE '%депозит%' OR type LIKE '%Бонус%' OR type LIKE '%бонус%' OR type LIKE '%Промокод%' OR type LIKE '%промокод%' OR type LIKE '%Реинвест%' OR type LIKE '%реинвест%' OR type LIKE '%Пополнение%' OR type LIKE '%пополнение%')",
        "withdraw": "AND (type LIKE '%Вывод%' OR type LIKE '%вывод%' OR type LIKE '%Выплат%' OR type LIKE '%выплат%')",
        "games": "AND (type LIKE '%Минёр%' OR type LIKE '%минёр%' OR type LIKE '%Минер%' OR type LIKE '%минер%' OR type LIKE '%Кост%' OR type LIKE '%кост%' OR type LIKE '%PvP%' OR type LIKE '%pvp%' OR type LIKE '%Дуэл%' OR type LIKE '%дуэл%' OR type LIKE '%Кейс%' OR type LIKE '%кейс%')",
        "transfer": "AND (type LIKE '%Перевод%' OR type LIKE '%перевод%')",
        "all": ""
    }
    clause = cat_clauses.get(category, "")

    async with get_db_connection() as db:
        count_cur = await db.execute(f"SELECT COUNT(*) FROM transactions WHERE user_id = ? {clause}", (uid,))
        total_count = (await count_cur.fetchone())[0]

        async with db.execute(
            f"SELECT type, amount, date, status FROM transactions WHERE user_id = ? {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            (uid, limit, offset)
        ) as cur:
            txs = await cur.fetchall()
            return txs, total_count

# --- АВТООТМЕНА ЗАВИСШИХ PvP СТОЛОВ (СТАРШЕ 24 ЧАСОВ) ---
async def auto_rollback_interrupted_pvp_lobbies() -> int:
    """Аварийный откат незавершенных игр (PLAYING) при перезапуске сервера с возвратом ставок."""
    now_iso = get_now_iso()
    count = 0
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT id, creator_id, opponent_id, bet FROM dice_pvp_lobbies WHERE status = 'PLAYING'") as cur:
                lobbies = await cur.fetchall()
            for lid, c_id, opp_id, bet in lobbies:
                await db.execute("UPDATE dice_pvp_lobbies SET status = 'CANCELLED' WHERE id = ?", (lid,))
                if c_id:
                    await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, c_id))
                    await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Откат дуэли (Рестарт)', ?, ?, 'Возвращено')", (c_id, bet, now_iso))
                if opp_id:
                    await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, opp_id))
                    await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Откат дуэли (Рестарт)', ?, ?, 'Возвращено')", (opp_id, bet, now_iso))
                count += 1
            await db.commit()
            return count
        except Exception:
            await db.execute("ROLLBACK")
            return 0

async def auto_cancel_expired_pvp_lobbies() -> int:
    """Фоновая отмена зависших столов старше 24 часов с возвратом ставок создателям."""
    now = get_now()
    cutoff_iso = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = get_now_iso()
    cancelled_count = 0

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT id, creator_id, bet FROM dice_pvp_lobbies WHERE status = 'OPEN' AND created_at < ?", (cutoff_iso,)) as cur:
                expired = await cur.fetchall()

            for lid, creator_id, bet in expired:
                await db.execute("UPDATE dice_pvp_lobbies SET status = 'CANCELLED' WHERE id = ?", (lid,))
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, creator_id))
                await db.execute(
                    "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Авто-отмена дуэли (24ч)', ?, ?, 'Возвращено')",
                    (creator_id, bet, now_iso)
                )
                cancelled_count += 1

            await db.commit()
            return cancelled_count
        except Exception:
            await db.execute("ROLLBACK")
            return 0

async def get_platform_stats_live() -> dict:
    async with get_db_connection() as db:
        # Пользователи
        cnt_row = await (await db.execute("SELECT COUNT(*) FROM users")).fetchone()
        cnt = cnt_row[0] if cnt_row else 0
        
        act_row = await (await db.execute("SELECT COUNT(*) FROM users WHERE invested > 0 AND is_banned = 0")).fetchone()
        active_inv = act_row[0] if act_row else 0

        inv_row = await (await db.execute("SELECT SUM(invested) FROM users WHERE is_banned = 0")).fetchone()
        inv = round(inv_row[0], 2) if inv_row and inv_row[0] else 0.0

        earn_row = await (await db.execute("SELECT SUM(earned) FROM users WHERE is_banned = 0")).fetchone()
        earn = round(earn_row[0], 2) if earn_row and earn_row[0] else 0.0

        # Пополнения депозитов
        dep_row = await (await db.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE type LIKE '%Пополнение%' AND status = 'Зачислено'")).fetchone()
        dep_cnt = dep_row[0] if dep_row else 0
        dep_sum = round(dep_row[1], 2) if dep_row and dep_row[1] else 0.0

        # Выводы: всего выплачено
        w_done_row = await (await db.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE (type = '📤 Выплата' OR type LIKE '%Вывод%') AND status = 'Одобрено'")).fetchone()
        w_done_cnt = w_done_row[0] if w_done_row else 0
        w_done_sum = round(w_done_row[1], 2) if w_done_row and w_done_row[1] else 0.0

        # Выводы: очередь на вывод (в обработке)
        w_q_row = await (await db.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE type = '📤 Вывод' AND status = 'В обработке'")).fetchone()
        w_q_cnt = w_q_row[0] if w_q_row else 0
        w_q_sum = round(w_q_row[1], 2) if w_q_row and w_q_row[1] else 0.0

        # Реинвестирование в депозит
        reinv_row = await (await db.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE type LIKE '%Реинвест%' AND status = 'Зачислено'")).fetchone()
        reinv_cnt = reinv_row[0] if reinv_row else 0
        reinv_sum = round(reinv_row[1], 2) if reinv_row and reinv_row[1] else 0.0

        # Кости PvE
        d_stats = await (await db.execute("SELECT COUNT(*), SUM(bet), SUM(win_amount) FROM dice_logs")).fetchone()
        d_cnt = d_stats[0] if d_stats else 0
        d_spent = (d_stats[1] or 0.0) if d_stats else 0.0
        d_won = (d_stats[2] or 0.0) if d_stats else 0.0
        d_profit = round(d_spent - d_won, 2)

        # Кости PvP
        pvp_row = await (await db.execute("SELECT COUNT(*), SUM(bet * 2), SUM(ROUND(bet * 2 * 0.0333, 2)) FROM dice_pvp_lobbies WHERE status = 'FINISHED' AND winner_id IS NOT NULL")).fetchone()
        pvp_cnt = pvp_row[0] if pvp_row else 0
        pvp_pot = round(pvp_row[1], 2) if pvp_row and pvp_row[1] else 0.0
        pvp_profit = round(pvp_row[2], 2) if pvp_row and pvp_row[2] else 0.0

        # P2P Переводы
        trans_row = await (await db.execute("SELECT COUNT(*), SUM(amount), SUM(fee) FROM transfer_logs")).fetchone()
        trans_cnt = trans_row[0] if trans_row else 0
        trans_vol = round(trans_row[1], 2) if trans_row and trans_row[1] else 0.0
        fees = round(trans_row[2], 2) if trans_row and trans_row[2] else 0.0

        return {
            "cnt": cnt,
            "active_inv": active_inv,
            "inv": inv,
            "earn": earn,
            "dep_cnt": dep_cnt,
            "dep_sum": dep_sum,
            "w_done_cnt": w_done_cnt,
            "w_done_sum": w_done_sum,
            "w_q_cnt": w_q_cnt,
            "w_q_sum": w_q_sum,
            "reinv_cnt": reinv_cnt,
            "reinv_sum": reinv_sum,
            "d_cnt": d_cnt,
            "d_profit": d_profit,
            "pvp_cnt": pvp_cnt,
            "pvp_pot": pvp_pot,
            "pvp_profit": pvp_profit,
            "trans_cnt": trans_cnt,
            "trans_vol": trans_vol,
            "fees": fees
        }
# --- ИГРА МИНЁР (MINES 5X5) ---
def calculate_mines_multiplier(mines_count: int, opened_count: int) -> float:
    if opened_count <= 0: return 1.0
    total_cells = 25
    safe_cells = total_cells - mines_count
    if opened_count > safe_cells: opened_count = safe_cells
    prob = 1.0
    for i in range(opened_count):
        prob *= (safe_cells - i) / (total_cells - i)
    raw_mult = 0.88 / prob
    return max(1.05, round(raw_mult, 2))

async def get_user_active_mines_game(user_id: int) -> Optional[dict]:
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, user_id, bet, mines_count, mines_positions, opened_cells, current_multiplier, status, win_amount FROM mines_games WHERE user_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            import json
            return {
                "id": row[0],
                "user_id": row[1],
                "bet": row[2],
                "mines_count": row[3],
                "mines_positions": json.loads(row[4]),
                "opened_cells": json.loads(row[5]),
                "current_multiplier": row[6],
                "status": row[7],
                "win_amount": row[8],
                "is_admin": await is_admin(user_id)
            }

async def create_mines_game(user_id: int, bet: float, mines_count: int) -> Tuple[bool, str, Optional[dict]]:
    if bet < MIN_MINES_BET or bet > MAX_MINES_BET:
        return False, f"Ставка в Минёр должна быть от {format_bold_num(MIN_MINES_BET)} до {format_bold_num(MAX_MINES_BET)}!", None
    if mines_count not in [3, 5, 10, 15]:
        return False, "Некорректное количество мин (выберите 3, 5, 1O или 15)!", None

    existing = await get_user_active_mines_game(user_id)
    if existing:
        return False, "У вас уже есть активная игра в Минёр! Завершите её.", existing

    u = await get_and_update_user(user_id)
    hold_amt = await get_user_transfer_hold_amount(user_id)
    avail = max(0.0, round(u['earned'] - hold_amt, 2))
    if avail < bet:
        hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
        return False, f"Недостаточно доступных средств{hold_msg}! Доступно: {format_bold_num(avail)}", None

    now_iso = get_now_iso()
    bet = round(bet, 2)
    import json, random
    mines_positions = random.sample(range(25), mines_count)
    opened_cells = []

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?",
                (bet, user_id, bet)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Недостаточно средств на балансе чистой прибыли!", None

            ins_cur = await db.execute(
                "INSERT INTO mines_games (user_id, bet, mines_count, mines_positions, opened_cells, current_multiplier, status, win_amount, profit_user, created_at) VALUES (?, ?, ?, ?, ?, 1.0, 'ACTIVE', 0.0, 0.0, ?)",
                (user_id, bet, mines_count, json.dumps(mines_positions), json.dumps(opened_cells), now_iso)
            )
            game_id = ins_cur.lastrowid
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '💣 Ставка в Минёр', ?, ?, 'Списано')",
                (user_id, bet, now_iso)
            )
            await db.commit()

            new_game = {
                "id": game_id,
                "user_id": user_id,
                "bet": bet,
                "mines_count": mines_count,
                "mines_positions": mines_positions,
                "opened_cells": opened_cells,
                "current_multiplier": 1.0,
                "status": "ACTIVE",
                "win_amount": 0.0,
                "is_admin": await is_admin(user_id)
            }
            return True, "Игра успешно создана!", new_game
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка создания игры: {e}", None

async def step_mines_game(user_id: int, cell_index: int) -> Tuple[bool, str, dict]:
    if cell_index < 0 or cell_index >= 25:
        return False, "Некорректная ячейка!", {}

    import json
    now_iso = get_now_iso()

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id, user_id, bet, mines_count, mines_positions, opened_cells, current_multiplier, status, win_amount FROM mines_games WHERE user_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
                (user_id,)
            ) as cur:
                row = await cur.fetchone()

            if not row:
                await db.execute("ROLLBACK")
                return False, "Активная игра не найдена!", {}

            game = {
                "id": row[0],
                "user_id": row[1],
                "bet": row[2],
                "mines_count": row[3],
                "mines_positions": json.loads(row[4]),
                "opened_cells": json.loads(row[5]),
                "current_multiplier": row[6],
                "status": row[7],
                "win_amount": row[8],
                "is_admin": await is_admin(user_id)
            }

            if cell_index in game["opened_cells"]:
                await db.execute("ROLLBACK")
                return False, "Эта ячейка уже открыта!", game

            bet = game["bet"]
            mines_count = game["mines_count"]

            # Проверка: наступил ли игрок на мину
            if cell_index in game["mines_positions"]:
                cur_loss = await db.execute(
                    "UPDATE mines_games SET status = 'LOST', profit_user = ?, finished_at = ? WHERE id = ? AND status = 'ACTIVE'",
                    (-bet, now_iso, game["id"])
                )
                if cur_loss.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False, "Игра уже завершена!", game
                await db.commit()
                game["status"] = "LOST"
                game["profit_user"] = -bet
                return True, "Взрыв", game

            # Игрок открыл безопасную ячейку
            new_opened = list(game["opened_cells"]) + [cell_index]
            new_mult = calculate_mines_multiplier(mines_count, len(new_opened))
            safe_cells_total = 25 - mines_count

            if len(new_opened) >= safe_cells_total:
                win_amt = round(bet * new_mult, 2)
                profit = round(win_amt - bet, 2)
                cur_win = await db.execute(
                    "UPDATE mines_games SET opened_cells = ?, current_multiplier = ?, status = 'WON', win_amount = ?, profit_user = ?, finished_at = ? WHERE id = ? AND status = 'ACTIVE'",
                    (json.dumps(new_opened), new_mult, win_amt, profit, now_iso, game["id"])
                )
                if cur_win.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False, "Игра уже завершена!", game
                await db.execute(
                    "UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?",
                    (win_amt, user_id)
                )
                await db.execute(
                    "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '💣 Выигрыш в Минёр (Джекпот)', ?, ?, 'Зачислено')",
                    (user_id, win_amt, now_iso)
                )
                await db.commit()
                game["status"] = "WON"
                game["opened_cells"] = new_opened
                game["current_multiplier"] = new_mult
                game["win_amount"] = win_amt
                game["full_clear"] = True
                return True, "Полная зачистка поля!", game

            cur_step = await db.execute(
                "UPDATE mines_games SET opened_cells = ?, current_multiplier = ? WHERE id = ? AND status = 'ACTIVE'",
                (json.dumps(new_opened), new_mult, game["id"])
            )
            if cur_step.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Игра уже завершена!", game
            await db.commit()
            game["opened_cells"] = new_opened
            game["current_multiplier"] = new_mult
            return True, "Алмаз найден", game
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка хода: {e}", {}

async def cashout_mines_game(user_id: int) -> Tuple[bool, str, dict]:
    game = await get_user_active_mines_game(user_id)
    if not game:
        return False, "Активная игра не найдена!", {}
    if len(game["opened_cells"]) == 0:
        return False, "Откройте хотя бы одну ячейку перед тем, как забрать банк!", game

    now_iso = get_now_iso()
    bet = game["bet"]
    mult = game["current_multiplier"]
    win_amt = round(bet * mult, 2)
    profit = round(win_amt - bet, 2)

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE mines_games SET status = 'WON', win_amount = ?, profit_user = ?, finished_at = ? WHERE id = ? AND status = 'ACTIVE'",
                (win_amt, profit, now_iso, game["id"])
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Игра уже завершена!", game

            await db.execute(
                "UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?",
                (win_amt, user_id)
            )
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '💣 Забрал выигрыш в Минёр', ?, ?, 'Зачислено')",
                (user_id, win_amt, now_iso)
            )
            await db.commit()
            game["status"] = "WON"
            game["win_amount"] = win_amt
            game["profit_user"] = profit
            return True, "Выигрыш успешно зачислен!", game
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка при заборе выигрыша: {e}", game

async def get_user_recent_mines_history(user_id: int, limit: int = 3) -> list:
    """Возвращает историю последних N завершенных игр пользователя в Минёр."""
    async with get_db_connection() as db:
        try:
            cur = await db.execute(
                "SELECT status, bet, current_multiplier, win_amount FROM mines_games WHERE user_id = ? AND status IN ('WON', 'LOST') ORDER BY id DESC LIMIT ?",
                (user_id, limit)
            )
            rows = await cur.fetchall()
            return [{"status": r[0], "bet": r[1], "mult": r[2], "win_amount": r[3]} for r in rows]
        except Exception:
            return []

async def get_user_last_mines_bet(user_id: int) -> float:
    """Возвращает сумму последней ставки пользователя в Минёр для быстрых множителей х2 и /2."""
    async with get_db_connection() as db:
        try:
            cur = await db.execute(
                "SELECT bet FROM mines_games WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                (user_id,)
            )
            row = await cur.fetchone()
            return float(row[0]) if row and row[0] else 100_000.0
        except Exception:
            return 100_000.0

async def get_mines_stats() -> dict:
    async with get_db_connection() as db:
        try:
            cur = await db.execute("SELECT COUNT(*), SUM(bet), SUM(win_amount) FROM mines_games WHERE status IN ('WON', 'LOST')")
            row = await cur.fetchone()
            cnt = row[0] if row and row[0] else 0
            bets = round(row[1], 2) if row and row[1] else 0.0
            wins = round(row[2], 2) if row and row[2] else 0.0
            profit = round(bets - wins, 2)
            rtp = round((wins / bets * 100), 1) if bets > 0 else 93.0
            return {
                "count": cnt,
                "bets": bets,
                "wins": wins,
                "profit": profit,
                "rtp": rtp
            }
        except Exception:
            return {"count": 0, "bets": 0.0, "wins": 0.0, "profit": 0.0, "rtp": 88.0}

async def auto_cancel_expired_mines_games() -> int:
    """Фоновое закрытие брошенных сессий Минёра старше 24 часов с фиксацией кэшаута."""
    now = get_now()
    cutoff_iso = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = get_now_iso()
    settled_count = 0

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id, user_id, bet, current_multiplier, opened_cells FROM mines_games WHERE status = 'ACTIVE' AND created_at < ?",
                (cutoff_iso,)
            ) as cur:
                expired = await cur.fetchall()

            for gid, uid, bet, mult, opened_json in expired:
                import json
                opened = json.loads(opened_json) if opened_json else []
                if len(opened) > 0:
                    win_amt = round(bet * mult, 2)
                    profit = round(win_amt - bet, 2)
                    await db.execute(
                        "UPDATE mines_games SET status = 'WON', win_amount = ?, profit_user = ?, finished_at = ? WHERE id = ?",
                        (win_amt, profit, now_iso, gid)
                    )
                    await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (win_amt, uid))
                    await db.execute(
                        "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '💣 Авто-кэшаут Минёр (24ч)', ?, ?, 'Зачислено')",
                        (uid, win_amt, now_iso)
                    )
                else:
                    await db.execute(
                        "UPDATE mines_games SET status = 'CANCELLED', profit_user = 0.0, finished_at = ? WHERE id = ?",
                        (now_iso, gid)
                    )
                    await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, uid))
                    await db.execute(
                        "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '💣 Возврат ставки Минёр (24ч)', ?, ?, 'Возвращено')",
                        (uid, bet, now_iso)
                    )
                settled_count += 1

            await db.commit()
            return settled_count
        except Exception:
            await db.execute("ROLLBACK")
            return 0

async def is_game_module_enabled(module_key: str) -> bool:
    """Проверка доступности игрового модуля (mines, dice_pve, dice_pvp, p2p)."""
    return (await get_setting(f"module_{module_key}", "1")) == "1"

async def toggle_game_module(module_key: str) -> bool:
    """Тумблер включения/выключения игрового модуля (Kill-Switch)."""
    cur = await is_game_module_enabled(module_key)
    new_val = "0" if cur else "1"
    await set_setting(f"module_{module_key}", new_val)
    return new_val == "1"

async def find_user_by_query(query: str) -> Optional[dict]:
    """Поиск пользователя по цифровому ID или по @username."""
    q = query.strip().replace("@", "")
    async with get_db_connection() as db:
        target_uid = None
        if q.isdigit():
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(q),)) as cur:
                row = await cur.fetchone()
                if row: target_uid = row[0]
        if not target_uid:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (q,)) as cur:
                row = await cur.fetchone()
                if row: target_uid = row[0]
        if target_uid:
            u = await get_and_update_user(target_uid)
            u["user_id"] = target_uid
            return u
    return None

async def wipe_entire_database(owner_id: int, owner_name: str = "") -> Tuple[bool, str]:
    """Полная очистка базы данных (WIPE) строго по подтверждению владельца."""
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            tables = [
                "users", "transactions", "transactions_archive", "mines_games",
                "dice_pvp_lobbies", "promo_activations", "promocodes",
                "admin_logs", "case_logs", "dice_logs", "transfer_logs", "settings"
            ]
            for tbl in tables:
                await db.execute(f"DELETE FROM {tbl}")
            try:
                await db.execute("DELETE FROM sqlite_sequence")
            except Exception:
                pass

            # Восстанавливаем аккаунт владельца с правами администратора
            await db.execute(
                "INSERT INTO users (user_id, invested, earned, last_accrual, is_admin, referrer_id, referral_rewarded, is_banned, last_active, username, is_blocked, notified_min, registered_at) VALUES (?, 0.0, 0.0, ?, 1, NULL, 0, 0, ?, ?, 0, 0, ?)",
                (owner_id, now_iso, now_iso, owner_name or "Owner", now_iso)
            )
            await db.commit()
            await db.execute("VACUUM")
            return True, "База данных успешно очищена! Счётчики сброшены, профиль владельца восстановлен."
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка сброса базы: {e}"


async def claim_daily_bonus(uid: int) -> Tuple[bool, float, Optional[timedelta]]:
    """Забрать ежедневный бонус раз в 24 часа."""
    now = get_now()
    now_iso = get_now_iso()
    
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT last_daily_bonus FROM users WHERE user_id = ?", (uid,)) as cur:
                row = await cur.fetchone()
            
            if not row:
                await db.execute("ROLLBACK")
                return False, 0.0, None
                
            last_bonus_str = row[0]
            if last_bonus_str:
                try:
                    cleaned = last_bonus_str.replace("T", " ")[:19]
                    last_dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
                    diff = now - last_dt
                    if diff < timedelta(hours=24):
                        await db.execute("ROLLBACK")
                        remaining = timedelta(hours=24) - diff
                        return False, 0.0, remaining
                except Exception:
                    pass
            
            # Начисляем случайный бонус от 25$ до 150$
            bonus_amt = float(random.randint(5_000, 25_000))
            
            await db.execute(
                "UPDATE users SET earned = earned + ?, last_daily_bonus = ? WHERE user_id = ?",
                (bonus_amt, now_iso, uid)
            )
            
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, 'daily_bonus', ?, ?, 'completed')",
                (uid, bonus_amt, now_iso)
            )
            
            await db.commit()
            return True, bonus_amt, None
        except Exception:
            try: await db.execute("ROLLBACK")
            except Exception: pass
            raise


async def can_claim_daily_bonus(uid: int) -> bool:
    """Проверяет, доступен ли ежедневный бонус для пользователя (прошло ли 24 часа)."""
    now = get_now()
    async with get_db_connection() as db:
        async with db.execute("SELECT last_daily_bonus FROM users WHERE user_id = ?", (uid,)) as cur:
            row = await cur.fetchone()
        if not row or not row[0]:
            return True
        try:
            cleaned = row[0].replace("T", " ")[:19]
            last_dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
            return (now - last_dt) >= timedelta(hours=24)
        except Exception:
            return True


# --- ТРАНЗАКЦИИ И ПЕРЕВОДЫ ---
async def add_tx(uid: int, tx_type: str, amount: float, status: str = "Выполнено", admin_chat_id: int = None, admin_msg_id: int = None) -> int:
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        cur = await db.execute(
            "INSERT INTO transactions (user_id, type, amount, date, status, admin_chat_id, admin_msg_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, tx_type, round(amount, 2), now_iso, status, admin_chat_id, admin_msg_id)
        )
        await db.commit()
        return cur.lastrowid

async def execute_p2p_transfer(sender_id: int, recip_id: int, amount: float, fee: float, net_amount: float, is_anonymous: int = 0, sender_uname: str = "") -> Tuple[bool, str]:
    if sender_id == recip_id:
        return False, "Нельзя переводить средства самому себе!" 
    await get_and_update_user(sender_id)
    await get_and_update_user(recip_id)
    now_iso = get_now_iso()
    amount = round(amount, 2)
    fee = round(fee, 2)
    net_amount = round(net_amount, 2)

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?",
                (amount, sender_id, amount)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Недостаточно средств на балансе прибыли!"

            await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (net_amount, recip_id))
            await db.execute(
                "INSERT INTO transfer_logs (sender_id, recipient_id, amount, fee, net_amount, is_anonymous, date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sender_id, recip_id, amount, fee, net_amount, is_anonymous, now_iso)
            )
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, ?, ?, ?, 'Выполнено')",
                (sender_id, f"💸 Перевод игроку ID {recip_id}", amount, now_iso)
            )
            
            if is_anonymous:
                recip_desc = "📥 Анонимный перевод"
            elif sender_uname:
                recip_desc = f"📥 Перевод от @{sender_uname}"
            else:
                recip_desc = f"📥 Перевод от ID {sender_id}"
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, ?, ?, ?, 'Зачислено')",
                (recip_id, recip_desc, net_amount, now_iso)
            )
            await db.commit()
            return True, "Перевод успешно выполнен!"
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка перевода: {e}"

async def log_admin_action(admin_id: int, admin_tag: str, action: str, target_id: int = None, details: str = ""):
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO admin_logs (admin_id, admin_tag, action, target_id, details, date) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, admin_tag, action, target_id, details, now_iso)
        )
        await db.commit()
