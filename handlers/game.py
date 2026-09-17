from typing import Optional, Dict
import random
import asyncio
import html
from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config.config import (
    REVIEWS_CHANNEL, EMOJIS, DICE_MULTIPLIER, MIN_DICE_BET, MAX_DICE_BET, MIN_DICE_PVP_BET, MAX_DICE_PVP_BET, EFFECT_CONFETTI_ID,
    fmt_o, fmt_mult, format_bold_num, format_raw_num, parse_amount, is_menu_trigger, MIN_MINES_BET, MAX_MINES_BET
)
from database.database import (
    get_and_update_user, is_user_banned, is_maintenance_active, get_db_connection,
    get_user_active_mines_game, create_mines_game, step_mines_game, cashout_mines_game, calculate_mines_multiplier, get_user_recent_mines_history, get_user_last_mines_bet, is_game_module_enabled, is_admin
)
from services.game_service import (
    process_dice_pve_outcome, create_dice_pvp_lobby,
    cancel_dice_pvp_lobby, get_open_dice_pvp_lobbies, get_user_open_pvp_lobbies, get_other_open_dice_pvp_lobbies, play_dice_pvp_match,
    rollback_dice_pvp_match, get_dice_pvp_lobby_by_id
)

router = Router()

async def send_review_channel_post(bot: Bot, text: str) -> bool:
    """
    Надежная отправка уведомлений о крупных выигрышах в канал отзывов.
    ВАЖНО: В Telegram Bot API message_effect_id СТРОГО запрещен для каналов и супергрупп.
    """
    import logging
    logger = logging.getLogger(__name__)

    from config.config import REVIEWS_CHANNEL, load_env_val
    raw_ch = load_env_val("REVIEWS_CHANNEL", str(REVIEWS_CHANNEL))
    targets = []
    for c in [raw_ch, REVIEWS_CHANNEL]:
        if c and c not in targets:
            targets.append(c)

    for target in targets:
        send_id = target
        try:
            if str(target).startswith("-") and (str(target)[1:].isdigit() or str(target).startswith("-100")):
                send_id = int(target)
        except Exception:
            send_id = target

        try:
            await bot.send_message(
                chat_id=send_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
            logger.info(f"✅ Успешно опубликован пост о выигрыше в канал: {send_id}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка отправки выигрыша в канал отзывов ({send_id}): {e}. Убедитесь, что бот добавлен в канал администратором!")

    return False


_cached_bot_username = None

async def get_bot_username(bot: Bot) -> str:
    global _cached_bot_username
    if not _cached_bot_username:
        try:
            b = await bot.get_me()
            _cached_bot_username = b.username or "bot"
        except Exception:
            _cached_bot_username = "bot"
    return _cached_bot_username

active_dice_pve_players: set[int] = set()
active_mines_players: set[int] = set()

class DicePvEState(StatesGroup):
    waiting_for_amount = State()

class DicePvPState(StatesGroup):
    waiting_for_amount = State()

class MinesState(StatesGroup):
    waiting_for_custom_bet = State()

def get_games_hub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💣 Минёр (Mines 5x5)", callback_data="btn_mines_menu")],
        [InlineKeyboardButton(text="🎲 Кости против Бота (PvE x1.8)", callback_data="btn_dice_pve_menu")],
        [InlineKeyboardButton(text="⚔️ Кости PvP (Дуэль игроков)", callback_data="btn_dice_pvp_menu")],
        [InlineKeyboardButton(text="◀️ В главное меню", callback_data="btn_games_close")]
    ])

