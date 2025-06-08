import logging
import sqlite3
import asyncio
import os
import json
import time
import subprocess

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from datetime import datetime, date
from contextlib import contextmanager
from telegram.error import Forbidden, BadRequest

from telegram import Update, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

from cryptography.fernet import Fernet

# --- Настройки ---
# --- Загрузка конфигурации из переменных окружения или использование значений по умолчанию ---

# 1. Токен Telegram бота
# Запасной токен, который будет использоваться, если переменная окружения не установлена
FALLBACK_TELEGRAM_BOT_TOKEN = "7944979086:AAH-tlkkPLDxMUIwCrcQluIZbSARrCVN_f8"
# Пытаемся получить токен из переменной окружения. Если ее нет, используем запасной.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", FALLBACK_TELEGRAM_BOT_TOKEN)

if TELEGRAM_BOT_TOKEN == FALLBACK_TELEGRAM_BOT_TOKEN:
    print("INFO: Переменная окружения TELEGRAM_BOT_TOKEN не найдена. Используется токен из кода.")
else:
    print("INFO: Используется TELEGRAM_BOT_TOKEN из переменной окружения.")

# 2. Ключ шифрования Fernet
FERNET_KEY_ENV_VAR = "FORUMNIK_FERNET_KEY"
# Запасной ключ, который будет использоваться, если переменная окружения не установлена
FALLBACK_ENCRYPTION_KEY = "zGWE7YquL1_SRRpMEASEFOHX-xvz4ZPAIJsW5F6jF7k="
# Пытаемся получить ключ из переменной окружения
ENCRYPTION_KEY = os.getenv(FERNET_KEY_ENV_VAR)
cipher_suite = None

if ENCRYPTION_KEY:
    print(f"INFO: Ключ шифрования загружен из переменной окружения {FERNET_KEY_ENV_VAR}.")
else:
    print(f"ПРЕДУПРЕЖДЕНИЕ: Переменная окружения {FERNET_KEY_ENV_VAR} не установлена. Используется запасной ключ из кода.")
    ENCRYPTION_KEY = FALLBACK_ENCRYPTION_KEY

# 3. Инициализация шифрования
try:
    cipher_suite = Fernet(ENCRYPTION_KEY.encode())
    print("INFO: Модуль шифрования успешно инициализирован.")
except Exception as e:
    print(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось инициализировать Fernet. Убедитесь, что ключ шифрования (из env или кода) корректен. Ошибка: {e}")

# --- Остальные настройки ---
DB_NAME = "forumnik_3_0.db"
USERS_TABLE_NAME = "Users_DB"
WHITELIST_TABLE_NAME = "judge_white_list"
CASES_TABLE_NAME = "Cases_DB"
HELPER_TABLE_NAME = "Helper_DB"
BOT_OWNER_ID = 6238356535
selenium_driver = None
selenium_service = None

# Состояния для ConversationHandler
ASK_NICKNAME, ASK_PASSWORD, AWAITING_CUSTOM_REPLY = range(3)

# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

logging.getLogger("apscheduler").setLevel(logging.WARNING)  # Снижаем уровень логов Telegram до WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)  # Снижаем уровень логов httpx до WARNING
# --- Конец настройки логирования ---

selenium_driver = None

# --- Функции Selenium ---
@contextmanager
def suppress_output():
    """Полностью подавляет stdout и stderr во время выполнения блока."""
    with open(os.devnull, 'w') as devnull:
        old_stdout = os.dup(1)
        old_stderr = os.dup(2)
        os.dup2(devnull.fileno(), 1)
        os.dup2(devnull.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(old_stdout, 1)
            os.dup2(old_stderr, 2)

def setup_selenium_driver():
    global selenium_driver, selenium_service 
    if selenium_driver is not None:
        logger.info("Selenium WebDriver уже запущен.")
        return selenium_driver

    logger.info("Инициализация Selenium WebDriver...")
    chrome_options = Options()
    # chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option("useAutomationExtension", False)

    try:
        selenium_service = Service(ChromeDriverManager().install())
        with suppress_output():
            # Передать сервис в драйвер
            selenium_driver = webdriver.Chrome(service=selenium_service, options=chrome_options)

        selenium_driver.implicitly_wait(5)
        logger.info("Selenium WebDriver успешно запущен.")

        target_url = "https://forum.arizona-rp.com/forums/3400/"
        logger.info(f"Открываем целевую страницу: {target_url}")
        selenium_driver.get(target_url)

        return selenium_driver
    
    except Exception as e:
        logger.error(f"Ошибка при запуске Selenium WebDriver: {e}", exc_info=True)
        selenium_driver = None
        selenium_service = None
        return None

def close_selenium_driver():
    global selenium_driver, selenium_service
    
    if selenium_driver:
        logger.info("Закрытие Selenium WebDriver...")
        try:
            selenium_driver.quit()
            logger.info("Selenium WebDriver успешно закрыт.")
        except Exception as e:
            logger.error(f"Ошибка при закрытии Selenium WebDriver: {e}", exc_info=True)
        finally:
            selenium_driver = None
            
    if selenium_service and selenium_service.is_connectable():
        logger.info("Остановка сервиса ChromeDriver...")
        try:
            selenium_service.stop()
            logger.info("Сервис ChromeDriver успешно остановлен.")
        except Exception as e:
            logger.error(f"Ошибка при остановке сервиса ChromeDriver: {e}", exc_info=True)
        finally:
            selenium_service = None
# --- Конец функций Selenium ---

# ---------------------- Перформ функции (вспомогательные) -----------------------------

# --- Вспомогательная функция для логирования ---
def add_note_to_case(conn: sqlite3.Connection, case_id: int, note_text: str):
    """
    Добавляет новую запись в поле 'notes' для указанного иска.
    """
    try:
        cursor = conn.cursor()
        
        # Получаем текущие заметки, чтобы не затереть их
        cursor.execute(f"SELECT notes FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        current_notes_result = cursor.fetchone()
        
        current_notes = current_notes_result[0] if current_notes_result and current_notes_result[0] else ""
        
        # Формируем новую запись
        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        new_note_entry = f"[{timestamp}] {note_text}"
        
        # Объединяем старые и новые заметки
        updated_notes = (current_notes + "\n" + new_note_entry).strip()
        
        # Обновляем запись в БД
        cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET notes = ? WHERE id = ?", (updated_notes, case_id))
        conn.commit()
        logger.info(f"В иск #{case_id} добавлена новая заметка: '{note_text}'")
    except sqlite3.Error as e:
        logger.error(f"Не удалось добавить заметку к иску #{case_id}: {e}")
        conn.rollback()
# --- Конец add_note_to_case ---

# --- Функция для входа на форум ---
async def login_perform(driver, conn, tg_user_id):
    logger.info(f"Начинаю процесс входа на форум для пользователя {tg_user_id}...")
    
    # 1. Получаем данные пользователя из нашей БД
    cursor = conn.cursor()
    cursor.execute(f"SELECT nick_name, password FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (tg_user_id,))
    user_data = cursor.fetchone()
    
    if not user_data or not user_data[0] or not user_data[1]:
        logger.error(f"Не удалось найти полные данные (ник/пароль) для входа для пользователя {tg_user_id}.")
        return False
        
    nick_name, encrypted_password = user_data
    
    # 2. Расшифровываем пароль
    password = decrypt_password(encrypted_password)
    if not password:
        logger.error(f"Не удалось расшифровать пароль для пользователя {tg_user_id}.")
        return False

    # 3. Выполняем действия в браузере
    try:
        login_url = "https://forum.arizona-rp.com/login/"
        logger.info(f"Переход на страницу входа: {login_url}")
        driver.get(login_url)
        
        wait = WebDriverWait(driver, 10) # Ждать до 10 секунд

        # Вводим логин
        login_field = wait.until(EC.presence_of_element_located((By.NAME, "login")))
        login_field.click()
        login_field.clear() # Очищаем поле на случай, если там что-то осталось
        login_field.send_keys(nick_name)
        logger.info(f"Никнейм '{nick_name}' введен в поле логина.")
        
        # Вводим пароль
        password_field = wait.until(EC.presence_of_element_located((By.NAME, "password")))
        password_field.click()
        password_field.clear()
        password_field.send_keys(password)
        logger.info("Пароль введен в соответствующее поле.")
        
        # Нажимаем кнопку "Войти"
        login_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.button--primary")))
        login_button.click()
        logger.info("Кнопка 'Войти' нажата.")

        # 4. Проверяем успешность входа
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.p-navgroup-link--user")))
        
        logger.info(f"Вход для пользователя '{nick_name}' выполнен успешно!")
        return True

    except Exception as e:
        logger.error(f"Произошла ошибка во время процесса входа в Selenium: {e}", exc_info=True)
        try:
            error_element = driver.find_element(By.CSS_SELECTOR, "div.block-body--error")
            logger.error(f"Найдено сообщение об ошибке на странице: {error_element.text}")
        except:
            pass
        return False
# --- Конец вспомогательной функции login_perform ---

# --- Начало вспомогательной функции logout_perform ---
async def logout_perform(driver):
    logger.info("Начинаю процесс выхода из аккаунта на форуме...")
    try:
        wait = WebDriverWait(driver, 10)  # Ждать до 10 секунд

        # 1. Проверяем, что пользователь уже авторизован
        account_menu_button = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.p-navgroup-link--user")))
        account_menu_button.click()
        logger.info("Открыто меню пользователя.")

        # 2. Находим кнопку "Выйти" и нажимаем на нее
        logout_link = wait.until(EC.presence_of_element_located((By.XPATH, "//a[normalize-space()='Выход']")))
        logout_link.click()
        logger.info("Кнопка 'Выход' нажата. Ожидаем подтверждения выхода...")

        #3. Проверяем, что пользователь успешно вышел
        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//div[@class='p-nav-opposite']//a[contains(., 'Вход')]")
        ))
        logger.info("Выход из аккаунта выполнен успешно!")
        return True
    
    except Exception as e:
        # Если на шаге 1 не удалось найти меню аккаунта, скорее всего, мы и так не в системе
        if "account_menu_button" not in locals():
            logger.info("Не удалось найти меню пользователя. Вероятно, мы уже вышли из системы.")
            return True # Считаем задачу выполненной

        logger.error(f"Произошла ошибка во время процесса выхода из системы: {e}", exc_info=True)
        return False
# --- Конец вспомогательной функции logout_perform ---

# --- Новая вспомогательная функция для публикации ответа на форуме ---
async def answer_perform(driver, case_url: str, reply_text: str) -> bool:
    logger.info(f"Начинаю процесс публикации ответа в теме: {case_url}")
    if not reply_text or not case_url:
        logger.error("URL иска или текст ответа не предоставлены для answer_perform.")
        return False

    try:
        wait = WebDriverWait(driver, 20) # Увеличим время ожидания до 20 секунд

        # 1. Переход по ссылке иска
        logger.info(f"Перехожу по ссылке иска...")
        driver.get(case_url)

        # 2. Поиск поля для ответа и вставка текста
        # Редакторы могут долго прогружаться, поэтому ждем появления
        logger.info("Ожидаю появления поля для ответа...")
        reply_textarea = wait.until(EC.presence_of_element_located(
            (By.CSS_SELECTOR, "div.fr-element.fr-view")
        ))
        reply_textarea.click()
        reply_textarea.send_keys(reply_text)
        logger.info("Текст для ответа успешно вставлен.")

        # 3. Поиск и нажатие кнопки "Ответить"
        reply_button = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.button--primary.button--icon--reply")
        ))
        reply_button.click()
        logger.info("Кнопка 'Ответить' нажата.")

        # 4. Проверка успешности публикации
        # Мы будем искать уникальную часть нашего ответа на странице,
        # чтобы убедиться, что пост появился.
        # Возьмем последние 100 символов из нашего ответа как уникальный фрагмент.
        confirmation_snippet = reply_text[-100:]
        
        logger.info("Ожидаю появления нового поста на странице...")
        wait.until(EC.presence_of_element_located(
            (By.XPATH, f"//*[contains(text(), '{confirmation_snippet}')]")
        ))
        
        logger.info("Публикация ответа успешно подтверждена!")
        return True

    except TimeoutException:
        logger.error("Элемент не был найден за отведенное время. Возможно, страница не загрузилась или изменилась структура.")
        return False
    except Exception as e:
        logger.error(f"Произошла непредвиденная ошибка при публикации ответа: {e}", exc_info=True)
        return False
# --- Конец вспомогательной функции постинга ответа ---

# --- Обновленная функция pin_perform без проверки успеха ---
async def pin_perform(driver) -> bool:
    logger.info("Начинаю процесс закрепления темы (без проверки ответа)...")
    
    try:
        wait = WebDriverWait(driver, 10)

        # 1. Находим и нажимаем на кнопку-меню "Редактирования темы"
        menu_trigger = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.menuTrigger[title='Дополнительно']")
        ))
        menu_trigger.click()
        logger.info("Нажато меню инструментов темы.")

        # 2. В открывшемся меню ищем и нажимаем на ссылку "Закрепить тему"
        pin_link = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//a[normalize-space()='Закрепить тему']")
        ))
        pin_link.click()
        logger.info("Нажата ссылка 'Закрепить тему' в меню.")

        # 3. Финальная проверка удалена. Сразу считаем операцию успешной.
        logger.info("Все действия для закрепления темы выполнены. Предполагаем успех.")
        return True

    except Exception as e:
        logger.error(f"Произошла ошибка при попытке закрепить тему: {e}", exc_info=True)
        return False
# --- Конец вспомогательной функции pin_perform ---

