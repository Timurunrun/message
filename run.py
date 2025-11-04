import asyncio
import signal
from loguru import logger
from dotenv import load_dotenv
import os

from app.core.config import AppConfig
from app.core.storage import Storage
from app.core.hub import Hub
from app.connectors.telegram_connector import TelegramConnector
from app.connectors.vk_connector import VKConnector
from app.crm.service import AmoCRMService
from app.crm.deps import set_amocrm_service
from app.ai.tools import register_amocrm_tools


async def main() -> None:
    load_dotenv()
    config = AppConfig.load_from_env()

    storage = Storage(db_path=config.db_path)
    await storage.initialize()

    connectors = []
    amocrm_service = None
    if config.amocrm_base_url and config.amocrm_access_token:
        try:
            amocrm_service = await AmoCRMService.create(
                base_url=config.amocrm_base_url,
                access_token=config.amocrm_access_token,
                storage=storage,
            )
            set_amocrm_service(amocrm_service)
            register_amocrm_tools()
            logger.info("AmoCRM сервис и инструменты инициализированы")
        except Exception as e:
            logger.error("Не удалось инициализировать AmoCRM: {}", e)
    else:
        logger.warning("Параметры AmoCRM не заданы. Интеграция будет отключена")
    if config.telegram_bot_token:
        connectors.append(TelegramConnector(bot_token=config.telegram_bot_token, on_clear_db=storage.clear_all))
    else:
        logger.warning("Токен API Telegram-бота TELEGRAM_BOT_TOKEN не задан; Telegram-коннектор будет отключён")

    if getattr(config, "vk_tokens", None):
        connectors.append(VKConnector(tokens=config.vk_tokens))
    else:
        logger.warning("Токены API VK-сообщества VK_COMMUNITY_TOKENS не заданы; VK-коннектор будет отключён")

    if not connectors:
        logger.error("Ни один коннектор не подключен. Для запуска укажите хотя бы один токен API в .env")
        return

    hub = Hub(storage=storage, connectors=connectors, config=config, crm_service=amocrm_service)

    stop_event = asyncio.Event()

    def _request_shutdown():
        if not stop_event.is_set():
            logger.info("Получен сигнал завершения работы")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # На Windows не работают обработчики сигналов
            pass

    await hub.start()
    logger.info("Хаб запущен. Ожидание сообщений...")

    try:
        await stop_event.wait()
    finally:
        logger.info("Остановка хаба...")
        await hub.stop()
        await storage.close()
        if amocrm_service:
            await amocrm_service.close()
        logger.info("Хаб остановлен.")


if __name__ == "__main__":
    asyncio.run(main())
