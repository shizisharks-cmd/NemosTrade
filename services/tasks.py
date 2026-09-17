import asyncio
import aiosqlite
import logging
from datetime import datetime, timedelta
from typing import Dict
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from config.config import (
    DB_FILE, OWNER_ID, get_now, get_now_iso, format_bold_num, format_raw_num, EMOJIS
)
from database.database import (
    get_and_update_user, get_platform_stats_live, get_db_connection,
    auto_cancel_expired_pvp_lobbies, auto_cancel_expired_mines_games
)

logger = logging.getLogger(__name__)

subscription_cache: Dict[int, float] = {}

async def check_user_subscription(bot: Bot, uid: int, channel_username: str) -> bool:
    """Проверка подписки пользователя на официальный новостной канал с кэшированием."""
    import time
    now_ts = time.time()
    if uid in subscription_cache and (now_ts - subscription_cache[uid]) < 600:
        return True
    try:
        m = await bot.get_chat_member(chat_id=channel_username, user_id=uid)
        is_sub = m.status in ["creator", "administrator", "member"]
        if is_sub:
            subscription_cache[uid] = now_ts
        return is_sub
    except Exception as e:
        logger.warning(f"Не удалось проверить подписку для {uid}: {e}")
        return True

async def cache_cleanup_loop():
    """Фоновая очистка устаревшего кэша подписок каждые 5 минут."""
    import time
    while True:
        await asyncio.sleep(300)
        now_ts = time.time()
        expired = [k for k, v in list(subscription_cache.items()) if now_ts - v > 600]
        for k in expired:
            subscription_cache.pop(k, None)

async def owner_morning_cash_digest_loop(bot: Bot):
    """Ежедневная сводка состояния кассы для владельца в O9:OO МСК."""
    last_sent_date = None
    while True:
        try:
            now = get_now()
            if now.hour == 9 and now.strftime("%Y-%m-%d") != last_sent_date:
                last_sent_date = now.strftime("%Y-%m-%d")
                st = await get_platform_stats_live()
                cnt, inv, earn = st.get("cnt", 0), st.get("inv", 0.0), st.get("earn", 0.0)
                d_profit = st.get("d_profit", 0.0)
                fees = st.get("fees", 0.0)
                pvp_profit = st.get("pvp_profit", 0.0)
                from database.database import get_mines_stats
                m_st = await get_mines_stats()
                m_profit = m_st.get("profit", 0.0)

                daily_pay = round(inv * 0.03, 2)
                d_profit_sign = "🟢 +" if d_profit >= 0 else "🔴 "
                m_profit_sign = "🟢 +" if m_profit >= 0 else "🔴 "
                pvp_profit_sign = "🟢 +" if pvp_profit >= 0 else "🔴 "

                digest = (
                    f"{EMOJIS['stats']} <b>УТРЕННЯЯ СВОДКА КАССЫ (O9:OO МСК)</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"👥 <b>Всего пользователей:</b> <b>{cnt} чел.</b>\n"
                    f"{EMOJIS['briefcase']} <b>Общий пул депозитов:</b> {format_bold_num(inv)}\n"
                    f"💳 <b>Балансы к выводу:</b> {format_bold_num(earn)}\n"
                    f"⏱ <b>Суточные обязательства (+3%):</b> <b>+{format_bold_num(daily_pay)}</b>\n"
                    f"💣 <b>Доход кассы с Минёра:</b> <b>{m_profit_sign}{format_raw_num(abs(m_profit))}</b>\n"
                    f"🎲 <b>Доход с костей (PvE):</b> <b>{d_profit_sign}{format_raw_num(abs(d_profit))}</b>\n"
                    f"⚔️ <b>Доход с дуэлей (PvP):</b> <b>{pvp_profit_sign}{format_raw_num(abs(pvp_profit))}</b>\n"
                    f"{EMOJIS['dollar']} <b>Доход с комиссий переводов:</b> {format_bold_num(fees)}\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🟢 <i>Система Nemos Trade функционирует стабильно!</i>"
                )
                await bot.send_message(OWNER_ID, digest, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка в утренней сводке кассы: {e}")
        await asyncio.sleep(60)

async def daily_morning_digest_loop(bot: Bot):
    """Утренняя рассылка для активных инвесторов в 10:00 МСК с курсорной пагинацией."""
    last_sent_date = None
    while True:
        try:
            now = get_now()
            if now.hour == 10 and now.strftime("%Y-%m-%d") != last_sent_date:
                last_sent_date = now.strftime("%Y-%m-%d")
                last_seen_uid = 0
                batch_size = 100
                
                while True:
                    async with get_db_connection() as db:
                        async with db.execute(
                            "SELECT user_id FROM users WHERE user_id > ? AND invested > 0 AND is_banned = 0 AND is_blocked = 0 ORDER BY user_id ASC LIMIT ?",
                            (last_seen_uid, batch_size)
                        ) as cur:
                            users = await cur.fetchall()
                    
                    if not users:
                        break
                    
                    for (uid,) in users:
                        last_seen_uid = uid
                        try:
                            u = await get_and_update_user(uid)
                            text = (
                                "🌅 <b>Доброе утро, инвестор!</b>\n"
                                "━━━━━━━━━━━━━━━━━━\n"
                                f"{EMOJIS['briefcase']} <b>Капитал в обороте:</b>\n↳ {format_bold_num(u['invested'])}\n\n"
                                f"💳 <b>Чистая прибыль к выводу:</b>\n↳ {format_bold_num(u['earned'])}\n"
                                "━━━━━━━━━━━━━━━━━━\n"
                                "🚀 <i>Деньги работают на вас 24/7! Проверьте баланс в личном кабинете.</i>"
                            )
                            await bot.send_message(uid, text, parse_mode="HTML")
                            await asyncio.sleep(0.04)
                        except TelegramRetryAfter as e:
                            await asyncio.sleep(e.retry_after)
                        except TelegramForbiddenError:
                            async with get_db_connection() as db:
                                await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (uid,))
                                await db.commit()
                        except Exception as e:
                            logger.warning(f"Ошибка отправки дайджеста для {uid}: {e}")
        except Exception as e:
            logger.error(f"Ошибка в утренней рассылке инвесторам: {e}")
        await asyncio.sleep(60)

