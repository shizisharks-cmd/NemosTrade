from typing import Tuple, List, Optional
import os
import html
import asyncio
import sqlite3
import aiosqlite
from aiogram import Router, F, types, Bot
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup, FSInputFile
from config.config import (
    CHANNEL_URL, CHANNEL_USERNAME, REVIEWS_CHANNEL, BANNER_FILE, ADMIN_CHAT_ID, OWNER_ID,
    REFERRAL_BONUS_INVITER, REFERRAL_BONUS_INVITED, MIN_DEPOSIT, MIN_WITHDRAW, MIN_REINVEST, MIN_TRANSFER,
    TRANSFER_FEE_PERCENT, OKA_GOS_PRICE, EFFECT_CONFETTI_ID, EMOJIS,
    fmt_o, format_bold_num, format_raw_num, parse_amount, format_datetime_display,
    is_menu_trigger
)
from database.database import (
    claim_daily_bonus, can_claim_daily_bonus,
    get_and_update_user, update_user_manual, is_admin, is_user_banned,
    is_maintenance_active, is_withdrawals_frozen, add_tx, execute_p2p_transfer,
    execute_reinvest, execute_withdrawal_creation, update_tx_admin_message,
    process_referral_reward, get_db_connection, get_recent_transfer_recipients,
    get_user_transactions_filtered, get_user_transfer_history, has_active_withdrawal, get_user_transfer_hold_amount, is_game_module_enabled
)
from services.game_service import get_dice_pvp_lobby_by_id
from services.tasks import check_user_subscription

router = Router()

class CalcState(StatesGroup):
    waiting_for_amount = State()

class WithdrawFlow(StatesGroup):
    waiting_for_amount = State()
    waiting_for_proof = State()
    confirm_withdraw = State()

class PromoState(StatesGroup):
    waiting_for_promo_code = State()

class TransferFlow(StatesGroup):
    waiting_for_recipient = State()
    waiting_for_amount = State()
    confirm_transfer = State()

class ReinvestFlow(StatesGroup):
    waiting_for_amount = State()

def get_main_keyboard(is_adm: bool = False):
    kb = [
        [KeyboardButton(text="👤 Личный кабинет")],
        [KeyboardButton(text="🎮 Игры"), KeyboardButton(text="🎁 Промокод")],
        [KeyboardButton(text="👥 Партнерам"), KeyboardButton(text="💬 Поддержка")],
    ]
    if is_adm:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_profile_kb(can_bonus: bool = False) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="📥 Пополнить", callback_data="btn_deposit"), InlineKeyboardButton(text="📤 Вывести", callback_data="btn_withdraw")],
        [InlineKeyboardButton(text="🔄 Реинвест", callback_data="btn_reinvest"), InlineKeyboardButton(text="💸 Перевести", callback_data="btn_transfer")],
    ]
    if can_bonus:
        kb.append([InlineKeyboardButton(text="🎁 Ежедневный бонус", callback_data="btn_daily_bonus")])
    kb.extend([
        [InlineKeyboardButton(text="🧮 Калькулятор", callback_data="btn_calc"), InlineKeyboardButton(text="📜 История", callback_data="hist_cat:all:0")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def format_calc_result(amt: float) -> str:
    amt = round(amt, 2)
    return (
        f"{EMOJIS['calc']} <b>Расчет прибыли (+3% в день)</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{EMOJIS['dollar']} <b>Сумма вклада:</b> {format_bold_num(amt)}\n\n"
        f"• 📅 <b>1 день:</b> <b>+{format_bold_num(amt * 0.03)}</b>\n\n"
        f"• 📅 <b>7 дней (неделя):</b> <b>+{format_bold_num(amt * 0.03 * 7)}</b>\n\n"
        f"• 📅 <b>3O дней (месяц):</b> <b>+{format_bold_num(amt * 0.03 * 30)}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )

# --- ГЛАВНОЕ МЕНЮ И СТАРТ (DEEP-LINK PVP ДУЭЛЕЙ) ---
@router.message(CommandStart())
async def cmd_start(msg: types.Message, command: CommandObject, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = msg.from_user.id
    uname = msg.from_user.username or ""

    if await is_user_banned(uid):
        return await msg.answer(f"{EMOJIS['stop']} <b>Вы заблокированы в системе бота.</b>", parse_mode="HTML")

    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm:
        return await msg.answer(f"{EMOJIS['gear']} <b>Ведутся плановые технические работы.</b>\n\n<b>Все начисления продолжают идти в штатном режиме. Бот скоро вернется к работе!</b>", parse_mode="HTML")

    # Переход по персональной ссылке (?start=pvp_123)
    if command.args and command.args.startswith("pvp_"):
        try:
            lid = int(command.args.replace("pvp_", ""))
            lobby = await get_dice_pvp_lobby_by_id(lid)
            if lobby and lobby[4] == "OPEN":
                c_id, c_name, bet = lobby[1], lobby[2], lobby[3]
                if int(c_id) != int(uid):
                    await get_and_update_user(uid, uname=uname)
                    c_display = html.escape(c_name or f"ID {c_id}")
                    t_invite = (
                        f"⚔️ <b>ВЫЗОВ НА PvP ДУЭЛЬ В КОСТИ №{fmt_o(lid)}</b> ⚔️\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>Создатель:</b> <b>{c_display}</b>\n"
                        f"💵 <b>Ставка:</b> {format_bold_num(bet)}\n"
                        f"💰 <b>Общий банк:</b> {format_bold_num(bet * 2)}\n"
                        "📊 <b>Комиссия стола:</b> <b>3.33%</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "<b>Вы готовы принять вызов и бросить кости?</b>"
                    )
                    kb_invite = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"⚔️ Принять вызов ({format_raw_num(bet)})", callback_data=f"dice_pvp_ask_join:{lid}")],
                        [InlineKeyboardButton(text="📋 Список всех дуэлей", callback_data="btn_dice_pvp_menu")]
                    ])
                    return await msg.answer(t_invite, reply_markup=kb_invite, parse_mode="HTML")
        except Exception:
            pass
    ref_id = int(command.args) if (command.args and command.args.isdigit()) else None
    user_info = await get_and_update_user(uid, ref_id=ref_id, uname=uname)

    if not await check_user_subscription(bot, uid, CHANNEL_USERNAME):
        sub_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
        ])
        return await msg.answer(f"{EMOJIS['lock']} <b>Доступ ограничен!</b>\n\n<b>Для использования функций бота и получения бонусов необходимо подписаться на наш новостной канал.</b>\n\n👉 <b>Подпишитесь и нажмите кнопку ниже:</b>", reply_markup=sub_kb, parse_mode="HTML")

    # Начисление бонуса за реферала только после подтверждения подписки
    await process_referral_reward(uid, bot=bot)

    w_text = (
        "🚀 <b>Добро пожаловать в Nemos Trade!</b>\n\n"
        "🎮 <b>Автоматизированная инвестиционная платформа в мире Grand Mobile.</b>\n\n"
        "📊 <b>Условия проекта:</b>\n\n"
        "• 📈 <b>Доходность:</b> <b>+3% в день</b> (начисление каждую секунду)\n\n"
        f"• 🎁 <b>Реферальный бонус:</b> {format_bold_num(REFERRAL_BONUS_INVITER)} <b>за друга</b> (другу {format_bold_num(REFERRAL_BONUS_INVITED)})\n\n"
        f"• 💳 <b>Минимальный вывод:</b> {format_bold_num(MIN_WITHDRAW)}\n\n"
        "👇 <b>Выберите нужный раздел в меню ниже:</b>"
    )
    await msg.answer(w_text, reply_markup=get_main_keyboard(adm), parse_mode="HTML")

active_sub_checkers: set[int] = set()

