import sqlite3
import requests
import time
import sys
import logging
import os
import html # <-- Добавляем необходимый импорт

# --- Настройки ---
TOKEN_FROM_ENV = os.environ.get("TELEGRAM_BOT_TOKEN")
BOT_TOKEN = TOKEN_FROM_ENV if TOKEN_FROM_ENV else "7944979086:AAH-tlkkPLDxMUIwCrcQluIZbSARrCVN_f8"

DB_NAME = "forumnik_3_0.db"
USERS_TABLE_NAME = "Users_DB"
WHITELIST_TABLE_NAME = "judge_white_list"

logging.basicConfig(format="%(asctime)s - NOTIFIER - %(levelname)s - %(message)s", level=logging.INFO)

def get_judge_tg_id(conn, judge_nick_name):
    if not judge_nick_name: return None
    cursor = conn.cursor()
    cursor.execute(f"SELECT tg_user_id FROM {USERS_TABLE_NAME} WHERE nick_name = ?", (judge_nick_name,))
    result = cursor.fetchone()
    return result[0] if result else None

def send_notification(message_text, target_user_id=None):
    if not BOT_TOKEN or "ВАШ" in BOT_TOKEN:
        logging.error("Токен бота не найден!")
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    user_ids_to_notify = []
    if target_user_id:
        user_ids_to_notify.append(target_user_id)
    else: 
        cursor.execute(f"""
            SELECT u.tg_user_id FROM {USERS_TABLE_NAME} u
            JOIN {WHITELIST_TABLE_NAME} w ON u.nick_name = w.nick_name
            WHERE u.authorization = 1
        """)
        results = cursor.fetchall()
        user_ids_to_notify = [row[0] for row in results]
    
    conn.close()

    if not user_ids_to_notify:
        logging.info("Не найдено пользователей для рассылки.")
        return
    
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    success_count, fail_count = 0, 0

    logging.info(f"Начинаю рассылку для {len(user_ids_to_notify)} пользователей...")
    for user_id in user_ids_to_notify:
        payload = {'chat_id': user_id, 'text': message_text, 'parse_mode': 'HTML'}
        
        try:
            response = requests.post(url, json=payload, timeout=5)
            if response.status_code == 200:
                success_count += 1
            else:
                # Улучшенное логирование
                logging.warning(f"Ошибка отправки пользователю {user_id}. Статус: {response.status_code}, Ответ: {response.text}")
                fail_count += 1
        except Exception as e:
            logging.error(f"Критическая ошибка при отправке сообщения пользователю {user_id}: {e}")
            fail_count += 1
        
        time.sleep(0.1)

    logging.info(f"Рассылка завершена. Успешно: {success_count}, Ошибки: {fail_count}")

if __name__ == "__main__":
    # Аргументы: python notifier.py "Тип уведомления" "Заголовок/текст" "ID" [опционально: tg_user_id]
    if len(sys.argv) > 3:
        notification_type = sys.argv[1]
        content = sys.argv[2]
        item_id = sys.argv[3]
        target_id = sys.argv[4] if len(sys.argv) > 4 else None
        
        # Формируем сообщение здесь, экранируя переменные части
        if notification_type == "new_case":
            message = f"📢 Поступил новый иск!\n\n<b>#{item_id}: {html.escape(content)}</b>\n\nИспользуйте <code>/list</code> в боте для просмотра."
        elif notification_type == "new_reply":
            message = f"🔔 По вашему иску <b>#{item_id}: {html.escape(content)}</b> появился новый ответ! Проверьте форум."
        else:
            message = html.escape(content) # По умолчанию просто экранируем
            
        send_notification(message, target_id)
    else:
        print("Ошибка: недостаточно аргументов.")