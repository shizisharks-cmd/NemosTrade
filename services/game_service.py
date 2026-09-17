import random
import aiosqlite
from typing import Tuple, Dict, Any, List, Optional
from config.config import (
    BOXES, DICE_MULTIPLIER, DICE_PVP_FEE_PERCENT, MAX_ACTIVE_PVP_LOBBIES, get_now_iso,
    format_raw_num, format_bold_num
)
from datetime import timedelta
from config.config import get_now
from database.database import get_and_update_user, get_db_connection, get_user_transfer_hold_amount

async def open_case(user_id: int, box_id: str) -> Tuple[bool, str, float, float, float]:
    """Открытие кейса с валидацией баланса, защитного 12ч холда и атомарной фиксацией выигрыша."""
    box = BOXES.get(box_id)
    if not box:
        return False, "Такой кейс не найден!", 0.0, 0.0, 0.0

    # Начисляем накопленные проценты перед проверкой баланса
    user = await get_and_update_user(user_id)
    price = round(box["price"], 2)

    hold_amt = await get_user_transfer_hold_amount(user_id)
    avail = max(0.0, round(user["earned"] - hold_amt, 2))
    if avail < price:
        hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
        return False, f"Недостаточно доступных средств{hold_msg}! Доступно: {format_bold_num(avail)}", 0.0, 0.0, 0.0

    drop = round(float(random.choices(box["drops"], weights=box["weights"])[0]), 2)
    profit = round(drop - price, 2)
    now_iso = get_now_iso()
    cutoff_iso = (get_now() - timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S")

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")

            # Проверка баланса и холда строго внутри транзакции
            async with db.execute("SELECT earned FROM users WHERE user_id = ?", (user_id,)) as cur_u:
                u_row = await cur_u.fetchone()
            if not u_row:
                await db.execute("ROLLBACK")
                return False, "Пользователь не найден!", 0.0, 0.0, 0.0
            cur_earned = u_row[0]

            async with db.execute(
                "SELECT SUM(net_amount) FROM transfer_logs WHERE recipient_id = ? AND date >= ?",
                (user_id, cutoff_iso)
            ) as cur_h:
                h_row = await cur_h.fetchone()
                tx_hold = round(h_row[0], 2) if h_row and h_row[0] else 0.0

            avail_inside = max(0.0, round(cur_earned - tx_hold, 2))
            if avail_inside < price:
                await db.execute("ROLLBACK")
                hold_msg = f" (часть средств {format_raw_num(tx_hold)} на защитном холде 12ч)" if tx_hold > 0 else ""
                return False, f"Недостаточно доступных средств{hold_msg}! Доступно: {format_bold_num(avail_inside)}", 0.0, 0.0, 0.0

            cur = await db.execute(
                "UPDATE users SET earned = ROUND(earned - ? + ?, 2) WHERE user_id = ? AND earned >= ?",
                (price, drop, user_id, price)
            )
            if cur.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Недостаточно средств на балансе прибыли!", 0.0, 0.0, 0.0

            await db.execute(
                "INSERT INTO case_logs (user_id, box_id, box_name, price, drop_amount, profit_user, date) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, box_id, box["name"], price, drop, profit, now_iso)
            )
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🎰 Кейс', ?, ?, 'Начислено')",
                (user_id, drop, now_iso)
            )
            await db.commit()
            return True, box["name"], price, drop, profit
        except Exception:
            await db.execute("ROLLBACK")
            return False, "Ошибка базы данных при открытии кейса!", 0.0, 0.0, 0.0

"Ошибка базы данных при открытии кейса!", 0.0, 0.0, 0.0