@router.callback_query(F.data == "check_sub")
async def process_check_sub(call: types.CallbackQuery, bot: Bot):
    uid = call.from_user.id
    if await is_user_banned(uid): return

    if uid in active_sub_checkers:
        return await call.answer("⏳ Проверяем подписку...", show_alert=False)

    active_sub_checkers.add(uid)
    try:
        if await check_user_subscription(bot, uid, CHANNEL_USERNAME):
            try: await call.message.delete()
            except Exception: pass
            adm = await is_admin(uid)
            await process_referral_reward(uid, bot=bot)
            w_sub_text = (
                "🚀 <b>Добро пожаловать в Nemos Trade!</b>\n\n"
                "🎮 <b>Автоматизированная инвестиционная платформа в мире Grand Mobile.</b>\n\n"
                "📊 <b>Условия проекта:</b>\n\n"
                "• 📈 <b>Доходность:</b> <b>+3% в день</b> (начисление каждую секунду)\n\n"
                f"• 🎁 <b>Реферальный бонус:</b> {format_bold_num(REFERRAL_BONUS_INVITER)} <b>за друга</b> (другу {format_bold_num(REFERRAL_BONUS_INVITED)})\n\n"
                f"• 💳 <b>Минимальный вывод:</b> {format_bold_num(MIN_WITHDRAW)}\n\n"
                "👇 <b>Выберите нужный раздел в меню ниже:</b>"
            )
            await call.message.answer(f"{EMOJIS['check']} <b>Подписка подтверждена!</b>\n\n" + w_sub_text, reply_markup=get_main_keyboard(adm), parse_mode="HTML")
        else:
            await call.answer("❌ Вы еще не подписались на канал!", show_alert=True)
    finally:
        active_sub_checkers.discard(uid)

# --- ЛИЧНЫЙ КАБИНЕТ ---
def get_exact_profile_text(uid: int, invested: float, earned: float) -> str:
    hourly = (invested * 0.03) / 24.0
    return (
        f"🏛 <b>Ваш личный кабинет</b> 🏛\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Ваш ID:</b> <code>{uid}</code>\n\n"
        f"💼 <b>Капитал в обороте:</b>\n"
        f"↳ {fmt_o(invested)}$\n\n"
        f"💳 <b>Чистая прибыль:</b>\n"
        f"↳ {fmt_o(earned)}$\n\n"
        f"⏱ <b>Доход в час:</b> +{fmt_o(hourly)}$\n"
        f"📈 <b>Ставка доходности:</b> +3% / 24ч\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "⏱ <b>Начисление прибыли каждую секунду онлайн</b>"
    )

@router.message(F.text.func(lambda t: t and ("кабинет" in t.lower() or "профиль" in t.lower() or "👤" in t)))
async def show_profile(msg: types.Message, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = msg.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm:
        return await msg.answer(f"{EMOJIS['gear']} <b>Ведутся технические работы. Пожалуйста, подождите.</b>", parse_mode="HTML")
    if not await check_user_subscription(bot, uid, CHANNEL_USERNAME): return

    u = await get_and_update_user(uid, uname=msg.from_user.username or "")
    text = get_exact_profile_text(uid, u['invested'], u['earned'])
    can_b = await can_claim_daily_bonus(uid)
    prof_kb = get_profile_kb(can_bonus=can_b)
    if os.path.exists(BANNER_FILE):
        try:
            return await msg.answer_photo(photo=FSInputFile(BANNER_FILE), caption=text, reply_markup=prof_kb, parse_mode="HTML")
        except Exception:
            pass
    await msg.answer(text, reply_markup=prof_kb, parse_mode="HTML")

@router.callback_query(F.data == "btn_daily_bonus")
async def cb_daily_bonus(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm:
        return await call.answer("🛠 Ведутся техработы.", show_alert=True)

    success, bonus, remaining = await claim_daily_bonus(uid)
    if success:
        await call.answer("🎉 Бонус успешно получен!", show_alert=False)
        u = await get_and_update_user(uid)
        text = (
            "🎁<b>ЕЖЕДНЕВНЫЙ БОНУС ПОЛУЧЕН</b>🎁\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰<b>Вам начислено:</b> +<b>{fmt_o(bonus)}$</b> <b>на баланс прибыли!</b>\n\n"
            f"💳<b>Ваш текущий баланс прибыли:</b> <b>{fmt_o(u['earned'])}$</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⏱<b>Возвращайтесь ровно через 24 часа за следующей наградой!</b>"
        )
        back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В личный кабинет", callback_data="refresh_profile")]])
        try:
            if call.message.photo:
                await call.message.edit_caption(caption=text, reply_markup=back_kb, parse_mode="HTML")
            else:
                await call.message.edit_text(text, reply_markup=back_kb, parse_mode="HTML")
        except Exception:
            await call.message.answer(text, reply_markup=back_kb, parse_mode="HTML")
    else:
        if remaining:
            total_sec = int(remaining.total_seconds())
            hours, remainder = divmod(total_sec, 3600)
            mins, secs = divmod(remainder, 60)
            time_left = f"{hours}ч {mins}м {secs}с"
            await call.answer(f"⏳ Вы уже забирали бонус!\nСледующий через: {time_left}", show_alert=True)
        else:
            await call.answer("❌ Ошибка получения бонуса. Попробуйте позже.", show_alert=True)

@router.callback_query(F.data == "refresh_profile")
async def refresh_profile_callback(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    if await is_user_banned(uid): return
    u = await get_and_update_user(uid, uname=call.from_user.username or "")
    text = get_exact_profile_text(uid, u['invested'], u['earned'])
    can_b = await can_claim_daily_bonus(uid)
    prof_kb = get_profile_kb(can_bonus=can_b)
    try:
        if call.message.photo:
            await call.message.edit_caption(caption=text, reply_markup=prof_kb, parse_mode="HTML")
            await call.answer("⚡ Баланс успешно обновлен!")
        else:
            try:
                await call.message.delete()
            except Exception:
                pass
            if os.path.exists(BANNER_FILE):
                try:
                    await call.message.answer_photo(photo=FSInputFile(BANNER_FILE), caption=text, reply_markup=prof_kb, parse_mode="HTML")
                    await call.answer()
                    return
                except Exception:
                    pass
            await call.message.answer(text, reply_markup=prof_kb, parse_mode="HTML")
            await call.answer()
    except Exception:
        await call.answer("⚡ Данные актуальны")

# --- АТОМАРНЫЙ РЕИНВЕСТ ---
@router.callback_query(F.data == "btn_reinvest")
async def cb_reinvest_start(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    if u['earned'] < MIN_REINVEST:
        return await call.answer(f"⚠️ Минимальная сумма для реинвеста: {format_raw_num(MIN_REINVEST)}!", show_alert=True)

    val_25 = round(u['earned'] * 0.25, 2)
    val_50 = round(u['earned'] * 0.50, 2)
    val_75 = round(u['earned'] * 0.75, 2)
    val_100 = round(u['earned'], 2)

    kb_rows = []
    row_top = []
    if val_25 >= MIN_REINVEST:
        row_top.append(InlineKeyboardButton(text=f"25% ({format_raw_num(val_25)})", callback_data="reinvest_pct:0.25"))
    if val_50 >= MIN_REINVEST:
        row_top.append(InlineKeyboardButton(text=f"5O% ({format_raw_num(val_50)})", callback_data="reinvest_pct:0.5"))
    if row_top:
        kb_rows.append(row_top)

    row_mid = []
    if val_75 >= MIN_REINVEST:
        row_mid.append(InlineKeyboardButton(text=f"75% ({format_raw_num(val_75)})", callback_data="reinvest_pct:0.75"))
    if val_100 >= MIN_REINVEST:
        row_mid.append(InlineKeyboardButton(text=f"1OO% ({format_raw_num(val_100)})", callback_data="reinvest_pct:1.0"))
    if row_mid:
        kb_rows.append(row_mid)

    kb_rows.append([
        InlineKeyboardButton(text="✏️ Своя сумма / All", callback_data="reinvest_custom"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="refresh_profile")
    ])

    t = (
        f"🔄 <b>РЕИНВЕСТИРОВАНИЕ СРЕДСТВ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Переведите чистую прибыль обратно в депозит, чтобы увеличить ежедневный пассивный доход (+3%)!</b>\n\n"
        f"💳 <b>Доступно к реинвесту:</b> {format_bold_num(u['earned'])}\n"
        f"💵 <b>Минимум для реинвеста:</b> {format_bold_num(MIN_REINVEST)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Выберите сумму реинвеста кнопками или укажите свою:</b>"
    )
    try: await call.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    except Exception: await call.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("reinvest_pct:"))
async def cb_reinvest_pct(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    pct = float(call.data.split(":")[1])
    u = await get_and_update_user(uid)
    amt = round(u['earned'] * pct, 2)
    if amt < MIN_REINVEST:
        return await call.answer(f"⚠️ Минимальная сумма для реинвеста: {format_raw_num(MIN_REINVEST)}!", show_alert=True)
    if amt > u['earned']:
        return await call.answer("❌ Недостаточно средств!", show_alert=True)

    ok, msg_text = await execute_reinvest(uid, amt)
    if not ok:
        return await call.answer(f"❌ {msg_text}", show_alert=True)

    await call.message.edit_text(
        f"{EMOJIS['sparkles']} <b>Реинвест успешно выполнен!</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>Сумма:</b> {format_bold_num(amt)}\n📈 <b>Ваш депозит увеличен и уже приносит +3% в день!</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 В кабинет", callback_data="refresh_profile")]]),        parse_mode="HTML"    )
    await call.answer("Реинвест завершен!")