# --- ИГРОВОЙ ХАБ ---
@router.message(F.text.func(lambda t: t and ("игры" in t.lower() or "игра" in t.lower() or "🎮" in t)))
async def msg_games_hub(message: types.Message, state: FSMContext):
    if state is not None:
        await state.clear()
    if await is_user_banned(message.from_user.id): return
    if await is_maintenance_active(): return

    t = (
        "🎮 <b>ИГРОВОЙ ХАБ NEMOS TRADE</b> 🎮\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Ваша чистая прибыль готова умножаться в реальном времени!</b>\n\n"
        "💣 <b>Минёр (Mines 5х5)</b> — находите спрятанные алмазы и забирайте выигрыш с растущим множителем! Выбирайте от 2 до 15 мин на поле!\n\n"
        "🎲 <b>Кости против Бота (PvE)</b> — моментальная игра против системы с коэффициентом <b>x1.8</b>!\n\n"
        "⚔️ <b>Кости PvP (Дуэль)</b> — честная битва между игроками за общий банк (комиссия стола всего 3.33%)!\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(t, reply_markup=get_games_hub_kb(), parse_mode="HTML")

@router.callback_query(F.data == "btn_games_menu")
async def cb_games_menu(callback: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    t = (
        "🎮 <b>ИГРОВОЙ ХАБ NEMOS TRADE</b> 🎮\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Ваша чистая прибыль готова умножаться в реальном времени!</b>\n\n"
        "💣 <b>Минёр (Mines 5х5)</b> — находите спрятанные алмазы и забирайте выигрыш с растущим множителем! Выбирайте от 2 до 15 мин на поле!\n\n"
        "🎲 <b>Кости против Бота (PvE)</b> — моментальная игра против системы с коэффициентом <b>x1.8</b>!\n\n"
        "⚔️ <b>Кости PvP (Дуэль)</b> — честная битва между игроками за общий банк (комиссия стола всего 3.33%)!\n"
        "━━━━━━━━━━━━━━━━━━"
    )
    try: await callback.message.edit_text(t, reply_markup=get_games_hub_kb(), parse_mode="HTML")
    except Exception: await callback.message.answer(t, reply_markup=get_games_hub_kb(), parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data == "btn_games_close")
async def cb_games_close(callback: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    try: await callback.message.delete()
    except Exception: pass
    await callback.answer()

# --- ИГРА МИНЁР (MINES 5X5) ---
def build_mines_keyboard(game: dict, is_revealed: bool = False, hit_mine_idx: int = -1) -> InlineKeyboardMarkup:
    opened = set(game.get("opened_cells", []))

    rows = []
    for r in range(5):
        row = []
        for c in range(5):
            idx = r * 5 + c
            if is_revealed:
                if idx == hit_mine_idx:
                    btn_text = "💥"
                    cb = "noop"
                elif idx in game.get("mines_positions", []):
                    btn_text = "💣"
                    cb = "noop"
                elif idx in opened:
                    btn_text = "💎"
                    cb = "noop"
                else:
                    btn_text = "◽"
                    cb = "noop"
            else:
                if idx in opened:
                    btn_text = "💎"
                    cb = "noop"
                else:
                    btn_text = "❓"
                    cb = f"mine_step:{idx}"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=cb))
        rows.append(row)

    if not is_revealed:
        cur_mult = game.get("current_multiplier", 1.0)
        bet = game.get("bet", 0.0)
        cur_win = round(bet * cur_mult, 2)
        m_count = game.get("mines_count", 3)
        unopened_left = 25 - len(opened)
        mine_risk_pct = round((m_count / unopened_left) * 100) if unopened_left > 0 else 100

        # Кнопка рандом выбора
        rows.append([InlineKeyboardButton(text="🎲 Случайный выбор", callback_data="mine_random")])
        if len(opened) > 0:
            if cur_mult >= 2.0:
                cashout_btn = InlineKeyboardButton(
                    text=f"🚨 ЗАБРАТЬ КУШ {format_raw_num(cur_win)} (x{fmt_mult(cur_mult)})",
                    callback_data="mine_cashout"
                )
            else:
                cashout_btn = InlineKeyboardButton(
                    text=f"💰 ЗАБРАТЬ {format_raw_num(cur_win)} (x{fmt_mult(cur_mult)})",
                    callback_data="mine_cashout"
                )
            rows.append([cashout_btn])

        if game.get("is_admin"):
            rows.append([
                InlineKeyboardButton(text="👁 Где мины? (Admin)", callback_data="adm_peek_mines"),
                InlineKeyboardButton(text="👑 Авто-зачистка (Test)", callback_data="adm_autoclear_mines")
            ])
    else:
        bet = game.get("bet", 0.0)
        m_count = game.get("mines_count", 3)
        if bet >= MIN_MINES_BET:
            repeat_text = f"⚡ Повторить ({fmt_o(m_count)} мин, {format_raw_num(bet)})"
            repeat_cb = f"mines_repeat:{m_count}:{bet}"
            rows.append([InlineKeyboardButton(text=repeat_text, callback_data=repeat_cb)])
        rows.append([
            InlineKeyboardButton(text="🔄 Сменить ставку", callback_data="btn_mines_menu"),
            InlineKeyboardButton(text="🎮 В меню игр", callback_data="btn_games_menu")
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def render_mines_text(game: dict) -> str:
    bet = game.get("bet", 0.0)
    m_count = game.get("mines_count", 3)
    opened = game.get("opened_cells", [])
    cur_mult = game.get("current_multiplier", 1.0)
    cur_win = round(bet * cur_mult, 2) if len(opened) > 0 else 0.0
    cur_profit = max(0.0, round(cur_win - bet, 2)) if len(opened) > 0 else 0.0
    safe_cells_total = 25 - m_count
    safe_left = safe_cells_total - len(opened)
    unopened_left = 25 - len(opened)

    if safe_left > 0 and unopened_left > 0:
        next_step = len(opened) + 1
        next_mult = calculate_mines_multiplier(m_count, next_step)
        next_win = round(bet * next_mult, 2)
        win_prob_pct = int(round((safe_left / unopened_left) * 100))
        chance_line = f"🎲 <b>Шанс алмаза:</b> <b>{fmt_o(win_prob_pct)}%</b>\n\n"
        next_line = f"🎯 <b>Следующий множитель:</b> <b>x{fmt_mult(next_mult)}</b> (+{format_bold_num(next_win)})\n\n"
    else:
        chance_line = ""
        next_line = "🎯 <b>Следующий множитель:</b> <b>ДЖЕКПОТ!</b> 🎆\n\n"

    win_now_line = f"💰 <b>Выигрыш сейчас:</b> <b>+{format_bold_num(cur_win)}</b>\n" if len(opened) > 0 else f"💰 <b>Текущий выигрыш:</b> <b>O$</b>\n"
    admin_hint = ""
    if game.get("is_admin"):
        mines = sorted(game.get("mines_positions", []))
        mine_nums = ", ".join(str(m + 1) for m in mines)
        admin_hint = f"🕵️ <b>АДМИН-РЕЖИМ (Где мины):</b> ячейки <code>{mine_nums}</code> (1-25)\n"
    mult_suffix = f"(+{format_bold_num(cur_profit)})" if len(opened) > 0 else "(+O$)"

    return (
        "💣 <b>ИГРА «МИНЁР» (MINES 5x5)</b> 💎\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Ваша ставка:</b> {format_bold_num(bet)}\n\n"
        f"💣 <b>Мины на поле:</b> <b>{fmt_o(m_count)}</b> шт. (алмазов: {fmt_o(safe_cells_total)})\n\n"
        f"💎 <b>Открыто алмазов:</b> <b>{fmt_o(len(opened))}</b> шт. (осталось: {fmt_o(safe_left)})\n\n"
        f"{chance_line}"
        f"📈 <b>Текущий множитель:</b> <b>x{fmt_mult(cur_mult)}{mult_suffix}</b>\n\n"
        f"{next_line}"
        f"{win_now_line}"
        f"{admin_hint}"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Нажимайте на ячейки, случайный выбор или заберите банк:</b>"
    )

@router.callback_query(F.data == "noop")
async def cb_noop(call: types.CallbackQuery):
    await call.answer()

@router.callback_query(F.data == "btn_mines_menu")
async def cb_mines_menu(call: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = call.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if not await is_game_module_enabled("mines"):
        return await call.answer("⚠️ Игра «Минёр» временно отключена администрацией на обслуживание!", show_alert=True)

    active = await get_user_active_mines_game(uid)
    if active:
        t = render_mines_text(active)
        kb = build_mines_keyboard(active, is_revealed=False)
        try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
        except Exception: await call.message.answer(t, reply_markup=kb, parse_mode="HTML")
        await call.answer("Возврат в активную игру!")
        return

    u = await get_and_update_user(uid)
    recent_games = await get_user_recent_mines_history(uid, limit=3)
    recent_str = ""
    if recent_games:
        lines = []
        for i, g in enumerate(recent_games):
            prefix = "↳ " if i == 0 else ""
            if g["status"] == "WON":
                lines.append(f"{prefix}💎 x{fmt_mult(g['mult'])} (+{format_raw_num(g['win_amount'])})")
            else:
                lines.append(f"{prefix}💥 -{format_raw_num(g['bet'])}")
        recent_str = f"📜 <b>Последние игры:</b>\n" + "\n".join(lines) + "\n━━━━━━━━━━━━━━━━━━\n"

    t = (
        "💣 <b>ИГРА «МИНЁР» (MINES 5x5)</b> 💣\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Находите спрятанные алмазы и забирайте куш до взрыва!</b>\n\n"
        f"💰 <b>Доступно на балансе:</b> {format_bold_num(u['earned'])}\n\n"
        "💵 <b>Лимиты ставок:</b> от <b>5OK</b> до <b>5KK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{recent_str}"
        "👇 <b>Шаг 1: Выберите количество мин на поле:</b>"
    )
    kb = [
        [
            InlineKeyboardButton(text="🟢 3 мины", callback_data="mines_m:3"),
            InlineKeyboardButton(text="🟡 5 мин", callback_data="mines_m:5")
        ],
        [
            InlineKeyboardButton(text="🟠 1O мин", callback_data="mines_m:10"),
            InlineKeyboardButton(text="🔴 15 мин", callback_data="mines_m:15")
        ],
        [InlineKeyboardButton(text="◀️ В меню игр", callback_data="btn_games_menu")]
    ]
    try: await call.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception: await call.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    await call.answer()

def get_mines_bet_builder_kb(m_count: int, cur_bet: float) -> InlineKeyboardMarkup:
    confirm_text = f"🚀 Подтвердить: {format_raw_num(cur_bet)}" if cur_bet >= MIN_MINES_BET else "🚀 Подтвердить ставку"
    confirm_cb = f"mines_start:{m_count}:{cur_bet}" if cur_bet >= MIN_MINES_BET else f"mines_start:{m_count}:0"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+5Oк", callback_data=f"mines_b_add:{m_count}:{cur_bet}:50000"),
            InlineKeyboardButton(text="+1OOk", callback_data=f"mines_b_add:{m_count}:{cur_bet}:100000"),
            InlineKeyboardButton(text="+25Ok", callback_data=f"mines_b_add:{m_count}:{cur_bet}:250000")
        ],
        [
            InlineKeyboardButton(text="+5OOk", callback_data=f"mines_b_add:{m_count}:{cur_bet}:500000"),
            InlineKeyboardButton(text="+1кк", callback_data=f"mines_b_add:{m_count}:{cur_bet}:1000000"),
            InlineKeyboardButton(text="+5кк", callback_data=f"mines_b_add:{m_count}:{cur_bet}:5000000")
        ],
        [
            InlineKeyboardButton(text="➗2", callback_data=f"mines_b_div:{m_count}:{cur_bet}"),
            InlineKeyboardButton(text="✖️2", callback_data=f"mines_b_mul:{m_count}:{cur_bet}"),
            InlineKeyboardButton(text="🗑 Сброс", callback_data=f"mines_b_reset:{m_count}")
        ],
        [InlineKeyboardButton(text="✏️ Ввести сумму вручную", callback_data=f"mines_custom:{m_count}")],
        [InlineKeyboardButton(text=confirm_text, callback_data=confirm_cb)],
        [InlineKeyboardButton(text="◀️ Назад к выбору мин", callback_data="btn_mines_menu")]
    ])

def render_mines_choose_bet_text(m_count: int, cur_bet: float, user_earned: float) -> str:
    return (
        f"💣 <b>МИНЁР: ВЫБРАНО {fmt_o(m_count)} МИН</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Ваш баланс:</b> {format_bold_num(user_earned)}\n\n"
        "💵 <b>Лимиты ставок:</b> от <b>5OK</b> до <b>5KK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Выбранная ставка:</b> <b>{format_raw_num(cur_bet)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Добавляйте сумму кнопками, удваивайте или нажмите подтвердить:</b>"
    )

@router.callback_query(F.data.startswith("mines_m:"))
async def cb_mines_choose_bet(call: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = call.from_user.id
    m_count = int(call.data.split(":")[1])
    u = await get_and_update_user(uid)
    cur_bet = 0.0

    t = render_mines_choose_bet_text(m_count, cur_bet, u['earned'])
    kb = get_mines_bet_builder_kb(m_count, cur_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: await call.message.answer(t, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("mines_b_add:"))
async def cb_mines_b_add(call: types.CallbackQuery):
    parts = call.data.split(":")
    m_count = int(parts[1])
    cur_bet = float(parts[2])
    add_val = float(parts[3])
    uid = call.from_user.id
    u = await get_and_update_user(uid)

    new_bet = min(MAX_MINES_BET, round(cur_bet + add_val, 2))
    t = render_mines_choose_bet_text(m_count, new_bet, u['earned'])
    kb = get_mines_bet_builder_kb(m_count, new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"+{format_raw_num(add_val)} -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("mines_b_mul:"))
async def cb_mines_b_mul(call: types.CallbackQuery):
    parts = call.data.split(":")
    m_count = int(parts[1])
    cur_bet = float(parts[2])
    uid = call.from_user.id
    u = await get_and_update_user(uid)

    base_bet = cur_bet if cur_bet >= MIN_MINES_BET else MIN_MINES_BET
    new_bet = min(MAX_MINES_BET, round(base_bet * 2.0, 2))
    t = render_mines_choose_bet_text(m_count, new_bet, u['earned'])
    kb = get_mines_bet_builder_kb(m_count, new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"✖️2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("mines_b_div:"))
async def cb_mines_b_div(call: types.CallbackQuery):
    parts = call.data.split(":")
    m_count = int(parts[1])
    cur_bet = float(parts[2])
    uid = call.from_user.id
    u = await get_and_update_user(uid)

    new_bet = max(MIN_MINES_BET, round(cur_bet / 2.0, 2))
    t = render_mines_choose_bet_text(m_count, new_bet, u['earned'])
    kb = get_mines_bet_builder_kb(m_count, new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"➗2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("mines_b_reset:"))
async def cb_mines_b_reset(call: types.CallbackQuery):
    parts = call.data.split(":")
    m_count = int(parts[1])
    uid = call.from_user.id
    u = await get_and_update_user(uid)

    new_bet = 0.0
    t = render_mines_choose_bet_text(m_count, new_bet, u['earned'])
    kb = get_mines_bet_builder_kb(m_count, new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer("Ставка сброшена!")

@router.callback_query(F.data.startswith("mines_custom:"))
async def cb_mines_custom(call: types.CallbackQuery, state: FSMContext):
    m_count = int(call.data.split(":")[1])
    await state.update_data(mines_count=m_count)
    await state.set_state(MinesState.waiting_for_custom_bet)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="btn_mines_menu")]])
    await call.message.edit_text(
        f"✏️ <b>Введите сумму ставки от {format_bold_num(MIN_MINES_BET)} до {format_bold_num(MAX_MINES_BET)} (например: 5OOk, 1kk, 5kk, all):</b>",
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )
    await call.answer()

@router.message(MinesState.waiting_for_custom_bet)
async def state_mines_custom_bet(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        from handlers.user import handle_menu_redirection
        return await handle_menu_redirection(msg, state, bot)

    uid = msg.from_user.id
    u = await get_and_update_user(uid)
    data = await state.get_data()
    m_count = data.get("mines_count", 3)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="btn_mines_menu")]])

    try:
        max_possible = min(u['earned'], MAX_MINES_BET)
        bet = parse_amount(msg.text, max_val=max_possible)
        if bet < MIN_MINES_BET or bet > MAX_MINES_BET:
            return await msg.answer(f"❌ <b>Ставка должна быть от {format_bold_num(MIN_MINES_BET)} до {format_bold_num(MAX_MINES_BET)}!</b> Попробуйте снова:", reply_markup=cancel_kb, parse_mode="HTML")
        if bet > u['earned']:
            return await msg.answer("❌ <b>Недостаточно средств на балансе прибыли!</b>", reply_markup=cancel_kb, parse_mode="HTML")

        await state.clear()
        ok, err, game = await create_mines_game(uid, bet, m_count)
        if not ok:
            return await msg.answer(f"❌ {err}", reply_markup=cancel_kb, parse_mode="HTML")

        t = render_mines_text(game)
        kb = build_mines_keyboard(game, is_revealed=False)
        await msg.answer(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer("❌ <b>Неверный формат.</b> Введите сумму (например: 5OOk, 1kk, all):", reply_markup=cancel_kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("mines_repeat:"))
async def cb_mines_repeat(call: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = call.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if not await is_game_module_enabled("mines"):
        return await call.answer("⚠️ Игра «Минёр» временно отключена администрацией на обслуживание!", show_alert=True)

    parts = call.data.split(":")
    m_count = int(parts[1])
    bet = float(parts[2])

    u = await get_and_update_user(uid)
    if bet < MIN_MINES_BET or bet > MAX_MINES_BET:
        return await call.answer(f"⚠️ Ставка должна быть от {format_raw_num(MIN_MINES_BET)} до {format_raw_num(MAX_MINES_BET)}!", show_alert=True)
    if bet > u['earned']:
        return await call.answer("❌ Недостаточно средств на балансе прибыли для повтора ставки!", show_alert=True)

    ok, err, game = await create_mines_game(uid, bet, m_count)
    if not ok:
        return await call.answer(f"❌ {err}", show_alert=True)

    t = render_mines_text(game)
    kb = build_mines_keyboard(game, is_revealed=False)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: await call.message.answer(t, reply_markup=kb, parse_mode="HTML")
    await call.answer("⚡ Ставка повторена! Удачи!")

@router.callback_query(F.data.startswith("mines_start:"))
async def cb_mines_start_preset(call: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = call.from_user.id
    if not await is_game_module_enabled("mines"):
        return await call.answer("⚠️ Игра «Минёр» временно отключена администрацией на обслуживание!", show_alert=True)
    parts = call.data.split(":")
    m_count = int(parts[1])
    arg_bet = parts[2]

    u = await get_and_update_user(uid)
    bet = min(u['earned'], MAX_MINES_BET) if arg_bet == "all" else float(arg_bet)

    if bet < MIN_MINES_BET:
        return await call.answer(f"⚠️ Минимальная ставка: {format_raw_num(MIN_MINES_BET)}! Добавьте сумму кнопками выше.", show_alert=True)
    if bet > MAX_MINES_BET:
        return await call.answer(f"⚠️ Максимальная ставка: {format_raw_num(MAX_MINES_BET)}!", show_alert=True)
    if bet > u['earned']:
        return await call.answer("❌ Недостаточно средств на балансе чистой прибыли!", show_alert=True)

    ok, err, game = await create_mines_game(uid, bet, m_count)
    if not ok:
        return await call.answer(f"❌ {err}", show_alert=True)

    t = render_mines_text(game)
    kb = build_mines_keyboard(game, is_revealed=False)
    await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    await call.answer("Игра началась! Удачи!")

async def process_mine_step(call: types.CallbackQuery, uid: int, cell_idx: int, is_random: bool = False):
    if uid in active_mines_players:
        return await call.answer("⏳ Выполняется ход, подождите...", show_alert=False)
    active_mines_players.add(uid)
    try:
        ok, res_text, game = await step_mines_game(uid, cell_idx)
        if not ok:
            return await call.answer(f"⚠️ {res_text}", show_alert=True)

        if game.get("status") == "LOST":
            kb = build_mines_keyboard(game, is_revealed=True, hit_mine_idx=cell_idx)
            t = (
                "💥 <b>БАБАХ! ВЫ НАСТУПИЛИ НА МИНУ!</b> 💣\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💵 <b>Сгоревшая ставка:</b> {format_bold_num(game['bet'])}\n"
                f"💎 <b>Было открыто алмазов:</b> <b>{fmt_o(len(game.get('opened_cells', [])))}</b> шт.\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "<i>Все мины поля раскрыты выше. Попробуйте снова!</i>"
            )
            try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
            except Exception: pass
            return await call.answer("💥 Взрыв! Мина!", show_alert=True)

        if game.get("status") == "WON" and game.get("full_clear"):
            kb = build_mines_keyboard(game, is_revealed=True)
            t = (
                "👑 <b>НЕВЕРОЯТНО! ПОЛНАЯ ЗАЧИСТКА ПОЛЯ!</b> 🎆\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "💎 <b>Вы открыли абсолютно все безопасные ячейки поля!</b>\n"
                f"📈 <b>Итоговый множитель:</b> <b>x{fmt_mult(game['current_multiplier'])}</b>\n"
                f"💵 <b>Ставка:</b> {format_bold_num(game['bet'])}\n"
                f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(game['win_amount'])}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🍾 <i>Все алмазы успешно открыты! Выигрыш моментально зачислен на баланс!</i>"
            )
            try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
            except Exception: pass

            cur_m = game.get("current_multiplier", 1.0)
            win_amt = game.get("win_amount", 0.0)
            bet = game.get("bet", 0.0)
            if win_amt > 0 and (win_amt >= 10_000_000.0 or (cur_m >= 5.0 and bet >= 500_000.0)):
                tid_str = str(uid)
                masked_id = f"ID {tid_str[:3]}•••" if len(tid_str) >= 3 else f"ID {tid_str}"
                review_text = (
                    f"👑 <b>ДЖЕКПОТ В ИГРЕ «МИНЁР»!</b> 💣\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Игрок:</b> <b>{masked_id}</b>\n"
                    "💎 <b>Открыл абсолютно все безопасные ячейки!</b>\n"
                    f"💵 <b>Ставка:</b> {format_bold_num(bet)}\n"
                    f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(win_amt)}</b> (x{fmt_mult(cur_m)})\n"
                    f"📈 <b>Коэффициент:</b> <b>x{fmt_mult(cur_m)}</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🎮 <b>Испытай удачу в Nemos Trade:</b> @NemosTrade_Bot"
                )
                await send_review_channel_post(call.bot, review_text)

            return await call.answer("👑 ДЖЕКПОТ! Все алмазы найдены!", show_alert=True)

        t = render_mines_text(game)
        kb = build_mines_keyboard(game, is_revealed=False)
        try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
        except Exception: pass

        cur_m = fmt_mult(game['current_multiplier'])
        if is_random:
            await call.answer(f"🎲 Случайный выбор: ячейка №{fmt_o(cell_idx + 1)} 💎 x{cur_m}")
        else:
            await call.answer(f"💎 Алмаз! x{cur_m}")
    finally:
        active_mines_players.discard(uid)

@router.callback_query(F.data.startswith("mine_step:"))
async def cb_mine_step(call: types.CallbackQuery):
    uid = call.from_user.id
    cell_idx = int(call.data.split(":")[1])
    await process_mine_step(call, uid, cell_idx, is_random=False)

@router.callback_query(F.data == "mine_random")
async def cb_mine_random(call: types.CallbackQuery):
    uid = call.from_user.id
    game = await get_user_active_mines_game(uid)
    if not game:
        return await call.answer("⚠️ Активная игра не найдена!", show_alert=True)

    opened = set(game.get("opened_cells", []))
    all_cells = set(range(25))
    unopened = list(all_cells - opened)

    if not unopened:
        return await call.answer("⚠️ Все ячейки уже открыты!", show_alert=True)

    cell_idx = random.choice(unopened)
    await process_mine_step(call, uid, cell_idx, is_random=True)

@router.callback_query(F.data == "mine_cashout")
async def cb_mine_cashout(call: types.CallbackQuery):
    uid = call.from_user.id
    if uid in active_mines_players:
        return await call.answer("⏳ Выполняется операция, подождите...", show_alert=False)
    active_mines_players.add(uid)
    try:
        ok, res_text, game = await cashout_mines_game(uid)
        if not ok:
            return await call.answer(f"⚠️ {res_text}", show_alert=True)

        kb = build_mines_keyboard(game, is_revealed=True)
        t = (
            "🎉 <b>ВЫ УСПЕШНО ЗАБРАЛИ ВЫИГРЫШ!</b> 🍾\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"📈 <b>Зафиксирован множитель:</b> <b>x{fmt_mult(game['current_multiplier'])}</b>\n"
            f"💵 <b>Ваша ставка:</b> {format_bold_num(game['bet'])}\n"
            f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(game['win_amount'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🚀 <i>Средства моментально зачислены на ваш баланс!</i>\n"
            "<i>(Расположение всех скрытых мин раскрыто на поле выше)</i>"
        )
        try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
        except Exception: pass
        await call.answer(f"🎉 Забрали +{format_raw_num(game['win_amount'])}!", show_alert=True)

        cur_m = game.get("current_multiplier", 1.0)
        win_amt = game.get("win_amount", 0.0)
        bet = game.get("bet", 0.0)
        if win_amt > 0 and (win_amt >= 10_000_000.0 or (cur_m >= 5.0 and bet >= 500_000.0)):
            tid_str = str(uid)
            masked_id = f"ID {tid_str[:3]}•••" if len(tid_str) >= 3 else f"ID {tid_str}"
            review_text = (
                f"💣 <b>КРУПНЫЙ ВЫИГРЫШ В ИГРЕ «МИНЁР»!</b> 💎\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Игрок:</b> <b>{masked_id}</b>\n"
                f"💵 <b>Ставка:</b> {format_bold_num(bet)}\n"
                f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(win_amt)}</b> (x{fmt_mult(cur_m)})\n"
                f"📈 <b>Коэффициент:</b> <b>x{fmt_mult(cur_m)}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🎮 <b>Испытай удачу в Nemos Trade:</b> @NemosTrade_Bot"
            )
            await send_review_channel_post(call.bot, review_text)
    finally:
        active_mines_players.discard(uid)


# --- КОСТИ ПРОТИВ БОТА (PvE x1.8: ИНТЕРАКТИВНЫЙ КОНСТРУКТОР СТАВКИ) ---
def get_dice_pve_builder_kb(cur_bet: float) -> InlineKeyboardMarkup:
    confirm_text = f"🎲 Бросить кубик ({format_raw_num(cur_bet)})" if cur_bet >= MIN_DICE_BET else "🎲 Бросить кубик"
    confirm_cb = f"dice_play_pve_go:{cur_bet}" if cur_bet >= MIN_DICE_BET else "dice_pve_zero"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+5Oк", callback_data=f"dice_pve_b_add:{cur_bet}:50000"),
            InlineKeyboardButton(text="+1OOk", callback_data=f"dice_pve_b_add:{cur_bet}:100000"),
            InlineKeyboardButton(text="+25Ok", callback_data=f"dice_pve_b_add:{cur_bet}:250000")
        ],
        [
            InlineKeyboardButton(text="+5OOk", callback_data=f"dice_pve_b_add:{cur_bet}:500000"),
            InlineKeyboardButton(text="+1кк", callback_data=f"dice_pve_b_add:{cur_bet}:1000000"),
            InlineKeyboardButton(text="+5кк", callback_data=f"dice_pve_b_add:{cur_bet}:5000000")
        ],
        [
            InlineKeyboardButton(text="➗2", callback_data=f"dice_pve_b_div:{cur_bet}"),
            InlineKeyboardButton(text="✖️2", callback_data=f"dice_pve_b_mul:{cur_bet}"),
            InlineKeyboardButton(text="🗑 Сброс", callback_data="dice_pve_b_reset")
        ],
        [InlineKeyboardButton(text="✏️ Ввести сумму вручную", callback_data="dice_pve_custom_bet")],
        [InlineKeyboardButton(text=confirm_text, callback_data=confirm_cb)],
        [InlineKeyboardButton(text="◀️ В игровой зал", callback_data="btn_games_menu")]
    ])