# --- Новая, упрощенная функция для закрытия темы ---
async def close_perform(driver) -> bool:
    logger.info("Начинаю процесс закрытия темы (упрощенный режим)...")
    
    try:
        wait = WebDriverWait(driver, 10)

        # --- ЭТАП 1: ОТКРЕПЛЕНИЕ (ЕСЛИ НЕОБХОДИМО) ---
        logger.info("Открываю меню инструментов...")
        menu_trigger = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.menuTrigger[title='Дополнительно']")
        ))
        menu_trigger.click()
        logger.info("Меню инструментов открыто.")

        # Проверяем, есть ли кнопка "Открепить тему"
        unpin_links = driver.find_elements(By.XPATH, "//a[normalize-space()='Открепить тему']")

        if unpin_links:
            # Если кнопка есть - нажимаем на нее. Страница перезагрузится.
            logger.info("Тема закреплена. Выполняю открепление...")
            unpin_links[0].click()
            logger.info("Нажата ссылка 'Открепить тему'.")
            # Ждем немного, чтобы страница успела начать перезагрузку
            await asyncio.sleep(1) 
        else:
            logger.info("Тема не закреплена, шаг открепления пропущен.")
            # Закрываем меню, чтобы оно не мешало
            menu_trigger.click()

        # --- ЭТАП 2: ЗАКРЫТИЕ ТЕМЫ ---
        
        # Снова открываем меню инструментов, так как оно либо закрылось, либо страница перезагрузилась
        logger.info("Открываю меню инструментов для закрытия темы...")
        menu_trigger = wait.until(EC.element_to_be_clickable(
            (By.CSS_SELECTOR, "button.menuTrigger[title='Дополнительно']")
        ))
        menu_trigger.click()

        # Ищем и нажимаем на ссылку "Закрыть тему"
        close_link = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//a[normalize-space()='Закрыть тему']")
        ))
        close_link.click()
        logger.info("Нажата ссылка 'Закрыть тему'.")
        
        # Модальных окон и проверок нет. Сразу считаем операцию успешной.
        logger.info("Все клики для закрытия темы выполнены. Предполагаем успех.")
        return True

    except Exception as e:
        logger.error(f"Произошла непредвиденная ошибка при закрытии темы: {e}", exc_info=True)
        return False
# --- Конец новой функции close_perform ---

# --- инкремент счётчика ---
async def check_and_increment_case_number(conn: sqlite3.Connection, case_id: int):
    logger.info(f"Запущена проверка инкремента номера для иска #{case_id}.")
    COUNTER_MARKER = 'true_case_num'
    cursor = conn.cursor()
    try:
        # 1. Получаем номер текущего иска
        cursor.execute(f"SELECT case_num FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        case_num_result = cursor.fetchone()
        if not case_num_result or not case_num_result[0]:
            logger.warning(f"Не удалось получить case_num для иска #{case_id}. Инкремент отменен.")
            return

        # 2. Получаем ожидаемый номер иска
        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (COUNTER_MARKER,))
        true_case_num_result = cursor.fetchone()
        if not true_case_num_result or not true_case_num_result[0]:
            logger.warning(f"Не удалось получить true_case_num из Helper_DB. Инкремент отменен.")
            return

        # 3. Сравниваем номера (приводим к строке для надежности)
        current_case_num_str = str(case_num_result[0]).strip()
        expected_case_num_str = str(true_case_num_result[0]).strip()

        logger.info(f"Сравнение: номер иска = '{current_case_num_str}', ожидаемый номер = '{expected_case_num_str}'.")

        if current_case_num_str == expected_case_num_str:
            # 4. Если совпадают, инкрементируем
            new_case_number = int(expected_case_num_str) + 1
            cursor.execute(f"UPDATE {HELPER_TABLE_NAME} SET marker_desc = ? WHERE marker = ?", (str(new_case_number), COUNTER_MARKER))
            conn.commit()
            logger.info(f"УСПЕХ! Номер иска совпал. Ожидаемый номер обновлен на {new_case_number}.")
        else:
            logger.info("Номер иска не совпадает с ожидаемым. Счетчик не изменен.")

    except (sqlite3.Error, ValueError) as e:
        logger.error(f"Ошибка при проверке и инкременте номера иска #{case_id}: {e}", exc_info=True)
        # В случае ошибки откатываем любые изменения, которые могли начаться
        conn.rollback()
# --- Конец инкремента счётчика ---

# --- ТЕСТОВЫЕ КОМАНДЫ ---
# --- Тестовая команда для проверки публикации ответа ---
async def test_answer_perform_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # 1. Проверяем, что команду вызывает владелец
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("Эта тестовая команда доступна только владельцу бота.")
        return

    # 2. Проверка и получение аргументов
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Неверный формат команды.\n"
            "Используйте: /aperftest <ссылка> <текст ответа>"
        )
        return

    case_url = args[0]
    reply_text = " ".join(args[1:])

    # Простая проверка, что первый аргумент похож на ссылку
    if not case_url.startswith("http"):
        await update.message.reply_text("Первый аргумент должен быть корректной ссылкой на тему иска.")
        return
        
    global selenium_driver
    if not selenium_driver:
        await update.message.reply_text("Selenium WebDriver не запущен. Не могу выполнить тест.")
        return

    await update.message.reply_text(f"▶️ Начинаю тестовую публикацию в теме:\n{case_url}")
    
    # 3. Вызываем нашу асинхронную функцию
    success = await answer_perform(selenium_driver, case_url, reply_text)
    
    # 4. Отправляем результат
    if success:
        await update.message.reply_text("✅ Тестовая публикация ответа прошла успешно!")
    else:
        await update.message.reply_text("❌ Ошибка во время тестовой публикации. Смотрите логи в консоли для деталей.")

# --- Тестовая команда для проверки входа ---
async def test_login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверяем, что команду вызывает владелец
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("Эта тестовая команда доступна только владельцу бота.")
        return
        
    global selenium_driver
    conn = context.bot_data['db_connection']

    if not selenium_driver:
        await update.message.reply_text("Selenium WebDriver не запущен. Не могу выполнить тест.")
        return

    await update.message.reply_text("▶️ Начинаю тестовый вход на форум...")
    
    # Вызываем нашу асинхронную функцию входа
    success = await login_perform(selenium_driver, conn, BOT_OWNER_ID)
    
    if success:
        await update.message.reply_text("✅ Тестовый вход выполнен успешно!")
        # После входа можно перейти обратно на страницу исков
        selenium_driver.get("https://forum.arizona-rp.com/forums/3400/")
    else:
        await update.message.reply_text("❌ Ошибка во время тестового входа. Смотрите логи в консоли для деталей.")
# --- Конец тестовой команды для проверки входа ---

# --- Тестовая команда для проверки выхода ---
async def test_logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Проверяем, что команду вызывает владелец
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("Эта тестовая команда доступна только владельцу бота.")
        return
        
    global selenium_driver
    if not selenium_driver:
        await update.message.reply_text("Selenium WebDriver не запущен. Не могу выполнить тест.")
        return

    await update.message.reply_text("▶️ Начинаю тестовый выход с форума...")
    
    # Вызываем нашу асинхронную функцию выхода
    success = await logout_perform(selenium_driver)
    
    if success:
        await update.message.reply_text("✅ Тестовый выход выполнен успешно!")
    else:
        await update.message.reply_text("❌ Ошибка во время тестового выхода. Смотрите логи в консоли для деталей.")
# --- Конец тестовой команды для проверки выхода ---

# --- Тестовая команда для проверки закрепления ---
async def test_pin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("Эта тестовая команда доступна только владельцу бота.")
        return
        
    global selenium_driver
    if not selenium_driver:
        await update.message.reply_text("Selenium WebDriver не запущен. Не могу выполнить тест.")
        return
    
    current_url = selenium_driver.current_url
    await update.message.reply_text(f"▶️ Начинаю тест закрепления темы на текущей странице:\n{current_url}")
    
    success = await pin_perform(selenium_driver)
    if success:
        await update.message.reply_text("✅ Тест закрепления темы прошел успешно!")
    else:
        await update.message.reply_text("❌ Ошибка во время теста закрепления. Смотрите логи в консоли для деталей.")
# --- Конец тестовой команды для проверки закрепления ---

# --- Тестовая команда для проверки закрытия ---
async def test_close_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != BOT_OWNER_ID:
        await update.message.reply_text("Эта тестовая команда доступна только владельцу бота.")
        return
        
    global selenium_driver
    if not selenium_driver:
        await update.message.reply_text("Selenium WebDriver не запущен. Не могу выполнить тест.")
        return
    
    current_url = selenium_driver.current_url
    await update.message.reply_text(f"▶️ Начинаю тест закрытия темы на текущей странице:\n{current_url}")
    
    success = await close_perform(selenium_driver)
    
    if success:
        await update.message.reply_text("✅ Тест закрытия темы прошел успешно!")
    else:
        await update.message.reply_text("❌ Ошибка во время теста закрытия. Смотрите логи в консоли.")
# --- Конец тестовой команды для проверки закрытия ---

# --- КОНЕЦ ТЕСТОВЫХ КОМАНД ---


# --- Функции работы с БД ---
def setup_database():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # Таблица пользователей с колонкой is_admin
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {USERS_TABLE_NAME} (
        tg_user_id INTEGER PRIMARY KEY,
        nick_name TEXT,
        password TEXT, 
        authorization INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0 -- Новая колонка для статуса администратора
    )
    """)
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {WHITELIST_TABLE_NAME} (
        nick_name TEXT PRIMARY KEY
    )
    """)
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {CASES_TABLE_NAME} (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        applicant_name TEXT,                 
        case_num TEXT,                       
        current_judge TEXT,                  
        full_text TEXT,                      
        media_references TEXT,               
        notes TEXT,                          
        officer_name TEXT,                   
        publication_time TEXT,               
        scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        status TEXT NOT NULL DEFAULT 'a',    
        topic_link TEXT UNIQUE,              
        topic_title TEXT                    
    )
    """)
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {HELPER_TABLE_NAME} (
        marker TEXT PRIMARY KEY,
        marker_desc TEXT
    )
    """)
    conn.commit()

    # Проверка и добавление колонки is_admin, если ее нет
    existing_columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({USERS_TABLE_NAME})").fetchall()]
    if 'is_admin' not in existing_columns:
        try:
            cursor.execute(f"ALTER TABLE {USERS_TABLE_NAME} ADD COLUMN is_admin INTEGER DEFAULT 0")
            logger.info(f"Добавлена колонка 'is_admin' в таблицу '{USERS_TABLE_NAME}'.")
            conn.commit()
        except sqlite3.Error as e_alter:
            logger.error(f"Ошибка при добавлении колонки 'is_admin' в '{USERS_TABLE_NAME}': {e_alter}")
            
    return conn