@router.callback_query(F.data == "reinvest_all")
async def cb_reinvest_all(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    amt = round(u['earned'], 2)
    if amt < MIN_REINVEST:
        return await call.answer(f"⚠️ Минимальная сумма для реинвеста: {format_raw_num(MIN_REINVEST)}!", show_alert=True)

    ok, msg_text = await execute_reinvest(uid, amt)
    if not ok:
        return await call.answer(f"❌ {msg_text}", show_alert=True)

    await call.message.edit_text(
        f"{EMOJIS['sparkles']} <b>Реинвест успешно выполнен!</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>Сумма:</b> {format_bold_num(amt)}\n📈 <b>Ваш депозит увеличен и уже приносит +3% в день!</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 В кабинет", callback_data="refresh_profile")]]),        parse_mode="HTML"    )
    await call.answer("Реинвест завершен!")

@router.callback_query(F.data == "reinvest_custom")
async def cb_reinvest_custom(call: types.CallbackQuery, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="refresh_profile")]])
    await call.message.edit_text(f"{EMOJIS['pencil']} <b>Введите сумму для реинвеста от {format_bold_num(MIN_REINVEST)} (например: 1OOk, 5OOk, 1kk, all):</b>", reply_markup=cancel_kb, parse_mode="HTML")
    await state.set_state(ReinvestFlow.waiting_for_amount)
    await call.answer()

@router.message(ReinvestFlow.waiting_for_amount)
async def state_reinvest_amount(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)

    uid = msg.from_user.id
    u = await get_and_update_user(uid)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="refresh_profile")]])
    try:
        amt = parse_amount(msg.text, max_val=u['earned'])
        if amt < MIN_REINVEST:
            return await msg.answer(f"❌ <b>Минимальная сумма для реинвеста:</b> {format_bold_num(MIN_REINVEST)}! Попробуйте снова:", reply_markup=cancel_kb, parse_mode="HTML")
        if amt > u['earned']:
            return await msg.answer("❌ <b>Недостаточно средств на балансе прибыли!</b> Попробуйте снова:", reply_markup=cancel_kb, parse_mode="HTML")

        ok, msg_text = await execute_reinvest(uid, amt)
        if not ok:
            return await msg.answer(f"❌ <b>{msg_text}</b>", reply_markup=cancel_kb, parse_mode="HTML")

        await state.clear()
        await msg.answer(
            f"{EMOJIS['sparkles']} <b>Реинвест успешно выполнен!</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>Сумма:</b> {format_bold_num(amt)}\n📈 <b>Депозит успешно увеличен!</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 В кабинет", callback_data="refresh_profile")]]),            parse_mode="HTML"        )
    except Exception:
        await msg.answer(f"❌ <b>Неверный формат суммы.</b> Введите от {format_bold_num(MIN_REINVEST)} (например: 1OOk, 5OOk, 1kk, all):", reply_markup=cancel_kb, parse_mode="HTML")

