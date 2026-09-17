import re
import os
import html
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Router, F, types, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, BufferedInputFile
from config.config import (
    DB_FILE, OWNER_ID, ADMIN_CHAT_ID, REVIEWS_CHANNEL, EFFECT_CONFETTI_ID, EMOJIS,
    OKA_GOS_PRICE, fmt_o, format_bold_num, format_raw_num, parse_amount, get_now, get_now_iso,
    format_datetime_display, is_menu_trigger
)
from database.database import (
    get_and_update_user, adjust_user_balance_atomic, update_user_manual, is_admin, is_maintenance_active,
    is_withdrawals_frozen, set_setting, set_user_ban_status, set_user_admin_status,
    log_admin_action, get_platform_stats_live, add_tx, get_db_connection,
    get_tx_by_id, get_mines_stats, is_game_module_enabled, toggle_game_module, find_user_by_query,
    wipe_entire_database
)

router = Router()

class AdminPanelState(StatesGroup):
    waiting_add_inv = State()
    waiting_sub_inv = State()
    waiting_add_earn = State()
    waiting_sub_earn = State()
    waiting_find_user = State()
    waiting_broadcast = State()
    waiting_make_adm = State()
    waiting_rem_adm = State()
    waiting_ban_user = State()
    waiting_unban_user = State()
    waiting_create_promo = State()

def get_admin_panel_kb(uid: int, maint_mode: bool, frozen_mode: bool):
    """Главное меню админ-панели со структурированными категориями."""
    kb = [
        [InlineKeyboardButton(text="📊 Финансы и касса", callback_data="adm_sec_finance"), InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_sec_users")],
        [InlineKeyboardButton(text="⚙️ Управление и игры", callback_data="adm_sec_control"), InlineKeyboardButton(text="👑 Команда и логи", callback_data="adm_sec_team")],
        [InlineKeyboardButton(text="💾 Система и данные", callback_data="adm_sec_system")]
    ]
    if uid == OWNER_ID:
        kb.append([InlineKeyboardButton(text="🔄 Обновить с GitHub (1 клик)", callback_data="adm_btn_git_update")])
    kb.append([InlineKeyboardButton(text="❌ Закрыть панель", callback_data="adm_btn_close")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_sec_finance_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Очередь выплат", callback_data="adm_btn_queue:0"), InlineKeyboardButton(text="⚖️ Нагрузка кассы", callback_data="adm_btn_risk")],
        [InlineKeyboardButton(text="📈 Динамика 24ч", callback_data="adm_btn_today"), InlineKeyboardButton(text="📊 Статистика", callback_data="adm_btn_stats")],
        [InlineKeyboardButton(text="💸 Логи переводов", callback_data="adm_btn_transfers:0")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]
    ])

def get_sec_users_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список игроков", callback_data="adm_btn_users_page:0"), InlineKeyboardButton(text="👤 Поиск игрока", callback_data="adm_btn_find_user")],
        [InlineKeyboardButton(text="📥 Пополнить деп", callback_data="adm_btn_add_inv"), InlineKeyboardButton(text="📉 Списать деп", callback_data="adm_btn_sub_inv")],
        [InlineKeyboardButton(text="💰 Начислить доход", callback_data="adm_btn_add_earn"), InlineKeyboardButton(text="📉 Списать баланс", callback_data="adm_btn_sub_earn")],
        [InlineKeyboardButton(text="⛔ Забанить", callback_data="adm_btn_ban_user"), InlineKeyboardButton(text="🍏 Разбанить", callback_data="adm_btn_unban_user")],
        [InlineKeyboardButton(text="🚫 Черный список", callback_data="adm_btn_banlist")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]
    ])

def get_sec_control_kb(maint_mode: bool, frozen_mode: bool):
    m_btn_text = "🛠 Техработы: 🔴" if maint_mode else "🛠 Техработы: 🟢"
    w_btn_text = "🔒 Выводы: 🔴" if frozen_mode else "🔒 Выводы: 🟢"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕹 Модули игр (Kill-Switch)", callback_data="adm_btn_modules")],
        [InlineKeyboardButton(text=w_btn_text, callback_data="adm_btn_toggle_frozen"), InlineKeyboardButton(text=m_btn_text, callback_data="adm_btn_toggle_maint")],
        [InlineKeyboardButton(text="🎁 Все промокоды", callback_data="adm_btn_promos"), InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_btn_broadcast")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]
    ])