def render_dice_pve_text(user_earned: float, cur_bet: float) -> str:
    possible_win = round(cur_bet * DICE_MULTIPLIER, 2)
    win_str = f"{format_bold_num(possible_win)}" if cur_bet >= MIN_DICE_BET else "O$"
    return (
        "🎲 <b>КОСТИ ПРОТИВ БОТА (PvE x1.8)</b> 🎲\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Правила игры против системы:</b>\n\n"
        "1️⃣ Вы делаете ставку с баланса чистой прибыли.\n"
        "2️⃣ Сначала бросаете кубик вы, затем свой бросок делает Бот 🎲\n"
        "3️⃣ Если ваше число БОЛЬШЕ — вы побеждаете и забираете <b>x1.8</b> от ставки\n"
        "4️⃣ Если число МЕНЬШЕ — ставка сгорает в пользу платформы.\n"
        "5️⃣ Если числа РАВНЫ (Ничья) — ставка в полном объеме возвращается 1:1.\n\n"
        f"💳 <b>Ваш баланс:</b> {format_bold_num(user_earned)}\n"
        "💵 <b>Лимиты ставок:</b> от <b>1OK</b> до <b>5KK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Выбранная ставка:</b> <b>{format_raw_num(cur_bet)}</b> (Выигрыш: {win_str})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Добавляйте сумму кнопками, удваивайте или нажмите бросить:</b>"
    )