# --- ПОПОЛНЕНИЕ ---
@router.callback_query(F.data == "btn_deposit")
@router.message(F.text.func(lambda t: t and "пополнить" in t.lower()))
async def cmd_deposit(event: types.Message | types.CallbackQuery, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = event.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if not await check_user_subscription(bot, uid, CHANNEL_USERNAME): return

    t = (
        "<b>Пополнение баланса | Grand Mobile №17</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Для открытия или увеличения депозита:</b>\n\n"
        f"• 💰 <b>Минимальный депозит:</b> {format_bold_num(MIN_DEPOSIT)}\n\n"
        "1️⃣ <b>Свяжитесь с официальным менеджером:</b> 👉 <b>@NemosTradeBot</b>\n"
        f"2️⃣ <b>Укажите ваш ID:</b> <code>{uid}</code> (нажмите чтобы скопировать) и сумму\n"
        "3️⃣ <b>Передайте игровую валюту на сервере</b>\n"
        "4️⃣ <b>Отправьте скриншот / чек передачи менеджеру</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚡️ <b>Регламент зачисления: 1–5 минут</b>\n"
        "📈 <b>Доход +3% в день начинает начисляться сразу после пополнения!</b>"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.reply(t, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(t, parse_mode="HTML")

# --- КАЛЬКУЛЯТОР ---
@router.callback_query(F.data == "btn_calc")
@router.message(F.text.func(lambda t: t and "калькулятор" in t.lower()))
async def start_calc(event: types.Message | types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    uid = event.from_user.id
    if await is_user_banned(uid): return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1,OOO,OOO$", callback_data="calc_val:1000000"), InlineKeyboardButton(text="1O,OOO,OOO$", callback_data="calc_val:10000000")],
        [InlineKeyboardButton(text="5O,OOO,OOO$", callback_data="calc_val:50000000"), InlineKeyboardButton(text="1OO,OOO,OOO$", callback_data="calc_val:100000000")],
        [InlineKeyboardButton(text="💼 Рассчитать мой депозит", callback_data="calc_my_deposit")],
        [InlineKeyboardButton(text="✏️ Своя сумма", callback_data="calc_custom"), InlineKeyboardButton(text="❌ Отмена", callback_data="calc_cancel")]
    ])
    t = f"{EMOJIS['calc']} <b>Выберите сумму для быстрого расчёта или введите свою:</b>"
    if isinstance(event, types.CallbackQuery):
        await event.message.reply(t, reply_markup=kb, parse_mode="HTML")
        await event.answer()
    else: await event.answer(t, reply_markup=kb, parse_mode="HTML")

@router.callback_query(F.data.startswith("calc_val:"))
async def calc_preset(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    await call.message.edit_text(format_calc_result(float(call.data.split(":")[1])), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "calc_my_deposit")
async def calc_my_deposit(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    u = await get_and_update_user(call.from_user.id)
    if u['invested'] <= 0: return await call.answer("⚠️ У вас пока нет активного депозита в обороте!", show_alert=True)
    await call.message.edit_text(format_calc_result(u['invested']), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data == "calc_custom")
async def calc_custom_call(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("✏️ <b>Введите сумму для расчёта (например: 5OOk, 5kk, 5Om, 1ккк):</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="calc_cancel")]]), parse_mode="HTML")
    await state.set_state(CalcState.waiting_for_amount)
    await call.answer()

@router.callback_query(F.data == "calc_cancel")
async def calc_cancel_call(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    await call.message.edit_text("❌ <b>Расчёт завершён.</b>", parse_mode="HTML")
    await call.answer("Расчёт завершён")

@router.message(CalcState.waiting_for_amount)
async def process_calc(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)
    try:
        u = await get_and_update_user(msg.from_user.id)
        a = parse_amount(msg.text, max_val=u['invested'])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Закончить расчет", callback_data="calc_cancel")]
        ])
        t_res = (
            f"{format_calc_result(a)}\n\n"
            f"💡 <i>Отправьте следующую сумму для нового расчёта или нажмите кнопку ниже:</i>"
        )
        await msg.answer(t_res, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.answer("❌ <b>Неверный формат.</b> Введите сумму (например: 5OOk, 5kk, 5Om, 1ккк):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="calc_cancel")]]), parse_mode="HTML")

# --- ВЫВОД СРЕДСТВ ---
@router.callback_query(F.data == "btn_withdraw")
@router.message(F.text.func(lambda t: t and "вывести" in t.lower()))
async def cmd_withdraw(event: types.Message | types.CallbackQuery, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = event.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm: return
    if await is_withdrawals_frozen() and not adm:
        t_frozen = "⏳ <b>Вывод средств временно приостановлен</b>\n━━━━━━━━━━━━━━━━━━\n<b>Ведутся технические работы.</b>\n\n📈 <b>Начисление прибыли (+3%) продолжается в штатном режиме каждую секунду!</b>"
        if isinstance(event, types.CallbackQuery): await event.message.reply(t_frozen, parse_mode="HTML"); await event.answer()
        else: await event.answer(t_frozen, parse_mode="HTML")
        return
    if not await check_user_subscription(bot, uid, CHANNEL_USERNAME): return

    if await has_active_withdrawal(uid):
        t_active = (
            "⚠️ <b>У вас уже есть активная заявка на вывод!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Дождитесь выполнения текущей заявки куратором на авторынке Drom перед созданием новой."
        )
        if isinstance(event, types.CallbackQuery):
            await event.message.reply(t_active, parse_mode="HTML")
            await event.answer()
        else:
            await event.answer(t_active, parse_mode="HTML")
        return

    u = await get_and_update_user(uid)
    hold_amt = await get_user_transfer_hold_amount(uid)
    avail_amt = max(0.0, round(u['earned'] - hold_amt, 2))

    if avail_amt < MIN_WITHDRAW:
        hold_note = f"\n• ⏳ <b>На защитном холде (12ч):</b> {format_bold_num(hold_amt)}" if hold_amt > 0 else ""
        t = (
            f"❌ <b>Недостаточно средств для вывода!</b>\n\n"
            f"• <b>Минимальная выплата:</b> {format_bold_num(MIN_WITHDRAW)}\n"
            f"• <b>На вашем балансе:</b> {format_bold_num(u['earned'])}\n"
            f"• 💳 <b>Доступно к выводу:</b> {format_bold_num(avail_amt)}"
            f"{hold_note}"
        )
        if isinstance(event, types.CallbackQuery): await event.message.reply(t, parse_mode="HTML"); await event.answer()
        else: await event.answer(t, parse_mode="HTML")
        return

    val_25, val_50, val_75, val_100 = round(avail_amt * 0.25, 2), round(avail_amt * 0.50, 2), round(avail_amt * 0.75, 2), round(avail_amt, 2)
    kb_rows = []
    row_top = []
    if val_25 >= MIN_WITHDRAW: row_top.append(InlineKeyboardButton(text=f"25% ({format_raw_num(val_25)})", callback_data="w_pct:0.25"))
    if val_50 >= MIN_WITHDRAW: row_top.append(InlineKeyboardButton(text=f"5O% ({format_raw_num(val_50)})", callback_data="w_pct:0.5"))
    if row_top: kb_rows.append(row_top)
    row_mid = []
    if val_75 >= MIN_WITHDRAW: row_mid.append(InlineKeyboardButton(text=f"75% ({format_raw_num(val_75)})", callback_data="w_pct:0.75"))
    if val_100 >= MIN_WITHDRAW: row_mid.append(InlineKeyboardButton(text=f"1OO% ({format_raw_num(val_100)})", callback_data="w_pct:1.0"))
    if row_mid: kb_rows.append(row_mid)
    kb_rows.append([InlineKeyboardButton(text="✏️ Своя сумма / All", callback_data="w_custom"), InlineKeyboardButton(text="❌ Отмена", callback_data="w_cancel")])

    hold_str = f"\n⏳ <b>На защитном холде (12ч):</b> {format_bold_num(hold_amt)}" if hold_amt > 0 else ""
    t = (
        "📤 <b>Вывод средств</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Всего на балансе:</b> {format_bold_num(u['earned'])}\n\n"
        f"💳 <b>Доступно к выводу:</b> {format_bold_num(avail_amt)}{hold_str}\n\n"
        f"💵 <b>Минимум:</b> {format_bold_num(MIN_WITHDRAW)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Выберите сумму вывода или нажмите «Своя сумма»:</b>"
    )
    if isinstance(event, types.CallbackQuery): await event.message.reply(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML"); await event.answer()
    else: await event.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await state.set_state(WithdrawFlow.waiting_for_amount)

@router.callback_query(F.data == "w_custom")
async def withdraw_custom_callback(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("✏️ <b>Введите сумму для вывода (например: 1OOk, 1kk, 1KK, все, all)</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="w_cancel")]]), parse_mode="HTML")
    await state.set_state(WithdrawFlow.waiting_for_amount)
    await call.answer()

@router.callback_query(F.data.startswith("w_pct:"))
async def withdraw_pct_callback(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    u = await get_and_update_user(uid)
    hold_amt = await get_user_transfer_hold_amount(uid)
    avail_amt = max(0.0, round(u['earned'] - hold_amt, 2))
    amt = round(avail_amt * float(call.data.split(":")[1]), 2)
    if amt < MIN_WITHDRAW: return await call.answer(f"⚠️ Сумма меньше минималки ({format_raw_num(MIN_WITHDRAW)})!", show_alert=True)
    drom_price = round(amt + OKA_GOS_PRICE, 2)
    await state.update_data(withdraw_amount=amt, drom_price=drom_price)
    t = (
        f"💵 <b>Сумма к выводу:</b> {format_bold_num(amt)}\n\n"
        f"🚗 <b>Гос. стоимость Оки:</b> <b>+{format_bold_num(OKA_GOS_PRICE)}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Выставьте Оку на Drom за:</b> <code>{fmt_o(drom_price)}</code> (нажмите чтобы скопировать)\n\n"
        "📸 <b>Отправьте скриншот выставленного авто на Drom или напишите свой ник:</b>"
    )
    await call.message.edit_text(t, parse_mode="HTML")
    await state.set_state(WithdrawFlow.waiting_for_proof)
    await call.answer()

@router.callback_query(F.data == "w_cancel")
async def withdraw_cancel_callback(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    try: await call.message.edit_text("❌ <b>Вывод средств отменён.</b>", parse_mode="HTML")
    except Exception: pass
    await call.answer("Вывод отменён")

@router.message(WithdrawFlow.waiting_for_amount)
async def withdraw_amount_step(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)

    uid = msg.from_user.id
    u = await get_and_update_user(uid)
    hold_amt = await get_user_transfer_hold_amount(uid)
    avail_amt = max(0.0, round(u['earned'] - hold_amt, 2))
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="w_cancel")]])
    try:
        amt = parse_amount(msg.text, max_val=avail_amt)
        if amt < MIN_WITHDRAW:
            return await msg.answer(f"❌ <b>Минимальная сумма:</b> {format_bold_num(MIN_WITHDRAW)}!", reply_markup=cancel_kb, parse_mode="HTML")
        if amt > avail_amt:
            hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
            return await msg.answer(f"❌ <b>Недостаточно доступных средств{hold_msg}!</b> Доступно: {format_bold_num(avail_amt)}", reply_markup=cancel_kb, parse_mode="HTML")

        drom_price = round(amt + OKA_GOS_PRICE, 2)
        await state.update_data(withdraw_amount=amt, drom_price=drom_price)
        t = (
            f"💵 <b>Сумма к выводу:</b> {format_bold_num(amt)}\n\n"
            f"🚗 <b>Гос. стоимость Оки:</b> <b>+{format_bold_num(OKA_GOS_PRICE)}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>Выставьте Оку на Drom за:</b> <code>{fmt_o(drom_price)}</code> (нажмите чтобы скопировать)\n\n"
            "📸 <b>Отправьте скриншот выставленного авто на Drom или напишите свой ник:</b>"
        )
        await msg.answer(t, reply_markup=cancel_kb, parse_mode="HTML")
        await state.set_state(WithdrawFlow.waiting_for_proof)
    except Exception:
        await msg.answer("❌ <b>Ошибка формата!</b> Введите сумму (например: 1OOk, 1kk, 1KK, все, all)", reply_markup=cancel_kb, parse_mode="HTML")

@router.message(WithdrawFlow.waiting_for_proof)
async def withdraw_proof_step(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)

    data = await state.get_data()
    amt = data.get("withdraw_amount")
    if not amt:
        if state is not None: await state.clear()
        return
    photo_id = msg.photo[-1].file_id if msg.photo else ""
    user_text = msg.text.strip() if msg.text else (msg.caption.strip() if msg.caption else "Скриншот авто")
    await state.update_data(photo_id=photo_id, user_text=user_text)

    t = (
        f"{EMOJIS['warn']} <b>ПОДТВЕРЖДЕНИЕ ЗАЯВКИ НА ВЫВОД</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Игровой ник / данные:</b> <code>{html.escape(user_text)}</code>\n\n"
        f"💵 <b>Сумма вывода:</b> {format_bold_num(amt)}\n\n"
        f"🚗 <b>Цена авто на Drom (+Ока):</b> {format_bold_num(data.get('drom_price', amt + OKA_GOS_PRICE))}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Вы подтверждаете отправку заявки?</b>"
    )
    await msg.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да, отправить", callback_data="confirm_w_send"), InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_w_cancel")]]), parse_mode="HTML")
    await state.set_state(WithdrawFlow.confirm_withdraw)

@router.callback_query(F.data == "confirm_w_cancel")
async def confirm_cancel_callback(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    try: await call.message.edit_text("❌ <b>Заявка на вывод отменена.</b>", parse_mode="HTML")
    except Exception: pass
    await call.answer("Заявка отменена")

@router.callback_query(F.data == "confirm_w_send", WithdrawFlow.confirm_withdraw)
async def confirm_send_callback(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    uid = call.from_user.id
    data = await state.get_data()
    if data.get("is_processing"):
        return await call.answer("⏳ Заявка уже обрабатывается...", show_alert=True)
    await state.update_data(is_processing=True)
    amt = data.get("withdraw_amount")
    drom_price = data.get("drom_price")
    photo_id = data.get("photo_id") or ""
    user_text = data.get("user_text") or ""

    if not amt:
        if state is not None: await state.clear()
        return await call.answer("⚠️ Сессия устарела. Начните вывод заново.", show_alert=True)

    ok, tx_id, err_msg = await execute_withdrawal_creation(uid, amt, proof_text=user_text, proof_photo=photo_id)
    if not ok:
        if state is not None: await state.clear()
        return await call.message.edit_text(f"❌ <b>Ошибка: {err_msg}</b>", parse_mode="HTML")
    try:
        from handlers.admin import send_whale_alert
        await send_whale_alert(bot, uid, call.from_user.username or "", "Заявка на вывод средств", amt)
    except Exception:
        pass

    adm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Взять в работу", callback_data=f"adm_take_work:{tx_id}:{uid}:{amt}"), InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ask_rej_menu:{tx_id}:{uid}:{amt}")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm_user_card:{uid}"), InlineKeyboardButton(text="⛔ Забанить", callback_data=f"ask_ban:{uid}")]
    ])
    cap = (
        f"{EMOJIS['siren']} <b>Новая заявка на вывод №{tx_id}</b> {EMOJIS['siren']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Игрок:</b> <b>{html.escape(call.from_user.first_name)}</b> (@{html.escape(call.from_user.username or 'нет')})\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"{EMOJIS['clipboard']} <b>Ник в игре:</b> <code>{html.escape(user_text)}</code> (нажмите чтобы скопировать)\n"
        f"💵 <b>Сумма вывода:</b> <code>{fmt_o(amt)}$</code>\n"
        f"🚗 <b>Цена на Drom:</b> <code>{fmt_o(drom_price)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Действия администратора:</b>"
    )

    try:
        sent_admin_msg = None
        if photo_id:
            sent_admin_msg = await bot.send_photo(ADMIN_CHAT_ID, photo_id, caption=cap, reply_markup=adm_kb, parse_mode="HTML")
        else:
            sent_admin_msg = await bot.send_message(ADMIN_CHAT_ID, cap, reply_markup=adm_kb, parse_mode="HTML")

        if sent_admin_msg:
            await update_tx_admin_message(tx_id, sent_admin_msg.chat.id, sent_admin_msg.message_id)
    except Exception:
        pass

    try:
        if OWNER_ID != ADMIN_CHAT_ID:
            if photo_id: await bot.send_photo(OWNER_ID, photo_id, caption=cap, reply_markup=adm_kb, parse_mode="HTML")
            else: await bot.send_message(OWNER_ID, cap, reply_markup=adm_kb, parse_mode="HTML")
    except Exception: pass

    t = (
        f"{EMOJIS['check']} <b>Заявка №{tx_id} успешно создана!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Указанный ник:</b> <code>{html.escape(user_text)}</code>\n"
        f"💵 <b>Сумма вывода:</b> {format_bold_num(amt)}\n"
        f"🚗 <b>Авто на Drom за:</b> {format_bold_num(drom_price)}\n"
        "⏳ <b>Ваша заявка будет обработана в течение 24 часов.</b>"
    )
    await call.message.edit_text(t, parse_mode="HTML")
    if state is not None:
        await state.clear()
    await call.answer("Заявка успешно отправлена!")

# --- P2P ПЕРЕВОДЫ С БЫСТРЫМ ВЫБОРОМ ПОЛУЧАТЕЛЕЙ И ПРЕСЕТАМИ СУММ ---
def render_transfer_confirmation(data: dict) -> Tuple[str, InlineKeyboardMarkup]:
    recip_id = data.get("recipient_id")
    recip_uname = data.get("recipient_uname")
    amt = data.get("transfer_amount", 0.0)
    fee = data.get("transfer_fee", 0.0)
    net_amt = data.get("transfer_net", 0.0)
    is_anon = data.get("is_anonymous", False)

    tag_str = f" (@{recip_uname})" if recip_uname else ""
    anon_status_text = "🎭 <b>Анонимно (скрыть мой ник)</b>" if is_anon else "👤 <b>Обычный (показать мой ник)</b>"
    anon_btn_text = "🎭 Анонимно: ✅ ВКЛ" if is_anon else "🎭 Анонимно: ❌ ВЫКЛ"

    t = (
        f"{EMOJIS['warn']} <b>ПОДТВЕРЖДЕНИЕ ПЕРЕВОДА</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Получатель:</b> ID <code>{recip_id}</code>{tag_str}\n\n"
        f"💵 <b>Сумма списания:</b> {format_bold_num(amt)}\n\n"
        f"📊 <b>Комиссия системы (2.22%):</b> {format_bold_num(fee)}\n\n"
        f"💰 <b>Получателю зачислится:</b> {format_bold_num(net_amt)}\n\n"
        f"🎭 <b>Режим:</b> {anon_status_text}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Вы подтверждаете отправку средств?</b>"
    )

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=anon_btn_text, callback_data="transfer_toggle_anon")],
        [InlineKeyboardButton(text="✅ Подтвердить перевод", callback_data="confirm_transfer_send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]
    ])
    return t, confirm_kb

@router.callback_query(F.data == "btn_transfer")
@router.message(F.text.func(lambda t: t and "перевести" in t.lower()))
async def btn_transfer_start(event: types.Message | types.CallbackQuery, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = event.from_user.id
    if await is_user_banned(uid): return
    adm = await is_admin(uid)
    if await is_maintenance_active() and not adm:
        if isinstance(event, types.CallbackQuery): return await event.answer("🛠 Ведутся технические работы!", show_alert=True)
        return await event.answer("🛠 Ведутся технические работы!")
    if not await is_game_module_enabled("p2p"):
        t_off = "⚠️ <b>P2P Переводы временно приостановлены администрацией на техническое обслуживание.</b>"
        if isinstance(event, types.CallbackQuery): return await event.answer("⚠️ Переводы временно отключены!", show_alert=True)
        return await event.answer(t_off, parse_mode="HTML")
    if not await check_user_subscription(bot, uid, CHANNEL_USERNAME): return

    u = await get_and_update_user(uid)
    if u['earned'] < MIN_TRANSFER:
        t_err = f"❌ Перевод доступен только с баланса прибыли! Минимум: {format_raw_num(MIN_TRANSFER)}"
        if isinstance(event, types.CallbackQuery): return await event.answer(t_err, show_alert=True)
        return await event.answer(t_err)

    recents = await get_recent_transfer_recipients(uid, limit=5)
    kb_rows = []
    
    # Кнопки недавних получателей для быстрого перевода
    if recents:
        for r_id, r_uname in recents:
            tag_str = f"@{r_uname}" if r_uname else f"ID {r_id}"
            kb_rows.append([InlineKeyboardButton(text=f"⚡ Перевести {tag_str}", callback_data=f"transfer_pick_recip:{r_id}")])

    kb_rows.append([InlineKeyboardButton(text="📜 История переводов", callback_data="transfer_history")])
    kb_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")])

    t = (
        "💸 <b>Перевод средств между игроками</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "• <b>Перевод осуществляется только с баланса чистой прибыли</b>\n\n"
        f"• <b>Минимальная сумма перевода:</b> {format_bold_num(MIN_TRANSFER)}\n\n"
        "• <b>Комиссия системы:</b> <b>2.22%</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )
    if recents:
        t += "👇 <b>Выберите недавнего получателя для быстрого перевода или введите ID / @username:</b>"
    else:
        t += "👉 <b>Введите ID получателя или его @username:</b>"

    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
        except Exception:
            await event.message.reply(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await state.set_state(TransferFlow.waiting_for_recipient)

@router.callback_query(F.data == "transfer_history")
async def cb_transfer_history(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    history = await get_user_transfer_history(uid, limit=5)

    kb_rows = []
    t = (
        "📜 <b>ИСТОРИЯ ВАШИХ ПЕРЕВОДОВ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    if not history:
        t += "<i>У вас пока нет исходящих переводов.</i>\n━━━━━━━━━━━━━━━━━━\n"
    else:
        unique_recips = {}
        for r_id, r_uname, amt, net_amt, dt, is_anon in history:
            disp_date = format_datetime_display(dt).replace("0", "O")
            tag_str = f"@{r_uname}" if r_uname else f"ID {r_id}"
            anon_str = "🎭 Анонимно" if is_anon else "👤 Обычный"
            t += (
                f"• <b>Кому:</b> {tag_str}\n"
                f"  💰 <b>Сумма:</b> {format_bold_num(amt)} (к зачислению: {format_raw_num(net_amt)})\n"
                f"  📅 <i>{disp_date}</i> | {anon_str}\n\n"
            )
            if r_id not in unique_recips:
                unique_recips[r_id] = tag_str

        t += "⚡ <b>Быстрый перевод в 1 клик (без ввода ника):</b>\n"
        for r_id, tag_str in unique_recips.items():
            kb_rows.append([InlineKeyboardButton(text=f"⚡ Перевести {tag_str}", callback_data=f"transfer_pick_recip:{r_id}")])

    kb_rows.append([InlineKeyboardButton(text="◀️ Назад к переводам", callback_data="btn_transfer")])
    kb_rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="transfer_cancel")])

    try:
        await call.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    except Exception:
        await call.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await call.answer()

@router.callback_query(F.data.startswith("transfer_pick_recip:"))
async def cb_transfer_pick_recip(call: types.CallbackQuery, state: FSMContext):
    recip_id = int(call.data.split(":")[1])
    sender_id = call.from_user.id
    if recip_id == sender_id:
        return await call.answer("❌ Нельзя перевести самому себе!", show_alert=True)

    recip_user = await get_and_update_user(recip_id)
    if recip_user.get('is_banned'):
        return await call.answer("❌ Игрок заблокирован!", show_alert=True)

    resolved_tag = recip_user.get('username') or ""
    await state.update_data(recipient_id=recip_id, recipient_uname=resolved_tag, is_anonymous=False)
    await show_transfer_amount_selection(call, recip_id, resolved_tag, sender_id, state)
    await call.answer()

@router.callback_query(F.data == "transfer_cancel")
async def transfer_cancel_callback(call: types.CallbackQuery, state: FSMContext):
    if state is not None:
        await state.clear()
    try: await call.message.edit_text("❌ <b>Перевод средств отменён.</b>", parse_mode="HTML")
    except Exception: pass
    await call.answer("Перевод отменён")

@router.message(TransferFlow.waiting_for_recipient)
async def transfer_recipient_step(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)

    raw_input = msg.text.strip().replace("@", "")
    sender_id = msg.from_user.id
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]])

    recip_id = None
    if raw_input.isdigit():
        candidate_id = int(raw_input)
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (candidate_id,)) as cur:
                row = await cur.fetchone()
                if row: recip_id = row[0]
        if not recip_id:
            return await msg.answer("❌ <b>Пользователь с таким ID не найден в боте!</b>\nИгрок должен хотя бы один раз запустить бота перед получением перевода.", reply_markup=cancel_kb, parse_mode="HTML")
    else:
        async with get_db_connection() as db:
            async with db.execute("SELECT user_id FROM users WHERE LOWER(username) = LOWER(?)", (raw_input,)) as cur:
                row = await cur.fetchone()
                if row: recip_id = row[0]
        if not recip_id:
            return await msg.answer("❌ <b>Пользователь с таким @username не найден в боте!</b>", reply_markup=cancel_kb, parse_mode="HTML")

    if recip_id == sender_id:
        return await msg.answer("❌ <b>Вы не можете перевести средства самому себе!</b>", reply_markup=cancel_kb, parse_mode="HTML")

    recip_user = await get_and_update_user(recip_id)
    if recip_user.get('is_banned'):
        return await msg.answer(f"{EMOJIS['stop']} <b>Данный игрок заблокирован в системе!</b>", reply_markup=cancel_kb, parse_mode="HTML")

    resolved_tag = recip_user.get('username') or ""
    await state.update_data(recipient_id=recip_id, recipient_uname=resolved_tag, is_anonymous=False)
    await show_transfer_amount_selection(msg, recip_id, resolved_tag, sender_id, state)

