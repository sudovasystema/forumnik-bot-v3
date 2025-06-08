import time
import re
import sqlite3 
import json 
import os
import subprocess
from bs4 import BeautifulSoup 
from datetime import datetime 
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin 
import html

# --- Настройки ---
FORUM_URL = "https://forum.arizona-rp.com/forums/3400/" 
REFRESH_INTERVAL_SECONDS = 5
DB_NAME = "forumnik_3_0.db" 
TABLE_NAME = "Cases_DB" 
IGNORED_MEDIA_URLS = {"https://i.imgur.com/jfsvriz.png"}
CHECK_REPLIES_EVERY_N_CYCLES = 4

# --- Селекторы CSS ---
TOPIC_CONTAINER_SELECTOR = "div.structItemContainer-group.js-threadList" 
INDIVIDUAL_THREAD_ITEM_SELECTOR = "div.structItem.structItem--thread.js-inlineModContainer" 
THREAD_TITLE_LINK_SELECTOR = "div.structItem-title > a" 
FIRST_POST_ARTICLE_SELECTOR = "article.message.message--post.js-post.js-inlineModContainer" 
MESSAGE_MAIN_CELL_SELECTOR = "div.message-cell.message-cell--main" 
POST_DATE_SELECTOR = "time.u-dt[datetime]" 
POST_TEXT_SELECTOR = "div.message-content.js-messageContent div.bbWrapper"

# --- Регулярные выражения ---
# Ищет любую группу цифр в строке
CASE_NUMBER_PATTERN = re.compile(r"(\d+)") 