# --- КОСТИ ПРОТИВ БОТА (PvE x1.8) ---
async def process_dice_pve_outcome(uid: int, amt: float, p_val: int, b_val: int) -> Tuple[str, float, float]:
    """Строгий расчет исходов: Игрок > Бот (WIN), Игрок < Бот (LOSS), Игрок == Бот (DRAW)."""
    now_iso = get_now_iso()
    amt = round(amt, 2)
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            if p_val > b_val:
                win_total = round(amt * DICE_MULTIPLIER, 2)
                profit = round(win_total - amt, 2)
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (win_total, uid))
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🎲 Выигрыш в кости (PvE)', ?, ?, 'Начислено')", (uid, win_total, now_iso))
                await db.execute("INSERT INTO dice_logs (user_id, bet, player_val, bot_val, result, win_amount, profit_user, date) VALUES (?, ?, ?, ?, 'WIN', ?, ?, ?)", (uid, amt, p_val, b_val, win_total, profit, now_iso))
                await db.commit()
                return "WIN", win_total, profit

            elif p_val < b_val:
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🎲 Проигрыш в кости (PvE)', ?, ?, 'Списано')", (uid, amt, now_iso))
                await db.execute("INSERT INTO dice_logs (user_id, bet, player_val, bot_val, result, win_amount, profit_user, date) VALUES (?, ?, ?, ?, 'LOSS', 0.0, ?, ?)", (uid, amt, p_val, b_val, -amt, now_iso))
                await db.commit()
                return "LOSS", 0.0, -amt

            else:
                # Строго равные очки — возврат ставки 1:1
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (amt, uid))
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '🎲 Ничья в костях (Возврат)', ?, ?, 'Возвращено')", (uid, amt, now_iso))
                await db.execute("INSERT INTO dice_logs (user_id, bet, player_val, bot_val, result, win_amount, profit_user, date) VALUES (?, ?, ?, ?, 'DRAW', ?, 0.0, ?)", (uid, amt, p_val, b_val, amt, now_iso))
                await db.commit()
                return "DRAW", amt, 0.0
        except Exception:
            await db.execute("ROLLBACK")
            return "DRAW", amt, 0.0

# --- КОСТИ PvP (ДУЭЛЬ ИГРОКОВ, КОМИССИЯ 3.33%, ЛИМИТ 3 ДУЭЛИ) ---
async def create_dice_pvp_lobby(creator_id: int, creator_name: str, bet: float) -> Tuple[bool, str, int]:
    """Создание лобби для дуэли с ограничением не более 3 активных столов на игрока."""
    u = await get_and_update_user(creator_id)
    from database.database import get_user_transfer_hold_amount
    hold_amt = await get_user_transfer_hold_amount(creator_id)
    avail = max(0.0, round(u['earned'] - hold_amt, 2))
    if avail < bet:
        hold_msg = f" (часть средств {format_raw_num(hold_amt)} на защитном холде 12ч)" if hold_amt > 0 else ""
        return False, f"❌ Недостаточно доступных средств{hold_msg}! Доступно: {format_bold_num(avail)}", 0

    now_iso = get_now_iso()
    bet = round(bet, 2)
    
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            
            # Проверка лимита активных дуэлей
            async with db.execute(
                "SELECT COUNT(*) FROM dice_pvp_lobbies WHERE creator_id = ? AND status = 'OPEN'",
                (creator_id,)
            ) as cur:
                count_row = await cur.fetchone()
                active_count = count_row[0] if count_row else 0

            if active_count >= MAX_ACTIVE_PVP_LOBBIES:
                await db.execute("ROLLBACK")
                return False, "❌ Вы не можете создать более 3 открытых дуэлей одновременно! Дождитесь завершения или отмените предыдущие.", 0

            # Атомарное списание ставки
            cur_debit = await db.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?", (bet, creator_id, bet))
            if cur_debit.rowcount == 0:
                await db.execute("ROLLBACK")
                return False, "Недостаточно средств на балансе чистой прибыли!", 0

            lobby_cur = await db.execute(
                "INSERT INTO dice_pvp_lobbies (creator_id, creator_name, bet, status, created_at) VALUES (?, ?, ?, 'OPEN', ?)",
                (creator_id, creator_name, bet, now_iso)
            )
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Ставка Кости PvP', ?, ?, 'Списано')", (creator_id, bet, now_iso))
            await db.commit()
            return True, "Лобби успешно создано!", lobby_cur.lastrowid
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка создания дуэли: {e}", 0