async def show_transfer_amount_selection(target: types.Message | types.CallbackQuery, recip_id: int, resolved_tag: str, sender_id: int, state: FSMContext):
    u = await get_and_update_user(sender_id)
    tag_str = f" (@{resolved_tag})" if resolved_tag else ""

    presets = [500_000, 1_000_000, 5_000_000, 10_000_000]
    kb_rows = [
        [InlineKeyboardButton(text="5OO,OOO$", callback_data=f"transfer_set_amt:{presets[0]}"), InlineKeyboardButton(text="1,OOO,OOO$", callback_data=f"transfer_set_amt:{presets[1]}")],
        [InlineKeyboardButton(text="5,OOO,OOO$", callback_data=f"transfer_set_amt:{presets[2]}"), InlineKeyboardButton(text="1O,OOO,OOO$", callback_data=f"transfer_set_amt:{presets[3]}")],
        [InlineKeyboardButton(text=f"🔄 Всё ({format_raw_num(u['earned'])})", callback_data=f"transfer_set_amt:{u['earned']}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]
    ]

    t = (
        f"👤 <b>Получатель:</b> ID <code>{recip_id}</code>{tag_str}\n"
        f"💰 <b>Доступно к переводу:</b> {format_bold_num(u['earned'])}\n"
        f"💳 <b>Минимум:</b> {format_bold_num(MIN_TRANSFER)}\n"
        "📊 <b>Комиссия системы:</b> <b>2.22%</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Выберите сумму перевода кнопками или введите вручную (например: 5OOk, 1kk):</b>"
    )

    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    else:
        await target.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await state.set_state(TransferFlow.waiting_for_amount)

@router.callback_query(F.data.startswith("transfer_set_amt:"))
async def cb_transfer_set_amt(call: types.CallbackQuery, state: FSMContext):
    amt = float(call.data.split(":")[1])
    sender_id = call.from_user.id
    u = await get_and_update_user(sender_id)

    if amt < MIN_TRANSFER:
        return await call.answer(f"❌ Минимальная сумма перевода: {format_raw_num(MIN_TRANSFER)}!", show_alert=True)
    hold_amt = await get_user_transfer_hold_amount(sender_id)
    avail = max(0.0, round(u['earned'] - hold_amt, 2))
    if amt > avail:
        hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
        return await call.answer(f"❌ Недостаточно доступных средств{hold_msg}! Доступно: {format_raw_num(avail)}", show_alert=True)

    fee = round(amt * TRANSFER_FEE_PERCENT, 2)
    net_amt = round(amt - fee, 2)
    await state.update_data(transfer_amount=amt, transfer_fee=fee, transfer_net=net_amt)

    data = await state.get_data()
    t, confirm_kb = render_transfer_confirmation(data)
    await call.message.edit_text(t, reply_markup=confirm_kb, parse_mode="HTML")
    await state.set_state(TransferFlow.confirm_transfer)
    await call.answer()

@router.callback_query(F.data == "transfer_toggle_anon", TransferFlow.confirm_transfer)
async def cb_transfer_toggle_anon(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_anon = not data.get("is_anonymous", False)
    await state.update_data(is_anonymous=new_anon)
    data["is_anonymous"] = new_anon

    t, kb = render_transfer_confirmation(data)
    try:
        await call.message.edit_text(t, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass
    status_str = "включен" if new_anon else "отключен"
    await call.answer(f"Анонимный режим {status_str}!")

@router.message(TransferFlow.waiting_for_amount)
async def transfer_amount_step(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)

    sender_id = msg.from_user.id
    u = await get_and_update_user(sender_id)
    hold_amt = await get_user_transfer_hold_amount(sender_id)
    avail = max(0.0, round(u['earned'] - hold_amt, 2))
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="transfer_cancel")]])

    try:
        amt = parse_amount(msg.text, max_val=avail)
        if amt < MIN_TRANSFER:
            return await msg.answer(f"❌ <b>Минимальная сумма перевода:</b> {format_bold_num(MIN_TRANSFER)}!", reply_markup=cancel_kb, parse_mode="HTML")
        if amt > avail:
            hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
            return await msg.answer(f"❌ <b>Недостаточно доступных средств{hold_msg}!</b> Доступно: {format_bold_num(avail)}", reply_markup=cancel_kb, parse_mode="HTML")

        fee = round(amt * TRANSFER_FEE_PERCENT, 2)
        net_amt = round(amt - fee, 2)
        await state.update_data(transfer_amount=amt, transfer_fee=fee, transfer_net=net_amt)

        data = await state.get_data()
        t, confirm_kb = render_transfer_confirmation(data)
        await msg.answer(t, reply_markup=confirm_kb, parse_mode="HTML")
        await state.set_state(TransferFlow.confirm_transfer)
    except Exception:
        await msg.answer("❌ <b>Ошибка формата!</b> Введите сумму (например: 5OOk, 1kk, all):", reply_markup=cancel_kb, parse_mode="HTML")