@router.callback_query(F.data == "btn_dice_pve_menu")
async def cb_dice_pve_menu(callback: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = callback.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if not await is_game_module_enabled("dice_pve"):
        return await callback.answer("⚠️ Игра «Кости против Бота» временно отключена на техобслуживание!", show_alert=True)

    u = await get_and_update_user(uid)
    cur_bet = 0.0
    t = render_dice_pve_text(u['earned'], cur_bet)
    kb = get_dice_pve_builder_kb(cur_bet)
    try: await callback.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: await callback.message.answer(t, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("dice_pve_b_add:"))
async def cb_dice_pve_b_add(call: types.CallbackQuery):
    parts = call.data.split(":")
    cur_bet = float(parts[1])
    add_val = float(parts[2])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = min(MAX_DICE_BET, round(cur_bet + add_val, 2))
    t = render_dice_pve_text(u['earned'], new_bet)
    kb = get_dice_pve_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"+{format_raw_num(add_val)} -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("dice_pve_b_mul:"))
async def cb_dice_pve_b_mul(call: types.CallbackQuery):
    cur_bet = float(call.data.split(":")[1])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    base_bet = cur_bet if cur_bet >= MIN_DICE_BET else MIN_DICE_BET
    new_bet = min(MAX_DICE_BET, round(base_bet * 2.0, 2))
    t = render_dice_pve_text(u['earned'], new_bet)
    kb = get_dice_pve_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"✖️2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("dice_pve_b_div:"))
async def cb_dice_pve_b_div(call: types.CallbackQuery):
    cur_bet = float(call.data.split(":")[1])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = max(MIN_DICE_BET, round(cur_bet / 2.0, 2))
    t = render_dice_pve_text(u['earned'], new_bet)
    kb = get_dice_pve_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"➗2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data == "dice_pve_b_reset")
async def cb_dice_pve_b_reset(call: types.CallbackQuery):
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = 0.0
    t = render_dice_pve_text(u['earned'], new_bet)
    kb = get_dice_pve_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer("Ставка сброшена!")

@router.callback_query(F.data == "dice_pve_zero")
async def cb_dice_pve_zero(call: types.CallbackQuery):
    await call.answer("⚠️ Сначала выберите сумму ставки кнопками выше!", show_alert=True)

@router.callback_query(F.data == "dice_pve_custom_bet")
async def cb_dice_pve_custom_bet(callback: types.CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="btn_dice_pve_menu")]])
    await callback.message.edit_text(f"{EMOJIS['pencil']} <b>Введите ставку от {format_bold_num(MIN_DICE_BET)} до {format_bold_num(MAX_DICE_BET)} (например: 5Ok, 5OOk, 1kk, all):</b>", reply_markup=cancel_kb, parse_mode="HTML")
    await state.set_state(DicePvEState.waiting_for_amount)
    await callback.answer()