async def get_dice_pvp_lobby_by_id(lobby_id: int) -> Optional[Tuple]:
    """Получение данных конкретного стола для deep-link ссылок."""
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, creator_id, creator_name, bet, status, created_at FROM dice_pvp_lobbies WHERE id = ?",
            (lobby_id,)
        ) as cur:
            return await cur.fetchone()

async def cancel_dice_pvp_lobby(lobby_id: int, user_id: int) -> Tuple[bool, str]:
    """Отмена созданной дуэли с полным возвратом ставки на баланс."""
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT bet, status FROM dice_pvp_lobbies WHERE id = ? AND creator_id = ?", (lobby_id, user_id)) as cur:
                row = await cur.fetchone()
            if not row or row[1] != "OPEN":
                await db.execute("ROLLBACK")
                return False, "Дуэль уже сыграна или не существует!"

            bet = round(row[0], 2)
            await db.execute("UPDATE dice_pvp_lobbies SET status = 'CANCELLED' WHERE id = ?", (lobby_id,))
            await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, user_id))
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Отмена Кости PvP', ?, ?, 'Возвращено')", (user_id, bet, now_iso))
            await db.commit()
            return True, "Ставка возвращена на ваш баланс!"
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка отмены дуэли: {e}"

async def rollback_dice_pvp_match(lobby_id: int, reason: str = "Сбой сети") -> bool:
    """Аварийный откат зависшей или сорванной дуэли с возвратом 100% ставок обоим игрокам."""
    now_iso = get_now_iso()
    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT creator_id, opponent_id, bet, status FROM dice_pvp_lobbies WHERE id = ?", (lobby_id,)) as cur:
                lobby = await cur.fetchone()

            if not lobby or lobby[3] in ["CANCELLED", "FINISHED"]:
                await db.execute("ROLLBACK")
                return False

            c_id, opp_id, bet, status = lobby
            bet = round(bet, 2)
            await db.execute("UPDATE dice_pvp_lobbies SET status = 'CANCELLED' WHERE id = ?", (lobby_id,))

            # Возврат создателю
            await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, c_id))
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Откат дуэли (Возврат)', ?, ?, 'Возвращено')", (c_id, bet, now_iso))

            # Если оппонент успел списать ставку
            if opp_id and status == "PLAYING":
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, opp_id))
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Откат дуэли (Возврат)', ?, ?, 'Возвращено')", (opp_id, bet, now_iso))

            await db.commit()
            return True
        except Exception:
            await db.execute("ROLLBACK")
            return False

async def get_user_open_pvp_lobbies(user_id: int) -> List[Tuple]:
    """Получение открытых столов текущего пользователя."""
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, creator_id, creator_name, bet, created_at FROM dice_pvp_lobbies WHERE creator_id = ? AND status = 'OPEN' ORDER BY id DESC",
            (user_id,)
        ) as cur:
            return await cur.fetchall()

async def get_other_open_dice_pvp_lobbies(user_id: int) -> List[Tuple]:
    """Получение открытых столов ДРУГИХ игроков для вступления."""
    async with get_db_connection() as db:
        async with db.execute(
            "SELECT id, creator_id, creator_name, bet, created_at FROM dice_pvp_lobbies WHERE creator_id != ? AND status = 'OPEN' ORDER BY id DESC LIMIT 10",
            (user_id,)
        ) as cur:
            return await cur.fetchall()

async def get_open_dice_pvp_lobbies() -> List[Tuple]:
    """Получение списка активных открытых столов."""
    async with get_db_connection() as db:
        async with db.execute("SELECT id, creator_id, creator_name, bet, created_at FROM dice_pvp_lobbies WHERE status = 'OPEN' ORDER BY id DESC LIMIT 10") as cur:
            return await cur.fetchall()