def get_user_details(conn, tg_user_id) -> tuple | None:
    """Получает nick_name, authorization, is_admin пользователя из Users_DB."""
    cursor = conn.cursor()
    cursor.execute(f"SELECT nick_name, authorization, is_admin FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (tg_user_id,))
    logger.info(f"Получение данных пользователя с TG ID {tg_user_id} из базы данных.")
    return cursor.fetchone() # Возвращает (nick_name, authorization, is_admin) или None

def add_or_update_user_nickname(conn, tg_user_id, nick_name):
    cursor = conn.cursor()
    # При добавлении/обновлении ника is_admin остается прежним или устанавливается в 0, если это новая запись
    cursor.execute(f"""
    INSERT INTO {USERS_TABLE_NAME} (tg_user_id, nick_name, authorization, is_admin) 
    VALUES (?, ?, 0, 0) 
    ON CONFLICT(tg_user_id) DO UPDATE SET 
        nick_name = excluded.nick_name, 
        authorization = 0 
        -- is_admin не меняется при простом обновлении ника через /auth, его должен менять админ
    """, (tg_user_id, nick_name))
    conn.commit()

def is_nick_in_whitelist(conn, nick_name):
    if not nick_name: return False
    cursor = conn.cursor()
    cursor.execute(f"SELECT 1 FROM {WHITELIST_TABLE_NAME} WHERE nick_name = ?", (nick_name,))
    return cursor.fetchone() is not None

def store_user_password(conn, tg_user_id, encrypted_password):
    cursor = conn.cursor()
    cursor.execute(f"""
    UPDATE {USERS_TABLE_NAME} 
    SET password = ?, authorization = 1 
    WHERE tg_user_id = ?
    """, (encrypted_password, tg_user_id))
    conn.commit()
    logger.info(f"Пароль и статус авторизации обновлены для пользователя {tg_user_id}")

def get_case_details_by_id(conn, case_id):
    """
    Получает детали иска, включая путь к скриншоту.
    """
    cursor = conn.cursor()
    # Добавляем колонку 'screen' в SELECT
    cursor.execute(f"""
    SELECT id, case_num, status, current_judge, full_text, media_references, topic_title, screen
    FROM {CASES_TABLE_NAME} 
    WHERE id = ?
    """, (case_id,))
    return cursor.fetchone()

def update_case_status_and_judge(conn, case_id, new_status, judge_nick_name):
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
        UPDATE {CASES_TABLE_NAME}
        SET status = ?, current_judge = ?
        WHERE id = ?
        """, (new_status, judge_nick_name, case_id))
        conn.commit()
        logger.info(f"Иск ID {case_id} обновлен: статус={new_status}, судья='{judge_nick_name}'")
        return True
    except sqlite3.Error as e:
        logger.error(f"Ошибка при обновлении иска ID {case_id}: {e}")
        return False

# --- Функции шифрования ---
def encrypt_password(password_text):
    if not cipher_suite:
        logger.error("Fernet не инициализирован. Невозможно зашифровать пароль.")
        return None
    return cipher_suite.encrypt(password_text.encode()).decode()

def decrypt_password(encrypted_password_text):
    if not cipher_suite:
        logger.error("Fernet не инициализирован. Невозможно расшифровать пароль.")
        return None
    if not encrypted_password_text:
        return None
    try:
        return cipher_suite.decrypt(encrypted_password_text.encode()).decode()
    except Exception as e:
        logger.error(f"Ошибка расшифровки пароля: {e}")
        return None

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ perform_wa_check ---
async def perform_wa_check(conn: sqlite3.Connection, tg_user_id: int, update: Update) -> tuple[bool, str | None, bool]:
    cursor = conn.cursor()
    
    # За один запрос получаем все нужные данные о пользователе
    cursor.execute(
        f"SELECT nick_name, authorization, is_admin, punished_until FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?",
        (tg_user_id,)
    )
    logger.info(f"Проверка доступа пользователя с TG ID {tg_user_id} в базе данных.")
    user_data = cursor.fetchone()

    # --- ПРОВЕРКА НАКАЗАНИЯ ---
    if user_data and user_data[3] and user_data[3] > int(time.time()):
        punished_until_ts = user_data[3]
        remaining_seconds = punished_until_ts - int(time.time())
        remaining_minutes = (remaining_seconds // 60) + 1  # Округляем вверх до следующей минуты
        logger.info(f"Пользователь {tg_user_id} заблокирован до {punished_until_ts} (осталось {remaining_minutes} мин).")

        await update.message.reply_text(
            f"⏳ Ваш доступ к командам временно заблокирован.\nПлаки-плаки🙂‍↕️🙂‍↕️🙂‍↕️",
            f"Оставшееся время блокировки: примерно {remaining_minutes} мин.🙌 \n Отдыхай малышка 🫶",
            logger.info(f"Сообщение о блокировке отправлено пользователю {tg_user_id}.")
        )
        # Возвращаем False, чтобы заблокировать команду, но передаем ник и админ-статус для логов
        user_nick_name = user_data[0]
        is_admin = bool(user_data[2])
        return False, user_nick_name, is_admin

    # --- Стандартные проверки, если наказания нет ---
    if not user_data:
        await update.message.reply_text(
            "Вы не найдены в системе. 🤔 \nПожалуйста, сначала используйте команду /auth для авторизации.👌"
        )
        logger.info(f"Пользователь {tg_user_id} не найден в базе данных.")
        return False, None, False

    user_nick_name, authorization_status, is_admin_db_val, _ = user_data
    is_admin = bool(is_admin_db_val)

    if not user_nick_name:
        await update.message.reply_text(
            "❗️Ваш никнейм не зарегистрирован в системе❗️\nПожалуйста, сначала пройдите регистрацию через /auth.☝️"
        )
        logger.info(f"Пользователь {tg_user_id} не имеет зарегистрированного никнейма.")
        return False, None, is_admin
    
    if not is_nick_in_whitelist(conn, user_nick_name):
        await update.message.reply_text(
            f"К сожалению, {user_nick_name}, вы не являетесь судьёй. 😭 \nЕсли Вы считаете что произошла ошибка, то пожалуйста, обратитесь к Администратору.🧐"
        )
        logger.info(f"Пользователь {tg_user_id} с никнеймом {user_nick_name} не найден в белом списке.")
        return False, user_nick_name, is_admin

    if authorization_status != 1:
        await update.message.reply_text(
            f"🫸Уважаемый {user_nick_name}, похоже, что вы не авторизованы🫷 \n❗️Вы не можете выполнить это действие❗️\n"
            "Пожалуйста, пройдите авторизацию используя /auth🤲"
        )
        logger.info(f"Пользователь {tg_user_id} с никнеймом {user_nick_name} не авторизован.")
        return False, user_nick_name, is_admin

    # Если все проверки пройдены
    logger.info(f"Пользователь {tg_user_id} с никнеймом {user_nick_name} успешно прошел все проверки.")
    return True, user_nick_name, is_admin 
# --- Конец функции perform_wa_check ---

# --- Вспомогательная функция text_editor_helper ---
async def text_editor_helper(
    conn: sqlite3.Connection, 
    template_text: str, 
    data_context: dict
) -> str:
    """
    Обрабатывает текстовый шаблон, заменяя плейсхолдеры (data0, data1...) реальными данными.

    :param conn: Соединение с базой данных.
    :param template_text: Текст шаблона с плейсхолдерами.
    :param data_context: Словарь с данными для подстановки. 
                         Ожидаемые ключи: 'applicant_name', 'officer_name', 
                         'judge_nick_name', 'custom_text', 'case_num'.
    :return: Обработанный текст.
    """
    if not template_text:
        return ""

    cursor = conn.cursor()
    
    # --- Получение данных, которые не зависят от контекста ---

    # 1. data0: Текущая дата
    today_date_str = date.today().strftime("%d.%m.%Y")
    logger.info(f"Текущая дата для замены в шаблоне получена: {today_date_str}")
    
    # 2. data6: Следующий номер иска
    next_case_number_str = "" # Значение по умолчанию
    try:
        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = 'true_case_num'")
        result = cursor.fetchone()
        if result and result[0]:
            next_case_number_str = result[0]
            logger.info(f"Следующий номер иска получен: {next_case_number_str}")
    except sqlite3.Error as e:
        logger.error(f"Не удалось получить 'true_case_num' в text_editor_helper: {e}")

    # 3. Создаем словарь замен
    # Используем .get() для безопасного извлечения данных из контекста
    replacements = {
        'data0': today_date_str,
        'data1': data_context.get('applicant_name', '[Имя заявителя]'),
        'data2': data_context.get('officer_name', '[Имя ответчика]'),
        'data3': data_context.get('judge_nick_name', '[Ник судьи]'),
        'data4': data_context.get('custom_text', '[Произвольный текст]'),
        'data5': data_context.get('case_num', '[Номер иска]'),
        'data6': next_case_number_str,
    }

    # --- Выполнение замен ---
    processed_text = template_text
    for placeholder, value in replacements.items():
        # Убедимся, что значение является строкой перед заменой
        processed_text = processed_text.replace(str(placeholder), str(value))
        
    logger.info("text_editor_helper успешно обработал шаблон.")
    return processed_text
# --- Конец вспомогательной функции text_editor_helper ---

# --- Обработчики диалога авторизации ---
async def auth_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']
    
    user_details = get_user_details(conn, tg_user_id)
    if user_details and user_details[1] == 1 : # user_details[1] это authorization_status
        await update.message.reply_text("Вы уже авторизованы.🙂")
        logger.info(f"Пользователь {tg_user_id} уже авторизован. Пропускаем процесс авторизации.")
        return ConversationHandler.END

    await update.message.reply_text(
        "Здравствуйте! \n👻Пожалуйста, введите ваш никнейм, как на форуме.👻"
    )
    logger.info(f"Пользователь {tg_user_id} начал процесс авторизации.")
    return ASK_NICKNAME

async def received_nickname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_user_id = update.effective_user.id
    nick_name = update.message.text.strip()
    conn = context.bot_data['db_connection']
    add_or_update_user_nickname(conn, tg_user_id, nick_name)
    context.user_data['nick_name'] = nick_name 
    logger.info(f"Пользователь {tg_user_id} ввел никнейм: {nick_name}")
    if is_nick_in_whitelist(conn, nick_name):
        logger.info(f"Никнейм {nick_name} найден в белом списке.")
        await update.message.reply_text("Введите ваш пароль от форумного аккаунта.💀💀💀")
        return ASK_PASSWORD
    else:
        logger.warning(f"Никнейм {nick_name} НЕ найден в белом списке для пользователя {tg_user_id}.")
        await update.message.reply_text(
            f"Уважаемый {nick_name}, к сожалению, вас нет в списке судей. Доступ закрыт.😢\n"
            "Если вы считаете, что это ошибка, обратитесь к администратору.🥸"
        )
        return ConversationHandler.END

async def received_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_user_id = update.effective_user.id
    password = update.message.text 
    nick_name = context.user_data.get('nick_name', "Пользователь")
    conn = context.bot_data['db_connection']
    if not cipher_suite:
        await update.message.reply_text("🤖Произошла внутренняя ошибка (шифрование недоступно)🤖 \nПожалуйста, сообщите администратору.")
        return ConversationHandler.END
    encrypted_password = encrypt_password(password)
    if not encrypted_password:
        await update.message.reply_text("🤖Произошла внутренняя ошибка при обработке пароля🤖 \nПожалуйста, сообщите администратору.")
        return ConversationHandler.END
    store_user_password(conn, tg_user_id, encrypted_password)
    logger.info(f"Пользователь {tg_user_id} ({nick_name}) успешно авторизован.")
    await update.message.reply_text(
        f"{nick_name}, добро пожаловать!🤗 \nВы успешно авторизовались и прошли проверку.🥳🥳🥳"
    )
    if 'nick_name' in context.user_data:
        del context.user_data['nick_name'] 
    return ConversationHandler.END

async def auth_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Процесс авторизации отменен.🤨🤨🤨", reply_markup=ReplyKeyboardRemove())
    if 'nick_name' in context.user_data:
        del context.user_data['nick_name']
        logger.info(f"Пользователь {update.effective_user.id} отменил процесс авторизации, никнейм удален из user_data.")
    return ConversationHandler.END

# --- Начало обновленной команды /list ---
async def list_cases_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']
    logger.info(f"Пользователь {tg_user_id} вызвал команду /list.")

    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    if not can_proceed:
        return

    cursor = conn.cursor()
    
    # --- Добавим отображение true_case_num ---
    COUNTER_MARKER = 'true_case_num'
    header_message = "" # Инициализируем пустой заголовок
    try:
        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (COUNTER_MARKER,))
        result = cursor.fetchone()
        if result and result[0]:
            # Формируем строку заголовка, если номер найден
            header_message = f"ℹ️ Следующий ожидаемый номер иска: <b>{result[0]}</b>ℹ️\n\n"
            logger.info(f"Получен следующий номер иска для /list: {result[0]}")
    except sqlite3.Error as e_sql:
        logger.error(f"Не удалось получить следующий номер иска для /list: {e_sql}")
        # Не останавливаем команду, просто номер не будет показан

    # Основной запрос на получение списка исков остается без изменений
    sql_query = f"""
    SELECT c.id, c.case_num, h.marker_desc, c.current_judge, c.status 
    FROM {CASES_TABLE_NAME} c
    LEFT JOIN {HELPER_TABLE_NAME} h ON c.status = h.marker
    WHERE c.status IN ('a', 'b', 'f')
    ORDER BY c.id ASC; 
    """ 
    try:
        cursor.execute(sql_query)
        cases = cursor.fetchall()
        
        if not cases:
            # Добавляем заголовок и к сообщению о том, что исков нет
            await update.message.reply_html(header_message + "☹️Нет исков, доступных к рассмотрению☹️")
            return

        # Добавляем заголовок в начало основного сообщения
        response_message = header_message + "⬇️<b>Иски, доступные к рассмотрению</b>⬇️\n\n"
        
        for case_data in cases:
            case_id_db, case_num_db, marker_desc_db, current_judge_db, status_code_db = case_data
            status_display = marker_desc_db if marker_desc_db else f"Статус {status_code_db}" 
            judge_display = current_judge_db if current_judge_db and current_judge_db.strip() else "Судья не назначен"
            case_num_display = case_num_db if case_num_db and case_num_db.strip() else "б/н"
            response_message += f"📂 {case_id_db}) Иск - {case_num_display}: {status_display}. ({judge_display})\n"
            
        await update.message.reply_html(response_message)
        logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}, Admin: {is_admin}) получил список исков.")

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при получении списка исков: {e_sql}")
        await update.message.reply_text("🌚Произошла ошибка при получении списка исков🌝 \nПожалуйста, попробуйте позже.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в list_cases_command: {e_general}", exc_info=True)
        await update.message.reply_text("🔥Произошла внутренняя ошибка🔥 \nПожалуйста, сообщите администратору.")
# --- Конец обновленной команды /list ---

# --- Финальная версия команды /select со скриншотами ---
async def select_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    if not can_proceed:
        return 

    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите именно ID иска после команды /select☝️ \nНапример: /select 123")
        return
    try:
        case_id_to_select = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID иска должен быть числом😬😬😬 Например: /select 123")
        return
    
    # Получаем данные, включая путь к скриншоту
    case_data = get_case_details_by_id(conn, case_id_to_select)

    if not case_data:
        await update.message.reply_text(f"Иск с ID {case_id_to_select} не найден🙄🙄🙄")
        return

    # Распаковываем данные (теперь их 8)
    db_id, db_case_num, db_status, db_current_judge, db_full_text, db_media_references_json, db_topic_title, db_screen_path = case_data
    case_num_display = db_case_num if db_case_num and db_case_num.strip() else "б/н"

    # --- Логика выбора иска ---
    header_text = ""
    if db_status not in ('a', 'b', 'f'):
        await update.message.reply_text(f"Иск №{case_num_display} уже обработан или находится в статусе, не предполагающем взятие в работу.")
        return

    if db_status == 'a': 
        if update_case_status_and_judge(conn, case_id_to_select, 'b', user_nick_name):
            add_note_to_case(conn, case_id_to_select, f"Иск взят в работу судьей {user_nick_name}.")
            header_text = f"✅ Вы успешно взяли иск №{case_num_display} в работу!\n\n"
        else:
            await update.message.reply_text(f"Не удалось обновить статус иска ID {case_id_to_select}.")
            return
    
    elif db_current_judge and db_current_judge.lower() != user_nick_name.lower():
        await update.message.reply_text(f"⚠️Похоже что иск №{case_num_display} уже в работе у судьи {db_current_judge}🧑‍⚖️")
        return
    else:
        header_text = f"Вы повторно открыли ваш иск №{case_num_display}.\n\n"

    # --- Формирование подписи и клавиатуры ---
    caption_part = f"<b>Детали по иску №{case_num_display} (ID: {db_id})</b>\n\n"
    # Для краткости в подписи не будем выводить полный текст, только материалы
    if db_media_references_json:
        try:
            media_links = json.loads(db_media_references_json)
            if media_links:
                caption_part += "🗂<b>Материалы дела:</b>\n"
                for link in media_links[:3]: # Ограничим до 3 ссылок, чтобы подпись не была слишком длинной
                    caption_part += f"🔗 {link}\n"
                if len(media_links) > 3:
                    caption_part += f"... и еще {len(media_links) - 3}\n"
        except json.JSONDecodeError:
            pass # Игнорируем ошибку парсинга
    
    keyboard = [
        [
            InlineKeyboardButton("❌ Неправ. номер", callback_data=f"reject:c:{db_id}"),
            InlineKeyboardButton("📋 Не по форме", callback_data=f"reject:d:{db_id}")
        ],
        [
            InlineKeyboardButton("🕹️ Несист. иск", callback_data=f"reject:e:{db_id}"),
            InlineKeyboardButton("❓ Запрос опровержения", callback_data=f"refutation:none:{db_id}")
        ],
        [
            InlineKeyboardButton("✍️ Свой ответ", callback_data=f"custom_reply:intermediate:{db_id}"),
            InlineKeyboardButton("✍️ Свой ответ (ФИНАЛ)", callback_data=f"custom_reply:final:{db_id}")
        ],
        [
            # --- ИЗМЕНЕНИЯ ЗДЕСЬ ---
            InlineKeyboardButton("📄 Показать полный текст иска", callback_data=f"show_full_text:none:{db_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # --- Отправка скриншота или текста ---
    final_caption = header_text + caption_part
    
    if db_screen_path and os.path.exists(db_screen_path):
        # Если есть скриншот - отправляем фото с подписью
        try:
            await update.message.reply_photo(
                photo=open(db_screen_path, 'rb'),
                caption=final_caption,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        except Exception as e:
            await update.message.reply_text(f"Не удалось отправить скриншот: {e}. Отправляю текстом.")
            await update.message.reply_html(final_caption + f"\n<b>Полный текст:</b>\n<pre>{db_full_text}</pre>", reply_markup=reply_markup)
    else:
        # Если скриншота нет - отправляем просто текст, как раньше
        logger.warning(f"Скриншот для иска #{db_id} не найден по пути: {db_screen_path}. Отправляю текстом.")
        await update.message.reply_html(final_caption + f"\n<b>Полный текст:</b>\n<pre>{db_full_text}</pre>", reply_markup=reply_markup)
# --- Конец команды /select ---

# --- Получаем полный текст иска по нажатию кнопки ---
async def handle_full_text_request(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int):
    query = update.callback_query
    conn = context.bot_data['db_connection']
    
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT full_text, case_num FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        result = cursor.fetchone()

        if not result:
            await query.answer("Иск не найден.", show_alert=True)
            return

        full_text, case_num = result
        case_num_display = case_num if case_num else f"ID {case_id}"
        
        if not full_text:
            await query.answer("У этого иска отсутствует подробное описание.", show_alert=True)
            return
            
        # Отправляем текст в виде нового сообщения в чат
        response_text = f"📄 <b>Полный текст иска №{case_num_display}:</b>\n\n<pre>{full_text}</pre>"
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=response_text,
            parse_mode='HTML'
        )
        # Отвечаем на сам колбэк, чтобы у пользователя пропали "часики"
        await query.answer("Полный текст иска отправлен в чат.")

    except Exception as e:
        logger.error(f"Ошибка при отправке полного текста для иска #{case_id}: {e}", exc_info=True)
        await query.answer("Произошла ошибка при получении текста.", show_alert=True)
# --- полныый текст иска

# --- обработка колбэков ---
async def button_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Этот обработчик "ловит" все нажатия на инлайн-кнопки и решает, какую функцию запустить.
    """
    query = update.callback_query
    await query.answer()

    try:
        parts = query.data.split(':', 2)
        action = parts[0]
        payload = parts[1]
        case_id = int(parts[2])
    except (ValueError, IndexError):
        await query.edit_message_text(text="Ошибка: неверный формат данных в кнопке.")
        return

    # В зависимости от "действия" в callback_data, вызываем нужный воркфлоу
    if action == 'reject':
        await handle_rejection_workflow(update, context, case_id, payload)
    
    elif action == 'refutation':
        await handle_refutation_workflow(update, context, case_id)
    
    elif action == 'rebuttal_choice':
        await handle_rebuttal_choice(update, context, case_id, payload)
    
    elif action == 'custom_reply':
        # Этот вызов не выполняет весь воркфлоу, а только НАЧИНАЕТ диалог
        return await custom_reply_start(update, context)

    elif action == 'show_full_text':
        await handle_full_text_request(update, context, case_id)
    
    else:
        await query.edit_message_text(text="Неизвестное действие или в разработке.")

# --- начало handle_rejection_workflow ---
async def handle_rejection_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int, rejection_type: str):
    """
    Полный воркфлоу для обработки кнопок отказа.
    Версия с исправленной логикой редактирования сообщений.
    """
    query = update.callback_query
    # Редактируем исходное сообщение с фото ОДИН РАЗ, убирая кнопки
    await query.edit_message_caption(caption=f"✅ Команда принята. Начинаю обработку отказа для иска #{case_id}...", reply_markup=None)
    
    # Создаем НОВОЕ сообщение для отображения статуса
    status_message = await context.bot.send_message(chat_id=query.message.chat_id, text="Пожалуйста, подождите... ⏳")

    conn = context.bot_data['db_connection']
    driver = selenium_driver
    judge_tg_user_id = query.from_user.id
    
    rejection_map = {'c': 'nomer', 'd': 'forma', 'e': 'system'}
    template_marker = rejection_map.get(rejection_type)

    if not template_marker:
        await status_message.edit_text(text="❌ Ошибка: неизвестный тип отказа.")
        return

    try:
        # 1. Подготовка данных
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET status = ? WHERE id = ?", (rejection_type, case_id))
        add_note_to_case(conn, case_id, f"Иск отклонен по причине: '{template_marker}'.")
        
        cursor.execute(f"SELECT applicant_name, case_num, topic_link FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        case_data_db = cursor.fetchone()
        
        cursor.execute(f"SELECT nick_name FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (judge_tg_user_id,))
        judge_nick_name = cursor.fetchone()[0]

        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (template_marker,))
        template_text = cursor.fetchone()[0]
        
        conn.commit()
        
        data_context = {
            'applicant_name': case_data_db[0],
            'case_num': case_data_db[1],
            'judge_nick_name': judge_nick_name,
        }
        topic_link = case_data_db[2]
        final_reply_text = await text_editor_helper(conn, template_text, data_context)
        
        # --- Сессия Судьи ---
        await status_message.edit_text(text=f"Вхожу в аккаунт судьи {judge_nick_name}...")
        if not await login_perform(driver, conn, judge_tg_user_id):
            raise Exception("Не удалось войти в аккаунт судьи.")
        
        await status_message.edit_text(text="Публикую ответ на форуме...")
        if not await answer_perform(driver, topic_link, final_reply_text):
            raise Exception("Не удалось опубликовать ответ на форуме.")
        
        await logout_perform(driver)

        # --- Сессия Владельца ---
        await status_message.edit_text(text="Вхожу в аккаунт владельца для закрытия темы...")
        if not await login_perform(driver, conn, BOT_OWNER_ID):
            raise Exception("Не удалось войти в аккаунт владельца.")
            
        driver.get(topic_link)
        
        await status_message.edit_text(text="Закрываю тему на форуме...")
        if not await close_perform(driver):
            raise Exception("Не удалось закрыть тему на форуме.")

        await logout_perform(driver)

        await status_message.edit_text(text=f"✅ Готово! Иск #{case_id} успешно отклонен и закрыт на форуме.")

    except Exception as e:
        logger.error(f"Ошибка в воркфлоу отказа для иска {case_id}: {e}", exc_info=True)
        await status_message.edit_text(text=f"❌ Произошла ошибка: {e}\n\nПожалуйста, проверьте состояние иска вручную.")
# - Конец обработчиков колбэков ---

# --- handle_refutation_workflow ---
async def handle_refutation_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int):
    """
    Полный воркфлоу для кнопки "Запрос опровержения".
    Версия с исправленной логикой редактирования сообщений.
    """
    query = update.callback_query
    await query.edit_message_caption(caption=f"✅ Команда принята. Начинаю запрос опровержения для иска #{case_id}...", reply_markup=None)
    
    status_message = await context.bot.send_message(chat_id=query.message.chat_id, text="Пожалуйста, подождите... ⏳")

    conn = context.bot_data['db_connection']
    driver = selenium_driver
    judge_tg_user_id = query.from_user.id
    template_marker = 'opra'

    try:
        # 1. Подготовка данных
        logger.info(f"Запрос опровержения для иска {case_id} от судьи {judge_tg_user_id}.")
        cursor = conn.cursor()
        
        cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET status = 'f' WHERE id = ?", (case_id,))
        add_note_to_case(conn, case_id, f"Запрошено опровержение судьей (никнейм будет здесь).") # Нужно будет передать ник
        
        cursor.execute(f"SELECT applicant_name, case_num, topic_link, officer_name FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        case_data_db = cursor.fetchone()
        
        cursor.execute(f"SELECT nick_name FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (judge_tg_user_id,))
        judge_nick_name = cursor.fetchone()[0]

        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (template_marker,))
        template_text = cursor.fetchone()[0]
        
        conn.commit()
        await check_and_increment_case_number(conn, case_id)
        
        data_context = {
            'applicant_name': case_data_db[0], 'case_num': case_data_db[1],
            'officer_name': case_data_db[3], 'judge_nick_name': judge_nick_name,
        }
        topic_link = case_data_db[2]
        final_reply_text = await text_editor_helper(conn, template_text, data_context)
        
        # 2. Сессия Судьи
        await status_message.edit_text(text=f"Иск #{case_id}: Вхожу в аккаунт судьи {judge_nick_name}...")
        if not await login_perform(driver, conn, judge_tg_user_id):
            raise Exception("Не удалось войти в аккаунт судьи.")
        
        await status_message.edit_text(text=f"Иск #{case_id}: Публикую ответ на форуме...")
        if not await answer_perform(driver, topic_link, final_reply_text):
            raise Exception("Не удалось опубликовать ответ на форуме.")
        
        await logout_perform(driver)

        # 3. Сессия Владельца
        await status_message.edit_text(text=f"Иск #{case_id}: Вхожу в аккаунт владельца для закрепления темы...")
        if not await login_perform(driver, conn, BOT_OWNER_ID):
            raise Exception("Не удалось войти в аккаунт владельца.")
            
        driver.get(topic_link)
        
        await status_message.edit_text(text=f"Иск #{case_id}: Закрепляю тему на форуме...")
        if not await pin_perform(driver):
            raise Exception("Не удалось закрепить тему на форуме.")

        await logout_perform(driver)
        
        # Удаляем промежуточное сообщение
        await status_message.delete()
        
        # Показываем новую клавиатуру
        rebuttal_keyboard = [
            [InlineKeyboardButton("🚓 Розыск", callback_data=f"rebuttal_choice:Розыск:{case_id}"), 
             InlineKeyboardButton("⛓️ Арест", callback_data=f"rebuttal_choice:Арест:{case_id}")],
            [InlineKeyboardButton("🅿️ Штрафстоянка", callback_data=f"rebuttal_choice:Штрафстоянка:{case_id}"), 
             InlineKeyboardButton("🧾 Штраф", callback_data=f"rebuttal_choice:Штраф:{case_id}")],
            [InlineKeyboardButton("⏳ Срок", callback_data=f"rebuttal_choice:Срок:{case_id}"), 
             InlineKeyboardButton("🧱 Карцер", callback_data=f"rebuttal_choice:Картцер:{case_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(rebuttal_keyboard)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Ответ на форуме опубликован, тема закреплена для иска #{case_id}.\n\nТеперь, пожалуйста, **выберите тип запрошенного опровержения**:",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Ошибка в воркфлоу запроса опровержения для иска {case_id}: {e}", exc_info=True)
        if conn: conn.rollback()
        await status_message.edit_text(text=f"❌ Произошла ошибка: {e}\n\nСтатус иска #{case_id} не был изменен. Проверьте логи.")
# --- Конец handle_refutation_workflow ---

# --- Начало handle_rebuttal_choice ---
async def handle_rebuttal_choice(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int, rebuttal_type: str):
    query = update.callback_query
    await query.edit_message_text(text=f"✅ Выбор принят: '{rebuttal_type}'.\n\nСобираю данные и запускаю обработчик...")

    conn = context.bot_data['db_connection']
    logger.info(f"Для иска #{case_id} выбран тип опровержения: '{rebuttal_type}'. Запускаю внешний скрипт.")

    try:
        # 1. Сбор данных (как и раньше)
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT topic_link, officer_name, current_judge FROM {CASES_TABLE_NAME} WHERE id = ?",
            (case_id,)
        )
        db_result = cursor.fetchone()
        topic_link, officer_name, current_judge = db_result

        cursor.execute(
            f"SELECT yarn_judge FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?",
            (query.from_user.id,)
        )
        yarn_judge_value = cursor.fetchone()[0] or "не_установлено"

        # 2. Подготовка и запуск субпроцесса (как и раньше)
        command_args = [
            'python', 'yarnabi_handler.py', str(topic_link), str(officer_name),
            str(current_judge), str(rebuttal_type), str(yarn_judge_value)
        ]
        
        logger.info(f"Запускаю субпроцесс: {command_args}")
        await query.edit_message_text(text="⚙️ Запустил обработчик... Ожидаю ответа...")

        process_result = subprocess.run(
            command_args, capture_output=True, text=True, check=False, encoding='utf-8'
        )

        # 3. ОБНОВЛЕННАЯ ОБРАБОТКА РЕЗУЛЬТАТА
        if process_result.returncode == 0:
            # Скрипт завершился без системных ошибок, теперь парсим его JSON-ответ
            try:
                json_output = json.loads(process_result.stdout)
                
                # Проверяем внутренний статус код из JSON
                if json_output.get("status_code") == 200:
                    # ПОЛНЫЙ УСПЕХ
                    success_message = json_output.get("message", "Получен пустой успешный ответ.")
                    logger.info(f"Скрипт yarnabi_handler.py успешно выполнен для иска #{case_id}.")
                    response_text = (
                        f"✅ Запрос опровержения '{rebuttal_type}' для иска #{case_id} успешно выполнен.\n\n"
                        f"<b>Ответ обработчика:</b>\n{success_message}"
                    )
                    await query.edit_message_text(text=response_text, parse_mode='HTML')
                else:
                    # Ошибка на уровне приложения (status_code не 200)
                    error_message = json_output.get("message", "Обработчик вернул ошибку без описания.")
                    logger.error(f"Скрипт yarnabi_handler.py вернул ошибку приложения: {error_message}")
                    response_text = (
                        f"❌ Обработчик вернул ошибку для запроса '{rebuttal_type}' (иск #{case_id}):\n\n"
                        f"<pre>{error_message}</pre>"
                    )
                    await query.edit_message_text(text=response_text, parse_mode='HTML')

            except (json.JSONDecodeError, KeyError) as e:
                # Ошибка, если вывод скрипта - невалидный JSON или в нем нет нужных ключей
                logger.error(f"Не удалось разобрать JSON-ответ от yarnabi_handler.py: {e}")
                logger.error(f"Полученный вывод: {process_result.stdout}")
                await query.edit_message_text(text="❌ Получен некорректный ответ от обработчика. Обратитесь к администратору.")
        
        else:
            # Ошибка на уровне системы (скрипт "упал")
            logger.error(f"Скрипт yarnabi_handler.py вернул системную ошибку для иска #{case_id}. stderr: {process_result.stderr}")
            response_text = (
                f"❌ При выполнении запроса '{rebuttal_type}' для иска #{case_id} произошла системная ошибка.\n\n"
                f"<b>Сообщение об ошибке:</b>\n<pre>{process_result.stderr}</pre>"
            )
            await query.edit_message_text(text=response_text, parse_mode='HTML')

    except Exception as e:
        logger.error(f"Критическая ошибка в воркфлоу handle_rebuttal_choice для иска #{case_id}: {e}", exc_info=True)
        await query.edit_message_text(text=f"❌ Произошла критическая ошибка в работе бота: {e}")
# --- Конец handle_rebuttal_choice ---

# --- Начало функций для диалога "Свой ответ" ---
async def custom_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Начинает диалог для кастомного ответа.
    Версия с исправленной логикой редактирования сообщений.
    """
    query = update.callback_query
    await query.answer()

    try:
        _, reply_type, case_id_str = query.data.split(':')
        case_id = int(case_id_str)
        
        context.user_data['custom_reply_case_id'] = case_id
        context.user_data['custom_reply_type'] = reply_type
        
        action_text = "финального ответа" if reply_type == 'final' else "промежуточного ответа"
        
        logger.info(f"Пользователь {query.from_user.id} начал ввод {action_text} для иска #{case_id}.")
        
        # Редактируем исходное сообщение с фото, убирая кнопки
        await query.edit_message_caption(
            caption=f"✍️ Вы выбрали действие '{action_text}' для иска #{case_id}. Ожидаю ваш текст...",
            reply_markup=None
        )
        
        # Отправляем НОВОЕ сообщение с инструкцией
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"Пожалуйста, введите текст вашего <b>{action_text}</b>.\n\nОтправьте /cancel для отмены.",
            parse_mode='HTML'
        )
        
        return AWAITING_CUSTOM_REPLY

    except (ValueError, IndexError):
        await query.edit_message_caption(caption="Ошибка: не удалось определить ID иска или тип ответа.", reply_markup=None)
        return ConversationHandler.END


async def received_custom_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает текст от судьи и запускает соответствующий воркфлоу публикации."""
    custom_text = update.message.text
    # Извлекаем из user_data и ID, и ТИП ответа
    case_id = context.user_data.get('custom_reply_case_id')
    reply_type = context.user_data.get('custom_reply_type')
    
    if not case_id or not reply_type:
        await update.message.reply_text("Произошла ошибка, ID иска или тип ответа не найден. Пожалуйста, начните заново.")
        return ConversationHandler.END

    await update.message.reply_text(f"✅ Текст получен. Начинаю публикацию для иска #{case_id}... ⏳")
    
    conn = context.bot_data['db_connection']
    driver = selenium_driver
    judge_tg_user_id = update.effective_user.id
    
    # Статус и маркер шаблона в зависимости от типа ответа
    target_status = 'g' if reply_type == 'final' else 'f'
    template_marker = 'custom2' if reply_type == 'final' else 'custom'

    try:
        # 1. Подготовка данных
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET status = ? WHERE id = ?", (target_status, case_id,))
        # Готовим краткую версию ответа для лога
        log_reply_snippet = (custom_text[:70] + '...') if len(custom_text) > 73 else custom_text
        
        # Определяем текст для лога
        log_message_action = "Опубликован финальный вердикт" if reply_type == 'final' else "Опубликован промежуточный ответ"
        
        # Собираем полную заметку
        full_log_message = f"{log_message_action} (Судья: {context.user_data.get('user_nick_name', 'Неизвестно')}): \"{log_reply_snippet}\""
        
        # Добавляем заметку в базу
        add_note_to_case(conn, case_id, full_log_message)
        cursor.execute(f"SELECT applicant_name, case_num, topic_link, officer_name FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        case_data_db = cursor.fetchone()
        cursor.execute(f"SELECT nick_name FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (judge_tg_user_id,))
        judge_nick_name = cursor.fetchone()[0]
        cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (template_marker,))
        template_text = cursor.fetchone()[0]

        conn.commit()

        # счётчик
        await check_and_increment_case_number(conn, case_id)
        
        data_context = {
            'applicant_name': case_data_db[0], 'case_num': case_data_db[1],
            'officer_name': case_data_db[3], 'judge_nick_name': judge_nick_name,
            'custom_text': custom_text
        }
        topic_link = case_data_db[2]
        final_reply_text = await text_editor_helper(conn, template_text, data_context)
        
        # 2. Сессия Судьи: логин, публикация, выход
        if not await login_perform(driver, conn, judge_tg_user_id):
            raise Exception("Не удалось войти в аккаунт судьи.")
        
        if not await answer_perform(driver, topic_link, final_reply_text):
            raise Exception("Не удалось опубликовать ответ на форуме.")
            
        await logout_perform(driver)
        
        # 3. ЕСЛИ ОТВЕТ ФИНАЛЬНЫЙ - ЗАПУСКАЕМ СЕССИЮ ВЛАДЕЛЬЦА ДЛЯ ЗАКРЫТИЯ
        if reply_type == 'final':
            await update.message.reply_text("Ответ опубликован. Вхожу под аккаунтом владельца для закрытия темы...")
            
            if not await login_perform(driver, conn, BOT_OWNER_ID):
                raise Exception("Не удалось войти в аккаунт владельца.")
            
            driver.get(topic_link)
            
            if not await close_perform(driver):
                raise Exception("Не удалось закрыть тему на форуме.")
            
            await asyncio.sleep (5)
            logger.info("Пауза в 5 секунд, чтобы убралась плашка")
                
            await logout_perform(driver)
            logger.info(f"Финальный ответ для иска #{case_id} опубликован, тема закрыта.")
            await update.message.reply_text(f"✅ Готово! Ваш финальный ответ для иска #{case_id} опубликован, тема на форуме закрыта.")
        else:
            logger.info(f"Промежуточный ответ для иска #{case_id} опубликован.")
            await update.message.reply_text(f"✅ Готово! Ваш промежуточный ответ для иска #{case_id} опубликован на форуме.")

    except Exception as e:
        logger.error(f"Ошибка в воркфлоу кастомного ответа для иска {case_id}: {e}", exc_info=True)
        conn.rollback()
        await update.message.reply_text(f"❌ Произошла ошибка: {e}\n\nПожалуйста, проверьте состояние иска вручную.")
        
    # Очищаем временные данные и выходим из диалога
    context.user_data.pop('custom_reply_case_id', None)
    context.user_data.pop('custom_reply_type', None)
    return ConversationHandler.END


async def cancel_custom_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет диалог ввода кастомного ответа."""
    if 'custom_reply_case_id' in context.user_data:
        del context.user_data['custom_reply_case_id']
        
    await update.message.reply_text("Действие отменено.", reply_markup=ReplyKeyboardRemove())
    logger.info(f"Пользователь {update.effective_user.id} отменил ввод кастомного ответа.")
    return ConversationHandler.END
# --- Конец функций для диалога ---

# --- Обработчик команды /details ---
async def details_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /details <case_id> для отображения подробной информации об иске.
    """
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']
    logger.info(f"Пользователь {tg_user_id} вызвал команду /details.")

    # 1. Проверка прав доступа пользователя
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}, Admin: {is_admin}) пытается получить детали иска.")
    if not can_proceed:
        logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) не прошел проверку доступа.")
        return
    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}, Admin: {is_admin}) прошел проверку доступа.")

    # 2. Проверка и получение ID иска из аргументов команды
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите именно ID иска после команды /details☝️\nНапример: /details 123")
        return
    try:
        case_id_to_view = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID иска должен быть числом🙄🙄🙄\nНапример: /details 123")
        return

    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}, Admin: {is_admin}) запрашивает детали иска ID: {case_id_to_view}")

    cursor = conn.cursor()
    logger.info(f"Получаем данные об иске ID {case_id_to_view} для пользователя {user_nick_name} (TG ID: {tg_user_id})")

    try:
        # 3. Извлечение данных об иске из Cases_DB
        cursor.execute(f"""
            SELECT id, case_num, status, applicant_name, officer_name, topic_link, current_judge
            FROM {CASES_TABLE_NAME}
            WHERE id = ?
        """, (case_id_to_view,))
        case_data = cursor.fetchone()
        logger.info(f"Данные об иске ID {case_id_to_view} получены: {case_data}")

        if not case_data:
            await update.message.reply_text(f"Иск с ID {case_id_to_view} не найден😬😬😬")
            logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) запросил несуществующий иск ID: {case_id_to_view}.")
            return

        (db_id, db_case_num, db_status, db_applicant_name, 
         db_officer_name, db_topic_link, db_current_judge) = case_data
        logger.info(f"Данные об иске ID {case_id_to_view} успешно извлечены: "
                    f"case_num={db_case_num}, status={db_status}, "
                    f"applicant_name={db_applicant_name}, officer_name={db_officer_name}, "
                    f"topic_link={db_topic_link}, current_judge={db_current_judge}")

        # 4. Извлечение описания статуса из Helper_DB
        status_description = db_status # Значение по умолчанию, если описание не найдено
        logger.info(f"Извлекаем описание статуса '{db_status}' для иска ID {case_id_to_view} из Helper_DB")
        if db_status:
            cursor.execute(f"""
                SELECT marker_desc
                FROM {HELPER_TABLE_NAME}
                WHERE marker = ?
            """, (db_status,))
            status_desc_row = cursor.fetchone()
            if status_desc_row:
                status_description = status_desc_row[0]
        
        # 5. Подготовка данных для отображения (обработка None или пустых строк)
        case_num_display = db_case_num if db_case_num and db_case_num.strip() else "Без номера"
        applicant_name_display = db_applicant_name if db_applicant_name and db_applicant_name.strip() else "Не найден"
        officer_name_display = db_officer_name if db_officer_name and db_officer_name.strip() else "Не найден"
        topic_link_display = db_topic_link if db_topic_link and db_topic_link.strip() else "Не найдено"
        current_judge_display = db_current_judge if db_current_judge and db_current_judge.strip() else "Не назначен"
        status_code_display = db_status if db_status and db_status.strip() else "Неизвестен"
        logger.info(f"Данные для отображения подготовлены: "
                    f"case_num_display={case_num_display}, "
                    f"applicant_name_display={applicant_name_display}, "
                    f"officer_name_display={officer_name_display}, "
                    f"topic_link_display={topic_link_display}, "
                    f"current_judge_display={current_judge_display}, "
                    f"status_code_display={status_code_display}")

        # 6. Формирование сообщения
        response_message = f"📚 <b>Детали по иску №{case_num_display}</b> 📚\n\n"
        response_message += f"🔍 Уникальный номер: {db_id}\n"
        response_message += f"🔖 Статус: {status_description}\n"
        response_message += f"🙋‍♂️ Заявитель: {applicant_name_display}\n"
        response_message += f"💁 Ответчик: {officer_name_display}\n"
        if topic_link_display != "нет":
            response_message += f"🔗 Ссылка: <a href=\"{db_topic_link}\">Перейти к иску</a>\n" # Делаем ссылку кликабельной
        else:
            response_message += f"🔗 Ссылка: {topic_link_display}\n"
        response_message += f"🤵🏻‍♂️ Судья: {current_judge_display}"

        await update.message.reply_html(response_message, disable_web_page_preview=True) # disable_web_page_preview для ссылок
        logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) успешно получил детали иска ID: {case_id_to_view}")

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при получении деталей иска ID {case_id_to_view} для {user_nick_name}: {e_sql}")
        await update.message.reply_text("😧Произошла ошибка при доступе к базе данных. Попробуйте позже😧")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в details_case_command (ID иска: {case_id_to_view}): {e_general}", exc_info=True)
        await update.message.reply_text("😲Произошла внутренняя ошибка. Пожалуйста, сообщите администратору😲")