@router.message(DicePvEState.waiting_for_amount)
async def state_dice_pve_custom_amount(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        from handlers.user import handle_menu_redirection
        return await handle_menu_redirection(msg, state, bot)

    uid = msg.from_user.id
    u = await get_and_update_user(uid)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="btn_dice_pve_menu")]])
    try:
        amt = parse_amount(msg.text, max_val=min(u['earned'], MAX_DICE_BET))
        if amt < MIN_DICE_BET or amt > MAX_DICE_BET:
            return await msg.answer(f"❌ <b>Ставка должна быть от {format_bold_num(MIN_DICE_BET)} до {format_bold_num(MAX_DICE_BET)}!</b>", reply_markup=cancel_kb, parse_mode="HTML")
        if amt > u['earned']:
            return await msg.answer("❌ <b>Недостаточно средств на балансе прибыли!</b>", reply_markup=cancel_kb, parse_mode="HTML")
        await state.clear()
        t = render_dice_pve_text(u['earned'], amt)
        kb = get_dice_pve_builder_kb(amt)
        await msg.answer(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer("❌ <b>Неверный формат!</b> Введите число (например: 1OOk, 1kk, all):", reply_markup=cancel_kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("dice_play_pve_go:"))
async def cb_dice_play_pve_go(callback: types.CallbackQuery, bot: Bot):
    uid = callback.from_user.id
    if not await is_game_module_enabled("dice_pve"):
        return await callback.answer("⚠️ Игра «Кости против Бота» временно отключена на техобслуживание!", show_alert=True)
    
    if uid in active_dice_pve_players:
        return await callback.answer("⏳ Дождитесь завершения текущего броска!", show_alert=True)
    
    active_dice_pve_players.add(uid)
    amt = float(callback.data.split(":")[1])

    try:
        u = await get_and_update_user(uid)
        from database.database import get_user_transfer_hold_amount
        hold_amt = await get_user_transfer_hold_amount(uid)
        avail = max(0.0, round(u['earned'] - hold_amt, 2))
        if avail < amt:
            hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
            return await callback.answer(f"❌ Недостаточно доступных средств{hold_msg}! Доступно: {format_raw_num(avail)}", show_alert=True)

        async with get_db_connection() as db:
            cur = await db.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?", (amt, uid, amt))
            await db.commit()
            if cur.rowcount == 0: 
                return await callback.answer("❌ Ошибка списания баланса!", show_alert=True)

        try: await callback.message.delete()
        except Exception: pass

        try:
            await bot.send_message(chat_id=uid, text="🎲 <b>ВАШ БРОСОК:</b>", parse_mode="HTML")
            p_msg = await bot.send_dice(chat_id=uid, emoji="🎲")
            await asyncio.sleep(3.5)
            p_val = p_msg.dice.value

            await bot.send_message(chat_id=uid, text="🤖 <b>БРОСОК БОТА:</b>", parse_mode="HTML")
            b_msg = await bot.send_dice(chat_id=uid, emoji="🎲")
            await asyncio.sleep(3.5)
            b_val = b_msg.dice.value
        except Exception:
            async with get_db_connection() as db:
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (amt, uid))
                await db.commit()
            await bot.send_message(chat_id=uid, text="⚠️ Ошибка связи с Telegram. Ставка возвращена на ваш баланс.", parse_mode="HTML")
            return

        outcome, win_total, profit = await process_dice_pve_outcome(uid, amt, p_val, b_val)
        
        replay_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔄 Повторить ({format_raw_num(amt)})", callback_data=f"dice_play_pve_go:{amt}")],
            [InlineKeyboardButton(text="🎲 Сменить ставку", callback_data="btn_dice_pve_menu")],
            [InlineKeyboardButton(text="◀️ В игровой зал", callback_data="btn_games_menu")]
        ])

        if outcome == "WIN":
            win_text = (
                f"🎉 <b>ТРИУМФАЛЬНАЯ ПОБЕДА!</b> 🎆 🍾\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Ваш кубик:</b> <b>{p_val}</b> 🎲\n"
                f"🤖 <b>Кубик Бота:</b> <b>{b_val}</b> 🎲\n\n"
                f"💵 <b>Ваша ставка:</b> {format_bold_num(amt)}\n"
                f"💰 <b>Выигрыш (x{DICE_MULTIPLIER}):</b> <b>+{format_bold_num(win_total)}</b> 🎇\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "✨ <i>Средства моментально начислены на ваш баланс чистой прибыли!</i>"
            )
            try: await bot.send_message(chat_id=uid, text=win_text, reply_markup=replay_kb, parse_mode="HTML", message_effect_id=EFFECT_CONFETTI_ID)
            except Exception: await bot.send_message(chat_id=uid, text=win_text, reply_markup=replay_kb, parse_mode="HTML")

            if win_total >= 5_000_000.0 or amt >= 5_000_000.0:
                tid_str = str(uid)
                masked_id = f"ID {tid_str[:3]}•••" if len(tid_str) >= 3 else f"ID {tid_str}"
                pve_post = (
                    f"🎲 <b>КРУПНЫЙ ВЫИГРЫШ В КОСТИ С БОТОМ!</b> 🤖\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Игрок:</b> <b>{masked_id}</b>\n"
                    f"💵 <b>Ставка:</b> {format_bold_num(amt)}\n"
                    f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(win_total)}</b> (x{fmt_mult(DICE_MULTIPLIER)})\n"
                    f"🎯 <b>Игрок {p_val} бот {b_val}</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🎮 <b>Играй и побеждай:</b> @NemosTrade_Bot"
                )
                await send_review_channel_post(bot, pve_post)
        elif outcome == "LOSS":
            loss_text = (
                "😔 <b>ПОРАЖЕНИЕ В ДУЭЛИ</b> 🎲\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Ваш кубик:</b> <b>{p_val}</b> 🎲\n"
                f"🤖 <b>Кубик Бота:</b> <b>{b_val}</b> 🎲\n\n"
                f"📉 <b>Сумма проигрыша:</b> {format_bold_num(amt)}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🍀 <i>Бот выбросил больше! Возьмите реванш прямо сейчас!</i>"
            )
            await bot.send_message(chat_id=uid, text=loss_text, reply_markup=replay_kb, parse_mode="HTML")
        else:
            draw_text = (
                "🤝 <b>БОЕВАЯ НИЧЬЯ!</b> 🎲\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Ваш кубик:</b> <b>{p_val}</b> 🎲\n"
                f"🤖 <b>Кубик Бота:</b> <b>{b_val}</b> 🎲\n\n"
                f"💵 <b>Очки равны! Ставка {format_bold_num(amt)} возвращена на баланс 1:1.</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "⚡ <i>Сделайте еще один бросок для выявления победителя!</i>"
            )
            await bot.send_message(chat_id=uid, text=draw_text, reply_markup=replay_kb, parse_mode="HTML")
    finally:
        active_dice_pve_players.discard(uid)

# --- КОСТИ PvP (ДУЭЛЬ ИГРОКОВ: ТОЧНЫЕ БРОСКИ БЕЗ ПЕРЕСЫЛОК) ---
@router.callback_query(F.data == "btn_dice_pvp_menu")
async def cb_dice_pvp_menu(callback: types.CallbackQuery, state: Optional[FSMContext] = None):
    if state is not None:
        await state.clear()
    uid = callback.from_user.id
    if not await is_game_module_enabled("dice_pvp"):
        return await callback.answer("⚠️ PvP Дуэли временно отключены на техобслуживание!", show_alert=True)
    u = await get_and_update_user(uid)

    my_lobbies = await get_user_open_pvp_lobbies(uid)
    other_lobbies = await get_other_open_dice_pvp_lobbies(uid)

    my_count_txt = f" ({fmt_o(len(my_lobbies))})" if my_lobbies else ""

    kb = [
        [
            InlineKeyboardButton(text="➕ Создать дуэль", callback_data="dice_pvp_create"),
            InlineKeyboardButton(text=f"👑 Мои дуэли{my_count_txt}", callback_data="dice_pvp_my_list")
        ]
    ]

    if other_lobbies:
        for lid, c_id, c_name, bet, _ in other_lobbies:
            clean_name = html.escape(c_name or f"ID {c_id}")
            kb.append([InlineKeyboardButton(text=f"⚔️ {clean_name} — {format_raw_num(bet)}", callback_data=f"dice_pvp_ask_join:{lid}")])

    kb.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="btn_dice_pvp_menu")])
    kb.append([InlineKeyboardButton(text="◀️ В игровой зал", callback_data="btn_games_menu")])

    t = (
        "⚔️ <b>КОСТИ PvP (ДУЭЛЬ ИГРОКОВ)</b> ⚔️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Битва в реальном времени между живыми игроками!</b>\n"
        "📜 <b>Правила честной битвы</b> ⚔️\n"
        "• Оба игрока делают бросок костей 🎲\n"
        "• Тот, у кого выпало БОЛЬШЕ — забирает весь банк!\n"
        "• Комиссия победителю составляет всего 3.33%\n"
        "• При ничьей — ставки возвращаются обоим игрокам 1OO%\n\n"
        f"💳 <b>Ваш баланс:</b> {format_bold_num(u['earned'])}\n"
        f"💵 <b>Минимальная ставка:</b> {format_bold_num(MIN_DICE_PVP_BET)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )
    if other_lobbies:
        t += f"📋 <b>Доступные столы других игроков ({fmt_o(len(other_lobbies))}):</b>\n<i>Нажмите на дуэль, чтобы принять вызов:</i>"
    else:
        t += "<i>Сейчас нет открытых столов от других игроков. Создайте свой стол кнопкой выше!</i>"

    try: await callback.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception: await callback.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    try: await callback.answer()
    except Exception: pass

@router.callback_query(F.data == "dice_pvp_my_list")
async def cb_dice_pvp_my_list(callback: types.CallbackQuery, state: Optional[FSMContext] = None):
    if state is not None:
        await state.clear()
    uid = callback.from_user.id
    my_lobbies = await get_user_open_pvp_lobbies(uid)

    kb = []
    if my_lobbies:
        for lid, _, _, bet, _ in my_lobbies:
            kb.append([InlineKeyboardButton(text=f"👑 Дуэль №{fmt_o(lid)} — {format_raw_num(bet)}", callback_data=f"dice_pvp_my_lobby:{lid}")])

    kb.append([InlineKeyboardButton(text="➕ Создать ещё дуэль", callback_data="dice_pvp_create")])
    kb.append([InlineKeyboardButton(text="◀️ К общему списку столов", callback_data="btn_dice_pvp_menu")])

    t = (
        "👑 <b>МОИ ОТКРЫТЫЕ ДУЭЛИ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )
    if my_lobbies:
        t += f"У вас открыто столов: <b>{fmt_o(len(my_lobbies))}</b> из 3.\n\nНажмите на нужную дуэль, чтобы скопировать ссылку для друга или отменить её:"
    else:
        t += "У вас пока нет активных открытых столов.\nНажмите «Создать дуэль», чтобы открыть свой стол!"

    try: await callback.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    except Exception: await callback.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
    try: await callback.answer()
    except Exception: pass

# --- ИНТЕРАКТИВНЫЙ КОНСТРУКТОР СТАВКИ PvP ДУЭЛИ ---
async def process_create_pvp_duel(target: types.Message | types.CallbackQuery, uid: int, amt: float, bot: Bot):
    u = await get_and_update_user(uid)
    if amt < MIN_DICE_PVP_BET or amt > MAX_DICE_PVP_BET or amt > u['earned']:
        err_text = "❌ Недостаточно средств на балансе прибыли!"
        if isinstance(target, types.CallbackQuery): return await target.answer(err_text, show_alert=True)
        return await target.answer(err_text, parse_mode="HTML")

    c_name = target.from_user.first_name or f"ID {uid}"
    ok, text, lid = await create_dice_pvp_lobby(uid, c_name, amt)
    if not ok:
        if isinstance(target, types.CallbackQuery): return await target.answer(text, show_alert=True)
        return await target.answer(f"❌ {text}", parse_mode="HTML")

    b = await bot.get_me()
    invite_link = f"https://t.me/{b.username}?start=pvp_{lid}"
    t_success = (
        f"✅ <b>Дуэль №{fmt_o(lid)} успешно создана!</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Ставка:</b> {format_bold_num(amt)}\n\n"
        f"🔗 <b>Ссылка для приглашения друга:</b>\n<code>{invite_link}</code>\n\n"
        "⏳ <i>Ожидаем подключения оппонента в лобби...</i>"
    )
    kb_success = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 В лобби дуэлей", callback_data="btn_dice_pvp_menu")]
    ])
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(t_success, reply_markup=kb_success, parse_mode="HTML")
        await target.answer("Дуэль создана!")
    else:
        await target.answer(t_success, reply_markup=kb_success, parse_mode="HTML")