@router.callback_query(F.data == "confirm_transfer_send", TransferFlow.confirm_transfer)
async def confirm_transfer_send_callback(call: types.CallbackQuery, state: FSMContext, bot: Bot):
    sender_id = call.from_user.id
    data = await state.get_data()
    if data.get("is_processing"):
        return await call.answer("⏳ Перевод уже обрабатывается...", show_alert=True)
    await state.update_data(is_processing=True)
    recip_id = data.get("recipient_id")
    recip_uname = data.get("recipient_uname") or ""
    amt = data.get("transfer_amount")
    fee = data.get("transfer_fee")
    net_amt = data.get("transfer_net")
    is_anon = 1 if data.get("is_anonymous", False) else 0

    if not recip_id or not amt:
        if state is not None: await state.clear()
        return await call.answer("⚠️ Сессия устарела!", show_alert=True)

    sender_uname = call.from_user.username or ""
    ok, err_msg = await execute_p2p_transfer(sender_id, recip_id, amt, fee, net_amt, is_anonymous=is_anon, sender_uname=sender_uname)
    if not ok:
        if state is not None: await state.clear()
        return await call.message.edit_text(f"❌ <b>Ошибка: {err_msg}</b>", parse_mode="HTML")
    try:
        from handlers.admin import send_whale_alert
        await send_whale_alert(bot, sender_id, sender_uname, f"P2P-перевод игроку ID {recip_id}", amt)
    except Exception:
        pass

    anon_txt = "\n🎭 <b>Режим:</b> Анонимный перевод" if is_anon else ""
    t_sender = (
        "✅ <b>Перевод успешно выполнен!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Получатель:</b> ID {recip_id} (@{recip_uname or 'нет'})\n"
        f"💵 <b>Списано с прибыли:</b> {fmt_o(amt)}$\n"
        f"📊 <b>Комиссия (2.22%):</b> {fmt_o(fee)}$\n"
        f"💰 <b>Зачислено получателю:</b> {fmt_o(net_amt)}$"
        f"{anon_txt}"
    )
    await call.message.edit_text(t_sender, parse_mode="HTML")
    if state is not None:
        await state.clear()

    try:
        if is_anon:
            t_recip = (
                f"{EMOJIS['sparkles']} <b>Вам поступил анонимный перевод!</b> 💸\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Сумма:</b> {format_bold_num(net_amt)}\n\n"
                "💳 <b>Средства доступны к выводу!</b>"
            )
        else:
            sender_display = f"@{sender_uname}" if sender_uname else html.escape(call.from_user.first_name or f"ID {sender_id}")
            t_recip = (
                f"{EMOJIS['sparkles']} <b>Вам поступил перевод от {sender_display}!</b> 💸\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Сумма:</b> {format_bold_num(net_amt)}\n\n"
                "💳 <b>Средства доступны к выводу!</b>"
            )
        await bot.send_message(recip_id, t_recip, parse_mode="HTML", message_effect_id=EFFECT_CONFETTI_ID)
    except Exception: pass

