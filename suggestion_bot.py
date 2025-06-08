import json
import time
import logging
import os

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- НАСТРОЙКИ ---
SUGGESTION_BOT_TOKEN = os.environ.get("SUGGESTION_BOT_TOKEN", "8004330201:AAHTfPT9gn16pFcQP5FDP2SJ2weZhcKfYy4")

# Ваш Telegram ID, куда будут приходить предложения
OWNER_ID = 6238356535

# Настройки кулдауна и файла для хранения
COOLDOWN_FILE = "cooldowns.json"
COOLDOWN_SECONDS = 15 * 60  # 15 минут

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - SUGGESTION_BOT - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Функции для работы с файлом кулдаунов ---

def load_cooldowns():
    """Загружает данные о кулдаунах из JSON-файла."""
    try:
        with open(COOLDOWN_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Если файл не найден или пуст/некорректен, возвращаем пустой словарь
        return {}

def save_cooldowns(cooldown_data):
    """Сохраняет данные о кулдаунах в JSON-файл."""
    try:
        with open(COOLDOWN_FILE, 'w') as f:
            json.dump(cooldown_data, f, indent=4)
    except IOError as e:
        logger.error(f"Не удалось сохранить файл с кулдаунами: {e}")

# --- Обработчики команд ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветственное сообщение."""
    await update.message.reply_text(
        "Здравствуйте! Я бот для сбора предложений по улучшению Forumnik.\n"
        "Просто отправьте мне вашу идею текстом, и я передам ее разработчику."
    )

async def handle_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает полученное предложение с проверкой кулдауна из файла."""
    user = update.effective_user
    
    # 1. Загружаем текущие кулдауны из файла
    cooldowns = load_cooldowns()
    user_id_str = str(user.id) # Ключи в JSON должны быть строками
    
    # 2. Проверяем кулдаун
    current_time = int(time.time())
    last_post_time = cooldowns.get(user_id_str)

    if last_post_time:
        time_since_last_post = current_time - last_post_time
        if time_since_last_post < COOLDOWN_SECONDS:
            remaining_time = COOLDOWN_SECONDS - time_since_last_post
            remaining_minutes = (remaining_time // 60) + 1
            logger.warning(f"Пользователь {user.id} попытался отправить предложение во время кулдауна.")
            await update.message.reply_text(f"⏳ Вы уже отправляли предложение недавно. Следующую идею можно будет отправить примерно через {remaining_minutes} мин.")
            return

    # 3. Если кулдауна нет, пересылаем предложение владельцу
    try:
        await context.bot.forward_message(
            chat_id=OWNER_ID,
            from_chat_id=user.id,
            message_id=update.message.message_id
        )
        logger.info(f"Сообщение от {user.id} ({user.username}) успешно переслано владельцу.")
    except Exception as e:
        logger.error(f"Не удалось переслать сообщение владельцу: {e}")
        await update.message.reply_text("❌ Произошла ошибка при отправке вашего предложения. Пожалуйста, попробуйте позже.")
        return

    # 4. Обновляем время в словаре и сохраняем в файл
    cooldowns[user_id_str] = current_time
    save_cooldowns(cooldowns)

    # 5. Отправляем подтверждение пользователю
    await update.message.reply_text("✅ Ваше предложение успешно передано! Спасибо за вашу идею.")

def main() -> None:
    """Запускает бота."""
    if "ВАШ_НОВЫЙ_ТОКЕН_ЗДЕСЬ" in SUGGESTION_BOT_TOKEN:
        logger.error("КРИТИЧЕСКАЯ ОШИБКА: Токен бота не установлен! Отредактируйте файл suggestion_bot.py.")
        return

    # База данных больше не нужна, убираем ее из контекста
    application = Application.builder().token(SUGGESTION_BOT_TOKEN).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_suggestion))
    
    logger.info("Бот для сбора предложений (файловая версия) запущен...")
    application.run_polling()
    logger.info("Бот для сбора предложений остановлен.")

if __name__ == "__main__":
    main()