async def play_dice_pvp_match(lobby_id: int, opponent_id: int, opponent_name: str, p1_val: int, p2_val: int) -> Tuple[bool, str, Dict[str, Any]]:
    """Проведение и атомарная фиксация результатов дуэли между игроками."""
    await get_and_update_user(opponent_id)
    now_iso = get_now_iso()

    async with get_db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT creator_id, creator_name, bet, status FROM dice_pvp_lobbies WHERE id = ?", (lobby_id,)) as cur:
                lobby = await cur.fetchone()

            if not lobby or lobby[3] not in ["OPEN", "PLAYING"]:
                await db.execute("ROLLBACK")
                return False, "Данная дуэль уже недоступна!", {}

            c_id, c_name, bet, status = lobby
            bet = round(bet, 2)
            if int(c_id) == int(opponent_id):
                await db.execute("ROLLBACK")
                return False, "Вы не можете играть против самого себя!", {}

            # Если ставка еще не была списана в лобби
            if status == "OPEN":
                cur_opp = await db.execute("UPDATE users SET earned = ROUND(earned - ?, 2) WHERE user_id = ? AND earned >= ?", (bet, opponent_id, bet))
                if cur_opp.rowcount == 0:
                    await db.execute("ROLLBACK")
                    return False, "Недостаточно средств на балансе прибыли для принятия дуэли!", {}

            # Закрываем лобби
            await db.execute(
                "UPDATE dice_pvp_lobbies SET status = 'FINISHED', opponent_id = ?, creator_val = ?, opponent_val = ? WHERE id = ?",
                (opponent_id, p1_val, p2_val, lobby_id)
            )

            # Определение исхода дуэли
            if p1_val > p2_val:
                winner_id = c_id
                winner_name = c_name
                loser_id = opponent_id
                is_draw = False
            elif p2_val > p1_val:
                winner_id = opponent_id
                winner_name = opponent_name
                loser_id = c_id
                is_draw = False
            else:
                is_draw = True

            total_pot = round(bet * 2, 2)
            if is_draw:
                # Возврат обеих ставок 100%
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, c_id))
                await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (bet, opponent_id))
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Ничья Кости PvP', ?, ?, 'Возвращено')", (c_id, bet, now_iso))
                await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Ничья Кости PvP', ?, ?, 'Возвращено')", (opponent_id, bet, now_iso))
                await db.commit()
                return True, "Ничья!", {"is_draw": True, "bet": bet}

            # Выплата победителю за вычетом 3.33% комиссии
            fee = round(total_pot * DICE_PVP_FEE_PERCENT, 2)
            win_net = round(total_pot - fee, 2)

            await db.execute("UPDATE users SET earned = ROUND(earned + ?, 2) WHERE user_id = ?", (win_net, winner_id))
            await db.execute("UPDATE dice_pvp_lobbies SET winner_id = ? WHERE id = ?", (winner_id, lobby_id))
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Победа Кости PvP', ?, ?, 'Зачислено')", (winner_id, win_net, now_iso))
            await db.execute("INSERT INTO transactions (user_id, type, amount, date, status) VALUES (?, '⚔️ Поражение Кости PvP', ?, ?, 'Списано')", (loser_id, bet, now_iso))
            
            await db.commit()

            result_data = {
                "is_draw": False,
                "creator_id": c_id,
                "creator_name": c_name,
                "opponent_id": opponent_id,
                "opponent_name": opponent_name,
                "bet": bet,
                "p1_val": p1_val,
                "p2_val": p2_val,
                "winner_id": winner_id,
                "winner_name": winner_name,
                "loser_id": loser_id,
                "win_net": win_net,
                "fee": fee
            }
            return True, "Матч завершен!", result_data
        except Exception as e:
            await db.execute("ROLLBACK")
            return False, f"Ошибка при проведении дуэли: {e}", {}