# --- ПРОМОКОДЫ ---
@router.message(F.text.func(lambda t: t and "промокод" in t.lower()))
@router.message(Command("promo"))
async def prompt_promo_code(msg: types.Message, state: FSMContext, command: CommandObject = None):
    if state is not None:
        await state.clear()
    uid = msg.from_user.id
    if await is_user_banned(uid): return
    if command and command.args:
        return await process_promo(msg, command.args.strip())
    await msg.answer(f"{EMOJIS['bag']} <b>Введите промокод для активации бонуса:</b>", parse_mode="HTML")
    await state.set_state(PromoState.waiting_for_promo_code)

@router.message(PromoState.waiting_for_promo_code)
async def process_promo_state(msg: types.Message, state: FSMContext, bot: Bot):
    if is_menu_trigger(msg.text):
        await state.clear()
        return await handle_menu_redirection(msg, state, bot)
    if state is not None:
        await state.clear()
    await process_promo(msg, msg.text.strip())

async def process_promo(msg: types.Message, code_str: str):
    uid = msg.from_user.id
    code = code_str.upper().strip()

    await get_and_update_user(uid)

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT amount, max_uses, current_uses FROM promocodes WHERE code = ?", (code,)) as cur:
                p = await cur.fetchone()
            if not p:
                await db.execute("ROLLBACK")
                return await msg.answer("❌ <b>Промокод не найден или истёк.</b>", parse_mode="HTML")

            amt, max_u, curr_u = p
            amt = round(amt, 2)
            if curr_u >= max_u:
                await db.execute("ROLLBACK")
                return await msg.answer("❌ <b>Лимит активаций этого промокода исчерпан!</b>", parse_mode="HTML")

            async with db.execute("SELECT id FROM promo_activations WHERE user_id = ? AND code = ?", (uid, code)) as cur:
                if await cur.fetchone():
                    await db.execute("ROLLBACK")
                    return await msg.answer(f"{EMOJIS['warn']} <b>Вы уже активировали этот промокод ранее!</b>", parse_mode="HTML")

            from config.config import get_now_iso
            now_iso = get_now_iso()
            await db.execute("UPDATE promocodes SET current_uses = current_uses + 1 WHERE code = ?", (code,))
            await db.execute("INSERT INTO promo_activations (user_id, code, activated_at) VALUES (?, ?, ?)", (uid, code, now_iso))
            await db.execute("UPDATE users SET invested = ROUND(invested + ?, 2), last_accrual = ? WHERE user_id = ?", (amt, now_iso, uid))
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🎁 Промокод', ?, ?, 'Зачислено')", (uid, amt, now_iso))
            await db.commit()

            t = f"{EMOJIS['sparkles']} <b>Промокод успешно активирован!</b>\n━━━━━━━━━━━━━━━━━━\n💰 <b>Бонус:</b> <b>+{format_bold_num(amt)}</b> <b>зачислено в ваш депозит!</b>\n📈 <i>Доход +3% в день уже начисляется на эту сумму.</i>"
            try: await msg.answer(t, parse_mode="HTML", message_effect_id=EFFECT_CONFETTI_ID)
            except Exception: await msg.answer(t, parse_mode="HTML")
        except (aiosqlite.IntegrityError, sqlite3.IntegrityError):
            await db.execute("ROLLBACK")
            await msg.answer("⚠️ <b>Вы уже активировали этот промокод ранее!</b>", parse_mode="HTML")
        except Exception:
            await db.execute("ROLLBACK")
            await msg.answer("❌ <b>Ошибка при активации промокода!</b>", parse_mode="HTML")