# --- Конц команды /details ---

# --- Обработчик команды /rejectcase ---
async def reject_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /rejectcase <case_id> для отказа судьи от дела.
    """
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']
    logger.info(f"Пользователь {tg_user_id} вызвал команду /rejectcase.")

    # 1. Проверка базовых прав доступа пользователя
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    if not can_proceed:
        return

    # 2. Проверка и получение ID иска из аргументов
    if not context.args:
        await update.message.reply_text(
            "Пожалуйста, укажите именно ID иска после команды /rejectcase 🤌\n"
            "Например: /rejectcase 123"
        )
        return
    try:
        case_id_to_reject = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "😵‍💫 ID иска должен быть числом 😵‍💫\n"
            "Например: /rejectcase 123"
        )
        return

    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) пытается отказаться от иска ID: {case_id_to_reject}")

    cursor = conn.cursor()
    try:
        # 3. Получение текущих данных иска (статус, текущий судья, номер дела)
        cursor.execute(f"""
            SELECT status, current_judge, case_num
            FROM {CASES_TABLE_NAME}
            WHERE id = ?
        """, (case_id_to_reject,))
        case_details = cursor.fetchone()

        if not case_details:
            await update.message.reply_text(f"Иск с ID {case_id_to_reject} не найден.")
            return

        db_status, db_current_judge, db_case_num = case_details
        case_num_display = db_case_num if db_case_num and db_case_num.strip() else "б/н"

        # 4. Проверка, позволяет ли статус иска отказ (должен быть 'b' или 'f')
        if db_status not in ('b', 'f'):
            status_description = db_status  # Значение по умолчанию
            if db_status:
                cursor.execute(f"""
                    SELECT marker_desc
                    FROM {HELPER_TABLE_NAME}
                    WHERE marker = ?
                """, (db_status,))
                desc_row = cursor.fetchone()
                if desc_row:
                    status_description = desc_row[0]
            
            await update.message.reply_text(
                f"👹 Нельзя отказаться от иска №{case_num_display} 👹\n"
                f"Его текущий статус: \"{status_description}\" ({db_status}).\n"
                "Отказаться можно только от тех дел, которые находятся у Вас в работе 😤"
            )
            return

        # 5. Проверка, является ли текущий пользователь ответственным судьей
        # Сравнение никнеймов без учета регистра
        if not db_current_judge or db_current_judge.lower() != user_nick_name.lower():
            assigned_judge_display = db_current_judge if db_current_judge and db_current_judge.strip() else "никому не назначен (возможно, ошибка данных)"
            await update.message.reply_text(
                f"Уважаемый {user_nick_name}, похоже, что не Вы ответственны за иск №{case_num_display} 🤔\n"
                f"Он назначен на судью: {assigned_judge_display}☝️\n"
                "Вы не можете от него отказаться🫰"
            )
            return

        # 6. Обновление иска: установка статуса 'a' и очистка current_judge
        cursor.execute(f"""
            UPDATE {CASES_TABLE_NAME}
            SET status = 'a', current_judge = NULL
            WHERE id = ?
        """, (case_id_to_reject,))
        conn.commit()

        logger.info(f"Пользователь {user_nick_name} успешно отказался от иска ID: {case_id_to_reject}. Иск возвращен в пул (статус 'a').")
        await update.message.reply_text(
            f"{user_nick_name}, 🙂‍↕️Вы успешно отказались от иска №{case_num_display}🙂‍↕️\n"
            "Теперь он доступен всем для рассмотрения😌"
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при отказе от иска ID {case_id_to_reject} для {user_nick_name}: {e_sql}")
        if conn: # Попытка отката транзакции, если соединение еще существует
            try:
                conn.rollback()
            except sqlite3.Error as e_rb:
                logger.error(f"Ошибка при откате транзакции: {e_rb}")
        await update.message.reply_text("🤕Произошла ошибка при обработке Вашего запроса к базе данных. Пожалуйста, попробуйте позже🤕")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в reject_case_command (ID иска: {case_id_to_reject}): {e_general}", exc_info=True)
        await update.message.reply_text("🤒Произошла внутренняя ошибка сервера. Пожалуйста, сообщите администратору🤒")
# --- Конец команды /rejectcase ---

# --- Начало close_case_command ---
async def close_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /close <case_id> для закрытия дела судьей.
    Дело переводится в статус 'g'.
    """
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']
    logger.info(f"Пользователь {tg_user_id} вызвал команду /close.")

    # 1. Проверка базовых прав доступа пользователя
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    if not can_proceed:
        # perform_wa_check уже отправил сообщение
        return

    # 2. Проверка и получение ID иска из аргументов
    if not context.args:
        await update.message.reply_text(
            "Пожалуйста, укажите ID иска после команды /close🤌\n"
            "Например: /close 123"
        )
        return
    try:
        case_id_to_close = int(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "😵‍💫ID иска должен быть числом😵‍💫\n"
            "Например: /close 123"
        )
        return

    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) пытается закрыть иск ID: {case_id_to_close}")

    cursor = conn.cursor()
    try:
        # 3. Получение текущих данных иска (статус, текущий судья, номер дела)
        cursor.execute(f"""
            SELECT status, current_judge, case_num
            FROM {CASES_TABLE_NAME}
            WHERE id = ?
        """, (case_id_to_close,))
        case_details = cursor.fetchone()

        if not case_details:
            await update.message.reply_text(f"Иск с ID {case_id_to_close} не найден🤷‍♂️")
            return

        db_status, db_current_judge, db_case_num = case_details
        case_num_display = db_case_num if db_case_num and db_case_num.strip() else "б/н"

        # 4. Проверка текущего статуса иска
        if db_status == 'a':
            status_a_desc = "Доступно к рассмотрению (a)" # Значение по умолчанию
            cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = 'a'")
            desc_row = cursor.fetchone()
            if desc_row and desc_row[0]:
                status_a_desc = f"{desc_row[0]} (a)"
            await update.message.reply_text(
                f"Нельзя закрыть иск №{case_num_display} 👀\n"
                f"Он находится в статусе: \"{status_a_desc}\"✨"
            )
            return
        elif db_status in ('c', 'd', 'e', 'g'):
            # Для этих статусов можно также получить описание из Helper_DB, если нужно
            # Но по диаграмме сообщение фиксированное для них
            status_desc = db_status
            cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (db_status,))
            desc_row = cursor.fetchone()
            if desc_row and desc_row[0]:
                status_desc = f"{desc_row[0]} ({db_status})" # например, "Закрыт (g)"
            
            await update.message.reply_text(
                f"🙌 Иск №{case_num_display} уже находится в завершающем статусе: \"{status_desc}\" 🙌"
            )
            return
        elif db_status not in ('b', 'f'): # Если статус не 'b' и не 'f', и не обработан выше
            status_desc = db_status
            cursor.execute(f"SELECT marker_desc FROM {HELPER_TABLE_NAME} WHERE marker = ?", (db_status,))
            desc_row = cursor.fetchone()
            if desc_row and desc_row[0]:
                status_desc = f"{desc_row[0]} ({db_status})"

            await update.message.reply_text(
                f"Нельзя закрыть иск №{case_num_display} из текущего статуса: \"{status_desc}\"😖\n"
                "Закрыть можно только дела, находящиеся в работе ('b') или на опровержении ('f')☝️"
            )
            return
        
        # Если статус 'b' или 'f', продолжаем

        # 5. Проверка, является ли текущий пользователь ответственным судьей
        if not db_current_judge or db_current_judge.lower() != user_nick_name.lower():
            assigned_judge_display = db_current_judge if db_current_judge and db_current_judge.strip() else "никому не назначен"
            await update.message.reply_text(
                f"Уважаемый {user_nick_name}, похоже, что не Вы ответственны за иск №{case_num_display} 🤔\n"
                f"Он назначен на судью: {assigned_judge_display}☝️\n"
                "Вы не можете от него отказаться🫰"
            )
            return

        # 6. Обновление иска: установка статуса 'g'
        cursor.execute(f"""
            UPDATE {CASES_TABLE_NAME}
            SET status = 'g'
            WHERE id = ?
        """, (case_id_to_close,))
        conn.commit()

        logger.info(f"Пользователь {user_nick_name} успешно закрыл иск ID: {case_id_to_close} (статус 'g').")
        await update.message.reply_text(
            f"✅{user_nick_name}, Вы успешно закрыли иск №{case_num_display}!✅"
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при закрытии иска ID {case_id_to_close} для {user_nick_name}: {e_sql}")
        if conn:
            try:
                conn.rollback()
            except sqlite3.Error as e_rb:
                logger.error(f"Ошибка при откате транзакции: {e_rb}")
        await update.message.reply_text("Произошла ошибка при обработке Вашего запроса к базе данных. Пожалуйста, попробуйте позже.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в close_case_command (ID иска: {case_id_to_close}): {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера. Пожалуйста, сообщите администратору.")
# --- Конец close_case_command ---

# --- Начало flist_command ---
async def flist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    # 1. Проверка прав доступа (администратор)
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    
    if not is_admin: # Проверяем именно флаг is_admin
        logger.warning(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) попытался использовать команду /flist без прав администратора.")
        await update.message.reply_text("Вы не являетесь администратором. Выполнение команды невозможно.")
        return

    if not can_proceed: # Дополнительная проверка, если perform_wa_check вернул False по другим причинам (хотя для админа это маловероятно)
        logger.info(f"Admin {user_nick_name} (TG ID: {tg_user_id}) не прошел perform_wa_check для /flist (неожиданно).")
        # Сообщение уже было отправлено perform_wa_check
        return

    # 2. Парсинг аргументов (период дат)
    date_range_str = " ".join(context.args)
    date_parts = date_range_str.split(" - ", 1)

    if len(date_parts) != 2:
        await update.message.reply_text(
            "Неверный формат периода дат. Используйте: /flist дд.мм.гг - дд.мм.гг\n"
            "Например: /flist 01.01.25 - 31.01.25"
        )
        return

    start_date_str, end_date_str = date_parts[0].strip(), date_parts[1].strip()

    try:
        start_date = datetime.strptime(start_date_str, "%d.%m.%y").date()
        end_date = datetime.strptime(end_date_str, "%d.%m.%y").date()
    except ValueError:
        await update.message.reply_text(
            "Неверный формат одной из дат. Пожалуйста, используйте формат дд.мм.гг\n"
            "Например: 01.01.25"
        )
        return

    if start_date > end_date:
        await update.message.reply_text("Дата начала периода не может быть позже даты окончания.")
        return

    logger.info(f"Администратор {user_nick_name} запросил /flist для периода: {start_date_str} - {end_date_str}")

    # Преобразуем даты в формат YYYY-MM-DD для SQL запроса
    sql_start_date = start_date.strftime("%Y-%m-%d")
    sql_end_date = end_date.strftime("%Y-%m-%d")

    cursor = conn.cursor()
    try:
        # 3. Запрос к БД для получения исков в указанном периоде
        cursor.execute(f"""
            SELECT c.id, c.case_num, c.status, h.marker_desc, c.current_judge, c.scraped_at
            FROM {CASES_TABLE_NAME} c
            LEFT JOIN {HELPER_TABLE_NAME} h ON c.status = h.marker
            WHERE date(c.scraped_at) BETWEEN ? AND ?
            ORDER BY c.scraped_at ASC
        """, (sql_start_date, sql_end_date))
        
        cases = cursor.fetchall()

        if not cases:
            await update.message.reply_text(f"За период с {start_date_str} по {end_date_str} иски не найдены.")
            return

        # 4. Формирование ответа
        response_lines = [f"<b>Иски, собранные с {start_date_str} по {end_date_str}:</b>"]
        for case in cases:
            (db_id, db_case_num, db_status_marker, db_marker_desc, 
             db_current_judge, db_scraped_at_str) = case

            case_num_display = db_case_num if db_case_num and db_case_num.strip() else "б/н"
            marker_display = db_status_marker if db_status_marker and db_status_marker.strip() else "статус?"
            marker_desc_display = db_marker_desc if db_marker_desc and db_marker_desc.strip() else "описание отсутствует"
            judge_display = db_current_judge if db_current_judge and db_current_judge.strip() else "не назначен"
            
            scraped_at_formatted = "дата неизв."
            if db_scraped_at_str:
                try:
                    # scraped_at из SQLite обычно в формате 'YYYY-MM-DD HH:MM:SS'
                    scraped_dt_obj = datetime.strptime(db_scraped_at_str, "%Y-%m-%d %H:%M:%S")
                    scraped_at_formatted = scraped_dt_obj.strftime("%d.%m.%Y %H:%M")
                except ValueError:
                    scraped_at_formatted = db_scraped_at_str # Если формат другой, отображаем как есть

            line = (f"{db_id}) Иск №{case_num_display} ({marker_display}): {marker_desc_display}. "
                    f"Судья ({judge_display}) - {scraped_at_formatted}")
            response_lines.append(line)
        
        # Отправка сообщения (может быть длинным, Telegram разобьет или нужно будет делить вручную)
        full_response = "\n".join(response_lines)
        
        if len(full_response) > 4096: # Максимальная длина сообщения в Telegram
            await update.message.reply_text("Найдено слишком много исков. Вывод будет сокращен (пока не реализована пагинация).")
            temp_response = ""
            for line in response_lines:
                if len(temp_response) + len(line) + 1 > 4090: # небольшой запас
                    break
                temp_response += line + "\n"
            full_response = temp_response.strip()


        await update.message.reply_html(full_response if full_response else "Нет данных для отображения после фильтрации длины.")

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при выполнении /flist для администратора {user_nick_name} ({sql_start_date} - {sql_end_date}): {e_sql}")
        await update.message.reply_text("Произошла ошибка при доступе к базе данных. Попробуйте позже.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в /flist для администратора {user_nick_name}: {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера. Пожалуйста, сообщите разработчику.")
# Конец flist_command ---

# --- Обновленная команда для просмотра логов иска ---
async def case_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает историю действий (заметки) по указанному иску,
    используя scraped_at как первую запись в логе.
    """
    conn = context.bot_data['db_connection']
    
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id=update.effective_user.id, update=update)
    if not can_proceed:
        return

    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID иска, для которого хотите посмотреть лог.\nПример: /caselog 123")
        return
    try:
        case_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID иска должен быть числом.")
        return

    try:
        cursor = conn.cursor()
        # Теперь получаем также и scraped_at
        cursor.execute(f"SELECT notes, case_num, scraped_at FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        result = cursor.fetchone()

        if not result:
            await update.message.reply_text(f"Иск с ID {case_id} не найден.")
            return

        notes, case_num, scraped_at_str = result
        case_num_display = case_num if case_num else f"ID {case_id}"
        
        # --- ФОРМИРОВАНИЕ ПЕРВОЙ ЗАПИСИ ИЗ scraped_at ---
        initial_log_entry = ""
        if scraped_at_str:
            try:
                # Преобразуем строку из БД в объект datetime
                dt_obj = datetime.strptime(scraped_at_str, "%Y-%m-%d %H:%M:%S")
                # Форматируем в нужный нам вид
                formatted_date = dt_obj.strftime("%d.%m.%Y %H:%M:%S")
                initial_log_entry = f"[{formatted_date}] Иск добавлен в систему скрапером."
            except (ValueError, TypeError):
                # Если формат даты некорректный, просто выводим как есть
                initial_log_entry = f"[{scraped_at_str}] Иск добавлен в систему скрапером."
        
        # Объединяем первую запись с остальными заметками
        full_log = (initial_log_entry + "\n" + notes) if notes else initial_log_entry
            
        if not full_log:
            await update.message.reply_text(f"Для иска №{case_num_display} пока нет записей в логе.")
            return
            
        # Формируем и отправляем ответ
        response_text = f"📋 <b>История действий по иску №{case_num_display}:</b>\n\n<pre>{full_log}</pre>"
        await update.message.reply_html(response_text)

    except Exception as e:
        logger.error(f"Ошибка при получении логов для иска #{case_id}: {e}", exc_info=True)
        await update.message.reply_text("Произошла ошибка при получении логов иска.")

# --- Начало help_command ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    # 1. Проверка базовых прав доступа пользователя
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    
    if not can_proceed:
        logger.info(f"Пользователь (TG ID: {tg_user_id}) не прошел perform_wa_check для команды /help.")
        return

    logger.info(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}, Admin: {is_admin}) запросил команду /help.")

    # 2. Определение маркера для текста справки в зависимости от роли
    if is_admin:
        help_marker_key = 'help_adm_desc'
    else:
        help_marker_key = 'help_user_desc'
        
    cursor = conn.cursor()
    help_text = None
    try:
        # 3. Извлечение текста справки из Helper_DB
        cursor.execute(f"""
            SELECT marker_desc
            FROM {HELPER_TABLE_NAME}
            WHERE marker = ?
        """, (help_marker_key,))
        
        result = cursor.fetchone()
        
        if result and result[0]:
            help_text = result[0]
        else:
            logger.warning(f"Текст справки для маркера '{help_marker_key}' не найден в Helper_DB.")
            help_text = "Извините, справочная информация для вашей роли временно недоступна."

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при получении справки для маркера '{help_marker_key}' (пользователь {user_nick_name}): {e_sql}")
        help_text = "Произошла ошибка при загрузке справочной информации. Пожалуйста, попробуйте позже."
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в help_command при получении справки для {user_nick_name}: {e_general}", exc_info=True)
        help_text = "Произошла внутренняя ошибка. Пожалуйста, сообщите администратору."

    # 4. Отправка сообщения пользователю
    await update.message.reply_html(help_text)
# Конец help_command ---

# --- Начало команды /addj ---
async def add_judge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    # 1. Проверка прав доступа (администратор)
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    
    if not is_admin:
        logger.warning(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) попытался использовать команду /addj без прав администратора.")
        await update.message.reply_text("Вы не являетесь администратором. Выполнение команды невозможно.")
        return

    if not can_proceed:
        # Этого не должно произойти, если is_admin = True, но на всякий случай
        logger.info(f"Admin {user_nick_name} (TG ID: {tg_user_id}) не прошел perform_wa_check для /addj.")
        return

    # 2. Получение никнейма судьи из аргументов
    # Объединяем все аргументы, чтобы никнеймы с пробелами работали корректно
    nickname_to_add = " ".join(context.args)

    if not nickname_to_add:
        await update.message.reply_text(
            "Пожалуйста, укажите никнейм судьи после команды.\n"
            "Например: /addj Rinat_Akhmetov"
        )
        return

    logger.info(f"Администратор {user_nick_name} пытается добавить судью '{nickname_to_add}' в белый список.")

    cursor = conn.cursor()
    try:
        # 3. Добавление никнейма в базу данных
        cursor.execute(f"""
            INSERT INTO {WHITELIST_TABLE_NAME} (nick_name) VALUES (?)
        """, (nickname_to_add,))
        conn.commit()

        # 4. Отправка подтверждения
        logger.info(f"Судья '{nickname_to_add}' успешно добавлен в белый список администратором {user_nick_name}.")
        await update.message.reply_text(
            f"✅ Судья <b>{nickname_to_add}</b> успешно добавлен в белый список.",
            parse_mode='HTML'
        )

    except sqlite3.IntegrityError:
        # Эта ошибка возникает, если такой никнейм уже существует (PRIMARY KEY constraint failed)
        logger.warning(f"Попытка добавить дублирующийся никнейм '{nickname_to_add}' в белый список.")
        await update.message.reply_text(
            f"⚠️ Судья с никнеймом <b>{nickname_to_add}</b> уже существует в белом списке.",
            parse_mode='HTML'
        )
    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при добавлении судьи '{nickname_to_add}' администратором {user_nick_name}: {e_sql}")
        if conn:
            try:
                conn.rollback()
            except sqlite3.Error as e_rb:
                logger.error(f"Ошибка при откате транзакции: {e_rb}")
        await update.message.reply_text("Произошла ошибка при работе с базой данных. Пожалуйста, попробуйте позже.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в add_judge_command при добавлении '{nickname_to_add}': {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера. Пожалуйста, сообщите разработчику.")
# --- Конец команды /addj ---

# --- Начало команды /removej ---
async def remove_judge_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    # 1. Проверка прав доступа (администратор)
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    
    if not is_admin:
        logger.warning(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) попытался использовать команду /removej без прав администратора.")
        await update.message.reply_text("Вы не являетесь администратором. Выполнение команды невозможно.")
        return

    if not can_proceed:
        logger.info(f"Admin {user_nick_name} (TG ID: {tg_user_id}) не прошел perform_wa_check для /removej.")
        return

    # 2. Получение никнейма судьи из аргументов
    nickname_to_remove = " ".join(context.args)

    if not nickname_to_remove:
        await update.message.reply_text(
            "Пожалуйста, укажите никнейм судьи, которого нужно удалить.\n"
            "Например: /removej Rinat_Akhmetov"
        )
        return

    logger.info(f"Администратор {user_nick_name} пытается удалить судью '{nickname_to_remove}' из белого списка.")

    cursor = conn.cursor()
    try:
        # Шаг 1: Проверить, существует ли судья в белом списке.
        cursor.execute(f"""
            SELECT 1 FROM {WHITELIST_TABLE_NAME} WHERE nick_name = ?
        """, (nickname_to_remove,))
        
        if cursor.fetchone() is None:
            await update.message.reply_text(
                f"⚠️ Судья с никнеймом <b>{nickname_to_remove}</b> не найден в белом списке.",
                parse_mode='HTML'
            )
            return

        # Шаг 2: Деавторизовать пользователя в Users_DB, если он там есть и авторизован.
        cursor.execute(f"""
            UPDATE {USERS_TABLE_NAME}
            SET authorization = 0
            WHERE nick_name = ? AND authorization = 1
        """, (nickname_to_remove,))
        
        if cursor.rowcount > 0:
            logger.info(f"Пользователь с ником '{nickname_to_remove}' был принудительно деавторизован.")

        # Шаг 3: Удалить судью из белого списка.
        cursor.execute(f"""
            DELETE FROM {WHITELIST_TABLE_NAME} WHERE nick_name = ?
        """, (nickname_to_remove,))

        # Применяем все изменения в транзакции
        conn.commit()

        # Шаг 4: Отправка подтверждения.
        logger.info(f"Судья '{nickname_to_remove}' успешно удалён из белого списка администратором {user_nick_name}.")
        await update.message.reply_text(
            f"✅ Судья <b>{nickname_to_remove}</b> успешно удалён из белого списка.",
            parse_mode='HTML'
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при удалении судьи '{nickname_to_remove}' администратором {user_nick_name}: {e_sql}")
        if conn:
            try:
                conn.rollback() # Откатываем транзакцию в случае любой SQL ошибки
            except sqlite3.Error as e_rb:
                logger.error(f"Ошибка при откате транзакции: {e_rb}")
        await update.message.reply_text("Произошла ошибка при работе с базой данных. Изменения были отменены.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в remove_judge_command при удалении '{nickname_to_remove}': {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера. Пожалуйста, сообщите разработчику.")
# --- Конец команды /removej ---

# --- Начало команды /broadcast ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /broadcast <сообщение> для рассылки всем пользователям.
    Доступно только для администраторов.
    """
    tg_user_id = update.effective_user.id
    conn = context.bot_data['db_connection']

    # 1. Проверка прав доступа (администратор)
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id, update)
    
    if not is_admin:
        logger.warning(f"Пользователь {user_nick_name} (TG ID: {tg_user_id}) попытался использовать команду /broadcast без прав администратора.")
        await update.message.reply_text("Вы не являетесь администратором. Выполнение команды невозможно.")
        return

    if not can_proceed:
        logger.info(f"Admin {user_nick_name} (TG ID: {tg_user_id}) не прошел perform_wa_check для /broadcast.")
        return

    # 2. Получение текста для рассылки
    broadcast_message = " ".join(context.args)

    if not broadcast_message:
        await update.message.reply_text(
            "Пожалуйста, введите сообщение для рассылки после команды.\n"
            "Например: /broadcast Всем привет!"
        )
        return

    logger.info(f"Администратор {user_nick_name} начал рассылку с текстом: '{broadcast_message}'")
    await update.message.reply_text("✅ Начинаю рассылку. Это может занять некоторое время...")

    cursor = conn.cursor()
    try:
        # 3. Получение списка всех пользователей
        cursor.execute(f"SELECT tg_user_id FROM {USERS_TABLE_NAME}")
        all_users = cursor.fetchall()

        if not all_users:
            await update.message.reply_text("В базе данных нет пользователей для рассылки.")
            return

        # 4. Цикл рассылки с защитой от ошибок и лимитов
        success_count = 0
        fail_count = 0

        for user_tuple in all_users:
            user_id = user_tuple[0]
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=broadcast_message,
                    parse_mode='HTML' # Позволяет админу использовать HTML-теги
                )
                success_count += 1
            except Forbidden:
                # Пользователь заблокировал бота
                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: бот заблокирован.")
                fail_count += 1
            except BadRequest as e:
                # Ошибка в запросе (например, неверный ID или другая проблема)
                logger.error(f"Не удалось отправить сообщение пользователю {user_id}: ошибка запроса - {e}")
                fail_count += 1
            except Exception as e:
                # Любая другая непредвиденная ошибка
                logger.error(f"Не удалось отправить сообщение пользователю {user_id}: непредвиденная ошибка - {e}")
                fail_count += 1
            
            # ЗАЩИТА ОТ ЛИМИТОВ TELEGRAM: делаем небольшую паузу
            await asyncio.sleep(0.1) # Пауза в 0.1 секунды (10 сообщений/сек)

        # 5. Отправка отчета администратору
        report_message = (
            f"📊 **Отчет о рассылке**\n\n"
            f"Рассылка завершена.\n"
            f"✅ Успешно отправлено: {success_count}\n"
            f"❌ Не удалось отправить: {fail_count}\n"
            f"🌀 Всего пользователей: {len(all_users)}"
        )
        await update.message.reply_text(report_message)

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при выполнении /broadcast для администратора {user_nick_name}: {e_sql}")
        await update.message.reply_text("Произошла ошибка при получении списка пользователей из базы данных.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в broadcast_command: {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера во время рассылки.")
