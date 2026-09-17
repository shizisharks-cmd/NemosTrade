import html
import asyncio
import logging
from typing import Callable, Dict, Any, Optional
from aiogram import Bot, Dispatcher, BaseMiddleware, types
from aiogram.types import TelegramObject
from aiogram.fsm.storage.memory import MemoryStorage
from config.config import TOKEN, REDIS_URL
from database.database import init_db, auto_rollback_interrupted_pvp_lobbies
from handlers.user import router as user_router
from handlers.game import router as game_router
from handlers.admin import router as admin_router
from services.tasks import (
    daily_morning_digest_loop,
    owner_morning_cash_digest_loop,
    inactivity_reminder_loop,
    cache_cleanup_loop,
    auto_archive_and_cleanup_loop,
    auto_cancel_expired_pvp_lobbies_loop
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s"
)
logger = logging.getLogger("NemosTrade")

# --- THROTTLING MIDDLEWARE (ГЛОБАЛЬНАЯ ЗАЩИТА ОТ ФЛУДА И СПАМА) ---
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, rate_limit: float = 0.5):
        self.rate_limit = rate_limit
        self.user_timestamps: Dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Any],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: Optional[types.User] = data.get("event_from_user")
        if user:
            user_id = user.id
            # Исключение для владельца и администраторов — свободное управление без троттлинга
            from config.config import OWNER_ID
            if user_id == OWNER_ID:
                return await handler(event, data)
            from database.database import is_admin
            if await is_admin(user_id):
                return await handler(event, data)

            now = asyncio.get_event_loop().time()
            last_time = self.user_timestamps.get(user_id, 0.0)
            if now - last_time < self.rate_limit:
                if isinstance(event, types.CallbackQuery):
                    try:
                        await event.answer("⏳ Не так быстро!", show_alert=False)
                    except Exception:
                        pass
                return
            self.user_timestamps[user_id] = now
            # Периодическая очистка старых временных меток
            if len(self.user_timestamps) > 10000:
                self.user_timestamps = {k: v for k, v in self.user_timestamps.items() if now - v < 5.0}
        return await handler(event, data)

# Подключение Redis или Fallback на MemoryStorage
try:
    from aiogram.fsm.storage.redis import RedisStorage
    import redis.asyncio as aioredis
    redis_client = aioredis.from_url(REDIS_URL)
    storage = RedisStorage(redis=redis_client)
    logger.info("📦 Подключено хранилище состояний Redis Storage")
except Exception:
    storage = MemoryStorage()
    logger.info("⚠️ Используется MemoryStorage (Redis не обнаружен или отключен)")

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=storage)

# Подключение middleware для защиты от флуда
dp.message.middleware(ThrottlingMiddleware(rate_limit=0.5))
dp.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.3))

# Регистрация роутеров
dp.include_router(user_router)
dp.include_router(game_router)
dp.include_router(admin_router)

# Глобальный перехватчик необработанных ошибок с алертом владельцу
@dp.error()
async def global_error_handler(event: types.ErrorEvent, bot: Bot):
    logger.error(f"Необработанная ошибка в событии: {event.exception}", exc_info=event.exception)
    try:
        err_str = str(event.exception).lower()
        if "query is too old" in err_str or "message is not modified" in err_str or "query id is invalid" in err_str or "bot was blocked" in err_str:
            return
        from services.tasks import send_owner_error_alert
        err_name = type(event.exception).__name__
        await send_owner_error_alert(bot, f"Событие {err_name}", str(event.exception))
    except Exception:
        pass

async def main():
    # Инициализация схемы БД
    await init_db()
    
    # Автоматический откат незавершенных игр при рестарте
    recovered = await auto_rollback_interrupted_pvp_lobbies()
    if recovered > 0:
        logger.info(f"🔄 Восстановлено незавершенных дуэлей после рестарта: {recovered}")
    
    # Запуск фоновых асинхронных служб
    bg_tasks = [
        asyncio.create_task(cache_cleanup_loop()),
        asyncio.create_task(daily_morning_digest_loop(bot)),
        asyncio.create_task(owner_morning_cash_digest_loop(bot)),
        asyncio.create_task(inactivity_reminder_loop(bot)),
        asyncio.create_task(auto_cancel_expired_pvp_lobbies_loop()),
        asyncio.create_task(auto_archive_and_cleanup_loop())
    ]
    
    # Проверка флага уведомления об успешном обновлении
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    notify_flag = os.path.join(base_dir, ".restart_notify.txt")
    if os.path.exists(notify_flag):
        try:
            with open(notify_flag, "r", encoding="utf-8") as nf:
                content = nf.read().strip()
            os.remove(notify_flag)
            if ":" in content:
                c_parts = content.split("\n", 1)
                owner_uid, count = c_parts[0].split(":", 1)
                changelog_text = c_parts[1].strip() if len(c_parts) > 1 else ""
                from handlers.admin import BOT_VERSION, CURRENT_CHANGELOG
                if not changelog_text:
                    changelog_text = CURRENT_CHANGELOG
                cl_block = ""
                if changelog_text:
                    cl_block = f"\n━━━━━━━━━━━━━━━━━━\n📝 <b>Что нового в этой версии:</b>\n{html.escape(changelog_text)}"
                await bot.send_message(
                    chat_id=int(owner_uid),
                    text=(
                        "🚀 <b>СИСТЕМА УСПЕШНО ОБНОВЛЕНА И ПЕРЕЗАПУЩЕНА!</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        f"🏷 <b>Версия ядра:</b> <code>{BOT_VERSION}</code> 🚀\n"
                        f"📦 Обновлено файлов: <b>{count}</b>"
                        f"{cl_block}\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "⚡ Бот работает на новой версии!"
                    ),
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о перезапуске: {e}")

    logger.info("🚀 Бот Nemos Trade успешно запущен и готов к работе!")
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("🛑 Остановка бота и завершение фоновых служб...")
        for task in bg_tasks:
            task.cancel()
        try:
            recovered = await auto_rollback_interrupted_pvp_lobbies()
            if recovered > 0:
                logger.info(f"🔄 Откачено незавершенных дуэлей при остановке: {recovered}")
        except Exception as e:
            logger.error(f"Ошибка отката при остановке: {e}")
        from database.database import close_db_pool
        await close_db_pool()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот корректно остановлен.")