# --- РЕФЕРАЛЫ И ПОДДЕРЖКА ---
@router.message(F.text.func(lambda t: t and ("партнер" in t.lower() or "реферал" in t.lower() or "👥" in t)))
async def cmd_referrals(msg: types.Message, state: FSMContext, bot: Bot):
    if state is not None:
        await state.clear()
    uid = msg.from_user.id
    if await is_user_banned(uid): return
    async with get_db_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ? AND referral_rewarded = 1", (uid,)) as cur:
            cnt = (await cur.fetchone())[0]

    b = await bot.get_me()
    t = (
        f"{EMOJIS['globe']} <b>Партнерская программа</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Приглашайте друзей и получайте бонусы вместе!</b>\n\n"
        f"{EMOJIS['bag']} <b>Вам за друга:</b> {format_bold_num(REFERRAL_BONUS_INVITER)} <b>на баланс</b>\n\n"
        f"🤝 <b>Другу на старт:</b> {format_bold_num(REFERRAL_BONUS_INVITED)} <b>в инвестиции</b>\n\n"
        f"{EMOJIS['stats']} <b>Приглашено партнеров:</b> <b>{fmt_o(cnt)} чел.</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{EMOJIS['link']} <b>Ваша ссылка для приглашения:</b>\n"
        f"<code>https://t.me/{b.username}?start={uid}</code>"
    )
    await msg.answer(t, parse_mode="HTML")

@router.message(F.text.func(lambda t: t and ("поддержк" in t.lower() or "💬" in t)))
@router.message(Command("support"))
async def cmd_support(msg: types.Message, state: FSMContext):
    if state is not None:
        await state.clear()
    t = (
        f"{EMOJIS['chat']} <b>Служба поддержки</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Возникли вопросы, проблемы с начислением или выводом?</b>\n\n"
        "👉 <b>Напишите нашему менеджеру:</b> <b>@NemosTradeBot</b>"
    )
    await msg.answer(t, parse_mode="HTML")

# --- КАТЕГОРИЙНАЯ ИСТОРИЯ ОПЕРАЦИЙ ---
@router.callback_query(F.data.startswith("hist_cat:"))
@router.message(F.text.func(lambda t: t and "история" in t.lower()))
async def show_history_filtered(event: types.Message | types.CallbackQuery, state: FSMContext = None):
    if state is not None:
        await state.clear()
    uid = event.from_user.id
    
    category = "all"
    page = 0
    if isinstance(event, types.CallbackQuery):
        parts = event.data.split(":")
        category = parts[1]
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    limit = 6
    offset = page * limit
    txs, total_count = await get_user_transactions_filtered(uid, category=category, limit=limit, offset=offset)

    cat_titles = {
        "all": "🔄 Все", "deposit": "📥 Депозиты", "withdraw": "📤 Выводы",
        "games": "🎮 Игры", "transfer": "💸 Переводы"
    }

    t = f"📜 <b>ИСТОРИЯ ОПЕРАЦИЙ: {cat_titles.get(category, 'Все')}</b>\n━━━━━━━━━━━━━━━━━━\n"
    if not txs:
        t += "<i>В данной категории пока нет операций.</i>\n"
    else:
        for item in txs:
            disp_date = format_datetime_display(item[2])
            t += f"• <b>{html.escape(str(item[0]))}:</b> {format_bold_num(item[1])}\n  📅 <i>{disp_date}</i> | <b>Статус:</b> <b>{html.escape(str(item[3]))}</b>\n\n"
    t += "━━━━━━━━━━━━━━━━━━"

    def b_txt(key, name): return f"• {name} •" if category == key else name
    filter_row1 = [
        InlineKeyboardButton(text=b_txt("all", "🔄 Все"), callback_data=f"hist_cat:all:0"),
        InlineKeyboardButton(text=b_txt("deposit", "📥 Деп"), callback_data=f"hist_cat:deposit:0"),
        InlineKeyboardButton(text=b_txt("withdraw", "📤 Вывод"), callback_data=f"hist_cat:withdraw:0")
    ]
    filter_row2 = [
        InlineKeyboardButton(text=b_txt("games", "🎮 Игры"), callback_data=f"hist_cat:games:0"),
        InlineKeyboardButton(text=b_txt("transfer", "💸 Переводы"), callback_data=f"hist_cat:transfer:0")
    ]

    kb = [filter_row1, filter_row2]
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"hist_cat:{category}:{page-1}"))
    if offset + limit < total_count:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"hist_cat:{category}:{page+1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([InlineKeyboardButton(text="👤 В личный кабинет", callback_data="refresh_profile")])

    if isinstance(event, types.CallbackQuery):
        if event.message.photo:
            try:
                await event.message.edit_caption(caption=t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
                await event.answer()
                return
            except Exception:
                pass
        try:
            await event.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        except Exception:
            try:
                await event.message.delete()
            except Exception:
                pass
            await event.message.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="HTML")

# --- ВСПОМОГАТЕЛЬНЫЙ МАРШРУТИЗАТОР ПЕРЕНАПРАВЛЕНИЯ ИЗ FSM ---
async def handle_menu_redirection(msg: types.Message, state: FSMContext, bot: Bot):
    t = msg.text.lower().strip()
    if "кабинет" in t or "профиль" in t or "👤" in t:
        return await show_profile(msg, state, bot)
    elif "игры" in t or "игра" in t or "🎮" in t:
        t_games = (
            "🎮 <b>ИГРОВОЙ ХАБ NEMOS TRADE</b> 🎮\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>Ваша чистая прибыль готова умножаться в реальном времени!</b>\n\n"
            "💣 <b>Минёр (Mines 5х5)</b> — находите спрятанные алмазы и забирайте выигрыш с растущим множителем! Выбирайте от 2 до 15 мин на поле!\n\n"
            "🎲 <b>Кости против Бота (PvE)</b> — моментальная игра против системы с коэффициентом <b>x1.8</b>!\n\n"
            "⚔️ <b>Кости PvP (Дуэль)</b> — честная битва между игроками за общий банк (комиссия стола всего 3.33%)!\n"
            "━━━━━━━━━━━━━━━━━━"
        )
        games_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💣 Минёр (Mines 5x5)", callback_data="btn_mines_menu")],
            [InlineKeyboardButton(text="🎲 Кости против Бота (PvE x1.8)", callback_data="btn_dice_pve_menu")],
            [InlineKeyboardButton(text="⚔️ Кости PvP (Дуэль игроков)", callback_data="btn_dice_pvp_menu")],
            [InlineKeyboardButton(text="◀️ В главное меню", callback_data="btn_games_close")]
        ])
        return await msg.answer(t_games, reply_markup=games_kb, parse_mode="HTML")
    elif "промокод" in t or "🎁" in t:
        return await prompt_promo_code(msg, state)
    elif "партнер" in t or "реферал" in t or "👥" in t:
        return await cmd_referrals(msg, state, bot)
    elif "поддержк" in t or "💬" in t:
        return await cmd_support(msg, state)
    elif "админ" in t or "👑" in t:
        from handlers.admin import open_admin_panel
        return await open_admin_panel(msg, state)
    elif "пополнить" in t:
        return await cmd_deposit(msg, state, bot)
    elif "вывести" in t:
        return await cmd_withdraw(msg, state, bot)
    elif "калькулятор" in t:
        return await start_calc(msg, state)
    elif "история" in t:
        return await show_history_filtered(msg, state)
    elif t.startswith("/start"):
        return await cmd_start(msg, CommandObject(prefix="/", command="start", args=msg.text.split()[1] if len(msg.text.split()) > 1 else None), state, bot)