async def inactivity_reminder_loop(bot: Bot):
    """Напоминания неактивным пользователям с депозитом <= 50,000$ по курсору."""
    while True:
        try:
            now = get_now()
            last_seen_uid = 0
            batch_size = 100
            
            while True:
                async with get_db_connection() as db:
                    async with db.execute(
                        "SELECT user_id, last_reminder, reminder_step FROM users WHERE user_id > ? AND invested <= 50000.0 AND is_banned = 0 AND is_blocked = 0 ORDER BY user_id ASC LIMIT ?",
                        (last_seen_uid, batch_size)
                    ) as cur:
                        users = await cur.fetchall()

                if not users:
                    break

                for uid, last_rem, step in users:
                    last_seen_uid = uid
                    should_send = False
                    if not last_rem:
                        should_send = True
                    else:
                        try:
                            cleaned_rem = last_rem.replace("T", " ")[:19]
                            dt_rem = datetime.strptime(cleaned_rem, "%Y-%m-%d %H:%M:%S").replace(tzinfo=now.tzinfo)
                            if (now - dt_rem).total_seconds() >= 86400:
                                should_send = True
                        except Exception:
                            should_send = True

                    if should_send:
                        msg_text = (
                            "⏳ <b>Ваш капитал может приносить больше!</b>\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "<b>Деньги в Grand Mobile должны работать на вас, а не лежать без дела.</b>\n\n"
                            f"{EMOJIS['briefcase']} <b>Плюсы открытия депозита:</b>\n"
                            "• 📈 <b>+3% чистой прибыли каждые 24 часа</b> (начисление каждую секунду онлайн)\n"
                            "• 💳 <b>Быстрый и прозрачный вывод через Drom</b>\n"
                            f"• {EMOJIS['shield']} <b>Полная безопасность ваших средств</b>\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "🚀 <i>Откройте свой первый депозит и начните получать прибыль прямо сейчас!</i>"
                        ) if step % 2 == 0 else (
                            f"{EMOJIS['stats']} <b>Не упускайте ежедневную прибыль в Nemos Trade!</b>\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "<b>Пока вы играете, ваш баланс мог бы расти каждую минуту на полном пассиве.</b>\n\n"
                            f"💡 <i>Пример:</i> вклад <b>1O,OOO,OOO$</b> приносит <b>+3OO,OOO$</b> чистой прибыли каждый день!\n"
                            "━━━━━━━━━━━━━━━━━━\n"
                            "👇 <b>Нажмите «📥 Пополнить» в меню ниже, чтобы открыть депозит:</b>"
                        )
                        try:
                            await bot.send_message(uid, msg_text, parse_mode="HTML")
                            next_step = (step + 1) % 2
                            now_iso = get_now_iso()
                            async with get_db_connection() as db:
                                await db.execute("UPDATE users SET last_reminder = ?, reminder_step = ? WHERE user_id = ?", (now_iso, next_step, uid))
                                await db.commit()
                            await asyncio.sleep(0.04)
                        except TelegramRetryAfter as e:
                            await asyncio.sleep(e.retry_after)
                        except TelegramForbiddenError:
                            async with get_db_connection() as db:
                                await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (uid,))
                                await db.commit()
                        except Exception as e:
                            logger.warning(f"Ошибка отправки дайджеста для {uid}: {e}")

        except Exception as e:
            logger.error(f"Ошибка в цикле напоминаний: {e}")
        await asyncio.sleep(3600)