def get_dice_pvp_create_builder_kb(cur_bet: float) -> InlineKeyboardMarkup:
    confirm_text = f"⚔️ Создать дуэль ({format_raw_num(cur_bet)})" if cur_bet >= MIN_DICE_PVP_BET else "⚔️ Создать дуэль"
    confirm_cb = f"dice_pvp_do_create:{cur_bet}" if cur_bet >= MIN_DICE_PVP_BET else "dice_pvp_zero"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="+5Oк", callback_data=f"dice_pvp_b_add:{cur_bet}:50000"),
            InlineKeyboardButton(text="+1OOk", callback_data=f"dice_pvp_b_add:{cur_bet}:100000"),
            InlineKeyboardButton(text="+25Ok", callback_data=f"dice_pvp_b_add:{cur_bet}:250000")
        ],
        [
            InlineKeyboardButton(text="+5OOk", callback_data=f"dice_pvp_b_add:{cur_bet}:500000"),
            InlineKeyboardButton(text="+1кк", callback_data=f"dice_pvp_b_add:{cur_bet}:1000000"),
            InlineKeyboardButton(text="+5кк", callback_data=f"dice_pvp_b_add:{cur_bet}:5000000")
        ],
        [
            InlineKeyboardButton(text="➗2", callback_data=f"dice_pvp_b_div:{cur_bet}"),
            InlineKeyboardButton(text="✖️2", callback_data=f"dice_pvp_b_mul:{cur_bet}"),
            InlineKeyboardButton(text="🗑 Сброс", callback_data="dice_pvp_b_reset")
        ],
        [InlineKeyboardButton(text="✏️ Ввести сумму вручную", callback_data="dice_pvp_custom_input")],
        [InlineKeyboardButton(text=confirm_text, callback_data=confirm_cb)],
        [InlineKeyboardButton(text="◀️ К списку дуэлей", callback_data="btn_dice_pvp_menu")]
    ])

def render_dice_pvp_create_text(user_earned: float, cur_bet: float) -> str:
    total_bank = round(cur_bet * 2.0, 2)
    bank_str = f"{format_bold_num(total_bank)}" if cur_bet >= MIN_DICE_PVP_BET else "O$"
    bet_str = f"{format_bold_num(cur_bet)}" if cur_bet >= MIN_DICE_PVP_BET else "O$"
    return (
        "⚔️ <b>СОЗДАНИЕ PvP ДУЭЛИ</b> ⚔️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💳 <b>Ваш баланс:</b> {format_bold_num(user_earned)}\n"
        f"💵 <b>Лимиты ставок:</b> от {format_bold_num(MIN_DICE_PVP_BET)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Выбранная ставка:</b> {bet_str} (Банк: {bank_str})\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Добавляйте сумму кнопками, удваивайте или нажмите создать:</b>"
    )

@router.callback_query(F.data == "dice_pvp_create")
async def cb_dice_pvp_create(callback: types.CallbackQuery, state: FSMContext):
    if state is not None: await state.clear()
    uid = callback.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if not await is_game_module_enabled("dice_pvp"):
        return await callback.answer("⚠️ PvP Дуэли временно отключены на техобслуживание!", show_alert=True)

    u = await get_and_update_user(uid)
    cur_bet = 0.0
    t = render_dice_pvp_create_text(u['earned'], cur_bet)
    kb = get_dice_pvp_create_builder_kb(cur_bet)
    await callback.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@router.callback_query(F.data.startswith("dice_pvp_b_add:"))
async def cb_dice_pvp_b_add(call: types.CallbackQuery):
    parts = call.data.split(":")
    cur_bet = float(parts[1])
    add_val = float(parts[2])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = min(MAX_DICE_PVP_BET, round(cur_bet + add_val, 2))
    t = render_dice_pvp_create_text(u['earned'], new_bet)
    kb = get_dice_pvp_create_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"+{format_raw_num(add_val)} -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("dice_pvp_b_mul:"))
async def cb_dice_pvp_b_mul(call: types.CallbackQuery):
    cur_bet = float(call.data.split(":")[1])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    base_bet = cur_bet if cur_bet >= MIN_DICE_PVP_BET else MIN_DICE_PVP_BET
    new_bet = min(MAX_DICE_PVP_BET, round(base_bet * 2.0, 2))
    t = render_dice_pvp_create_text(u['earned'], new_bet)
    kb = get_dice_pvp_create_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"✖️2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data.startswith("dice_pvp_b_div:"))
async def cb_dice_pvp_b_div(call: types.CallbackQuery):
    cur_bet = float(call.data.split(":")[1])
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = max(MIN_DICE_PVP_BET, round(cur_bet / 2.0, 2))
    t = render_dice_pvp_create_text(u['earned'], new_bet)
    kb = get_dice_pvp_create_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer(f"➗2 -> {format_raw_num(new_bet)}")

@router.callback_query(F.data == "dice_pvp_b_reset")
async def cb_dice_pvp_b_reset(call: types.CallbackQuery):
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    new_bet = 0.0
    t = render_dice_pvp_create_text(u['earned'], new_bet)
    kb = get_dice_pvp_create_builder_kb(new_bet)
    try: await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception: pass
    await call.answer("Ставка сброшена!")

@router.callback_query(F.data == "dice_pvp_zero")
async def cb_dice_pvp_zero(call: types.CallbackQuery):
    await call.answer("⚠️ Сначала выберите сумму ставки кнопками выше!", show_alert=True)

@router.callback_query(F.data.startswith("dice_pvp_do_create:"))
async def cb_dice_pvp_do_create(callback: types.CallbackQuery, bot: Bot, state: FSMContext):
    if state is not None: await state.clear()
    uid = callback.from_user.id
    amt = float(callback.data.split(":")[1])
    u = await get_and_update_user(uid)
    if amt < MIN_DICE_PVP_BET:
        return await callback.answer(f"⚠️ Минимальная ставка для дуэли: {format_raw_num(MIN_DICE_PVP_BET)}!", show_alert=True)
    if amt > MAX_DICE_PVP_BET:
        return await callback.answer(f"⚠️ Максимальная ставка: {format_raw_num(MAX_DICE_PVP_BET)}!", show_alert=True)
    if u['earned'] < amt:
        return await callback.answer("❌ Недостаточно средств на балансе чистой прибыли!", show_alert=True)
    await process_create_pvp_duel(callback, uid, amt, bot)

@router.callback_query(F.data == "dice_pvp_custom_input")
async def cb_dice_pvp_custom_input(callback: types.CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="dice_pvp_create")]])
    await callback.message.edit_text(f"{EMOJIS['pencil']} <b>Введите ставку от {format_bold_num(MIN_DICE_PVP_BET)} до {format_bold_num(MAX_DICE_PVP_BET)} (например: 1OOk, 1kk, 5kk, all):</b>", reply_markup=cancel_kb, parse_mode="HTML")
    await state.set_state(DicePvPState.waiting_for_amount)
    await callback.answer()