def get_sec_team_kb(uid: int):
    kb = [
        [InlineKeyboardButton(text="📋 Админы", callback_data="adm_btn_admins"), InlineKeyboardButton(text="📋 Логи действий", callback_data="adm_btn_logs:0")]
    ]
    if uid == OWNER_ID:
        kb.append([InlineKeyboardButton(text="👑 Выдать админа", callback_data="adm_btn_make_adm"), InlineKeyboardButton(text="❌ Снять админа", callback_data="adm_btn_rem_adm")])
    kb.append([InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_sec_system_kb(uid: int):
    kb = [
        [InlineKeyboardButton(text="💾 Бэкап (.db)", callback_data="adm_btn_backup"), InlineKeyboardButton(text="📑 Экспорт CSV", callback_data="adm_btn_export_menu")]
    ]
    if uid == OWNER_ID:
        kb.append([InlineKeyboardButton(text="📦 Скачать проект (.zip)", callback_data="adm_btn_download_src")])
        kb.append([InlineKeyboardButton(text="🗑 Полный сброс БД (Wipe)", callback_data="ask_wipe_db")])
    kb.append([InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_to_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]])

def get_back_to_user_card_kb(tid: int):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в карточку", callback_data=f"adm_user_card:{tid}")]])

async def send_or_edit_panel_message(call: types.CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup):
    """Безопасная смена содержимого с защитой от дублирования сообщений при неизменном тексте."""
    try:
        if call.message.photo:
            await call.message.delete()
            await call.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await call.message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            pass

BOT_VERSION = "v3.2.0-AutoDeploy"
CURRENT_CHANGELOG = """• 🚀 Auto-Deploy: бот автоматически подтягивает коммиты с GitHub и перезапускается без нажатия кнопок
• 💣 Игра «Минёр»: снижен базовый RTP до 80% (скорректированы коэффициенты в пользу кассы)
• 📈 Плавная прогрессия множителей без просадок на начальных ходах""".strip()
BOT_START_TIME = datetime.now()

def get_admin_panel_text() -> str:
    uptime_delta = datetime.now() - BOT_START_TIME
    total_seconds = int(uptime_delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}ч {minutes}м {seconds}с"
    return (
        f"{EMOJIS['crown']} <b>Панель управления Nemos Trade</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Версия ядра:</b> <code>{BOT_VERSION}</code> 🚀\n"
        f"⏱ <b>Аптайм бота:</b> <code>{uptime_str}</code>\n"
        "🛡 <b>Система:</b> 🟢 <code>Работает штатно</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Выберите нужное действие кнопками ниже:</b>"
    )

# --- ТОЧКА ВХОДА В АДМИНКУ (СТРОГО В ЛИЧНЫХ СООБЩЕНИЯХ) ---
@router.message(F.chat.type == "private", F.text.func(lambda t: t and ("админ-панель" in t.lower() or "админка" in t.lower() or "👑" in t)))
@router.message(F.chat.type == "private", Command("admin"))
async def open_admin_panel(msg: types.Message, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = msg.from_user.id
    if not await is_admin(uid): return
    maint, froz = await is_maintenance_active(), await is_withdrawals_frozen()
    await msg.answer(
        get_admin_panel_text(),
        reply_markup=get_admin_panel_kb(uid, maint, froz),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "adm_back_panel")
async def cb_adm_back_panel(call: types.CallbackQuery, state: Optional[FSMContext] = None):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    if not await is_admin(uid): return
    maint, froz = await is_maintenance_active(), await is_withdrawals_frozen()
    t = get_admin_panel_text()
    await send_or_edit_panel_message(call, t, get_admin_panel_kb(uid, maint, froz))
    await call.answer()

# --- КАТЕГОРИИ АДМИН-ПАНЕЛИ (РАЗДЕЛЫ) ---
@router.callback_query(F.data == "adm_sec_finance")
async def cb_sec_finance(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    t = (
        """📊 <b>РАЗДЕЛ: ФИНАНСЫ И КАССА</b>
━━━━━━━━━━━━━━━━━━
• Управление очередью выплат и заявками
• Мониторинг рисков и нагрузки на кассу
• Аналитика суточной динамики и общая статистика
• Аудит и логи финансовых переводов между игроками""".strip()
    )
    await send_or_edit_panel_message(call, t, get_sec_finance_kb())
    await call.answer()

@router.callback_query(F.data == "adm_sec_users")
async def cb_sec_users(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    t = (
        """👥 <b>РАЗДЕЛ: ПОЛЬЗОВАТЕЛИ И БАЛАНСЫ</b>
━━━━━━━━━━━━━━━━━━
• Просмотр списков и детальный поиск по ID / юзернейму
• Ручная корректировка балансов депозита и дохода
• Блокировка, разблокировка и черный список игроков""".strip()
    )
    await send_or_edit_panel_message(call, t, get_sec_users_kb())
    await call.answer()

@router.callback_query(F.data == "adm_sec_control")
async def cb_sec_control(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    maint, froz = await is_maintenance_active(), await is_withdrawals_frozen()
    t = (
        """⚙️ <b>РАЗДЕЛ: УПРАВЛЕНИЕ И ИГРЫ</b>
━━━━━━━━━━━━━━━━━━
• Аварийное отключение игр (Kill-Switch)
• Режим технических работ и заморозка вывода
• Создание и учет промокодов
• Массовая рассылка сообщений пользователям""".strip()
    )
    await send_or_edit_panel_message(call, t, get_sec_control_kb(maint, froz))
    await call.answer()

@router.callback_query(F.data == "adm_sec_team")
async def cb_sec_team(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    t = (
        """👑 <b>РАЗДЕЛ: КОМАНДА И ЛОГИ</b>
━━━━━━━━━━━━━━━━━━
• Просмотр состава администрации проекта
• Назначение и отзыв прав администраторов (только Владелец)
• Детальный журнал всех действий администраторов""".strip()
    )
    await send_or_edit_panel_message(call, t, get_sec_team_kb(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "adm_sec_system")
async def cb_sec_system(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    t = (
        """💾 <b>РАЗДЕЛ: СИСТЕМА И ДАННЫЕ</b>
━━━━━━━━━━━━━━━━━━
• Скачивание резервной копии базы данных (.db)
• Выгрузка отчетов и аналитики в формате CSV
• Скачивание исходного кода проекта (.zip)
• Обновление системы из облака и экстренный сброс (Wipe)""".strip()
    )
    await send_or_edit_panel_message(call, t, get_sec_system_kb(call.from_user.id))
    await call.answer()

@router.callback_query(F.data == "adm_btn_close")
async def adm_btn_close_callback(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    try: await call.message.delete()
    except Exception: pass
    await call.answer("Панель закрыта")

# --- СИСТЕМНЫЕ ПЕРЕКЛЮЧАТЕЛИ ---
@router.callback_query(F.data == "adm_btn_toggle_maint")
async def cb_toggle_maint(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    cur = await is_maintenance_active()
    new_val = "0" if cur else "1"
    await set_setting("maintenance", new_val)
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    await log_admin_action(call.from_user.id, adm_tag, "Переключение техработ", details=f"Новое значение: {new_val}")
    await cb_sec_control(call)

@router.callback_query(F.data == "adm_btn_toggle_frozen")
async def cb_toggle_frozen(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    cur = await is_withdrawals_frozen()
    new_val = "0" if cur else "1"
    await set_setting("frozen", new_val)
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    await log_admin_action(call.from_user.id, adm_tag, "Переключение заморозки выплат", details=f"Новое значение: {new_val}")
    await cb_sec_control(call)

# --- РЕЗЕРВНАЯ КОПИЯ (BACKUP) ---
@router.callback_query(F.data == "adm_btn_backup")
@router.message(F.chat.type == "private", Command("backup"))
async def adm_btn_backup(event: types.CallbackQuery | types.Message):
    uid = event.from_user.id
    if not await is_admin(uid): return
    if os.path.exists(DB_FILE):
        doc = FSInputFile(DB_FILE)
        cap = f"💾 <b>Резервная копия базы данных:</b>\n📅 <i>{get_now().strftime('%d.%m.%Y %H:%M')} (МСК)</i>"
        if isinstance(event, types.CallbackQuery):
            await event.message.reply_document(document=doc, caption=cap, parse_mode="HTML")
            await event.answer()
        else:
            await event.reply_document(document=doc, caption=cap, parse_mode="HTML")
    else:
        if isinstance(event, types.CallbackQuery): await event.answer("Файл БД не найден!", show_alert=True)
        else: await event.reply("Файл БД не найден!")

# --- ВЫГРУЗКА ИСХОДНОГО КОДА ПРОЕКТА (.ZIP) СТРОГО ДЛЯ OWNER_ID ---
@router.callback_query(F.data == "adm_btn_download_src")
@router.message(F.chat.type == "private", Command("download_src"))
async def cb_download_src(event: types.CallbackQuery | types.Message, bot: Bot):
    uid = event.from_user.id
    if uid != OWNER_ID:
        if isinstance(event, types.CallbackQuery):
            return await event.answer("⛔ Доступно исключительно создателю проекта!", show_alert=True)
        return

    if isinstance(event, types.CallbackQuery):
        await event.answer("⏳ Собираю актуальный архив...", show_alert=False)
        status_msg = await event.message.answer("📦 <b>Сборка актуального архива проекта с сервера...</b>", parse_mode="HTML")
    else:
        status_msg = await event.answer("📦 <b>Сборка актуального архива проекта с сервера...</b>", parse_mode="HTML")

    import tempfile, zipfile, io
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SKIP_EXTS = {".db", ".db-wal", ".db-shm", ".pyc", ".log", ".backup", ".env"}
    SKIP_DIRS = {"__pycache__", ".git", ".idea", "venv", "node_modules", "downloads", ".pytest_cache"}
    SKIP_FILES = {".env", "token.env", "Token.env", "token.txt", ".restart_notify.txt", "bot_database.db", "bot_database.db-wal", "bot_database.db-shm"}

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_zip = tmp.name

    try:
        file_count = 0
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for f in files:
                    f_lower = f.lower()
                    if any(f_lower.endswith(ext) for ext in SKIP_EXTS):
                        continue
                    if f_lower in SKIP_FILES or f_lower.startswith(".env") or f_lower.startswith(".restart_notify") or "token" in f_lower:
                        continue
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, base_dir)
                    z.write(full_path, arcname=rel_path)
                    file_count += 1

        doc = FSInputFile(tmp_zip, filename=f"nemos_trade_live_{get_now().strftime('%Y%m%d_%H%M')}.zip")
        cap = (
            f"📦 <b>Актуальный исходный код Nemos Trade</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Версия ядра:</b> <code>{BOT_VERSION}</code> 🚀\n"
            f"📁 <b>Файлов в архиве:</b> <code>{file_count}</code>\n"
            f"📅 <b>Дата сборки:</b> <i>{get_now().strftime('%d.%m.%Y %H:%M')} (МСК)</i>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔒 <i>Архив сформирован прямо из рабочей директории сервера. Доступен строго создателю бота.</i>"
        )
        try: await status_msg.delete()
        except Exception: pass
        if isinstance(event, types.CallbackQuery):
            await event.message.answer_document(document=doc, caption=cap, parse_mode="HTML")
        else:
            await event.answer_document(document=doc, caption=cap, parse_mode="HTML")
    finally:
        try: os.remove(tmp_zip)
        except Exception: pass



# --- ЭКСПОРТ ДАННЫХ В CSV (EXCEL) И WHALE ALERTS ---
async def send_whale_alert(bot: Bot, uid: int, uname: str, action: str, amount: float):
    """Отправляет мгновенное уведомление владельцу о крупной операции (>= 5 000 000$ / 5кк)."""
    if amount < 5_000_000.0:
        return
    try:
        u_display = f"@{uname}" if uname else f"ID {uid}"
        text = (
            "🐋 <b>ВНИМАНИЕ: КРУПНАЯ ОПЕРАЦИЯ (WHALE ALERT)!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Игрок:</b> <b>{html.escape(u_display)}</b> (<code>{uid}</code>)\n"
            f"💰 <b>Сумма:</b> <b>{format_bold_num(amount)}</b>\n"
            f"⚡ <b>Событие:</b> <b>{html.escape(action)}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>Нажмите кнопку ниже для перехода в карточку игрока:</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Карточка игрока", callback_data=f"adm_user_card:{uid}")]
        ])
        await bot.send_message(OWNER_ID, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


def generate_users_table_png_bytes(rows, title="Nemos Trade — Пользователи") -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    import io, os

    def get_font(size=14, bold=False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf"
        ]
        for p in candidates:
            if os.path.exists(p):
                try: return ImageFont.truetype(p, size)
                except Exception: pass
        return ImageFont.load_default()

    font_title = get_font(18, bold=True)
    font_sub = get_font(13, bold=False)
    font_header = get_font(13, bold=True)
    font_cell = get_font(12, bold=False)

    width = 1080
    row_height = 38
    header_height = 85
    table_header_height = 40
    total_rows = min(len(rows), 30)
    height = header_height + table_header_height + max(1, total_rows) * row_height + 30

    img = Image.new("RGB", (width, height), color=(18, 22, 28))
    draw = ImageDraw.Draw(img)

    # Header panel
    draw.rectangle([(0, 0), (width, 75)], fill=(26, 33, 44))
    draw.text((25, 18), title, fill=(255, 215, 0), font=font_title)
    
    # Subtitle requested: пользователи по депозиту / дата обновления
    draw.text((25, 48), f"Пользователи по депозиту / Дата обновления: {get_now().strftime('%d.%m.%Y %H:%M')}", fill=(160, 175, 195), font=font_sub)

    cols = [
        ("ID", 20, 135),
        ("Username", 140, 310),
        ("Депозит ($)", 315, 465),
        ("Прибыль ($)", 470, 620),
        ("Выведено ($)", 625, 775),
        ("Реферер", 780, 915),
        ("Статус", 920, 1060)
    ]

    y = header_height
    draw.rectangle([(20, y), (width - 20, y + table_header_height)], fill=(38, 48, 66))
    for c_title, x1, x2 in cols:
        draw.text((x1 + 8, y + 11), c_title, fill=(255, 255, 255), font=font_header)

    y += table_header_height
    for idx in range(total_rows):
        r = rows[idx]
        bg = (24, 30, 40) if idx % 2 == 0 else (20, 25, 34)
        draw.rectangle([(20, y), (width - 20, y + row_height)], fill=bg)
        
        u_tag = f"@{r[1]}" if r[1] else "Нет"
        ref_txt = str(r[5]) if r[5] else "—"
        status_txt = "Забанен" if r[6] else "Активен"
        status_col = (255, 100, 100) if r[6] else (100, 255, 150)

        def _fmt_money(val) -> str:
            try:
                v = float(val)
                if v % 1 == 0:
                    return f"{int(v):,}$"
                return f"{v:,.2f}$"
            except Exception:
                return f"{val}$"

        draw.text((cols[0][1] + 8, y + 10), str(r[0]), fill=(200, 210, 225), font=font_cell)
        draw.text((cols[1][1] + 8, y + 10), u_tag, fill=(100, 200, 255), font=font_cell)
        draw.text((cols[2][1] + 8, y + 10), _fmt_money(r[2]), fill=(100, 255, 150), font=font_cell)
        draw.text((cols[3][1] + 8, y + 10), _fmt_money(r[3]), fill=(255, 220, 100), font=font_cell)
        draw.text((cols[4][1] + 8, y + 10), _fmt_money(r[4]), fill=(255, 160, 120), font=font_cell)
        draw.text((cols[5][1] + 8, y + 10), ref_txt, fill=(160, 175, 195), font=font_cell)
        draw.text((cols[6][1] + 8, y + 10), status_txt, fill=status_col, font=font_cell)
        y += row_height

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

@router.callback_query(F.data == "export_png:users")
async def cb_export_users_png(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    await call.answer("🖼 Генерирую PNG-таблицу...", show_alert=False)
    
    try:
        async with get_db_connection() as db:
            async with db.execute(
                """SELECT 
                    u.user_id, 
                    u.username, 
                    u.invested, 
                    u.earned, 
                    COALESCE((
                        SELECT SUM(amount) 
                        FROM transactions 
                        WHERE user_id = u.user_id 
                          AND (type LIKE '%Вывод%' OR type LIKE '%Выплат%') 
                          AND status = 'Одобрено'
                    ), 0.0) as total_withdrawn,
                    u.referrer_id, 
                    u.is_banned 
                FROM users u 
                ORDER BY u.invested DESC 
                LIMIT 30"""
            ) as cur:
                rows = await cur.fetchall()
                
        png_bytes = generate_users_table_png_bytes(rows)
        photo_file = BufferedInputFile(png_bytes, filename="nemos_users.png")
        
        caption = (
            f"🖼 <b>Таблица пользователей Nemos Trade</b>\n"
            f"📊 <b>Пользователи по депозиту / Дата обновления:</b> <i>{get_now().strftime('%d.%m.%Y %H:%M')}</i>\n"
            f"👥 <b>Показано:</b> <code>{len(rows)}</code> игроков"
        )
        await call.message.reply_photo(photo=photo_file, caption=caption, parse_mode="HTML")
    except Exception as e:
        await call.message.answer(f"❌ Ошибка генерации таблицы: <code>{html.escape(str(e))}</code>", parse_mode="HTML")

@router.callback_query(F.data == "adm_btn_export_menu")
async def cb_export_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Таблица игроков (PNG-картинка)", callback_data="export_png:users")],
        [InlineKeyboardButton(text="👥 Выгрузить пользователей (CSV файл)", callback_data="export_csv:users")],
        [InlineKeyboardButton(text="💳 Выгрузить историю транзакций (CSV файл)", callback_data="export_csv:txs")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(
        call,
        "📑 <b>Центр экспорта данных Nemos Trade</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Выберите удобный формат для просмотра или выгрузки данных:\n\n"
        "• 🖼 <b>PNG-картинка:</b> Наглядная таблица с депозитами, прибылью и выводами прямо в чате.\n"
        "• 👥 <b>CSV (Excel):</b> Полный список игроков в табличном формате.\n"
        "• 💳 <b>CSV (Транзакции):</b> Реестр заявок на ввод, вывод, переводы и игры.\n",
        kb
    )
    await call.answer()

@router.callback_query(F.data == "export_csv:users")
async def cb_export_users_csv(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    await call.answer("⏳ Формирую CSV пользователей...", show_alert=False)
    
    import csv, io
    async with get_db_connection() as db:
        async with db.execute(
            """SELECT 
                u.user_id, 
                u.username, 
                u.invested, 
                u.earned, 
                COALESCE((
                    SELECT SUM(amount) 
                    FROM transactions 
                    WHERE user_id = u.user_id 
                      AND (type LIKE '%Вывод%' OR type LIKE '%Выплат%') 
                      AND status = 'Одобрено'
                ), 0.0) as total_withdrawn,
                u.referrer_id, 
                u.registered_at, 
                u.is_banned, 
                u.is_admin 
            FROM users u 
            ORDER BY u.invested DESC"""
        ) as cur:
            rows = await cur.fetchall()
            
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID", "Username", "Депозит ($)", "Прибыль ($)", "Выведено ($)", "Пригласитель (ID)", "Дата регистрации", "Забанен", "Администратор"])
    for r in rows:
        u_tag = f"@{r[1]}" if r[1] else "Нет"
        writer.writerow([
            r[0], u_tag,
            f"{r[2]:.2f}".replace(".", ","),
            f"{r[3]:.2f}".replace(".", ","),
            f"{r[4]:.2f}".replace(".", ","),
            r[5] or "Нет",
            r[6] or "Неизвестно",
            "Да" if r[7] else "Нет",
            "Да" if r[8] else "Нет"
        ])
        
    csv_bytes = output.getvalue().encode("utf-8-sig")
    doc = BufferedInputFile(csv_bytes, filename=f"nemos_users_{get_now().strftime('%Y%m%d_%H%M')}.csv")
    
    await call.message.reply_document(
        document=doc,
        caption=(
            f"📑 <b>База пользователей Nemos Trade</b>\n"
            f"👥 <b>Всего записей:</b> <code>{len(rows)}</code>\n"
            f"📅 <i>{get_now().strftime('%d.%m.%Y %H:%M')} (МСК)</i>"
        ),
        parse_mode="HTML"
    )

@router.callback_query(F.data == "export_csv:txs")
async def cb_export_txs_csv(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    await call.answer("⏳ Формирую CSV транзакций...", show_alert=False)
    
    import csv, io
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, user_id, type, amount, status, date FROM transactions ORDER BY id DESC LIMIT 5000"
        ) as cur:
            rows = await cur.fetchall()
            
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ID транзакции", "ID пользователя", "Тип", "Сумма ($)", "Статус", "Дата"])
    for r in rows:
        writer.writerow([
            r[0], r[1], r[2],
            f"{r[3]:.2f}".replace(".", ","),
            r[4],
            r[5] or "Неизвестно"
        ])
        
    csv_bytes = output.getvalue().encode("utf-8-sig")
    doc = BufferedInputFile(csv_bytes, filename=f"nemos_transactions_{get_now().strftime('%Y%m%d_%H%M')}.csv")
    
    await call.message.reply_document(
        document=doc,
        caption=(
            f"💳 <b>История транзакций Nemos Trade</b>\n"
            f"📋 <b>Всего записей:</b> <code>{len(rows)}</code>\n"
            f"📅 <i>{get_now().strftime('%d.%m.%Y %H:%M')} (МСК)</i>"
        ),
        parse_mode="HTML"
    )

# --- СТАТИСТИКА, НАГРУЗКА И ДИНАМИКА 24Ч ---
@router.callback_query(F.data == "adm_btn_stats")
async def cb_adm_stats_fixed(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    st = await get_platform_stats_live()
    m_st = await get_mines_stats()

    m_sign = "🟢 +" if m_st["profit"] >= 0 else "🔴 "
    d_sign = "🟢 +" if st["d_profit"] >= 0 else "🔴 "
    total_platform_profit = round(m_st["profit"] + st["d_profit"] + st["pvp_profit"] + st["fees"], 2)
    tot_sign = "🟢 +" if total_platform_profit >= 0 else "🔴 "

    upd_time = get_now().strftime("%d.%m.%Y %H:%M:%S").replace("0", "O")

    t = (
        f"{EMOJIS['stats']} <b>Статистика платформы Nemos Trade</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего участников: <b>{fmt_o(st['cnt'])} чел.</b>\n"
        f"• Активных инвесторов: <b>{fmt_o(st['active_inv'])} чел.</b>\n\n"
        f"💼 <b>Капитал и балансы:</b>\n"
        f"• Общий пул депозитов: {format_bold_num(st['inv'])}\n"
        f"• Накоплено к выводу: {format_bold_num(st['earn'])}\n\n"
        f"📥 <b>Пополнения депозитов:</b>\n"
        f"• Всего внесено: {format_bold_num(st['dep_sum'])} (операций: {fmt_o(st['dep_cnt'])})\n\n"
        f"📤 <b>Вывод средств:</b>\n"
        f"• Выплачено игрокам: {format_bold_num(st['w_done_sum'])} (заявок: {fmt_o(st['w_done_cnt'])})\n"
        f"• Очередь на вывод: {format_bold_num(st['w_q_sum'])} (в обработке: {fmt_o(st['w_q_cnt'])})\n\n"
        f"🔄 <b>Реинвестирование:</b>\n"
        f"• Направлено в депозит: {format_bold_num(st['reinv_sum'])} (операций: {fmt_o(st['reinv_cnt'])})\n\n"
        f"💣 <b>Минёр (Mines 5х5):</b>\n"
        f"• Игр сыграно: <b>{fmt_o(m_st['count'])}</b>\n"
        f"• Оборот ставок: {format_bold_num(m_st['bets'])}\n"
        f"• Выплачено игрокам: {format_bold_num(m_st['wins'])}\n"
        f"• Прибыль кассы: <b>{m_sign}{format_bold_num(abs(m_st['profit']))}</b> (RTP {fmt_o(m_st['rtp'])}%)\n\n"
        f"🎲 <b>Кости против Бота (PvE x1.8):</b>\n"
        f"• Игр сыграно: <b>{fmt_o(st['d_cnt'])}</b>\n"
        f"• Прибыль кассы: <b>{d_sign}{format_bold_num(abs(st['d_profit']))}</b>\n\n"
        f"⚔️ <b>Кости PvP (Дуэли):</b>\n"
        f"• Дуэлей завершено: <b>{fmt_o(st['pvp_cnt'])}</b>\n"
        f"• Банк дуэлей: {format_bold_num(st['pvp_pot'])}\n"
        f"• Доход с комиссий (3.33%): <b>+{format_bold_num(st['pvp_profit'])}</b>\n\n"
        f"💸 <b>P2P Переводы:</b>\n"
        f"• Переводов выполнено: <b>{fmt_o(st['trans_cnt'])}</b>\n"
        f"• Оборот переводов: {format_bold_num(st['trans_vol'])}\n"
        f"• Доход с комиссий (2.22%): <b>+{format_bold_num(st['fees'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Чистый доход системы:</b> <b>{tot_sign}{format_bold_num(abs(total_platform_profit))}</b>\n"
        f"⏱ <b>Обновлено:</b> <code>{upd_time} МСК</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_btn_stats"), InlineKeyboardButton(text="◀️ Назад в меню", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(call, t, kb)
    await call.answer("⚡ Статистика обновлена!", show_alert=False)

@router.callback_query(F.data == "adm_btn_risk")
async def cb_adm_risk(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    st = await get_platform_stats_live()
    m_st = await get_mines_stats()
    inv, earn, pvp_profit, d_profit, fees = st["inv"], st["earn"], st["pvp_profit"], st["d_profit"], st["fees"]
    daily_obligations = round(inv * 0.03, 2)
    weekly_obligations = round(daily_obligations * 7, 2)
    game_profit_total = round(m_st["profit"] + d_profit + fees + pvp_profit, 2)
    coverage_pct = round((game_profit_total / daily_obligations * 100), 1) if daily_obligations > 0 else 100.0
    status_marker = "🟢 Стабильно (Профицит)" if game_profit_total >= daily_obligations else ("🟡 Умеренно (Баланс)" if game_profit_total >= daily_obligations * 0.5 else "🔴 Внимание (Дефицит)")

    t = (
        "⚖️ <b>ДАШБОРД ФИНАНСОВОЙ УСТОЙЧИВОСТИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Общий депозитный пул:</b> {format_bold_num(inv)}\n"
        f"💳 <b>Обязательства (+3%/сутки):</b> <b>+{format_bold_num(daily_obligations)} / день</b>\n"
        f"📅 <b>Нагрузка за 7 дней:</b> <b>+{format_bold_num(weekly_obligations)} / нед</b>\n\n"
        "🎰 <b>Фактический доход кассы:</b>\n"
        f"• 💣 Минёр: <b>+{format_bold_num(m_st['profit'])}</b>\n"
        f"• 🎲 Кости PvE: <b>+{format_bold_num(d_profit)}</b>\n"
        f"• ⚔️ Кости PvP (3.33%): <b>+{format_bold_num(pvp_profit)}</b>\n"
        f"• 💸 P2P Переводы (2.22%): <b>+{format_bold_num(fees)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Суммарный доход кассы:</b> <b>{format_bold_num(game_profit_total)}</b>\n"
        f"📊 <b>Покрытие суточных выплат:</b> <b>{fmt_o(coverage_pct)}%</b>\n"
        f"🛡 <b>Статус устойчивости:</b> <b>{status_marker}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Положительный баланс означает полную финансовую автономность проекта.</i>"
    )
    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await call.answer()

@router.callback_query(F.data == "adm_btn_today")
async def cb_adm_today(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    iso_24h = (get_now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    async with get_db_connection() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE registered_at >= ?", (iso_24h,))
        new_users = (await cur.fetchone())[0]

        cur = await db.execute("""SELECT SUM(amount) FROM transactions 
           WHERE date >= ? 
           AND (type LIKE '%депозит%' OR type LIKE '%Депозит%' OR type LIKE '%пополнение%' OR type LIKE '%Пополнение%') 
           AND status = 'Зачислено'""", (iso_24h,))
        dep_row = await cur.fetchone()
        dep_24h = round(dep_row[0], 2) if dep_row and dep_row[0] else 0.0

        cur = await db.execute("""SELECT SUM(amount) FROM transactions 
           WHERE date >= ? 
           AND (type LIKE '%прибыли%' OR type LIKE '%Прибыли%') 
           AND status = 'Зачислено'""", (iso_24h,))
        earn_row = await cur.fetchone()
        earn_24h = round(earn_row[0], 2) if earn_row and earn_row[0] else 0.0

        cur = await db.execute("""SELECT SUM(amount) FROM transactions 
           WHERE date >= ? 
           AND (type LIKE '%выплата%' OR type LIKE '%Выплата%') 
           AND status = 'Одобрено'""", (iso_24h,))
        pay_row = await cur.fetchone()
        pay_24h = round(pay_row[0], 2) if pay_row and pay_row[0] else 0.0

    t = (
        f"📈 <b>ДИНАМИКА ПЛАТФОРМЫ ЗА ПОСЛЕДНИЕ 24 ЧАСА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Новых регистраций:</b> <b>+{new_users} чел.</b>\n\n"
        f"📥 <b>Внесено в депозиты:</b> <b>+{format_bold_num(dep_24h)}</b>\n\n"
        f"💰 <b>Начислено баланса:</b> <b>+{format_bold_num(earn_24h)}</b>\n\n"
        f"📤 <b>Одобрено выплат:</b> <b>-{format_bold_num(pay_24h)}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await call.answer()

# --- ОЧЕРЕДЬ ВЫПЛАТ (СПИСОК И ДЕТАЛЬНЫЕ КАРТОЧКИ) ---
@router.callback_query(F.data.startswith("adm_btn_queue"))
async def cb_adm_queue(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    if state is not None:
        await state.clear()

    parts = call.data.split(":")
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    limit = 6
    offset = page * limit

    async with get_db_connection() as db:
        cur = await db.execute("SELECT COUNT(*) FROM transactions WHERE type = '📤 Вывод' AND status IN ('В обработке', 'В работе')")
        total_pending = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT id, user_id, amount, date, proof_text, status FROM transactions WHERE type = '📤 Вывод' AND status IN ('В обработке', 'В работе') ORDER BY id ASC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        txs = await cur.fetchall()

    if not txs:
        t = "⏳ <b>Очередь выплат пуста!</b>\n━━━━━━━━━━━━━━━━━━\nВсе заявки на вывод успешно обработаны."
        await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
        return await call.answer()

    t = f"⏳ <b>ОЧЕРЕДЬ ЗАЯВОК НА ВЫВОД (Всего: {total_pending})</b>\n━━━━━━━━━━━━━━━━━━\n<i>Нажмите на заявку для просмотра деталей:</i>\n\n"
    kb = []
    for item in txs:
        tx_id, uid, amt, tx_date, proof_txt = item[0], item[1], item[2], item[3], item[4]
        tx_status = item[5] if len(item) > 5 else "В обработке"
        disp_date = format_datetime_display(tx_date)
        nick_hint = f" ({proof_txt})" if proof_txt else ""
        status_tag = "🚗 [В РАБОТЕ]" if tx_status == "В работе" else "⏳ [В ОЧЕРЕДИ]"
        t += f"• <b>Заявка №{tx_id} {status_tag}:</b> ID <code>{uid}</code> — {format_bold_num(amt)} (<i>{disp_date}</i>)\n"
        kb.append([
            InlineKeyboardButton(text=f"{status_tag} #{tx_id} | {fmt_o(amt)}$", callback_data=f"adm_tx_card:{tx_id}:{page}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_btn_queue:{page-1}"))
    if offset + limit < total_pending:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"adm_btn_queue:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")])
    
    await send_or_edit_panel_message(call, t, InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

@router.callback_query(F.data.startswith("adm_tx_card:"))
async def cb_adm_tx_card(call: types.CallbackQuery, state: FSMContext):
    """Детальная карточка заявки с фото, ником и кнопками модерации."""
    if not await is_admin(call.from_user.id): return
    if state is not None:
        await state.clear()

    _, tx_id_str, page_str = call.data.split(":")
    tx_id = int(tx_id_str)
    page = int(page_str)

    tx_row = await get_tx_by_id(tx_id)
    if not tx_row or tx_row[5] not in ["В обработке", "В работе"]:
        await call.answer("⚠️ Заявка уже обработана или не существует!", show_alert=True)
        call.data = f"adm_btn_queue:{page}"
        return await cb_adm_queue(call, state)

    _, uid, _, amt, tx_date, status, _, _, proof_text, proof_photo, *rest = tx_row
    u = await get_and_update_user(uid)
    drom_price = round(amt + OKA_GOS_PRICE, 2)
    disp_date = format_datetime_display(tx_date)
    uname_str = f"@{u['username']}" if u.get('username') else "не указан"

    cap = (
        f"📋 <b>КАРТОЧКА ЗАЯВКИ НА ВЫВОД №{tx_id}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Игрок:</b> ID <code>{uid}</code> ({uname_str})\n"
        f"🏷 <b>Указанный ник / данные:</b> <code>{html.escape(proof_text or 'Не указан')}</code>\n\n"
        f"💵 <b>Сумма к выплате:</b> {format_bold_num(amt)}\n"
        f"🚗 <b>Цена авто на Drom (+Ока):</b> {format_bold_num(drom_price)}\n"
        f"📅 <b>Дата создания:</b> <i>{disp_date}</i>\n"
        f"📊 <b>Статус:</b> <b>{status}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    curator_id = tx_row[10] if len(tx_row) > 10 else None
    if status == "В работе":
        if curator_id == call.from_user.id or call.from_user.id == OWNER_ID:
            action_rows = [
                [InlineKeyboardButton(text=f"{EMOJIS['check']} Одобрить выплату", callback_data=f"pay_ask:{tx_id}:{uid}:{amt}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ask_rej_menu:{tx_id}:{uid}:{amt}")],
                [InlineKeyboardButton(text="↩️ Снять с работы", callback_data=f"adm_untake_work:{tx_id}:{uid}:{amt}")]
            ]
        else:
            action_rows = [
                [InlineKeyboardButton(text="🔒 В работе у другого куратора", callback_data="noop")]
            ]
    else:
        action_rows = [
            [InlineKeyboardButton(text="🚗 Взять в работу", callback_data=f"adm_take_work:{tx_id}:{uid}:{amt}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ask_rej_menu:{tx_id}:{uid}:{amt}")]
        ]
    kb = InlineKeyboardMarkup(inline_keyboard=action_rows + [
        [InlineKeyboardButton(text="👤 Профиль игрока", callback_data=f"adm_user_card:{uid}")],
        [InlineKeyboardButton(text="🔙 Назад в очередь", callback_data=f"adm_btn_queue:{page}")]
    ])

    try:
        if proof_photo:
            await call.message.delete()
            await call.message.answer_photo(photo=proof_photo, caption=cap, reply_markup=kb, parse_mode="HTML")
        else:
            await send_or_edit_panel_message(call, cap, kb)
    except Exception:
        await call.message.answer(cap, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("pay_ask:"))
async def pay_ask_callback(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    _, tx_id, tid, amt = call.data.split(":")
    tx_data = await get_tx_by_id(int(tx_id))
    if tx_data and tx_data[5] == "В работе":
        curator_id = tx_data[10] if len(tx_data) > 10 else None
        if curator_id and curator_id != call.from_user.id and call.from_user.id != OWNER_ID:
            return await call.answer("⛔ Заявка находится в работе у другого куратора!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚠️ Подтвердить выплату {fmt_o(float(amt))}$?", callback_data=f"pay_yes:{tx_id}:{tid}:{amt}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_tx_card:{tx_id}:0" if call.message.chat.type == "private" else "adm_btn_close")]
    ])
    try:
        await call.message.edit_reply_markup(reply_markup=kb)
    except Exception: pass
    await call.answer()

@router.callback_query(F.data.startswith("pay_yes:"))
async def process_pay_yes(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    parts = call.data.split(":")
    tx_id = int(parts[1])
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"

    tx_data = await get_tx_by_id(tx_id)
    if not tx_data:
        return await call.answer("⚠️ Заявка не найдена в базе!", show_alert=True)
    # Считываем достоверные user_id и amount напрямую из базы данных
    tid = int(tx_data[1])
    amt = float(tx_data[3])
    if not tx_data or tx_data[5] not in ["В обработке", "В работе"]:
        return await call.answer("⚠️ Заявка уже была обработана ранее!", show_alert=True)
    if tx_data[5] == "В работе":
        curator_id = tx_data[10] if len(tx_data) > 10 else None
        if curator_id and curator_id != call.from_user.id and call.from_user.id != OWNER_ID:
            return await call.answer("⛔ Эту заявку взял другой куратор! Только он может завершить выплату.", show_alert=True)

    admin_chat_id, admin_msg_id = tx_data[6], tx_data[7]

    async with get_db_connection() as db:
        cur = await db.execute("UPDATE transactions SET status = 'Одобрено', type = '📤 Выплата' WHERE id = ? AND status IN ('В обработке', 'В работе')", (tx_id,))
        await db.commit()
        if cur.rowcount == 0:
            return await call.answer("⚠️ Заявка уже была обработана ранее!", show_alert=True)

    await log_admin_action(call.from_user.id, adm_tag, "Одобрение вывода", tid, f"Заявка №{tx_id} на {amt}$")
    
    # Синхронизация статуса в группе выплат
    if admin_chat_id and admin_msg_id:
        sync_text = (
            f"✅ <b>ВЫПЛАЧЕНО №{tx_id}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Игрок:</b> ID <code>{tid}</code>\n"
            f"💰 <b>Сумма:</b> {format_bold_num(amt)}\n"
            f"🛡 <b>Одобрил:</b> {adm_tag}"
        )
        try:
            await bot.edit_message_caption(chat_id=admin_chat_id, message_id=admin_msg_id, caption=sync_text, reply_markup=None, parse_mode="HTML")
        except Exception:
            try:
                await bot.edit_message_text(chat_id=admin_chat_id, message_id=admin_msg_id, text=sync_text, reply_markup=None, parse_mode="HTML")
            except Exception: pass

    try:
        if call.message.photo:
            await call.message.edit_caption(caption=f"✅ <b>Заявка №{tx_id} на {format_bold_num(amt)} (ID {tid}) успешно одобрена!</b>", reply_markup=get_back_to_panel_kb() if call.message.chat.type == "private" else None, parse_mode="HTML")
        else:
            await call.message.edit_text(f"✅ <b>Заявка №{tx_id} на {format_bold_num(amt)} (ID {tid}) успешно одобрена!</b>", reply_markup=get_back_to_panel_kb() if call.message.chat.type == "private" else None, parse_mode="HTML")
    except Exception:
        await call.message.reply(f"✅ <b>Заявка №{tx_id} на {format_bold_num(amt)} (ID {tid}) успешно одобрена!</b>", parse_mode="HTML")

    try:
        from services.tasks import generate_receipt_image
        from aiogram.types import BufferedInputFile

        tid_str = str(tid)
        masked_id = f"ID {tid_str[:3]}•••{tid_str[-3:]}" if len(tid_str) >= 6 else f"ID {tid_str}"
        disp_date = get_now().strftime('%d.%m.%Y %H:%M').replace('0', 'O')
        amt_display = f"+{format_raw_num(amt)}"
        
        receipt_bytes = generate_receipt_image(
            op_type="ВЫПЛАТА ЧИСТОЙ ПРИБЫЛИ",
            amount_str=amt_display,
            client_str=masked_id,
            date_str=disp_date,
            method_str="Drom (Покупка авто Ока)",
            tx_num=f"TX-{tx_id}"
        )
        photo_file = BufferedInputFile(receipt_bytes, filename=f"receipt_{tx_id}.png")
        await bot.send_photo(
            chat_id=tid,
            photo=photo_file,
            caption=f"{EMOJIS['sparkles']} <b>Ваша выплата одобрена!</b>\n\n💵 <b>Сумма:</b> {format_bold_num(amt)} <b>успешно выплачена на ваш аккаунт.</b>",
            parse_mode="HTML",
            message_effect_id=EFFECT_CONFETTI_ID
        )
    except Exception:
        try:
            await bot.send_message(tid, f"{EMOJIS['sparkles']} <b>Ваша выплата одобрена!</b>\n\n💵 <b>Сумма:</b> {format_bold_num(amt)} <b>успешно выплачена на ваш аккаунт.</b>", parse_mode="HTML", message_effect_id=EFFECT_CONFETTI_ID)
        except Exception: pass

    try:
        from services.tasks import generate_receipt_image
        from aiogram.types import BufferedInputFile

        tid_str = str(tid)
        masked_id = f"ID {tid_str[:3]}•••{tid_str[-3:]}" if len(tid_str) >= 6 else f"ID {tid_str}"
        disp_date = get_now().strftime('%d.%m.%Y %H:%M').replace('0', 'O')
        receipt_bytes = generate_receipt_image(
            op_type="ВЫПЛАТА ЧИСТОЙ ПРИБЫЛИ",
            amount_str=f"+{format_raw_num(amt)}",
            client_str=masked_id,
            date_str=disp_date,
            method_str="Drom (Покупка авто Ока)",
            tx_num=f"TX-{tx_id}"
        )
        channel_photo = BufferedInputFile(receipt_bytes, filename=f"review_{tx_id}.png")
        await bot.send_photo(
            chat_id=REVIEWS_CHANNEL,
            photo=channel_photo,
            caption=f"💸 <b>УСПЕШНАЯ ВЫПЛАТА</b> 💸\n━━━━━━━━━━━━━━━━━━\n👤 <b>Аккаунт:</b> <b>{masked_id}</b>\n💰 <b>Сумма:</b> {format_bold_num(amt)}\n🚗 <b>Способ:</b> <b>Drom</b>\n📅 <b>Дата:</b> <i>{disp_date} (МСК)</i>\n━━━━━━━━━━━━━━━━━━\n🚀 <b>Начни приумножать капитал:</b> @NemosTrade",
            parse_mode="HTML"
        )
    except Exception: pass

    await call.answer("Заявка одобрена!")

@router.callback_query(F.data.startswith("ask_rej_menu:"))
async def ask_rej_menu_callback(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    _, tx_id, tid, amt = call.data.split(":")
    tx_data = await get_tx_by_id(int(tx_id))
    if tx_data and tx_data[5] == "В работе":
        curator_id = tx_data[10] if len(tx_data) > 10 else None
        if curator_id and curator_id != call.from_user.id and call.from_user.id != OWNER_ID:
            return await call.answer("⛔ Заявка находится в работе у другого куратора!", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Авто не на Drom", callback_data=f"do_rej:{tx_id}:{tid}:{amt}:1")],
        [InlineKeyboardButton(text="🏷 Неверная цена", callback_data=f"do_rej:{tx_id}:{tid}:{amt}:2")],
        [InlineKeyboardButton(text="👤 Не найден ник", callback_data=f"do_rej:{tx_id}:{tid}:{amt}:3")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_tx_card:{tx_id}:0" if call.message.chat.type == "private" else "adm_btn_close")]
    ])
    try: await call.message.edit_reply_markup(reply_markup=kb)
    except Exception: pass
    await call.answer()

@router.callback_query(F.data.startswith("do_rej:"))
async def do_rej_callback(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    parts = call.data.split(":")
    tx_id = int(parts[1])
    r_code = parts[4] if len(parts) > 4 else "0"
    reason = {"1": "Автомобиль не найден на Drom.", "2": "Указана неверная стоимость выставления на Drom.", "3": "Не найден указанный игровой никнейм."}.get(r_code, "Технические несоответствия заявки.")
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"

    tx_data = await get_tx_by_id(tx_id)
    if not tx_data or tx_data[5] not in ["В обработке", "В работе"]:
        return await call.answer("⚠️ Заявка уже обработана!", show_alert=True)
    if tx_data[5] == "В работе":
        curator_id = tx_data[10] if len(tx_data) > 10 else None
        if curator_id and curator_id != call.from_user.id and call.from_user.id != OWNER_ID:
            return await call.answer("⛔ Эту заявку взял другой куратор! Только он может отклонить её.", show_alert=True)

    # Исключаем подмену параметров из callback_data: считываем реальные user_id и amount из базы данных
    tid = int(tx_data[1])
    amt = float(tx_data[3])
    admin_chat_id, admin_msg_id = tx_data[6], tx_data[7]

    now_iso = get_now_iso()
    # Единая неделимая транзакция: смена статуса + возврат средств пользователю + запись транзакции возврата
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE transactions SET status = 'Отклонено' WHERE id = ? AND status IN ('В обработке', 'В работе')",
                (tx_id,)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return await call.answer("⚠️ Заявка уже обработана!", show_alert=True)

            await db.execute(
                "UPDATE users SET earned = ROUND(earned + ?, 2), last_accrual = ? WHERE user_id = ?",
                (amt, now_iso, tid)
            )
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '↩️ Возврат вывода', ?, ?, 'Отклонено')",
                (tid, amt, now_iso)
            )
            await db.commit()
        except Exception:
            await db.execute("ROLLBACK")
            return await call.answer("❌ Ошибка базы данных при отклонении заявки!", show_alert=True)

    await log_admin_action(call.from_user.id, adm_tag, "Отклонение вывода", tid, f"Заявка №{tx_id} | Причина: {reason}")

    if admin_chat_id and admin_msg_id:
        sync_text = (
            f"❌ <b>ОТКЛОНЕНО №{tx_id}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Игрок:</b> ID <code>{tid}</code>\n"
            f"💰 <b>Сумма:</b> {format_bold_num(amt)}\n"
            f"📝 <b>Причина:</b> {reason}\n"
            f"🛡 <b>Отклонил:</b> {adm_tag}"
        )
        try:
            await bot.edit_message_caption(chat_id=admin_chat_id, message_id=admin_msg_id, caption=sync_text, reply_markup=None, parse_mode="HTML")
        except Exception:
            try:
                await bot.edit_message_text(chat_id=admin_chat_id, message_id=admin_msg_id, text=sync_text, reply_markup=None, parse_mode="HTML")
            except Exception: pass

    try:
        if call.message.photo:
            await call.message.edit_caption(caption=f"❌ <b>Заявка №{tx_id} отклонена. Средства {format_bold_num(amt)} возвращены игроку ID {tid}.</b>", reply_markup=get_back_to_panel_kb() if call.message.chat.type == "private" else None, parse_mode="HTML")
        else:
            await call.message.edit_text(f"❌ <b>Заявка №{tx_id} отклонена. Средства {format_bold_num(amt)} возвращены игроку ID {tid}.</b>", reply_markup=get_back_to_panel_kb() if call.message.chat.type == "private" else None, parse_mode="HTML")
    except Exception:
        await call.message.reply(f"❌ <b>Заявка №{tx_id} отклонена. Средства возвращены игроку ID {tid}.</b>", parse_mode="HTML")

    try:
        await bot.send_message(tid, f"{EMOJIS['warn']} <b>Ваша заявка на вывод отклонена:</b>\n\n📝 <b>Причина:</b> {reason}\n💵 <b>Сумма {format_bold_num(amt)} возвращена на баланс!</b>", parse_mode="HTML")
    except Exception: pass
    await call.answer("Заявка отклонена")

# --- КАРТОЧКА И ПОИСК ИГРОКА ---
async def render_user_card(target_uid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = await get_and_update_user(target_uid)
    uname = f"@{u['username']}" if u.get('username') else "не указан"
    ban_text = "🔴 Заблокирован" if u.get('is_banned') else "🟢 Активен"
    adm_text = "👑 Администратор" if u.get('is_admin') else "👤 Пользователь"

    t = (
        f"👤 <b>КАРТОЧКА ИГРОКА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID:</b> <code>{target_uid}</code>\n"
        f"🏷 <b>Username:</b> {uname}\n"
        f"🛡 <b>Статус:</b> <b>{adm_text}</b>\n"
        f"🚫 <b>Блокировка:</b> <b>{ban_text}</b>\n\n"
        f"💼 <b>Депозит (в обороте):</b> {format_bold_num(u['invested'])}\n"
        f"💳 <b>Чистая прибыль:</b> {format_bold_num(u['earned'])}\n"
        f"👥 <b>Пригласил (ID):</b> <code>{u.get('referrer_id') or 'нет'}</code>\n"
        f"📅 <b>Дата регистрации:</b> <i>{format_datetime_display(u.get('registered_at'))}</i>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    ban_btn_text = "🍏 Разбанить" if u.get('is_banned') else "⛔ Забанить"
    ban_cb = f"do_unban:{target_uid}" if u.get('is_banned') else f"ask_ban:{target_uid}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Депозит", callback_data=f"quick_add_inv:{target_uid}"), InlineKeyboardButton(text="➖ Депозит", callback_data=f"quick_sub_inv:{target_uid}")],
        [InlineKeyboardButton(text="➕ Прибыль", callback_data=f"quick_add_earn:{target_uid}"), InlineKeyboardButton(text="➖ Прибыль", callback_data=f"quick_sub_earn:{target_uid}")],
        [InlineKeyboardButton(text="📜 История транзакций", callback_data=f"adm_user_txs:{target_uid}")],
        [InlineKeyboardButton(text=ban_btn_text, callback_data=ban_cb), InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back_panel")]
    ])
    return t, kb

@router.callback_query(F.data == "adm_btn_find_user")
async def cb_adm_find_user(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await send_or_edit_panel_message(call, "🔍 <b>Введите ID игрока или его @username для поиска:</b>", get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_find_user)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_find_user)
async def state_find_user_process(msg: types.Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        if state is not None: await state.clear()
        return
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    u_found = await find_user_by_query(msg.text)
    if not u_found:
        return await msg.answer("❌ <b>Игрок не найден в базе данных.</b> Попробуйте снова или нажмите отмену:", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    target_id = u_found.get("user_id")

    if state is not None:
        await state.clear()
    t, kb = await render_user_card(target_id)
    await msg.answer(t, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("adm_user_card:"))
async def cb_user_card(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    if state is not None:
        await state.clear()
    target_uid = int(call.data.split(":")[1])
    t, kb = await render_user_card(target_uid)
    await send_or_edit_panel_message(call, t, kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_user_txs:"))
async def cb_user_txs(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    target_uid = int(call.data.split(":")[1])
    async with get_db_connection() as db:
        cur = await db.execute("SELECT type, amount, date, status FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 10", (target_uid,))
        txs = await cur.fetchall()

    if not txs:
        t = f"📜 <b>История транзакций игрока ID <code>{target_uid}</code> пуста.</b>"
    else:
        t = f"📜 <b>ИСТОРИЯ ОПЕРАЦИЙ (ID: <code>{target_uid}</code>)</b>\n━━━━━━━━━━━━━━━━━━\n"
        for item in txs:
            disp_date = format_datetime_display(item[2])
            t += f"• <b>{html.escape(str(item[0]))}:</b> {format_bold_num(item[1])}\n  📅 <i>{disp_date}</i> | <b>Статус:</b> <b>{html.escape(str(item[3]))}</b>\n\n"
        t += "━━━━━━━━━━━━━━━━━━"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Назад к карточке", callback_data=f"adm_user_card:{target_uid}")]])
    await send_or_edit_panel_message(call, t, kb)
    await call.answer()

# --- СПИСОК ВСЕХ ПОЛЬЗОВАТЕЛЕЙ ---
@router.callback_query(F.data.startswith("adm_btn_users_page:"))
async def cb_users_page(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    page = int(call.data.split(":")[1])
    limit = 8
    offset = page * limit

    async with get_db_connection() as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cur.fetchone())[0]

        cur = await db.execute("SELECT user_id, username, invested, earned FROM users ORDER BY invested DESC LIMIT ? OFFSET ?", (limit, offset))
        users = await cur.fetchall()

    t = f"👥 <b>СПИСОК ПОЛЬЗОВАТЕЛЕЙ (Стр. {page+1}, всего: {total})</b>\n━━━━━━━━━━━━━━━━━━\n"
    kb = []
    for uid, uname, inv, earn in users:
        u_tag = f"@{uname}" if uname else f"ID {uid}"
        kb.append([InlineKeyboardButton(text=f"{u_tag} | Д: {fmt_o(inv)}$ | П: {fmt_o(earn)}$", callback_data=f"adm_user_card:{uid}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_btn_users_page:{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"adm_btn_users_page:{page+1}"))
    if nav: kb.append(nav)

    kb.append([InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back_panel")])
    await send_or_edit_panel_message(call, t, InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

# --- БЫСТРЫЕ КНОПКИ В КАРТОЧКЕ ИГРОКА ---
@router.callback_query(F.data.startswith("quick_"))
async def cb_quick_balance_action(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    action, tid_str = call.data.split(":")
    tid = int(tid_str)
    if state is not None:
        await state.clear()
        await state.update_data(target_quick_uid=tid)

    prompts = {
        "quick_add_inv": ("📥 <b>ПОПОЛНЕНИЕ ДЕПОЗИТА</b>\n"
                          f"Игрок: ID <code>{tid}</code>\n"
                          "Введите сумму для пополнения (например: <code>1kk</code>, <code>5OOk</code>):", AdminPanelState.waiting_add_inv),
        "quick_sub_inv": ("📉 <b>СПИСАНИЕ ДЕПОЗИТА</b>\n"
                          f"Игрок: ID <code>{tid}</code>\n"
                          "Введите сумму для списания (например: <code>1kk</code>, <code>5OOk</code>):", AdminPanelState.waiting_sub_inv),
        "quick_add_earn": ("💰 <b>НАЧИСЛЕНИЕ ПРИБЫЛИ</b>\n"
                           f"Игрок: ID <code>{tid}</code>\n"
                           "Введите сумму для начисления (например: <code>1kk</code>, <code>25Ok</code>):", AdminPanelState.waiting_add_earn),
        "quick_sub_earn": ("📉 <b>СПИСАНИЕ ПРИБЫЛИ</b>\n"
                           f"Игрок: ID <code>{tid}</code>\n"
                           "Введите сумму для списания (например: <code>1kk</code>, <code>1OOk</code>):", AdminPanelState.waiting_sub_earn),
    }

    text, target_state = prompts.get(action, ("", None))
    if target_state:
        await send_or_edit_panel_message(call, text, get_back_to_user_card_kb(tid))
        await state.set_state(target_state)
    await call.answer()

# --- БАЛАНСОВЫЕ ОПЕРАЦИИ (ОБЩЕЕ МЕНЮ И КАРТОЧКА С ДВОЙНЫМ ВВОДОМ) ---
@router.callback_query(F.data.in_(["adm_btn_add_inv", "adm_btn_sub_inv", "adm_btn_add_earn", "adm_btn_sub_earn"]))
async def cb_balance_action_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    if state is not None:
        await state.clear()
    action = call.data
    prompts = {
        "adm_btn_add_inv": ("📥 <b>ПОПОЛНЕНИЕ ДЕПОЗИТА</b>\nВведите: <code>ID_ИГРОКА СУММА</code> (например: <code>123456789 1kk</code>):", AdminPanelState.waiting_add_inv),
        "adm_btn_sub_inv": ("📉 <b>СПИСАНИЕ ДЕПОЗИТА</b>\nВведите: <code>ID_ИГРОКА СУММА</code> (например: <code>123456789 5OOk</code>):", AdminPanelState.waiting_sub_inv),
        "adm_btn_add_earn": ("💰 <b>НАЧИСЛЕНИЕ ПРИБЫЛИ</b>\nВведите: <code>ID_ИГРОКА СУММА</code> (например: <code>123456789 25Ok</code>):", AdminPanelState.waiting_add_earn),
        "adm_btn_sub_earn": ("📉 <b>СПИСАНИЕ БАЛАНСА ПРИБЫЛИ</b>\nВведите: <code>ID_ИГРОКА СУММА</code> (например: <code>123456789 1OOk</code>):", AdminPanelState.waiting_sub_earn),
    }
    text, target_state = prompts[action]
    await send_or_edit_panel_message(call, text, get_back_to_panel_kb())
    await state.set_state(target_state)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_add_inv)
@router.message(F.chat.type == "private", AdminPanelState.waiting_sub_inv)
@router.message(F.chat.type == "private", AdminPanelState.waiting_add_earn)
@router.message(F.chat.type == "private", AdminPanelState.waiting_sub_earn)
async def state_balance_process(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return
    if not await is_admin(msg.from_user.id):
        return

    current_state = await state.get_state()
    data = await state.get_data() if state is not None else {}
    quick_uid = data.get("target_quick_uid")
    adm_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"ID {msg.from_user.id}"

    parts = msg.text.strip().split()
    target_uid = None
    amt = None
    try:
        if len(parts) >= 2:
            raw_target = parts[0].strip().replace("@", "")
            if raw_target.isdigit():
                target_uid = int(raw_target)
            else:
                async with get_db_connection() as db:
                    async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw_target,)) as cur:
                        row = await cur.fetchone()
                        if row: target_uid = row[0]
            u_t = await get_and_update_user(target_uid)
            max_p = u_t['invested'] if 'inv' in current_state else u_t['earned']
            amt = parse_amount(parts[1], max_val=max_p)
        elif quick_uid and len(parts) == 1:
            target_uid = int(quick_uid)
            u_t = await get_and_update_user(target_uid)
            max_p = u_t['invested'] if 'inv' in current_state else u_t['earned']
            amt = parse_amount(parts[0], max_val=max_p)
        else:
            raise ValueError
        if not target_uid or amt is None or amt <= 0:
            raise ValueError
    except Exception:
        hint = "Введите сумму (например: <code>5OOk</code>):" if quick_uid else "Введите: <code>ID_или_НИК СУММА</code> (например: <code>123456789 5OOk</code> или <code>@username 5OOk</code>):"
        return await msg.answer(f"❌ <b>Неверный формат или игрок не найден!</b>\n{hint}", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    u = await get_and_update_user(target_uid)

    if current_state == AdminPanelState.waiting_add_inv.state:
        await adjust_user_balance_atomic(target_uid, inv_delta=amt)
        await add_tx(target_uid, "📥 Пополнение депозита", amt, "Зачислено")
        await log_admin_action(msg.from_user.id, adm_tag, "Пополнение депозита", target_uid, f"+{amt}$")
        from database.database import process_referral_reward
        await process_referral_reward(target_uid, bot)
        try:
            await bot.send_message(
                target_uid,
                f"{EMOJIS['sparkles']} <b>Ваш депозит пополнен!</b>\n\n💰 <b>Сумма:</b> <b>+{format_bold_num(amt)}</b>\n📈 <i>Доход +3% начисляется каждую секунду!</i>",
                parse_mode="HTML",
                message_effect_id=EFFECT_CONFETTI_ID
            )
        except Exception:
            pass
        back_kb = get_back_to_user_card_kb(target_uid) if quick_uid else get_back_to_panel_kb()
        await msg.answer(f"✅ <b>Депозит игрока ID <code>{target_uid}</code> успешно увеличен на {format_bold_num(amt)}!</b>", reply_markup=back_kb, parse_mode="HTML")

    elif current_state == AdminPanelState.waiting_sub_inv.state:
        await adjust_user_balance_atomic(target_uid, inv_delta=-amt)
        await add_tx(target_uid, "📉 Списание депозита", amt, "Списано")
        await log_admin_action(msg.from_user.id, adm_tag, "Списание депозита", target_uid, f"-{amt}$")
        back_kb = get_back_to_user_card_kb(target_uid) if quick_uid else get_back_to_panel_kb()
        await msg.answer(f"✅ <b>С депозита игрока ID <code>{target_uid}</code> списано {format_bold_num(amt)}!</b>", reply_markup=back_kb, parse_mode="HTML")

    elif current_state == AdminPanelState.waiting_add_earn.state:
        await adjust_user_balance_atomic(target_uid, earn_delta=amt)
        await add_tx(target_uid, "💰 Начисление прибыли", amt, "Зачислено")
        await log_admin_action(msg.from_user.id, adm_tag, "Начисление прибыли", target_uid, f"+{amt}$")
        try: await bot.send_message(target_uid, f"{EMOJIS['sparkles']} <b>Вам начислена прибыль!</b>\n\n💳 <b>Сумма:</b> <b>+{format_bold_num(amt)}</b> (доступно к выводу)", parse_mode="HTML")
        except Exception: pass
        back_kb = get_back_to_user_card_kb(target_uid) if quick_uid else get_back_to_panel_kb()
        await msg.answer(f"✅ <b>Баланс прибыли игрока ID <code>{target_uid}</code> пополнен на {format_bold_num(amt)}!</b>", reply_markup=back_kb, parse_mode="HTML")

    elif current_state == AdminPanelState.waiting_sub_earn.state:
        await adjust_user_balance_atomic(target_uid, earn_delta=-amt)
        await add_tx(target_uid, "📉 Списание прибыли", amt, "Списано")
        await log_admin_action(msg.from_user.id, adm_tag, "Списание прибыли", target_uid, f"-{amt}$")
        back_kb = get_back_to_user_card_kb(target_uid) if quick_uid else get_back_to_panel_kb()
        await msg.answer(f"✅ <b>С баланса прибыли игрока ID <code>{target_uid}</code> списано {format_bold_num(amt)}!</b>", reply_markup=back_kb, parse_mode="HTML")

    if state is not None:
        await state.clear()

# --- БАНЫ И РАЗБАНЫ ---
@router.callback_query(F.data == "adm_btn_banlist")
async def cb_banlist(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    async with get_db_connection() as db:
        cur = await db.execute("SELECT user_id, username FROM users WHERE is_banned = 1")
        banned = await cur.fetchall()

    if not banned:
        t = "🚫 <b>Список заблокированных игроков пуст!</b>"
    else:
        t = f"🚫 <b>ЗАБЛОКИРОВАННЫЕ ПОЛЬЗОВАТЕЛИ ({len(banned)} чел.)</b>\n━━━━━━━━━━━━━━━━━━\n"
        for uid, uname in banned:
            tag = f"@{uname}" if uname else "нет"
            t += f"• ID <code>{uid}</code> ({tag})\n"
        t += "━━━━━━━━━━━━━━━━━━"

    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await call.answer()

@router.callback_query(F.data == "adm_btn_ban_user")
async def cb_ban_user_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await send_or_edit_panel_message(call, "⛔ <b>Введите ID или @username игрока для блокировки:</b>", get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_ban_user)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_ban_user)
async def state_ban_user(msg: types.Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        if state is not None: await state.clear()
        return
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    adm_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"ID {msg.from_user.id}"
    raw = msg.text.strip().replace("@", "")
    tid = None
    if raw.isdigit():
        tid = int(raw)
    else:
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw,)) as cur:
                row = await cur.fetchone()
                if row: tid = row[0]

    if not tid:
        return await msg.answer("❌ <b>Пользователь не найден в базе!</b> Введите ID или @username игрока:", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    if tid == OWNER_ID:
        return await msg.answer("⛔ <b>Нельзя заблокировать главного владельца проекта!</b>", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    u = await get_and_update_user(tid)
    tag = f" (@{u['username']})" if u.get('username') else ""
    await set_user_ban_status(tid, 1)
    await log_admin_action(msg.from_user.id, adm_tag, "Блокировка пользователя", tid)
    await msg.answer(f"⛔ <b>Игрок ID <code>{tid}</code>{tag} успешно заблокирован!</b>", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    if state is not None:
        await state.clear()

@router.callback_query(F.data == "adm_btn_unban_user")
async def cb_unban_user_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await send_or_edit_panel_message(call, "🍏 <b>Введите ID или @username игрока для разблокировки:</b>", get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_unban_user)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_unban_user)
async def state_unban_user(msg: types.Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        if state is not None: await state.clear()
        return
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    adm_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"ID {msg.from_user.id}"
    raw = msg.text.strip().replace("@", "")
    tid = None
    if raw.isdigit():
        tid = int(raw)
    else:
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw,)) as cur:
                row = await cur.fetchone()
                if row: tid = row[0]

    if not tid:
        return await msg.answer("❌ <b>Пользователь не найден в базе!</b> Введите ID или @username игрока:", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    u = await get_and_update_user(tid)
    tag = f" (@{u['username']})" if u.get('username') else ""
    await set_user_ban_status(tid, 0)
    await log_admin_action(msg.from_user.id, adm_tag, "Разблокировка пользователя", tid)
    await msg.answer(f"🍏 <b>Игрок ID <code>{tid}</code>{tag} успешно разблокирован!</b>", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    if state is not None:
        await state.clear()

@router.callback_query(F.data.startswith("ask_ban:"))
async def cb_ask_ban_card(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    tid = int(call.data.split(":")[1])
    if tid == OWNER_ID:
        return await call.answer("⛔ Нельзя заблокировать главного владельца!", show_alert=True)
    await set_user_ban_status(tid, 1)
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    await log_admin_action(call.from_user.id, adm_tag, "Блокировка из карточки", tid)
    await call.answer(f"⛔ Игрок {tid} заблокирован!", show_alert=True)
    t, kb = await render_user_card(tid)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass

@router.callback_query(F.data.startswith("do_unban:"))
async def cb_do_unban_card(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    tid = int(call.data.split(":")[1])
    await set_user_ban_status(tid, 0)
    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    await log_admin_action(call.from_user.id, adm_tag, "Разблокировка из карточки", tid)
    await call.answer(f"🍏 Игрок {tid} разблокирован!", show_alert=True)
    t, kb = await render_user_card(tid)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass

# --- ПРОМОКОДЫ ---
@router.callback_query(F.data == "adm_btn_promos")
async def cb_adm_promos(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    async with get_db_connection() as db:
        cur = await db.execute("SELECT code, amount, current_uses, max_uses, created_at FROM promocodes ORDER BY created_at DESC LIMIT 15")
        promos = await cur.fetchall()

    t = "🎁 <b>СПИСОК ВСЕХ ПРОМОКОДОВ</b>\n━━━━━━━━━━━━━━━━━━\n"
    if not promos:
        t += "<i>Промокоды еще не создавались.</i>\n"
    else:
        for code, amt, cur_u, max_u, c_at in promos:
            disp_date = format_datetime_display(c_at)
            t += f"• <code>{code}</code> — {format_bold_num(amt)} (Активаций: <b>{cur_u}/{max_u}</b> | <i>{disp_date}</i>)\n"
    t += "━━━━━━━━━━━━━━━━━━"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm_btn_create_promo")],
        [InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(call, t, kb)
    await call.answer()

@router.callback_query(F.data == "adm_btn_create_promo")
async def cb_create_promo_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    t = (
        "🎁 <b>СОЗДАНИЕ НОВОГО ПРОМОКОДА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Введите данные в одну строку через пробел:\n"
        "<code>НАЗВАНИЕ СУММА МАКС_АКТИВАЦИЙ</code>\n\n"
        "💡 <i>Пример:</i> <code>NEMOS2026 5OOk 100</code>"
    )
    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_create_promo)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_create_promo)
async def state_create_promo_process(msg: types.Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        if state is not None: await state.clear()
        return
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    adm_tag = f"@{msg.from_user.username}" if msg.from_user.username else f"ID {msg.from_user.id}"
    try:
        parts = msg.text.strip().split()
        if len(parts) < 3: raise ValueError
        code = parts[0].upper()
        amt = parse_amount(parts[1])
        max_uses = int(parts[2])
        if amt <= 0 or max_uses <= 0: raise ValueError
    except Exception:
        return await msg.answer("❌ <b>Неверный формат!</b> Введите: <code>КОД СУММА АКТИВАЦИИ</code> (например: <code>BONUS 5OOk 5O</code>):", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    now_iso = get_now_iso()
    async with get_db_connection() as db:
        await db.execute(
            "INSERT OR REPLACE INTO promocodes (code, amount, max_uses, current_uses, created_at) VALUES (?, ?, ?, 0, ?)",
            (code, amt, max_uses, now_iso)
        )
        await db.commit()

    await log_admin_action(msg.from_user.id, adm_tag, "Создание промокода", details=f"Код: {code}, Сумма: {amt}$, Лимит: {max_uses}")
    if state is not None:
        await state.clear()
    await msg.answer(
        f"✅ <b>Промокод <code>{code}</code> на сумму {format_bold_num(amt)} (Лимит: {max_uses}) успешно создан!</b>",
        reply_markup=get_back_to_panel_kb(),
        parse_mode="HTML"
    )

# --- ЛОГИ ТРАНСФЕРОВ И ДЕЙСТВИЙ АДМИНИСТРАЦИИ ---
@router.callback_query(F.data.startswith("adm_btn_transfers:"))
async def cb_adm_transfers(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    page = int(call.data.split(":")[1])
    limit = 6
    offset = page * limit

    async with get_db_connection() as db:
        cur = await db.execute("SELECT COUNT(*) FROM transfer_logs")
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT sender_id, recipient_id, amount, fee, date FROM transfer_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        logs = await cur.fetchall()

    t = f"💸 <b>ЛОГИ P2P ПЕРЕВОДОВ (Всего: {total})</b>\n━━━━━━━━━━━━━━━━━━\n"
    if not logs:
        t += "<i>Переводы еще не совершались.</i>\n"
    else:
        for s_id, r_id, amt, fee, dt in logs:
            disp_date = format_datetime_display(dt)
            t += f"• <code>{s_id}</code> ➡️ <code>{r_id}</code>: {format_bold_num(amt)} (Ком: {fmt_o(fee)}$ | <i>{disp_date}</i>)\n\n"
    t += "━━━━━━━━━━━━━━━━━━"

    kb = []
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_btn_transfers:{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"adm_btn_transfers:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back_panel")])

    await send_or_edit_panel_message(call, t, InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

@router.callback_query(F.data.startswith("adm_btn_logs:"))
async def cb_adm_logs(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    page = int(call.data.split(":")[1])
    limit = 6
    offset = page * limit

    async with get_db_connection() as db:
        cur = await db.execute("SELECT COUNT(*) FROM admin_logs")
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT admin_tag, action, target_id, details, date FROM admin_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        logs = await cur.fetchall()

    t = f"📋 <b>ЖУРНАЛ ДЕЙСТВИЙ АДМИНИСТРАЦИИ (Всего: {total})</b>\n━━━━━━━━━━━━━━━━━━\n"
    if not logs:
        t += "<i>Журнал пуст.</i>\n"
    else:
        for a_tag, action, tid, det, dt in logs:
            disp_date = format_datetime_display(dt)
            tid_str = f" | Игрок: <code>{tid}</code>" if tid else ""
            t += f"• <b>{html.escape(a_tag)}:</b> {html.escape(action)}{tid_str}\n  💬 <i>{html.escape(det or '')}</i> (<i>{disp_date}</i>)\n\n"
    t += "━━━━━━━━━━━━━━━━━━"

    kb = []
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_btn_logs:{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"adm_btn_logs:{page+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="◀️ В админку", callback_data="adm_back_panel")])

    await send_or_edit_panel_message(call, t, InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()

# --- НЕБЛОКИРУЮЩАЯ ФОНОВАЯ РАССЫЛКА ---
async def run_broadcast_task(bot: Bot, admin_id: int, text: Optional[str], photo_id: Optional[str], caption: Optional[str]):
    """Фоновая рассылка, не блокирующая панель управления."""
    async with get_db_connection() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned = 0 AND is_blocked = 0")
        users = await cur.fetchall()

    sent = 0
    blocked = 0

    for (uid,) in users:
        try:
            if photo_id:
                await bot.send_photo(uid, photo=photo_id, caption=caption, parse_mode="HTML")
            else:
                await bot.send_message(uid, text=text, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.04)
        except Exception as err:
            blocked += 1
            err_str = str(err).lower()
            if "bot was blocked" in err_str or "forbidden" in err_str or "user is deactivated" in err_str:
                try:
                    async with get_db_connection() as db:
                        await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (uid,))
                        await db.commit()
                except Exception:
                    pass

    try:
        report = (
            f"📢 <b>Фоновая рассылка завершена!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📬 <b>Успешно доставлено:</b> <b>{sent} чел.</b>\n"
            f"🚫 <b>Не доставлено (блок/ошибка):</b> <b>{blocked} чел.</b>"
        )
        await bot.send_message(admin_id, report, parse_mode="HTML")
    except Exception:
        pass

@router.callback_query(F.data == "adm_btn_broadcast")
async def cb_broadcast_start(call: types.CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    t = (
        "📢 <b>МАССОВАЯ РАССЫЛКА СООБЩЕНИЙ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Отправьте текст (или фото с описанием), который получат все пользователи бота:\n\n"
        "⚡ <i>Рассылка будет запущена в фоновом режиме.</i>"
    )
    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_broadcast)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_broadcast)
async def state_broadcast_process(msg: types.Message, state: FSMContext, bot: Bot):
    if not await is_admin(msg.from_user.id):
        if state is not None: await state.clear()
        return
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    if state is not None:
        await state.clear()
    
    photo_id = msg.photo[-1].file_id if msg.photo else None
    caption = msg.caption if msg.photo else None
    text = msg.text if not msg.photo else None

    # Запуск в фоновом потоке
    asyncio.create_task(run_broadcast_task(bot, msg.from_user.id, text, photo_id, caption))

    await msg.answer(
        "🚀 <b>Рассылка успешно запущена в фоновом режиме!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Вы можете продолжать работу с ботом. По завершении вам придет отчет.",
        reply_markup=get_back_to_panel_kb(),
        parse_mode="HTML"
    )

# --- УПРАВЛЕНИЕ АДМИНИСТРАТОРАМИ (ТОЛЬКО ВЛАДЕЛЕЦ) ---
@router.callback_query(F.data == "adm_btn_admins")
async def cb_adm_admins(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    async with get_db_connection() as db:
        cur = await db.execute("SELECT user_id, username FROM users WHERE is_admin = 1 OR user_id = ?", (OWNER_ID,))
        adms = await cur.fetchall()

    t = f"👑 <b>СПИСОК АДМИНИСТРАТОРОВ ({len(adms)} чел.)</b>\n━━━━━━━━━━━━━━━━━━\n"
    for uid, uname in adms:
        tag = f"@{uname}" if uname else "нет"
        role = " (Владелец)" if uid == OWNER_ID else ""
        t += f"• ID <code>{uid}</code> ({tag}){role}\n"
    t += "━━━━━━━━━━━━━━━━━━"

    await send_or_edit_panel_message(call, t, get_back_to_panel_kb())
    await call.answer()

@router.callback_query(F.data == "adm_btn_make_adm")
async def cb_make_adm_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступно только главному владельцу!", show_alert=True)
    await send_or_edit_panel_message(call, "👑 <b>Введите Telegram ID или @username для назначения администратором:</b>", get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_make_adm)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_make_adm)
async def state_make_adm_process(msg: types.Message, state: FSMContext):
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    if msg.from_user.id != OWNER_ID: return
    raw = msg.text.strip().replace("@", "")
    tid = None
    if raw.isdigit():
        tid = int(raw)
    else:
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw,)) as cur:
                row = await cur.fetchone()
                if row: tid = row[0]

    if not tid:
        return await msg.answer("❌ <b>Пользователь не найден в базе!</b> Введите ID или @username игрока:", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    u = await get_and_update_user(tid)
    tag = f" (@{u['username']})" if u.get('username') else ""
    await set_user_admin_status(tid, 1)
    await log_admin_action(msg.from_user.id, "@owner", "Назначение администратора", tid)
    await msg.answer(f"👑 <b>Игрок ID <code>{tid}</code>{tag} успешно назначен администратором!</b>", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    if state is not None:
        await state.clear()

@router.callback_query(F.data == "adm_btn_rem_adm")
async def cb_rem_adm_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступно только главному владельцу!", show_alert=True)
    await send_or_edit_panel_message(call, "❌ <b>Введите Telegram ID или @username для снятия прав администратора:</b>", get_back_to_panel_kb())
    await state.set_state(AdminPanelState.waiting_rem_adm)
    await call.answer()

@router.message(F.chat.type == "private", AdminPanelState.waiting_rem_adm)
async def state_rem_adm_process(msg: types.Message, state: FSMContext):
    if is_menu_trigger(msg.text):
        await state.clear()
        return

    if msg.from_user.id != OWNER_ID: return
    raw = msg.text.strip().replace("@", "")
    tid = None
    if raw.isdigit():
        tid = int(raw)
    else:
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw,)) as cur:
                row = await cur.fetchone()
                if row: tid = row[0]

    if not tid:
        return await msg.answer("❌ <b>Пользователь не найден в базе!</b> Введите ID или @username игрока:", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    if tid == OWNER_ID:
        return await msg.answer("⛔ Нельзя снять права с главного владельца!", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")

    u = await get_and_update_user(tid)
    tag = f" (@{u['username']})" if u.get('username') else ""
    await set_user_admin_status(tid, 0)
    await log_admin_action(msg.from_user.id, "@owner", "Снятие прав администратора", tid)
    await msg.answer(f"❌ <b>С игрока ID <code>{tid}</code>{tag} успешно сняты права администратора!</b>", reply_markup=get_back_to_panel_kb(), parse_mode="HTML")
    if state is not None:
        await state.clear()
@router.callback_query(F.data == "adm_btn_modules")
async def cb_adm_modules(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    m_mines = "🟢 Вкл" if await is_game_module_enabled("mines") else "🔴 Выкл"
    m_dice_pve = "🟢 Вкл" if await is_game_module_enabled("dice_pve") else "🔴 Выкл"
    m_dice_pvp = "🟢 Вкл" if await is_game_module_enabled("dice_pvp") else "🔴 Выкл"
    m_p2p = "🟢 Вкл" if await is_game_module_enabled("p2p") else "🔴 Выкл"

    t = (
        "🕹 <b>УПРАВЛЕНИЕ МОДУЛЯМИ ПЛАТФОРМЫ (KILL-SWITCH)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Вы можете временно отключить любую игру или переводы для проведения техработ:</i>\n\n"
        f"• 💣 <b>Минёр (Mines 5x5):</b> <b>{m_mines}</b>\n"
        f"• 🎲 <b>Кости против Бота (PvE):</b> <b>{m_dice_pve}</b>\n"
        f"• ⚔️ <b>Кости PvP (Дуэли):</b> <b>{m_dice_pvp}</b>\n"
        f"• 💸 <b>P2P Переводы:</b> <b>{m_p2p}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Нажмите на кнопку для переключения статуса:</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💣 Минёр: {m_mines}", callback_data="adm_toggle_mod:mines")],
        [InlineKeyboardButton(text=f"🎲 Кости PvE: {m_dice_pve}", callback_data="adm_toggle_mod:dice_pve")],
        [InlineKeyboardButton(text=f"⚔️ Кости PvP: {m_dice_pvp}", callback_data="adm_toggle_mod:dice_pvp")],
        [InlineKeyboardButton(text=f"💸 P2P Переводы: {m_p2p}", callback_data="adm_toggle_mod:p2p")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(call, t, kb)
    await call.answer()

@router.callback_query(F.data.startswith("adm_toggle_mod:"))
async def cb_adm_toggle_mod(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id): return
    mod = call.data.split(":")[1]
    new_state = await toggle_game_module(mod)
    state_txt = "включен 🟢" if new_state else "выключен 🔴"
    await call.answer(f"Модуль {mod} {state_txt}!", show_alert=False)
    await cb_adm_modules(call)

@router.message(Command("user"), F.chat.type == "private")
@router.message(Command("find"), F.chat.type == "private")
async def cmd_quick_find_user(msg: types.Message):
    if not await is_admin(msg.from_user.id): return
    args = msg.text.strip().split()
    if len(args) < 2:
        return await msg.answer("💡 <i>Использование:</i> <code>/user @username</code> или <code>/user 123456789</code>", parse_mode="HTML")
    target_user = await find_user_by_query(args[1])
    if not target_user:
        return await msg.answer("❌ <b>Игрок не найден!</b>", parse_mode="HTML")
    t, kb = await render_user_card(target_user.get("user_id"))
    await msg.answer(t, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("adm_take_work:"))
async def cb_adm_take_work(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    _, tx_id, tid, amt = call.data.split(":")
    tx_id, tid, amt = int(tx_id), int(tid), float(amt)

    tx_data = await get_tx_by_id(tx_id)
    if not tx_data or tx_data[5] not in ["В обработке", "В работе"]:
        return await call.answer("⚠️ Заявка уже была обработана ранее!", show_alert=True)
    if tx_data[5] == "В работе":
        return await call.answer("⚠️ Эту заявку уже взял в работу другой куратор!", show_alert=True)

    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    async with get_db_connection() as db:
        cur = await db.execute(
            "UPDATE transactions SET status = 'В работе', curator_id = ? WHERE id = ? AND status = 'В обработке'",
            (call.from_user.id, tx_id)
        )
        await db.commit()
        if cur.rowcount == 0:
            return await call.answer("⚠️ Заявку уже взял другой куратор!", show_alert=True)

    await log_admin_action(call.from_user.id, adm_tag, "Взял заявку в работу", tid, f"Заявка №{tx_id} на {amt}$")

    work_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{EMOJIS['check']} Одобрить выплату", callback_data=f"pay_ask:{tx_id}:{tid}:{amt}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ask_rej_menu:{tx_id}:{tid}:{amt}")],
        [InlineKeyboardButton(text="↩️ Снять с работы", callback_data=f"adm_untake_work:{tx_id}:{tid}:{amt}")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm_user_card:{tid}")]
    ])

    cur_caption = call.message.caption or call.message.text or ""
    work_header = f"🚗 <b>В РАБОТЕ:</b> <b>{adm_tag}</b> (направляется на Drom)\n"
    new_caption = work_header + cur_caption
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=new_caption, reply_markup=work_kb, parse_mode="HTML")
        else:
            await call.message.edit_text(text=new_caption, reply_markup=work_kb, parse_mode="HTML")
    except Exception:
        pass

    await call.answer("🚗 Вы взяли заявку в работу! Перейдите на Drom для выкупа Оки.", show_alert=False)

    # Уведомление пользователю: ПОЛНОСТЬЮ АНОНИМНО (никаких тегов куратора или владельца)
    try:
        await bot.send_message(
            chat_id=tid,
            text=(
                f"🚗 <b>Ваша заявка №{tx_id} принята в обработку!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Куратор выплат зашёл на 17 сервер Grand Mobile и скоро выкупит ваше авто!\n\n"
                "⏳ <i>Пожалуйста, убедитесь, что ваш автомобиль выставлен на продажу по указанной стоимости и не снимайте его с продажи!</i>"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass

@router.callback_query(F.data.startswith("adm_untake_work:"))
async def cb_adm_untake_work(call: types.CallbackQuery, bot: Bot):
    if not await is_admin(call.from_user.id): return
    _, tx_id, tid, amt = call.data.split(":")
    tx_id, tid, amt = int(tx_id), int(tid), float(amt)

    tx_data = await get_tx_by_id(tx_id)
    if not tx_data or tx_data[5] != "В работе":
        return await call.answer("⚠️ Заявка не находится в работе!", show_alert=True)

    curator_id = tx_data[10] if len(tx_data) > 10 else None
    if curator_id and curator_id != call.from_user.id and call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Эту заявку взял другой куратор! Вы не можете снять её с работы.", show_alert=True)

    async with get_db_connection() as db:
        cur = await db.execute("UPDATE transactions SET status = 'В обработке', curator_id = NULL WHERE id = ? AND status = 'В работе'", (tx_id,))
        await db.commit()
        if cur.rowcount == 0:
            return await call.answer("⚠️ Заявка уже изменена!", show_alert=True)

    adm_tag = f"@{call.from_user.username}" if call.from_user.username else f"ID {call.from_user.id}"
    await log_admin_action(call.from_user.id, adm_tag, "Снял заявку с работы", tid, f"Заявка №{tx_id}")

    take_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Взять в работу", callback_data=f"adm_take_work:{tx_id}:{tid}:{amt}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ask_rej_menu:{tx_id}:{tid}:{amt}")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm_user_card:{tid}"), InlineKeyboardButton(text="⛔ Забанить", callback_data=f"ask_ban:{tid}")]
    ])

    cur_caption = call.message.caption or call.message.text or ""
    clean_caption = re.sub(r"^🚗 <b>В РАБОТЕ:</b> .*?\n", "", cur_caption)
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=clean_caption, reply_markup=take_kb, parse_mode="HTML")
        else:
            await call.message.edit_text(text=clean_caption, reply_markup=take_kb, parse_mode="HTML")
    except Exception:
        pass

    await call.answer("↩️ Заявка возвращена в общую очередь.", show_alert=False)


# --- ФУНКЦИЯ ПРЯМОЙ ЗАГРУЗКИ С GOOGLE ДИСКА ---
async def download_file_from_google_drive(file_id_or_url: str) -> bytes:
    """Скачивает файл или последний архив из папки Google Диска с обходом подтверждений."""
    import aiohttp, re, html

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    async with aiohttp.ClientSession(headers=headers, cookie_jar=aiohttp.CookieJar()) as session:
        file_ids = []
        
        # Если передан URL папки Google Drive
        if "folders/" in file_id_or_url:
            m_f = re.search(r"folders/([a-zA-Z0-9_-]+)", file_id_or_url)
            folder_id = m_f.group(1) if m_f else file_id_or_url.strip()
            f_url = f"https://drive.google.com/drive/folders/{folder_id}"
            try:
                async with session.get(f_url, allow_redirects=True) as resp_f:
                    if resp_f.status == 200:
                        f_html = await resp_f.text(errors="ignore")
                        found1 = re.findall(r'data-id="([a-zA-Z0-9_-]{25,})"', f_html)
                        found2 = re.findall(r'\["([a-zA-Z0-9_-]{28,35})"', f_html)
                        found3 = re.findall(r'/file/d/([a-zA-Z0-9_-]{25,})', f_html)
                        for fid in found1 + found2 + found3:
                            if fid not in file_ids and fid != folder_id:
                                file_ids.append(fid)
            except Exception:
                pass
        elif "drive.google.com" in file_id_or_url:
            m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", file_id_or_url)
            if not m:
                m = re.search(r"id=([a-zA-Z0-9_-]+)", file_id_or_url)
            if not m:
                m = re.search(r"/d/([a-zA-Z0-9_-]+)", file_id_or_url)
            if m:
                file_ids.append(m.group(1))
            else:
                file_ids.append(file_id_or_url.strip())
        else:
            file_ids.append(file_id_or_url.strip())

        if not file_ids:
            file_ids = ["1G0lkbEvyKpeITofJ3KQxysR6AyIAUm9i"]

        # Пробуем скачать каждый найденный ID (начиная с самых свежих)
        last_err = "Файл не найден"
        for file_id in file_ids:
            direct_urls = [
                f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
                f"https://drive.usercontent.google.com/download?id={file_id}&export=download",
                f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t",
                f"https://drive.google.com/uc?export=download&id={file_id}",
            ]

            for d_url in direct_urls:
                try:
                    async with session.get(d_url, allow_redirects=True) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data.startswith(b"PK"):
                                return data
                except Exception:
                    continue

            uc_url = f"https://drive.google.com/uc?export=download&id={file_id}"
            try:
                async with session.get(uc_url, allow_redirects=True) as resp:
                    content = await resp.read()
                    if content.startswith(b"PK"):
                        return content

                    html_text = content.decode("utf-8", errors="ignore")
                    for line in html_text.splitlines():
                        if "drive.usercontent.google.com/download" in line:
                            m = re.search(r"https://drive\\.usercontent\\.google\\.com/download\\?[^\s\"<>]+", line)
                            if m:
                                dl_url = html.unescape(m.group(0))
                                async with session.get(dl_url, allow_redirects=True) as resp2:
                                    if resp2.status == 200:
                                        d2 = await resp2.read()
                                        if d2.startswith(b"PK"):
                                            return d2

                    m_title = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE)
                    err_title = m_title.group(1).strip() if m_title else "Доступ закрыт"
                    last_err = err_title
            except Exception as e:
                last_err = str(e)

        raise Exception(f"Google Drive не отдал ZIP-архив ({last_err}).")


@router.callback_query(F.data == "adm_btn_cloud_update")
@router.message(F.chat.type == "private", F.text.func(lambda t: t and ("drive.google.com" in t)))
async def handle_cloud_update(event: types.CallbackQuery | types.Message, bot: Bot):
    uid = event.from_user.id
    if uid != OWNER_ID:
        if isinstance(event, types.CallbackQuery):
            return await event.answer("⛔ Доступно исключительно владельцу проекта!", show_alert=True)
        return

    from config.config import CLOUD_UPDATE_URL
    target_url = event.text.strip() if isinstance(event, types.Message) else CLOUD_UPDATE_URL

    status_msg = await (event.message.answer if isinstance(event, types.CallbackQuery) else event.answer)(
        "⏳ <b>Связываюсь с Google Диском и скачиваю обновление...</b>",
        parse_mode="HTML"
    )

    import tempfile, zipfile
    try:
        zip_bytes = await download_file_from_google_drive(target_url)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
            tmp_p = tmp_f.name
            tmp_f.write(zip_bytes)

        changelog_text = ""
        target_version = "v3.0.1"
        with zipfile.ZipFile(tmp_p, "r") as z:
            for n in z.namelist():
                base_n = os.path.basename(n).lower()
                if base_n in ("changelog.txt", "changelog.md", "release_notes.txt"):
                    raw = z.read(n).decode("utf-8", errors="ignore").strip()
                    if raw:
                        changelog_text = raw[:1000]
                if n.endswith("admin.py") or base_n == "version.txt":
                    try:
                        raw_c = z.read(n).decode("utf-8", errors="ignore")
                        m_v = re.search(r"BOT_VERSION\s*=\s*[\'\"]([^\'\"]+)[\'\"]", raw_c)
                        if m_v:
                            target_version = m_v.group(1)
                        elif base_n == "version.txt" and raw_c.strip():
                            target_version = raw_c.strip()
                    except Exception:
                        pass
                    
        if not changelog_text:
            changelog_text = CURRENT_CHANGELOG

        PENDING_OWNER_UPDATES[uid] = {
            "file_id": None,
            "local_path": tmp_p,
            "file_name": "nemos_cloud_patch.zip",
            "type": "zip",
            "changelog": changelog_text
        }

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Применить и перезапустить", callback_data="apply_update")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_update")]
        ])

        await status_msg.edit_text(
            f"☁️ <b>Обнаружен архив обновления (Google Диск):</b> <code>nemos_patch.zip</code>\n"
            f"🏷 <b>Версия обновления:</b> <code>{html.escape(target_version)}</code> 🚀\n"
            f"📊 <b>Размер:</b> <code>{round(len(zip_bytes) / 1024, 1)} КБ</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📝 <b>Что нового / Список изменений:</b>\n"
            f"{html.escape(changelog_text)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>Применить обновление прямо сейчас?</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка загрузки с Google Диска:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )

# ========================================================
# СИСТЕМА САМООБНОВЛЕНИЯ ЧЕРЕЗ TELEGRAM (СТРОГО OWNER_ID)
# ========================================================
PENDING_OWNER_UPDATES: dict[int, dict] = {}
from aiogram.filters import StateFilter
@router.message(F.chat.type == "private", F.document, StateFilter("*"))
async def handle_owner_update_document(msg: types.Message, bot: Bot, state: Optional[FSMContext] = None):
    if state is not None:
        await state.clear()
    if msg.from_user.id != OWNER_ID:
        return

    doc = msg.document
    file_name = doc.file_name or ""
    is_zip = file_name.lower().endswith(".zip")
    is_py = file_name.lower().endswith(".py")
    
    if not (is_zip or is_py):
        return

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await msg.answer("❌ Размер файла превышает лимит Telegram (20 МБ)!", parse_mode="HTML")

    import tempfile, zipfile
    changelog_text = ""
    
    if is_zip:
        try:
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
                tmp_p = tmp_f.name
            await bot.download(doc.file_id, destination=tmp_p)
            with zipfile.ZipFile(tmp_p, "r") as z:
                for n in z.namelist():
                    if n.lower() in ("changelog.txt", "changelog.md", "release_notes.txt"):
                        raw = z.read(n).decode("utf-8", errors="ignore").strip()
                        if raw:
                            changelog_text = raw[:1000]
                        break
            try: os.remove(tmp_p)
            except Exception: pass
        except Exception:
            pass
        
        if not changelog_text:
            changelog_text = CURRENT_CHANGELOG
            
        update_type = "zip"
        type_title = "архив обновления"
        desc_note = "• Пакетное обновление компонентов проекта"
    else:
        update_type = "py"
        type_title = "одиночный файл-патч"
        changelog_text = f"• Быстрый точечный патч файла: {file_name}"
        desc_note = f"• Будет заменён только файл {file_name}. Весь остальной проект останется без изменений."

    PENDING_OWNER_UPDATES[msg.from_user.id] = {
        "file_id": doc.file_id,
        "file_name": file_name,
        "type": update_type,
        "changelog": changelog_text
    }

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Применить и перезапустить", callback_data="apply_update")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_update")]
    ])

    msg_parts = [
        f"📦 <b>Обнаружен {type_title}:</b> <code>{html.escape(file_name)}</code>",
        f"📊 <b>Размер:</b> <code>{round(doc.file_size / 1024, 1)} КБ</code>",
        "━━━━━━━━━━━━━━━━━━",
        "<b>Применить обновление прямо сейчас?</b>"
    ]
    await msg.answer(chr(10).join(msg_parts), reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "apply_update")
async def cb_apply_update(call: types.CallbackQuery, bot: Bot):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)

    up_data = PENDING_OWNER_UPDATES.get(call.from_user.id)
    if not up_data:
        return await call.answer("❌ Файл обновления устарел. Отправьте файл заново!", show_alert=True)
        
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    status_msg = await call.message.answer("⏳ <b>Загрузка и применение обновления...</b>", parse_mode="HTML")

    import tempfile, zipfile, shutil, sys, py_compile
    from config.config import DB_FILE
    base_db_name = os.path.basename(DB_FILE).lower()
    PROTECTED_FILES = {
        base_db_name, f"{base_db_name}-wal", f"{base_db_name}-shm",
        "bot_database.db", "bot_database.db-wal", "bot_database.db-shm",
        "database.db", "database.db-wal", "database.db-shm",
        ".env", "token.env", "Token.env", "token.txt"
    }

    file_id = up_data["file_id"]
    file_name = up_data["file_name"]
    u_type = up_data["type"]
    cl_text = up_data["changelog"]
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        if u_type == "py":
            # 1. Скачиваем одиночный py файл
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp_f:
                tmp_py_path = tmp_f.name
            await bot.download(file_id, destination=tmp_py_path)
            
            # 2. Проверяем синтаксис
            await status_msg.edit_text(f"🔍 <b>Проверка синтаксиса {html.escape(file_name)}...</b>", parse_mode="HTML")
            try:
                py_compile.compile(tmp_py_path, doraise=True)
            except py_compile.PyCompileError as pe:
                os.remove(tmp_py_path)
                return await status_msg.edit_text(
                    f"❌ <b>Ошибка синтаксиса в файле {file_name}!</b>\n<code>{html.escape(str(pe))}</code>\n\nПатч отменён для защиты бота.",
                    parse_mode="HTML"
                )
            
            # 3. Ищем, где этот файл лежит в проекте
            target_path = None
            for r, _, fs in os.walk(base_dir):
                if file_name in fs:
                    target_path = os.path.join(r, file_name)
                    break
            if not target_path:
                target_path = os.path.join(base_dir, file_name)
                
            shutil.copy2(tmp_py_path, target_path)
            os.remove(tmp_py_path)
            copied_count = 1
            
        else:
            # ZIP обновление
            if up_data.get("local_path") and os.path.exists(up_data.get("local_path")):
                tmp_zip_path = up_data["local_path"]
            else:
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_file:
                    tmp_zip_path = tmp_file.name
                await bot.download(file_id, destination=tmp_zip_path)

            await status_msg.edit_text("🔍 <b>Проверка целостности архива...</b>", parse_mode="HTML")
            with zipfile.ZipFile(tmp_zip_path, "r") as z:
                bad_file = z.testzip()
                if bad_file:
                    os.remove(tmp_zip_path)
                    return await status_msg.edit_text(f"❌ <b>Архив повреждён!</b> Ошибка в файле: <code>{bad_file}</code>", parse_mode="HTML")

            await status_msg.edit_text("🔍 <b>Проверка синтаксиса кода...</b>", parse_mode="HTML")
            temp_extract_dir = tempfile.mkdtemp(prefix="nemos_update_")
            with zipfile.ZipFile(tmp_zip_path, "r") as z:
                z.extractall(temp_extract_dir)

            source_root = temp_extract_dir
            subitems = os.listdir(temp_extract_dir)
            if len(subitems) == 1 and os.path.isdir(os.path.join(temp_extract_dir, subitems[0])) and not os.path.exists(os.path.join(temp_extract_dir, "main.py")):
                source_root = os.path.join(temp_extract_dir, subitems[0])

            for root, _, files in os.walk(source_root):
                for f in files:
                    if f.endswith(".py"):
                        full_path = os.path.join(root, f)
                        try:
                            py_compile.compile(full_path, doraise=True)
                        except py_compile.PyCompileError as pe:
                            shutil.rmtree(temp_extract_dir, ignore_errors=True)
                            if os.path.exists(tmp_zip_path):
                                os.remove(tmp_zip_path)
                            return await status_msg.edit_text(
                                f"❌ <b>Ошибка синтаксиса в файле {f}!</b>\n<code>{html.escape(str(pe))}</code>\n\nОбновление отменено для защиты бота.",
                                parse_mode="HTML"
                            )

            from config.config import DB_FILE
            real_db_path = os.path.join(base_dir, DB_FILE)
            if os.path.exists(real_db_path):
                shutil.copy2(real_db_path, real_db_path + ".backup_pre_update")

            await status_msg.edit_text("📦 <b>Применение обновлений...</b>", parse_mode="HTML")
            copied_count = 0
            for root, _, files in os.walk(source_root):
                rel_dir = os.path.relpath(root, source_root)
                dest_dir = os.path.join(base_dir, rel_dir) if rel_dir != "." else base_dir
                os.makedirs(dest_dir, exist_ok=True)

                for f in files:
                    f_lower = f.lower()
                    if f_lower in PROTECTED_FILES or f_lower.endswith(".db") or f_lower.endswith(".db-wal") or f_lower.endswith(".db-shm") or f_lower.endswith(".log") or f_lower.startswith(".env") or "token" in f_lower:
                        continue
                    src_file = os.path.join(root, f)
                    dst_file = os.path.join(dest_dir, f)
                    shutil.copy2(src_file, dst_file)
                    copied_count += 1

            shutil.rmtree(temp_extract_dir, ignore_errors=True)
            if os.path.exists(tmp_zip_path):
                os.remove(tmp_zip_path)

        # 6. Записываем флаг с чейнджлогом для уведомления после успешного рестарта
        notify_flag = os.path.join(base_dir, ".restart_notify.txt")
        with open(notify_flag, "w", encoding="utf-8") as nf:
            nf.write(f"{call.from_user.id}:{copied_count}\n{cl_text}")

        await status_msg.edit_text(
            f"✅ <b>Обновлено {copied_count} файлов!</b>\n"
            f"🚀 <b>Выполняется перезапуск бота...</b>\n"
            f"<i>Через 3 секунды бот вернётся в строй.</i>",
            parse_mode="HTML"
        )

        await bot.session.close()
        await asyncio.sleep(1.0)
        os.execv(sys.executable, [sys.executable, "main.py"])

    except Exception as err:
        await status_msg.edit_text(f"❌ <b>Ошибка обновления:</b> <code>{html.escape(str(err))}</code>", parse_mode="HTML")

@router.callback_query(F.data == "cancel_update")
async def cb_cancel_update(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)
    PENDING_OWNER_UPDATES.pop(call.from_user.id, None)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("❌ Обновление отменено.")

# ========================================================
# ФУНКЦИЯ ПОЛНОГО СБРОСА БАЗЫ ДАННЫХ (WIPE) СТРОГО ДЛЯ ВЛАДЕЛЬЦА
# ========================================================
@router.message(F.chat.type == "private", Command("wipe_db", "wipe"))
@router.callback_query(F.data == "ask_wipe_db")
async def cb_ask_wipe_db(event: types.Message | types.CallbackQuery):
    uid = event.from_user.id
    if uid != OWNER_ID:
        if isinstance(event, types.CallbackQuery):
            return await event.answer("⛔ Доступ разрешён строго владельцу проекта!", show_alert=True)
        return

    text = (
        "⚠️ <b>ВНИМАНИЕ: ПОЛНЫЙ СБРОС БАЗЫ ДАННЫХ (WIPE)</b> ⚠️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Это действие <b>безвозвратно удалит</b>:\n"
        "• Всех пользователей и их балансы\n"
        "• Все заявки на вывод и транзакции\n"
        "• Все игры («Минёр», лобби дуэлей, кейсы)\n"
        "• Все промокоды и логи операций\n\n"
        f"🛡 <i>Ваш аккаунт владельца (ID <code>{OWNER_ID}</code>) будет автоматически сохранён с правами администратора.</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Вы уверены, что хотите выполнить полный сброс?</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, ПЕРЕЙТИ К СБРОСУ", callback_data="confirm_wipe_db_step1")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back_panel")]
    ])
    if isinstance(event, types.CallbackQuery):
        await send_or_edit_panel_message(event, text, kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data == "confirm_wipe_db_step1")
async def cb_confirm_wipe_db_step1(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)

    text = (
        "🚨 <b>ФИНАЛЬНОЕ ПОДТВЕРЖДЕНИЕ ВАЙПА</b> 🚨\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Вы собираетесь полностью очистить базу данных сервера прямо сейчас.\n"
        "Все балансы игроков будут обнулены до нуля, а счётчики транзакций начнутся с №1.\n\n"
        "<b>Нажмите кнопку ниже для окончательного подтверждения:</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💥 ПОДТВЕРЖДАЮ: ОБНУЛИТЬ ВСЁ", callback_data="execute_wipe_db")],
        [InlineKeyboardButton(text="❌ Отмена (Назад)", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(call, text, kb)
    await call.answer()

@router.callback_query(F.data == "execute_wipe_db")
async def cb_execute_wipe_db(call: types.CallbackQuery):
    if call.from_user.id != OWNER_ID:
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)

    uname = call.from_user.username or call.from_user.first_name or "Owner"
    ok, msg = await wipe_entire_database(OWNER_ID, uname)
    if not ok:
        await call.answer(f"❌ {msg}", show_alert=True)
        return

    text = (
        "✅ <b>БАЗА ДАННЫХ УСПЕШНО СБРОШЕНА!</b> 🚀\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• Все таблицы очищены до нуля\n"
        "• Счётчик транзакций сброшен (следующая операция: №1)\n"
        "• Профиль владельца восстановлен с правами администратора\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎉 <i>Сервер полностью готов к приёму игроков с чистого листа!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👑 В админ-панель", callback_data="adm_back_panel")]
    ])
    await send_or_edit_panel_message(call, text, kb)
    await call.answer("База данных успешно очищена!", show_alert=True)


# --- АВТОМАТИЧЕСКОЕ ОБНОВЛЕНИЕ С GITHUB В 1 КЛИК ПО КНОПКЕ ---
@router.callback_query(F.data == "adm_btn_git_update")
@router.message(F.chat.type == "private", Command("git_update"))
async def handle_git_update(event: types.CallbackQuery | types.Message, bot: Bot):
    uid = event.from_user.id
    if uid != OWNER_ID:
        if isinstance(event, types.CallbackQuery):
            return await event.answer("⛔ Доступно исключительно владельцу проекта!", show_alert=True)
        return

    status_msg = await (event.message.answer if isinstance(event, types.CallbackQuery) else event.answer)(
        "⏳ <b>Связываюсь с GitHub и запрашиваю обновления (git pull)...</b>",
        parse_mode="HTML"
    )

    import asyncio, os, sys
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        git_dir = os.path.join(base_dir, ".git")
        if not os.path.exists(git_dir):
            proc = await asyncio.create_subprocess_shell(
                "git init && git branch -M main && git remote remove origin 2>/dev/null; git remote add origin https://github.com/shizisharks-cmd/NemosTrade.git && git fetch origin main && git reset --hard origin/main",
                cwd=base_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                "git fetch origin main && git reset --hard origin/main",
                cwd=base_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
        stdout, stderr = await proc.communicate()
        out_str = stdout.decode("utf-8", errors="ignore").strip()
        err_str = stderr.decode("utf-8", errors="ignore").strip()

        if "Already up to date" in out_str or "Уже обновлено" in out_str:
            return await status_msg.edit_text(
                "✅ <b>Установлена самая актуальная версия!</b>\n<i>Новых изменений на GitHub не обнаружено.</i>",
                parse_mode="HTML"
            )

        if proc.returncode != 0 and not out_str:
            raise Exception(err_str or "Команда git pull завершилась с ошибкой.")

        await status_msg.edit_text(
            f"✅ <b>Файлы успешно обновлены с GitHub!</b>\n"
            f"<code>{html.escape(out_str[:300])}</code>\n\n"
            "🚀 <b>Перезапуск бота...</b>\n<i>Через 3 секунды бот вернётся в строй.</i>",
            parse_mode="HTML"
        )

        await bot.session.close()
        await asyncio.sleep(1.0)
        os.execv(sys.executable, [sys.executable, "main.py"])

    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка обновления с GitHub:</b>\n<code>{html.escape(str(e))}</code>\n\n"
            "<i>Убедитесь, что сервер Wispbyte привязан к репозиторию GitHub.</i>",
            parse_mode="HTML"
        )