async def auto_cancel_expired_pvp_lobbies_loop():
    """Фоновый процесс: проверка и автоотмена зависших дуэлей и партий Минёра старше 24 часов каждый час."""
    while True:
        await asyncio.sleep(3600)
        try:
            cancelled = await auto_cancel_expired_pvp_lobbies()
            if cancelled > 0:
                logger.info(f"Автоматически отменено зависших PvP-дуэлей (24ч): {cancelled}")
            m_settled = await auto_cancel_expired_mines_games()
            if m_settled > 0:
                logger.info(f"Автоматически рассчитано зависших сессий Минёра (24ч): {m_settled}")
        except Exception as e:
            logger.error(f"Ошибка в цикле автоотмены дуэлей/минёра: {e}")

async def auto_archive_and_cleanup_loop():
    """Фоновая архивация старых транзакций, логов и завершенных дуэлей по ISO 8601."""
    while True:
        await asyncio.sleep(86400)
        try:
            now = get_now()
            limit_date_iso = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
            log_limit_iso = (now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
            
            async with get_db_connection() as db:
                await db.execute("BEGIN IMMEDIATE")
                await db.execute("""INSERT OR IGNORE INTO transactions_archive 
                    SELECT id, user_id, type, amount, date, status, admin_chat_id, admin_msg_id 
                    FROM transactions WHERE date < ? AND status IN ('Одобрено', 'Отклонено', 'Выполнено', 'Зачислено', 'Списано')""", (limit_date_iso,))
                
                await db.execute("DELETE FROM transactions WHERE date < ? AND status IN ('Одобрено', 'Отклонено', 'Выполнено', 'Зачислено', 'Списано')", (limit_date_iso,))
                await db.execute("DELETE FROM admin_logs WHERE date < ?", (log_limit_iso,))
                
                pvp_limit_iso = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
                await db.execute("DELETE FROM dice_pvp_lobbies WHERE created_at < ? AND status IN ('FINISHED', 'CANCELLED')", (pvp_limit_iso,))
                
                await db.commit()
                logger.info("Архивация и очистка старых записей успешно выполнена.")

            # Периодическая оптимизация SQLite
            async with get_db_connection() as db:
                await db.execute("PRAGMA incremental_vacuum;")
                await db.execute("PRAGMA optimize;")
                await db.commit()
            logger.info("Профилактическая оптимизация базы данных (Vacuum/Optimize) выполнена.")
        except Exception as e:
            logger.error(f"Ошибка при очистке архивов: {e}")

# --- ГЕНЕРАЦИЯ БРЕНДИРОВАННЫХ ЭЛЕКТРОННЫХ ЧЕКОВ (PILLOW) ---
def generate_receipt_image(op_type: str, amount_str: str, client_str: str, date_str: str, method_str: str, tx_num: str) -> bytes:
    """Генерация фирменного электронного чека транзакции в темном стиле Nemos Trade со случайным фоном."""
    import io
    import os
    import random
    from PIL import Image, ImageDraw, ImageFont, ImageEnhance

    w, h = 800, 520

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    search_dirs = ['.', base_dir, '/home/container', os.path.join(base_dir, 'banners')]
    valid_exts = ('.png', '.jpg', '.jpeg', '.webp')
    found_bgs = set()

    for d in search_dirs:
        if os.path.isdir(d):
            try:
                for fn in os.listdir(d):
                    fn_lower = fn.lower()
                    if fn_lower.endswith(valid_exts):
                        # Исключаем баннер личного кабинета (banner.png)
                        if fn_lower.startswith('banner.') or fn_lower == 'banner.png':
                            continue
                        if fn_lower.startswith(('gemini_generated_image', 'banner1', 'banner2', 'banner3', 'bg')):
                            found_bgs.add(os.path.join(d, fn))
            except Exception:
                pass

    for d in search_dirs:
        for prefix in ['banner1', 'banner2', 'banner3', 'bg1', 'bg2', 'bg3']:
            for ext in valid_exts:
                candidate = os.path.join(d, prefix + ext)
                if os.path.isfile(candidate):
                    found_bgs.add(candidate)

    img = None
    if found_bgs:
        try:
            chosen_bg = random.choice(list(found_bgs))
            with Image.open(chosen_bg) as bg_img:
                bg_img = bg_img.convert('RGB')
                target_ratio = w / h
                img_ratio = bg_img.width / bg_img.height
                if img_ratio > target_ratio:
                    new_w = int(bg_img.height * target_ratio)
                    left = (bg_img.width - new_w) // 2
                    bg_img = bg_img.crop((left, 0, left + new_w, bg_img.height))
                else:
                    new_h = int(bg_img.width / target_ratio)
                    top = (bg_img.height - new_h) // 2
                    bg_img = bg_img.crop((0, top, bg_img.width, top + new_h))

                resized = bg_img.resize((w, h), Image.Resampling.LANCZOS)
                enhancer = ImageEnhance.Brightness(resized)
                img = enhancer.enhance(0.28)
        except Exception:
            img = None

    if img is None:
        img = Image.new('RGB', (w, h), color='#0f1115')

    draw = ImageDraw.Draw(img)

    # Внешние рамки
    draw.rounded_rectangle([(15, 15), (w - 15, h - 15)], radius=20, outline='#242b35', width=2)
    draw.rounded_rectangle([(22, 22), (w - 22, h - 22)], radius=16, outline='#1b2028', width=1)

    # Шапка чека
    draw.rounded_rectangle([(30, 30), (w - 30, 110)], radius=12, fill='#161920', outline='#2a3240', width=1)

    try:
        font_logo = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 30)
        font_sub = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 15)
        font_lbl = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 16)
        font_val = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 17)
        font_amt = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 28)
        font_foot = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 13)
    except Exception:
        font_logo = font_sub = font_lbl = font_val = font_amt = font_foot = ImageFont.load_default()

    draw.text((50, 42), 'NEMOS TRADE', fill='#00d2ff', font=font_logo)
    draw.text((50, 80), 'ОФИЦИАЛЬНЫЙ ЭЛЕКТРОННЫЙ ЧЕК ПЛАТФОРМЫ', fill='#8a95a5', font=font_sub)
    draw.text((w - 200, 55), f'#{tx_num}', fill='#606f85', font=font_amt)

    rows = [
        ('ТИП ОПЕРАЦИИ:', op_type, '#ffffff'),
        ('СУММА:', amount_str, '#00ff88'),
        ('ПОЛУЧАТЕЛЬ / КЛИЕНТ:', client_str, '#ffffff'),
        ('СЕРВЕР:', 'Grand Mobile #17', '#ffffff'),
        ('СПОСОБ:', method_str, '#ffffff'),
        ('ДАТА И ВРЕМЯ (МСК):', date_str, '#8a95a5'),
        ('СТАТУС:', 'ВЫПОЛНЕНО / УСПЕШНО', '#00ff88')
    ]

    y = 135
    for lbl, val, color in rows:
        draw.text((50, y), lbl, fill='#6b798d', font=font_lbl)
        draw.text((320, y), val, fill=color, font=font_val if lbl != 'СУММА:' else font_amt)
        draw.line([(50, y + 36), (w - 50, y + 36)], fill='#1c212a', width=1)
        y += 46

    draw.text((50, h - 45), 'Проверено сервисом безопасности Nemos Trade  •  @NemosTrade', fill='#4d5868', font=font_foot)

    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


# --- АЛЕРТЫ ОБ ОШИБКАХ ДЛЯ ВЛАДЕЛЬЦА В ТЕЛЕГРАМ ---
import html
import time
_last_owner_error_alerts: Dict[str, float] = {}

async def send_owner_error_alert(bot: Bot, error_title: str, error_detail: str):
    """Отправка алерта о критической ошибке в ЛС владельцу бота с защитой от спама."""
    now_ts = time.time()
    err_key = error_title[:40]
    if now_ts - _last_owner_error_alerts.get(err_key, 0.0) < 60.0:
        return
    _last_owner_error_alerts[err_key] = now_ts

    try:
        now_str = get_now().strftime("%d.%m.%Y %H:%M:%S").replace("0", "O")
        clean_detail = html.escape(str(error_detail)[:700])
        msg_text = (
            f"🚨 <b>АЛЕРТ СИСТЕМЫ NEMOS TRADE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Модуль:</b> {html.escape(error_title)}\n"
            f"❌ <b>Ошибка:</b> <code>{clean_detail}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <i>Время: {now_str} (МСК)</i>"
        )
        await bot.send_message(OWNER_ID, msg_text, parse_mode="HTML")
    except Exception:
        pass