@router.message(DicePvPState.waiting_for_amount)
async def state_dice_pvp_custom_amount(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        from handlers.user import handle_menu_redirection
        return await handle_menu_redirection(msg, state, bot)

    uid = msg.from_user.id
    u = await get_and_update_user(uid)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="dice_pvp_create")]])
    try:
        amt = parse_amount(msg.text, max_val=min(u['earned'], MAX_DICE_PVP_BET))
        if amt < MIN_DICE_PVP_BET or amt > MAX_DICE_PVP_BET:
            return await msg.answer(f"❌ <b>Ставка должна быть от {format_bold_num(MIN_DICE_PVP_BET)} до {format_bold_num(MAX_DICE_PVP_BET)}!</b>", reply_markup=cancel_kb, parse_mode="HTML")
        if amt > u['earned']:
            return await msg.answer("❌ <b>Недостаточно средств на балансе прибыли!</b>", reply_markup=cancel_kb, parse_mode="HTML")

        await state.clear()
        t = render_dice_pvp_create_text(u['earned'], amt)
        kb = get_dice_pvp_create_builder_kb(amt)
        await msg.answer(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer("❌ <b>Неверный формат!</b> Введите число (например: 1OOk, 1kk, 5kk, all):", reply_markup=cancel_kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("dice_pvp_my_lobby:"))
async def cb_dice_pvp_my_lobby(callback: types.CallbackQuery, bot: Bot):
    lid = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    lobby = await get_dice_pvp_lobby_by_id(lid)
    if not lobby or lobby[4] != "OPEN" or int(lobby[1]) != int(uid):
        try:
            await callback.answer("❌ Дуэль уже не активна!", show_alert=True)
        except Exception:
            pass
        return await cb_dice_pvp_menu(callback, None)

    bet = lobby[3]
    username = await get_bot_username(bot)
    invite_link = f"https://t.me/{username}?start=pvp_{lid}"

    t = (
        f"⚔️ <b>УПРАВЛЕНИЕ ДУЭЛЬЮ №{fmt_o(lid)}</b> ⚔️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Ваша ставка:</b> {format_bold_num(bet)}\n"
        f"💰 <b>Банк дуэли:</b> {format_bold_num(bet * 2)}\n"
        "📊 <b>Комиссия стола:</b> 3.33%\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Ссылка для приглашения друга:</b>\n<code>{invite_link}</code>\n\n"
        "⏳ <i>Ожидаем подключения оппонента в лобби...</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить дуэль (Возврат)", callback_data=f"dice_pvp_cancel:{lid}")],
        [InlineKeyboardButton(text="◀️ Назад к моим дуэлям", callback_data="dice_pvp_my_list")],
        [InlineKeyboardButton(text="📋 К общему списку столов", callback_data="btn_dice_pvp_menu")]
    ])
    try:
        await callback.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(t, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass

@router.callback_query(F.data.startswith("dice_pvp_cancel:"))
async def cb_dice_pvp_cancel(callback: types.CallbackQuery):
    lid = int(callback.data.split(":")[1])
    ok, text = await cancel_dice_pvp_lobby(lid, callback.from_user.id)
    await callback.answer(text, show_alert=True)
    await cb_dice_pvp_menu(callback, None)

# --- ПРОВЕДЕНИЕ ДУЭЛИ: ПОДТВЕРЖДЕНИЕ ВХОДА (ЗАЩИТА ОТ МИСКЛИКОВ) ---
@router.callback_query(F.data.startswith("dice_pvp_ask_join:") | F.data.startswith("dice_pvp_join:"))
async def cb_dice_pvp_ask_join(callback: types.CallbackQuery):
    lid = int(callback.data.split(":")[1])
    uid = callback.from_user.id
    u = await get_and_update_user(uid)

    lobby = await get_dice_pvp_lobby_by_id(lid)
    if not lobby or lobby[4] != "OPEN":
        await callback.answer("❌ Дуэль уже недоступна или завершена!", show_alert=True)
        return await cb_dice_pvp_menu(callback, None)

    c_id, c_name, bet = lobby[1], lobby[2], lobby[3]
    if int(c_id) == int(uid):
        await callback.answer("❌ Это ваша собственная дуэль!", show_alert=True)
        return await cb_dice_pvp_menu(callback, None)

    if u['earned'] < bet:
        return await callback.answer(f"❌ Недостаточно средств на балансе прибыли! Ставка: {format_raw_num(bet)}", show_alert=True)

    clean_cname = html.escape(c_name or f"ID {c_id}")
    t = (
        f"⚔️ <b>ПОДТВЕРЖДЕНИЕ ВХОДА В ДУЭЛЬ №{fmt_o(lid)}</b> ⚔️\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Создатель стола:</b> <b>{clean_cname}</b>\n"
        f"💵 <b>Ваша ставка:</b> {format_bold_num(bet)}\n"
        f"💰 <b>Общий банк дуэли:</b> {format_bold_num(bet * 2)}\n"
        "📊 <b>Комиссия стола:</b> <b>3.33%</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>Сумма {format_bold_num(bet)} спишется с вашего баланса чистой прибыли.</i>\n\n"
        "<b>Вы уверены, что хотите бросить кости?</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎲 Да, бросить кости ({format_raw_num(bet)})", callback_data=f"dice_pvp_confirm_join:{lid}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="btn_dice_pvp_menu")]
    ])
    try:
        await callback.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await callback.message.answer(t, reply_markup=kb, parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass

@router.callback_query(F.data.startswith("dice_pvp_confirm_join:"))
async def cb_dice_pvp_confirm_join(callback: types.CallbackQuery, bot: Bot):
    lid = int(callback.data.split(":")[1])
    opp_uid = callback.from_user.id
    opp_name = callback.from_user.first_name or f"ID {opp_uid}"

    u = await get_and_update_user(opp_uid)
    
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT creator_id, creator_name, bet, status FROM dice_pvp_lobbies WHERE id = ?", (lid,)) as cur:
                lobby = await cur.fetchone()

            if not lobby or lobby[3] != "OPEN":
                await db.execute("ROLLBACK")
                await callback.answer("❌ Дуэль уже недоступна или завершена!", show_alert=True)
                return await cb_dice_pvp_menu(callback, None)

            c_id, c_name, bet, _ = lobby
            if int(c_id) == int(opp_uid):
                await db.execute("ROLLBACK")
                await callback.answer("❌ Это ваша собственная дуэль! В неё должен зайти второй игрок со своего аккаунта.", show_alert=True)
                return await cb_dice_pvp_menu(callback, None)

            from database.database import get_user_transfer_hold_amount
            opp_hold = await get_user_transfer_hold_amount(opp_uid)
            opp_avail = max(0.0, round(u['earned'] - opp_hold, 2))
            if opp_avail < bet:
                await db.execute("ROLLBACK")
                hold_msg = f" (часть средств {format_raw_num(opp_hold)} на защитном холде 12ч)" if opp_hold > 0 else ""
                return await callback.answer(f"❌ Недостаточно доступных средств{hold_msg}! Доступно: {format_raw_num(opp_avail)}", show_alert=True)

            cur_opp = await db.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?", (bet, opp_uid, bet))
            if cur_opp.rowcount == 0:
                await db.execute("ROLLBACK")
                return await callback.answer("❌ Ошибка баланса прибыли!", show_alert=True)

            await db.execute("UPDATE dice_pvp_lobbies SET status = 'PLAYING', opponent_id = ? WHERE id = ?", (opp_uid, lid))
            await db.commit()
        except Exception:
            return await callback.answer("❌ Ошибка при входе в дуэль!", show_alert=True)

    await callback.answer("Дуэль началась!")
    clean_cname = html.escape(c_name or f"ID {c_id}")
    clean_oppname = html.escape(opp_name or f"ID {opp_uid}")

    try:
        await callback.message.edit_text(
            f"⚔️ <b>Дуэль №{fmt_o(lid)} началась!</b>\n"
            f"👤 {clean_cname} ⚔️ {clean_oppname}\n"
            f"💵 Банк: {format_bold_num(bet * 2)}\n\n"
            f"🎲 <i>Бросаем кости...</i>",
            parse_mode="HTML"
        )
    except Exception:
        pass

    clean_cname = html.escape(c_name or f"ID {c_id}")
    clean_oppname = html.escape(opp_name or f"ID {opp_uid}")

    d1_val = None
    d2_val = None

    try:
        # 1. Первым бросает Создатель стола (2 кубика)
        try:
            await bot.send_message(
                chat_id=c_id,
                text=(
                    f"⚔️ <b>Оппонент {clean_oppname} принял ваш вызов в дуэли №{fmt_o(lid)}!</b>\n"
                    f"💰 <b>Банк:</b> {format_bold_num(bet * 2)}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🎲 <b>ВАШ ХОД! Бросаем 2 кубика:</b>"
                ),
                parse_mode="HTML"
            )
            d1_m1 = await bot.send_dice(chat_id=c_id, emoji="🎲")
            d1_m2 = await bot.send_dice(chat_id=c_id, emoji="🎲")
            d1_1 = d1_m1.dice.value
            d1_2 = d1_m2.dice.value
            d1_val = d1_1 + d1_2
        except Exception:
            import random
            d1_1 = random.randint(1, 6)
            d1_2 = random.randint(1, 6)
            d1_val = d1_1 + d1_2

        try:
            await bot.send_message(
                chat_id=opp_uid,
                text=(
                    f"⚔️ <b>Дуэль №{fmt_o(lid)} началась!</b>\n"
                    f"👤 {clean_cname} ⚔️ {clean_oppname}\n"
                    f"💰 <b>Банк:</b> {format_bold_num(bet * 2)}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"🎲 <i>Ожидаем бросок от {clean_cname} (2 кубика)...</i>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        # Ждем завершения анимации броска кубиков Создателя
        await asyncio.sleep(3.8)

        # 2. Интрига: объявляем результат Создателя обоим игрокам
        try:
            await bot.send_message(
                chat_id=c_id,
                text=(
                    f"🎯 <b>Вы выбросили: {d1_1} + {d1_2} = {d1_val} 🎲!</b>\n"
                    f"⏳ <i>Теперь бросает {clean_oppname}... Момент истины!</i>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        if d1_val < 12:
            target_hint = f"Вам нужно набрать от {d1_val + 1} очков для победы (или {d1_val} для ничьей)!"
        else:
            target_hint = "Создатель выбил максимум (12)! Вам нужно 12 для ничьей!"

        try:
            await bot.send_message(
                chat_id=opp_uid,
                text=(
                    f"🎯 <b>{clean_cname} выбросил: {d1_1} + {d1_2} = {d1_val} 🎲!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ <i>{target_hint}</i>\n\n"
                    "🎲 <b>ТЕПЕРЬ ВАШ ХОД! Бросаем 2 кубика:</b>"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        # 3. Вторым бросает Оппонент (2 кубика)
        try:
            d2_m1 = await bot.send_dice(chat_id=opp_uid, emoji="🎲")
            d2_m2 = await bot.send_dice(chat_id=opp_uid, emoji="🎲")
            d2_1 = d2_m1.dice.value
            d2_2 = d2_m2.dice.value
            d2_val = d2_1 + d2_2
        except Exception:
            import random
            d2_1 = random.randint(1, 6)
            d2_2 = random.randint(1, 6)
            d2_val = d2_1 + d2_2

        try:
            await bot.send_message(
                chat_id=c_id,
                text=f"🎲 <i>{clean_oppname} бросает свои 2 кубика... Считаем результат!</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

        # Ждем завершения анимации броска кубиков Оппонента
        await asyncio.sleep(3.8)

    except Exception as net_err:
        await rollback_dice_pvp_match(lid, str(net_err))
        try: await bot.send_message(c_id, "⚠️ <b>Дуэль отменена из-за ошибки связи. Ставка возвращена на баланс.</b>", parse_mode="HTML")
        except Exception: pass
        try: await bot.send_message(opp_uid, "⚠️ <b>Дуэль отменена из-за ошибки связи. Ставка возвращена на баланс.</b>", parse_mode="HTML")
        except Exception: pass
        return

    # 4. Фиксация результата матча с точными очками из Telegram
    ok, msg_text, res = await play_dice_pvp_match(lid, opp_uid, opp_name, d1_val, d2_val)
    if not ok:
        await bot.send_message(chat_id=opp_uid, text=f"❌ {msg_text}")
        return

    if res["is_draw"]:
        draw_msg = (
            f"⚔️ <b>РЕЗУЛЬТАТ PvP ДУЭЛИ №{fmt_o(lid)}</b> ⚔️\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 {clean_cname}: {d1_1} + {d1_2} = <b>{d1_val} 🎲</b>\n"
            f"👤 {clean_oppname}: {d2_1} + {d2_2} = <b>{d2_val} 🎲</b>\n"
            f"💵 <b>Банк дуэли:</b> {format_bold_num(bet * 2)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🤝 <b>БОЕВАЯ НИЧЬЯ! Очки равны ({d1_val} : {d2_val})!</b>\n"
            f"💰 <i>Ставки {format_bold_num(bet)} возвращены обоим игрокам 1OO%!</i>"
        )
        try: await bot.send_message(c_id, draw_msg, parse_mode="HTML")
        except Exception: pass
        try: await bot.send_message(opp_uid, draw_msg, parse_mode="HTML")
        except Exception: pass
    else:
        winner_name = res["winner_name"]
        win_net = res["win_net"]
        winner_id = res["winner_id"]
        loser_id = res["loser_id"]

        win_msg = (
            "🎉 <b>ВЫ ПОБЕДИЛИ В ДУЭЛИ!</b> 🎆\n\n"
            f"⚔️ <b>РЕЗУЛЬТАТ PvP ДУЭЛИ №{fmt_o(lid)}</b> ⚔️\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 {clean_cname}: {d1_1} + {d1_2} = <b>{d1_val} 🎲</b>\n"
            f"👤 {clean_oppname}: {d2_1} + {d2_2} = <b>{d2_val} 🎲</b>\n"
            f"💵 <b>Банк дуэли:</b> {format_bold_num(bet * 2)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> <b>{winner_name}</b> 🎉\n"
            f"💰 <b>Выигрыш:</b> <b>+{format_bold_num(win_net)}</b> 🍾 <i>уже у вас на балансе</i>"
        )

        loss_msg = (
            "😔 <b>ПОРАЖЕНИЕ В ДУЭЛИ</b>\n\n"
            f"⚔️ <b>РЕЗУЛЬТАТ PvP ДУЭЛИ №{fmt_o(lid)}</b> ⚔️\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 {clean_cname}: {d1_1} + {d1_2} = <b>{d1_val} 🎲</b>\n"
            f"👤 {clean_oppname}: {d2_1} + {d2_2} = <b>{d2_val} 🎲</b>\n"
            f"💵 <b>Банк дуэли:</b> {format_bold_num(bet * 2)}\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏆 <b>ПОБЕДИТЕЛЬ:</b> <b>{winner_name}</b>\n"
            f"📉 <b>Проигрыш:</b> -{format_bold_num(bet)}\n"
            "🍀 <i>Не расстраивайтесь! Возьмите реванш в лобби!</i>"
        )

        total_bank = bet * 2
        if total_bank >= 10_000_000.0:
            w_str = str(winner_id)
            w_masked = f"ID {w_str[:3]}•••" if len(w_str) >= 3 else f"ID {w_str}"
            pvp_post = (
                f"⚔️ <b>КРУПНАЯ ПОБЕДА В PvP ДУЭЛИ!</b> 🎲\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Победитель:</b> <b>{w_masked}</b>\n"
                f"💰 <b>Выигранный банк:</b> <b>+{format_bold_num(win_net)}</b>\n"
                f"💵 <b>Общий банк дуэли:</b> {format_bold_num(total_bank)}\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🎮 <b>Бросай вызов живым игрокам:</b> @NemosTrade_Bot"
            )
            await send_review_channel_post(bot, pvp_post)

        try: await bot.send_message(winner_id, win_msg, parse_mode="HTML", message_effect_id=EFFECT_CONFETTI_ID)
        except Exception: await bot.send_message(winner_id, win_msg, parse_mode="HTML")

        try: await bot.send_message(loser_id, loss_msg, parse_mode="HTML")
        except Exception: pass

@router.callback_query(F.data.startswith("dice_pve_set_bet:"))
async def cb_dice_pve_set_bet(call: types.CallbackQuery, bot: Bot):
    amt = call.data.split(":")[1]
    call.data = f"dice_play_pve_go:{amt}"
    return await cb_dice_play_pve_go(call, bot)

@router.callback_query(F.data == "adm_peek_mines")
async def cb_adm_peek_mines(call: types.CallbackQuery):
    uid = call.from_user.id
    if not await is_admin(uid):
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)

    game = await get_user_active_mines_game(uid)
    if not game or game.get("status") != "ACTIVE":
        return await call.answer("⚠️ Нет активной игры в минёра!", show_alert=True)

    mines = sorted(game.get("mines_positions", []))
    opened = set(game.get("opened_cells", []))
    safe_cells = [i for i in range(25) if i not in mines and i not in opened]

    grid_lines = []
    for r in range(5):
        row_icons = []
        for c in range(5):
            idx = r * 5 + c
            if idx in mines:
                row_icons.append("💣")
            elif idx in opened:
                row_icons.append("💎")
            else:
                row_icons.append("▫️")
        grid_lines.append(" ".join(row_icons))
    grid_str = "\n".join(grid_lines)

    mine_nums = ", ".join(str(m + 1) for m in mines)
    safe_nums = ", ".join(str(s + 1) for s in safe_cells)

    # 1. Компактный alert до 200 символов (исключает ошибку MESSAGE_TOO_LONG)
    alert_text = f"💣 Мины (1-25): {mine_nums}\n💎 Безопасных: {len(safe_cells)} шт.\n\nКарта отправлена в чат 👇"
    try:
        await call.answer(alert_text, show_alert=True)
    except Exception:
        await call.answer(f"💣 Мины: {mine_nums}", show_alert=True)

    # 2. Полная визуальная схема 5x5 отправляется в чат
    chat_map = (
        "🕵️ <b>КАРТА МИН (АДМИН-РЕЖИМ)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{grid_str}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💣 <b>Мины (ячейки 1-25):</b> <code>{mine_nums}</code>\n"
        f"💎 <b>Безопасные клетки ({len(safe_cells)} шт.):</b>\n"
        f"<code>{safe_nums}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Клетки нумеруются с 1 по 25 слева направо.</i>"
    )
    try:
        await call.message.answer(chat_map, parse_mode="HTML")
    except Exception:
        pass

@router.callback_query(F.data == "adm_autoclear_mines")
async def cb_adm_autoclear_mines(call: types.CallbackQuery):
    uid = call.from_user.id
    if not await is_admin(uid):
        return await call.answer("⛔ Доступ запрещен!", show_alert=True)

    game = await get_user_active_mines_game(uid)
    if not game or game.get("status") != "ACTIVE":
        return await call.answer("⚠️ Нет активной игры в минёра!", show_alert=True)

    mines = set(game.get("mines_positions", []))
    opened = set(game.get("opened_cells", []))
    safe_cells = [i for i in range(25) if i not in mines and i not in opened]

    if not safe_cells:
        return await call.answer("⚠️ Все безопасные ячейки уже открыты!", show_alert=True)

    for cell_idx in safe_cells[:-1]:
        await step_mines_game(uid, cell_idx)

    last_cell = safe_cells[-1]
    await process_mine_step(call, uid, last_cell)