# --- Функции для работы с базой данных SQLite ---
def setup_database(db_name, table_name):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT, applicant_name TEXT, case_num TEXT,                       
        current_judge TEXT, full_text TEXT, media_references TEXT, notes TEXT,                          
        officer_name TEXT, publication_time TEXT, scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        status TEXT NOT NULL DEFAULT 'a', topic_link TEXT UNIQUE, topic_title TEXT,
        screen TEXT, answers TEXT, post_count INTEGER DEFAULT 1
    )""")
    # Проверка и добавление колонок
    existing_columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()]
    columns_to_add = {'screen': 'TEXT', 'answers': 'TEXT', 'post_count': 'INTEGER DEFAULT 1'}
    for col, col_type in columns_to_add.items():
        if col not in existing_columns:
            try:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col} {col_type}")
                print(f"Добавлена колонка '{col}' в таблицу '{table_name}'.")
            except sqlite3.Error as e: print(f"Ошибка при добавлении колонки {col}: {e}")
    conn.commit()
    return conn

def load_processed_topics_from_db(conn, table_name):
    processed_links = set()
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT topic_link FROM {table_name}")
        processed_links.update(row[0] for row in cursor.fetchall())
    except sqlite3.Error as e: print(f"Ошибка при загрузке ссылок из БД: {e}")
    return processed_links

def insert_topic_data(conn, table_name, data_dict):
    sql = f"INSERT INTO {table_name} (applicant_name, case_num, full_text, media_references, notes, officer_name, publication_time, status, topic_link, topic_title) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    cursor = conn.cursor()
    try:
        initial_note = f"[{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}] Иск добавлен в систему скрапером."
        cursor.execute(sql, (
            data_dict.get('applicant_name'), data_dict.get('case_num'), data_dict.get('full_text'),
            data_dict.get('media_references'), initial_note, data_dict.get('officer_name'),
            data_dict.get('publication_time'), 'a', data_dict.get('topic_link'),
            data_dict.get('topic_title')
        ))
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError: return None
    except sqlite3.Error as e:
        print(f"  Ошибка при вставке данных в БД: {e}")
        return None

def get_judge_tg_id(conn, judge_nick_name):
    if not judge_nick_name: return None
    cursor = conn.cursor()
    cursor.execute(f"SELECT tg_user_id FROM Users_DB WHERE nick_name = ?", (judge_nick_name,))
    result = cursor.fetchone()
    return result[0] if result else None

# --- НОВАЯ, ПОЛНОСТЬЮ ПЕРЕПИСАННАЯ ФУНКЦИЯ ПАРСИНГА ---
def parse_post_text_details(text_content):
    print("  -- Начало парсинга деталей иска по вашим правилам --")
    details = {"applicant": None, "officer": None}
    if not text_content: 
        print("  Парсинг отменен: текст пустой.")
        return details

    lines = text_content.splitlines()
    for line in lines:
        stripped_line = line.strip()
        
        # Правило для заявителя
        if stripped_line.startswith("1)"):
            if "):" in stripped_line:
                details["applicant"] = stripped_line.split("):", 1)[1].strip()
            else:
                details["applicant"] = stripped_line.split("1)", 1)[1].strip()
            print(f"  Найден заявитель: '{details['applicant']}'")

        # Правило для ответчика
        if stripped_line.startswith("2)"):
            if "ал:" in stripped_line:
                details["officer"] = stripped_line.split("ал:", 1)[1].strip()
            else:
                details["officer"] = stripped_line.split("2)", 1)[1].strip()
            print(f"  Найден ответчик: '{details['officer']}'")
    
    if not details["applicant"]: print("  ПРЕДУПРЕЖДЕНИЕ: Имя заявителя не найдено.")
    if not details["officer"]: print("  ПРЕДУПРЕЖДЕНИЕ: Имя ответчика не найдено.")

    print("  -- Конец парсинга деталей иска --")
    return details


# --- Остальные функции (setup_driver, extract_media_links_from_html, и т.д.) ---
def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36")
    chrome_options.add_argument("--start-maximized")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10) 
    return driver

def extract_media_links_from_html(html_content, base_url):
    soup = BeautifulSoup(html_content, 'html.parser')
    links = set()
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        if href and href.strip().lower() not in IGNORED_MEDIA_URLS and '#' not in href:
            if href.startswith('http://') or href.startswith('https://'):
                links.add(href.strip())
            elif not href.startswith(('mailto:', 'tel:')):
                try: links.add(urljoin(base_url, href.strip()))
                except Exception: pass
    return sorted(list(links))

def get_topic_details(driver, topic_url, base_url_for_links):
    print(f"  Перехожу на страницу темы: {topic_url}")
    driver.get(topic_url)
    screenshot_path, pub_date, plain_text, parsed_details, media_links = None, None, None, {}, []
    try:
        first_post = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, FIRST_POST_ARTICLE_SELECTOR)))
        try:
            screenshots_dir = 'screenshots'
            os.makedirs(screenshots_dir, exist_ok=True)
            temp_screenshot_path = os.path.join(screenshots_dir, 'temp_case_screenshot.png')
            if first_post.screenshot(temp_screenshot_path):
                screenshot_path = temp_screenshot_path
                print(f"  Скриншот первого поста сохранен во временный файл.")
        except Exception as e: print(f"    Предупреждение: Не удалось сделать скриншот: {e}")
        main_cell = first_post.find_element(By.CSS_SELECTOR, MESSAGE_MAIN_CELL_SELECTOR)
        pub_date = main_cell.find_element(By.CSS_SELECTOR, POST_DATE_SELECTOR).get_attribute("title")
        text_container = main_cell.find_element(By.CSS_SELECTOR, POST_TEXT_SELECTOR)
        html_content = text_container.get_attribute('innerHTML')
        plain_text = BeautifulSoup(html_content, 'html.parser').get_text(separator='\n', strip=True)
        parsed_details = parse_post_text_details(plain_text)
        media_links = extract_media_links_from_html(html_content, base_url_for_links)
        return pub_date, plain_text, parsed_details, media_links, screenshot_path
    except Exception as e:
        print(f"  Ошибка при сборе деталей темы {topic_url}: {e}")
        return None, None, {}, [], None

def scrape_thread_answers(driver, topic_url):
    print(f"  Собираю все ответы из темы: {topic_url}")
    driver.get(topic_url)
    all_answers_text, page_post_count = "", 0
    try:
        post_elements = WebDriverWait(driver, 15).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, "article.message")))
        page_post_count = len(post_elements)
        for i, post in enumerate(post_elements[1:], start=1):
            try:
                author = post.find_element(By.CSS_SELECTOR, "h4.message-name").text.strip()
                post_text_element = post.find_element(By.CSS_SELECTOR, "div.bbWrapper")
                post_text = post_text_element.text.strip()
                links = [a.get_attribute('href') for a in post_text_element.find_elements(By.TAG_NAME, 'a')]
                answer_block = f"Ответ {i} от {author}: =============================\n{post_text}\n"
                if links: answer_block += "(Ссылки: " + ", ".join(filter(None, links)) + ")\n"
                answer_block += f"Конец ответа {i}: ========================\n\n"
                all_answers_text += answer_block
            except Exception as e_post: print(f"    - Ошибка при парсинге поста #{i+1}: {e_post}")
        return all_answers_text.strip(), page_post_count
    except Exception as e:
        print(f"  Ошибка при сборе ответов из темы {topic_url}: {e}")
        return "", page_post_count

# --- Основной скрипт ---
if __name__ == "__main__":
    db_connection = setup_database(DB_NAME, TABLE_NAME) 
    selenium_driver = setup_driver()
    cycle_counter = 0
    try:
        print(f"Запускаю мониторинг форума...")
        while True: 
            cycle_counter += 1
            if cycle_counter > 1 and cycle_counter % CHECK_REPLIES_EVERY_N_CYCLES == 0:
                print("\n--- [Цикл проверки ответов] ---")
                cursor = db_connection.cursor()
                cursor.execute(f"SELECT id, topic_link, post_count, current_judge, topic_title FROM {TABLE_NAME} WHERE status = 'f'")
                cases_to_check = cursor.fetchall()
                print(f"Найдено {len(cases_to_check)} исков в статусе 'f' для проверки.")
                for case_id, topic_link, db_post_count, current_judge, topic_title in cases_to_check:
                    print(f"Проверяю иск #{case_id}...")
                    answers_text, page_post_count = scrape_thread_answers(selenium_driver, topic_link)
                    if page_post_count > (db_post_count or 0):
                        print(f"  ! ОБНАРУЖЕН НОВЫЙ ОТВЕТ в иске #{case_id} ({page_post_count} > {db_post_count})")
                        cursor.execute(f"UPDATE {TABLE_NAME} SET answers = ?, post_count = ? WHERE id = ?", (answers_text, page_post_count, case_id))
                        db_connection.commit()
                        judge_tg_id = get_judge_tg_id(db_connection, current_judge)
                        if judge_tg_id:
                            print(f"  -> Запускаю уведомитель для судьи {current_judge} по иску #{case_id}...")
                            subprocess.run(['python', 'notifier.py', 'new_reply', topic_title, str(case_id), str(judge_tg_id)])
                print("--- [Цикл проверки ответов завершен] ---")

            print("\n--- [Цикл проверки новых исков] ---")
            processed_links = load_processed_topics_from_db(db_connection, TABLE_NAME)
            selenium_driver.get(FORUM_URL)
            topic_container = WebDriverWait(selenium_driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, TOPIC_CONTAINER_SELECTOR)))
            
            all_topics_on_page = []
            thread_items = topic_container.find_elements(By.CSS_SELECTOR, INDIVIDUAL_THREAD_ITEM_SELECTOR)
            for item in thread_items:
                try:
                    if 'is-locked' in item.get_attribute("class"): continue
                    link_element = item.find_element(By.CSS_SELECTOR, THREAD_TITLE_LINK_SELECTOR)
                    title = link_element.text.strip()
                    url = urljoin(FORUM_URL, link_element.get_attribute("href"))
                    if url: all_topics_on_page.append({'title': title, 'url': url})
                except StaleElementReferenceException:
                    print("  Предупреждение: StaleElementReferenceException, страница обновилась. Начинаю заново.")
                    break
            
            new_topic_found_this_cycle = False
            for topic_data in all_topics_on_page:
                title = topic_data['title']
                url = topic_data['url']
                if url not in processed_links:
                    print(f"\n  Найдена новая тема: {title}")
                    pub_date, plain_text, parsed_details, media_links, temp_screenshot = get_topic_details(selenium_driver, url, url)
                    
                    if parsed_details is None: parsed_details = {}
                    
                    case_num_match = CASE_NUMBER_PATTERN.search(title)
                    extracted_case_number = case_num_match.group(1) if case_num_match else None
                    if not extracted_case_number: print(f"    ПРЕДУПРЕЖДЕНИЕ: Не удалось извлечь номер иска из заголовка '{title}'.")

                    data_to_save = {
                        'topic_title': title, 'topic_link': url, 'publication_time': pub_date, 
                        'full_text': plain_text, 'case_num': extracted_case_number,
                        'applicant_name': parsed_details.get('applicant'), 'officer_name': parsed_details.get('officer'),
                        'media_references': json.dumps(media_links) if media_links else None
                    }
                    new_id = insert_topic_data(db_connection, TABLE_NAME, data_to_save)
                    if new_id:
                        if temp_screenshot and os.path.exists(temp_screenshot):
                            final_screenshot_path = os.path.join('screenshots', f'case_{new_id}.png')
                            os.rename(temp_screenshot, final_screenshot_path)
                            cursor = db_connection.cursor()
                            cursor.execute(f"UPDATE {TABLE_NAME} SET screen = ? WHERE id = ?", (final_screenshot_path, new_id))
                            db_connection.commit()
                        
                        subprocess.run(['python', 'notifier.py', 'new_case', title, str(new_id)])
                    
                    new_topic_found_this_cycle = True
                    break
            
            if not new_topic_found_this_cycle: print("Новых тем не найдено.")
            print(f"Следующая проверка через {REFRESH_INTERVAL_SECONDS} секунд...")
            time.sleep(REFRESH_INTERVAL_SECONDS)
    except KeyboardInterrupt: 
        print("\nСкрипт остановлен пользователем.")
    finally: 
        if 'selenium_driver' in locals() and selenium_driver: selenium_driver.quit() 
        if 'db_connection' in locals() and db_connection: db_connection.close() 
        print("Скрипт завершил работу.")