# --- Конец команды /broadcast ---

# --- Начало команды /aset ---
async def set_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data['db_connection']
    cursor = conn.cursor()
    user_id_caller = update.effective_user.id

    # 1. Проверка, что команду вызывает владелец бота
    if user_id_caller != BOT_OWNER_ID:
        # --- ЛОГИКА НАКАЗАНИЯ ДЛЯ НЕ-ВЛАДЕЛЬЦЕВ ---
        logger.warning(
            f"Пользователь {update.effective_user.full_name} (TG ID: {user_id_caller}) "
            f"попытался использовать команду /aset, не являясь владельцем."
        )
        
        # Получаем никнейм нарушителя для сообщения
        cursor.execute(f"SELECT nick_name FROM {USERS_TABLE_NAME} WHERE tg_user_id = ?", (user_id_caller,))
        user_record = cursor.fetchone()
        display_name = user_record[0] if user_record and user_record[0] else update.effective_user.full_name
        
        # Применяем наказание: бан на 1 час (3600 секунд)
        punishment_duration_seconds = 3600
        punished_until_timestamp = int(time.time()) + punishment_duration_seconds
        
        try:
            cursor.execute(f"UPDATE {USERS_TABLE_NAME} SET punished_until = ? WHERE tg_user_id = ?", (punished_until_timestamp, user_id_caller))
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"Не удалось применить наказание для пользователя {user_id_caller} из-за ошибки БД: {e}")
            # Даже если наказание не удалось применить, доступ все равно запрещаем
        
        # Отправляем комбинированное сообщение о наказании
        punishment_message = (
            f"😅 Ай-ай-ай, <b>{display_name}</b>! 😅\nКто это тут у нас пытается стать главным? 🤨🤨🤨\n"
            f"Маленький ещё! ☝️☝️☝️\nЭта команда будет доступна, только когда вырастешь. 😉\n\n"
            f"За эту попытку вы не сможете пользоваться командами бота в течение <b>1 часа</b>. 🫡"
        )
        await update.message.reply_html(punishment_message)
        return # Завершаем выполнение функции

    # --- ЛОГИКА ДЛЯ ВЛАДЕЛЬЦА БОТА ---
    # (Этот код выполняется, только если проверка на ID владельца пройдена)
    
    # 2. Получение и проверка аргументов
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Неверный формат команды. Используйте: /aset <никнейм> <1 или 0>\n"
            "Пример: /aset Nickname 1 (назначить)\n"
            "Пример: /aset Nickname 0 (снять)"
        )
        return
        
    flag_str = args[-1]
    nickname_to_modify = " ".join(args[:-1])

    if flag_str not in ('0', '1'):
        await update.message.reply_text("Неверный флаг. Последний аргумент должен быть 1 (назначить) или 0 (снять).")
        return

    is_admin_value = int(flag_str)
    action_text = "предоставлены" if is_admin_value == 1 else "сняты"
    
    logger.info(f"Владелец бота (ID: {BOT_OWNER_ID}) пытается изменить права админа для '{nickname_to_modify}' на {is_admin_value}.")
    
    try:
        # 3. Обновление статуса is_admin для указанного пользователя
        cursor.execute(f"""
            UPDATE {USERS_TABLE_NAME}
            SET is_admin = ?
            WHERE nick_name = ?
        """, (is_admin_value, nickname_to_modify))

        # 4. Проверка, был ли пользователь найден и обновлен
        if cursor.rowcount == 0:
            await update.message.reply_text(
                f"⚠️ Пользователь с никнеймом <b>{nickname_to_modify}</b> не найден в базе данных.\n"
                "Убедитесь, что пользователь уже взаимодействовал с ботом и его ник указан верно.",
                parse_mode='HTML'
            )
            return

        conn.commit()

        # 5. Отправка подтверждения владельцу
        logger.info(f"Права администратора для '{nickname_to_modify}' успешно изменены на {is_admin_value}.")
        await update.message.reply_text(
            f"✅ Права администратора успешно <b>{action_text}</b> для пользователя <b>{nickname_to_modify}</b>.",
            parse_mode='HTML'
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при изменении прав администратора для '{nickname_to_modify}': {e_sql}")
        if conn:
            try:
                conn.rollback()
            except sqlite3.Error as e_rb:
                logger.error(f"Ошибка при откате транзакции: {e_rb}")
        await update.message.reply_text("Произошла ошибка при работе с базой данных. Изменения были отменены.")
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в set_admin_command для '{nickname_to_modify}': {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера. Пожалуйста, сообщите разработчику.")
# --- Конец команды /aset ---

# --- Начало /adm ---
async def admin_modify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data['db_connection']
    
    # 1. Проверка прав доступа (администратор)
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id=update.effective_user.id, update=update)
    if not can_proceed:
        # Сообщение уже отправлено внутри perform_wa_check
        logger.warning(f"Доступ для {user_nick_name or update.effective_user.id} к /adm был прерван функцией perform_wa_check.")
        return
    
    if not is_admin:
        await update.message.reply_text("Эта команда доступна только администраторам.")
        if user_nick_name:
             logger.warning(f"Пользователь {user_nick_name} (ID: {update.effective_user.id}) попытался использовать /adm.")
        return

    # 2. Получение и проверка аргументов
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Неверный формат команды. Используйте: /adm <ID иска> <новый_статус | ник_судьи | null>"
        )
        return
        
    try:
        case_id = int(args[0])
    except ValueError:
        await update.message.reply_text("ID иска должен быть числом.")
        return
        
    parameter_to_set = " ".join(args[1:])

    logger.info(f"Администратор {user_nick_name} выполняет /adm для иска {case_id} с параметром '{parameter_to_set}'.")
    
    cursor = conn.cursor()
    try:
        # Предварительная проверка: существует ли вообще иск с таким ID
        cursor.execute(f"SELECT 1 FROM {CASES_TABLE_NAME} WHERE id = ?", (case_id,))
        if cursor.fetchone() is None:
            await update.message.reply_text(f"Иск с ID {case_id} не найден в базе данных.")
            return

        # 3. Проверка на специальное слово 'null'
        if parameter_to_set.lower() == 'null':
            # Снимаем судью с дела
            cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET current_judge = NULL WHERE id = ?", (case_id,))
            conn.commit()
            await update.message.reply_text(f"✅ Судья с иска <b>{case_id}</b> успешно снят.", parse_mode='HTML')
            logger.info(f"С иска {case_id} снят судья администратором {user_nick_name}.")
            return # Завершаем работу

        # 4. Проверка, является ли параметр СТАТУСОМ
        cursor.execute(f"SELECT 1 FROM {HELPER_TABLE_NAME} WHERE marker = ?", (parameter_to_set,))
        if cursor.fetchone() is not None:
            # Это статус. Обновляем статус.
            cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET status = ? WHERE id = ?", (parameter_to_set, case_id))
            conn.commit()
            await update.message.reply_text(f"✅ Статус иска <b>{case_id}</b> успешно изменен на <b>'{parameter_to_set}'</b>.", parse_mode='HTML')
            logger.info(f"Иску {case_id} установлен статус '{parameter_to_set}' администратором {user_nick_name}.")
            return # Завершаем работу

        # 5. Если не статус, проверяем, является ли параметр СУДЬЕЙ
        cursor.execute(f"SELECT 1 FROM {WHITELIST_TABLE_NAME} WHERE nick_name = ?", (parameter_to_set,))
        if cursor.fetchone() is not None:
            # Это судья. Обновляем судью.
            cursor.execute(f"UPDATE {CASES_TABLE_NAME} SET current_judge = ? WHERE id = ?", (parameter_to_set, case_id))
            conn.commit()
            await update.message.reply_text(f"✅ На иск <b>{case_id}</b> успешно назначен судья <b>{parameter_to_set}</b>.", parse_mode='HTML')
            logger.info(f"На иск {case_id} назначен судья '{parameter_to_set}' администратором {user_nick_name}.")
            return # Завершаем работу

        # 6. Если параметр не найден нигде
        await update.message.reply_text(
            f"⚠️ Параметр <b>'{parameter_to_set}'</b> не является ни существующим статусом, "
            f"ни именем судьи из белого списка, ни командой 'null'.",
            parse_mode='HTML'
        )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при выполнении /adm для иска {case_id}: {e_sql}")
        if conn: conn.rollback()
        await update.message.reply_text("Произошла ошибка при работе с базой данных.")
        
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в /adm для иска {case_id}: {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера.")
# --- Конец /adm ---

# --- Начало /number ---
async def number_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data['db_connection']
    COUNTER_MARKER = 'true_case_num'

    # 1. ОСНОВНАЯ ПРОВЕРКА ДОСТУПА
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id=update.effective_user.id, update=update)
    if not can_proceed:
        # Сообщение уже отправлено внутри perform_wa_check
        return 
    
    cursor = conn.cursor()
    try:
        # 2. Проверяем, есть ли аргументы, чтобы определить режим
        if context.args:
            # --- РЕЖИМ УСТАНОВКИ (Только для администраторов) ---
            if not is_admin:
                await update.message.reply_text("Устанавливать номер иска могут только администраторы.")
                logger.warning(f"Пользователь {user_nick_name} (ID: {update.effective_user.id}) попытался установить номер иска, не будучи админом.")
                return
            
            new_number_str = context.args[0]
            
            # Проверяем, что введенное значение - целое положительное число
            try:
                new_number = int(new_number_str)
                if new_number < 0:
                    raise ValueError("Число не может быть отрицательным.")
            except ValueError:
                await update.message.reply_text("Ошибка. Пожалуйста, укажите целое положительное число.")
                return

            # Используем INSERT OR REPLACE для создания/обновления записи
            cursor.execute(f"""
                INSERT OR REPLACE INTO {HELPER_TABLE_NAME} (marker, marker_desc)
                VALUES (?, ?)
            """, (COUNTER_MARKER, str(new_number)))
            
            conn.commit()
            logger.info(f"Администратор {user_nick_name} установил следующий номер иска: {new_number}.")
            await update.message.reply_text(f"✅ Следующий номер иска успешно установлен: <b>{new_number}</b>", parse_mode='HTML')

        else:
            # --- РЕЖИМ ПРОСМОТРА (Для всех авторизованных судей) ---
            logger.info(f"Пользователь {user_nick_name} запросил текущий номер иска.")
            cursor.execute(f"""
                SELECT marker_desc FROM {HELPER_TABLE_NAME}
                WHERE marker = ?
            """, (COUNTER_MARKER,))
            
            result = cursor.fetchone()
            
            if result and result[0]:
                current_number = result[0]
                await update.message.reply_text(f"ℹ️ Следующий ожидаемый номер иска: <b>{current_number}</b>", parse_mode='HTML')
            else:
                await update.message.reply_text(
                    "Счетчик номера иска еще не установлен.\n"
                    "Задать его может администратор командой <code>/number [число]</code>.",
                    parse_mode='HTML'
                )

    except sqlite3.Error as e_sql:
        logger.error(f"Ошибка SQL при выполнении /number для пользователя {user_nick_name}: {e_sql}")
        if conn: conn.rollback()
        await update.message.reply_text("Произошла ошибка при работе с базой данных.")
        
    except Exception as e_general:
        logger.error(f"Непредвиденная ошибка в /number: {e_general}", exc_info=True)
        await update.message.reply_text("Произошла внутренняя ошибка сервера.")
