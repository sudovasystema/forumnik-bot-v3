import sqlite3
import requests
import time
import sys
import logging
import os # <-- Добавлен импорт os

# --- Настройки ---
TOKEN_FROM_ENV = os.environ.get("TELEGRAM_BOT_TOKEN")
BOT_TOKEN = TOKEN_FROM_ENV if TOKEN_FROM_ENV else "7944979086:AAH-tlkkPLDxMUIwCrcQluIZbSARrCVN_f8"

DB_NAME = "forumnik_3_0.db"
USERS_TABLE_NAME = "Users_DB"
WHITELIST_TABLE_NAME = "judge_white_list"

# Настройка логирования для этого скрипта
logging.basicConfig(
    format="%(asctime)s - NOTIFIER - %(levelname)s - %(message)s", level=logging.INFO
)

def send_notifications(case_title: str, case_id: str):
    """
    Рассылает уведомления о новом иске всем авторизованным судьям.
    """
    if not BOT_TOKEN:
        logging.error("Токен бота не найден! Установите переменную окружения TELEGRAM_BOT_TOKEN или укажите его в коде.")
        return

    # 1. Подключаемся к БД
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        logging.error(f"Критическая ошибка: не удалось подключиться к базе данных {DB_NAME}: {e}")
        return

    # 2. Получаем всех авторизованных пользователей из белого списка
    try:
        cursor.execute(f"""
            SELECT u.tg_user_id FROM {USERS_TABLE_NAME} u
            JOIN {WHITELIST_TABLE_NAME} w ON u.nick_name = w.nick_name
            WHERE u.authorization = 1
        """)
        all_users = cursor.fetchall()
    except Exception as e:
        logging.error(f"Ошибка получения пользователей из БД: {e}")
        conn.close()
        return
    
    conn.close()

    if not all_users:
        logging.info("Не найдено авторизованных судей для рассылки.")
        return

    # 3. Рассылаем сообщения
    message_text = f"📢 Внимание, поступил новый иск!\n\n<b>#{case_id}: {case_title}</b>\n\nИспользуйте <code>/list</code> в боте для просмотра."
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    success_count = 0
    fail_count = 0

    logging.info(f"Начинаю рассылку для {len(all_users)} пользователей...")
    for user_tuple in all_users:
        user_id = user_tuple[0]
        payload = {'chat_id': user_id, 'text': message_text, 'parse_mode': 'HTML'}
        
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                success_count += 1
            else:
                logging.warning(f"Ошибка отправки пользователю {user_id}: {response.text}")
                fail_count += 1
        except Exception as e:
            logging.error(f"Критическая ошибка при отправке сообщения пользователю {user_id}: {e}")
            fail_count += 1
        
        time.sleep(0.1) # Защита от лимитов Telegram

    logging.info(f"Рассылка завершена. Успешно: {success_count}, Ошибки: {fail_count}")

if __name__ == "__main__":
    if TOKEN_FROM_ENV:
        print("Используется токен из переменной окружения.")
    else:
        print("Используется токен, указанный в коде.")

    if len(sys.argv) > 2:
        case_title_arg = sys.argv[1]
        case_id_arg = sys.argv[2]
        send_notifications(case_title_arg, case_id_arg)
    else:
        print("Ошибка: недостаточно аргументов.")
        print("Использование: python notifier.py \"Название иска\" \"ID иска\"")