# --- Конец /number ---

# --- Новая команда для предложений ---
async def init_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет информационное сообщение со ссылкой на бота для предложений.
    Доступно всем авторизованным судьям.
    """
    conn = context.bot_data['db_connection']
    
    # Проверяем, что команду вызывает авторизованный судья
    can_proceed, user_nick_name, is_admin = await perform_wa_check(conn, tg_user_id=update.effective_user.id, update=update)
    if not can_proceed:
        return

    logger.info(f"Пользователь {user_nick_name} вызвал команду /init.")

    # Формируем и отправляем статичное сообщение
    message_text = (
        "👀 Я смотрю, у тебя появилась идея, как можно улучшить наш функционал?\n\n"
        "Напиши боту свою идею, и я обязательно постараюсь воплотить её в жизнь! 💡\n\n"
        "✍️ **Бот для идей -> @court_init_bot**"
    )
    
    await update.message.reply_text(message_text, disable_web_page_preview=True)
# --- Конец ---

# --- Другие обработчики команд ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_html(
        rf"Привет, {user.mention_html()}! Используйте /auth для авторизации.",
    )

async def check_driver_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global selenium_driver
    if selenium_driver:
        try:
            current_url = selenium_driver.current_url
            await update.message.reply_text(f"Selenium WebDriver активен. Текущий URL: {current_url}")
        except Exception as e:
            await update.message.reply_text(f"Selenium WebDriver запущен, но возникла ошибка при доступе: {e}")
    else:
        await update.message.reply_text("Selenium WebDriver не активен.")

# --- Функции жизненного цикла приложения ---
async def post_application_init(application: Application) -> None:
    db_conn = setup_database()
    application.bot_data['db_connection'] = db_conn
    logger.info(f"Соединение с БД {DB_NAME} установлено и сохранено в bot_data.")
    setup_selenium_driver()

async def post_application_shutdown(application: Application) -> None:
    close_selenium_driver()
    db_conn = application.bot_data.get('db_connection')
    if db_conn:
        logger.info("Закрытие соединения с БД...")
        db_conn.close()
        logger.info("Соединение с БД закрыто.")

# --- Основная функция ---
def main() -> None:
    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN" or not TELEGRAM_BOT_TOKEN: 
        logger.error("КРИТИЧЕСКАЯ ОШИБКА: TELEGRAM_BOT_TOKEN не установлен!")
        return
    if not cipher_suite: 
        logger.error("КРИТИЧЕСКАЯ ОШИБКА: Fernet (шифрование) не инициализирован. Бот не может быть запущен.")
        return

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_application_init)
        .post_shutdown(post_application_shutdown) 
        .build()
    )
    
    auth_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("auth", auth_start)],
        states={
            ASK_NICKNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_nickname)],
            ASK_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_password)],
        },
        fallbacks=[CommandHandler("cancel", auth_cancel)],
    )

    custom_reply_conv_handler = ConversationHandler(
        entry_points=[
            # entry_point здесь - это наш обработчик кнопок, отфильтрованный по паттерну
            CallbackQueryHandler(button_callback_router, pattern="^custom_reply:.*")
        ],
        states={
            AWAITING_CUSTOM_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, received_custom_reply)],
        },
        fallbacks=[CommandHandler("cancel", cancel_custom_reply)],
        # Позволяет другим хендлерам (например, /list) работать, пока бот в диалоге
        per_message=False 
    )

    regular_button_handler = CallbackQueryHandler(button_callback_router, pattern="^(?!custom_reply:.*)")

    # Регистрируем все обработчики
    application.add_handler(auth_conv_handler)
    application.add_handler(custom_reply_conv_handler) #  новый диалог
    application.add_handler(regular_button_handler) #  обработчик для остальных кнопок

    # Все остальные обработчики команд
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check_driver", check_driver_status_command))
    application.add_handler(CommandHandler("list", list_cases_command))
    application.add_handler(CommandHandler("select", select_case_command))
    application.add_handler(CommandHandler("details", details_case_command))
    application.add_handler(CommandHandler("rejectcase", reject_case_command))
    application.add_handler(CommandHandler("flist", flist_command))
    application.add_handler(CommandHandler("close", close_case_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addj", add_judge_command))
    application.add_handler(CommandHandler("removej", remove_judge_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(CommandHandler("aset", set_admin_command))
    application.add_handler(CommandHandler("adm", admin_modify_command))
    application.add_handler(CommandHandler("number", number_command))
    application.add_handler(CommandHandler("caselog", case_log_command))
    application.add_handler(CommandHandler("init", init_command))

    # Добавление тестовых команд
    application.add_handler(CommandHandler("testlog", test_login_command))
    application.add_handler(CommandHandler("testlogout", test_logout_command))
    application.add_handler(CommandHandler("aperftest", test_answer_perform_command))
    application.add_handler(CommandHandler("testpin", test_pin_command))
    application.add_handler(CommandHandler("testclose", test_close_command))

    logger.info("Запуск бота...")
    application.run_polling()
    logger.info("Бот остановлен.")

if __name__ == "__main__":
    main()