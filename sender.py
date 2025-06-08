import time
import re
import sqlite3 
import json 
import os

from bs4 import BeautifulSoup 
from datetime import datetime 

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urljoin 

import subprocess

# ------------------ НОВЫЙ ------------------

# --- Настройки ---
FORUM_URL = "https://forum.arizona-rp.com/forums/3400/" 
REFRESH_INTERVAL_SECONDS = 5
DB_NAME = "forumnik_3_0.db" 
TABLE_NAME = "Cases_DB" 
IGNORED_MEDIA_URLS = {"https://i.imgur.com/jfsvriz.png"} 

# --- Селекторы CSS ---
TOPIC_CONTAINER_SELECTOR = "div.structItemContainer-group.js-threadList" 
INDIVIDUAL_THREAD_ITEM_SELECTOR = "div.structItem.structItem--thread.js-inlineModContainer" 
THREAD_TITLE_LINK_SELECTOR = "div.structItem-title > a" 
FIRST_POST_ARTICLE_SELECTOR = "article.message.message--post.js-post.js-inlineModContainer" 
MESSAGE_MAIN_CELL_SELECTOR = "div.message-cell.message-cell--main" 
POST_DATE_SELECTOR = "time.u-dt[datetime]" 
POST_TEXT_SELECTOR = "div.message-content.js-messageContent div.bbWrapper"

# --- Регулярные выражения ---
NAME_CAPTURE_PATTERN = r"(\w+(?:[ _]\w+)*)"
CASE_NUMBER_PATTERN = re.compile(r"(\d+)") 

GENERAL_APPLICANT_PATTERNS = [
    re.compile(r"^(?:[*\s]*|Форма подачи заявления:\s*\n\s*)?(?:Имя\s+фамилия|Ваш\s+игровой\s+ник(?:нейм)?|Ник(?:нейм)?\s*заявителя|Заявитель|Ник)\s*\(?(?:никнейм|на\s+английском(?:\s+языке)?)\)?\s*[:\-\s]+\s*" + NAME_CAPTURE_PATTERN, re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Мой ник-нейм\s*[:\-\=\s]*\s*" + NAME_CAPTURE_PATTERN, re.IGNORECASE | re.MULTILINE)
]
GENERAL_OFFICER_PATTERNS = [
    re.compile(r"^(?:[*\s]*)?(?:Сотрудник(?:,?\s*который\s+вас\s+задержал)?|Ник(?:нейм)?\s*(?:сотрудника|нарушителя|полицейского)|Ответчик|Офицер|Кто выдал наказание)\s*[:\-\s]+\s*" + NAME_CAPTURE_PATTERN, re.IGNORECASE | re.MULTILINE)
]
HEADER_APPLICANT_PATTERN = re.compile(r"В Окружной суд штата \w+\s+от Гражданина\s*" + NAME_CAPTURE_PATTERN, re.IGNORECASE)

# --- Функции для работы с базой данных SQLite ---
def setup_database(db_name, table_name):
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
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
    conn.commit()

    existing_columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()]
    columns_to_ensure = {
        'applicant_name': "TEXT", 'case_num': "TEXT", 'current_judge': "TEXT",
        'full_text': "TEXT", 'media_references': "TEXT", 'notes': "TEXT",
        'officer_name': "TEXT", 'publication_time': "TEXT", 
        'status': "TEXT NOT NULL DEFAULT 'a'", 'topic_link': "TEXT UNIQUE", 
        'topic_title': "TEXT"
    }
    for col_name, col_type in columns_to_ensure.items():
        if col_name not in existing_columns:
            actual_col_type = col_type.split(" UNIQUE")[0] if "UNIQUE" in col_type else col_type 
            try:
                cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {actual_col_type}")
                print(f"Добавлена колонка '{col_name}' в таблицу '{table_name}'.")
            except sqlite3.Error as e_alter:
                print(f"Ошибка при добавлении колонки {col_name} в {table_name}: {e_alter}")
    conn.commit()
    return conn

def load_processed_topics_from_db(conn, table_name):
    processed_links = set()
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT topic_link FROM {table_name}")
        rows = cursor.fetchall()
        for row in rows:
            processed_links.add(row[0])
        print(f"Загружено {len(processed_links)} ранее обработанных тем из БД ('{table_name}').")
    except sqlite3.Error as e:
        print(f"Ошибка при загрузке ссылок из БД ('{table_name}'): {e}")
    return processed_links

def insert_topic_data(conn, table_name, data_dict):
    """
    Вставляет данные о теме в БД. Теперь не включает скриншот.
    """
    sql = f"""
    INSERT INTO {table_name} (
        applicant_name, case_num, full_text, media_references, notes, 
        officer_name, publication_time, status, topic_link, topic_title
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    cursor = conn.cursor()
    try:
        cursor.execute(sql, (
            data_dict.get('applicant_name'), data_dict.get('case_num'), data_dict.get('full_text'),
            data_dict.get('media_references'), data_dict.get('notes'), data_dict.get('officer_name'),
            data_dict.get('publication_time'), data_dict.get('status', 'a'), data_dict.get('topic_link'),
            data_dict.get('topic_title')
        ))
        conn.commit()
        new_case_id = cursor.lastrowid
        print(f"  Данные по теме '{data_dict.get('topic_title')}' сохранены в БД под ID: {new_case_id}.")
        return new_case_id
    except sqlite3.IntegrityError:
        print(f"  Тема со ссылкой {data_dict.get('topic_link')} уже существует в БД. Пропускаем.")
        return None
    except sqlite3.Error as e:
        print(f"  Ошибка при вставке данных в БД для темы {data_dict.get('topic_link')}: {e}")
        return None

# --- Функции Selenium и парсинга ---
def setup_driver():
    chrome_options = Options()
    # chrome_options.add_argument("--headless") 
    chrome_options.add_argument("--no-sandbox") 
    chrome_options.add_argument("--disable-dev-shm-usage") 
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36")
    chrome_options.add_argument("--start-maximized") 
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10) 
    return driver

def parse_post_text_details(text_content):
    details = { "applicant": None, "officer": None }
    if not text_content: 
        return details

    applicant_found_by_priority = False
    officer_found_by_priority = False

    lines = text_content.splitlines() 
    for line in lines:
        stripped_line = line.strip()

        if not applicant_found_by_priority and stripped_line.startswith("1)"):
            if ":" in stripped_line:
                parts = stripped_line.split(":", 1)
                if len(parts) > 1:
                    details["applicant"] = parts[1].strip()
                    applicant_found_by_priority = True
            elif "никнейм)" in stripped_line.lower(): 
                keyword_pos = stripped_line.lower().find("никнейм)")
                if keyword_pos != -1:
                    details["applicant"] = stripped_line[keyword_pos + len("никнейм)"):].strip()
                    applicant_found_by_priority = True
        
        if not officer_found_by_priority and stripped_line.startswith("2)"):
            if ":" in stripped_line:
                parts = stripped_line.split(":", 1)
                if len(parts) > 1:
                    details["officer"] = parts[1].strip()
                    officer_found_by_priority = True
            elif "задержал" in stripped_line.lower(): 
                keyword_pos = stripped_line.lower().find("задержал")
                if keyword_pos != -1:
                    details["officer"] = stripped_line[keyword_pos + len("задержал"):].strip()
                    if details["officer"].startswith(":"):
                        details["officer"] = details["officer"][1:].strip()
                    officer_found_by_priority = True
        
        if applicant_found_by_priority and officer_found_by_priority:
            break
            
    if not applicant_found_by_priority:
        for pattern in GENERAL_APPLICANT_PATTERNS:
            match = pattern.search(text_content) 
            if match and match.group(match.lastindex):  # type: ignore
                extracted_value = match.group(match.lastindex).strip().replace('\n', ' ') # type: ignore
                if " - " in extracted_value: 
                    parts = extracted_value.split(" - ", 1)
                    if len(parts) > 1 and parts[1].strip():
                        potential_name = parts[1].strip()
                        if re.fullmatch(NAME_CAPTURE_PATTERN, potential_name):
                            extracted_value = potential_name
                details["applicant"] = extracted_value
                break 
        if not details["applicant"]: 
            match = HEADER_APPLICANT_PATTERN.search(text_content)
            if match and match.group(1): 
                extracted_value = match.group(1).strip().replace('\n', ' ')
                if " - " in extracted_value: 
                    parts = extracted_value.split(" - ", 1)
                    if len(parts) > 1 and parts[1].strip():
                        potential_name = parts[1].strip()
                        if re.fullmatch(NAME_CAPTURE_PATTERN, potential_name):
                            extracted_value = potential_name
                details["applicant"] = extracted_value # type: ignore

    if not officer_found_by_priority:
        for pattern in GENERAL_OFFICER_PATTERNS:
            match = pattern.search(text_content) 
            if match and match.group(match.lastindex):  # type: ignore
                extracted_value = match.group(match.lastindex).strip().replace('\n', ' ') # type: ignore
                if " - " in extracted_value: 
                    parts = extracted_value.split(" - ", 1)
                    if len(parts) > 1 and parts[1].strip():
                        potential_name = parts[1].strip()
                        if re.fullmatch(NAME_CAPTURE_PATTERN, potential_name):
                            extracted_value = potential_name
                details["officer"] = extracted_value
                break
    
    # --- ИЗМЕНЕНИЕ: Устанавливаем "Гражданин", если имена не найдены ---
    if not details["applicant"] or details["applicant"].isspace(): # Проверяем также на пустую строку или только пробелы
        details["applicant"] = "Гражданин" # type: ignore
    if not details["officer"] or details["officer"].isspace():
        details["officer"] = "Гражданин" # type: ignore
        
    return details

def extract_media_links_from_html(html_content, base_url):
    soup = BeautifulSoup(html_content, 'html.parser')
    links = set() 
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'] # type: ignore
        if href and href.strip().lower() not in IGNORED_MEDIA_URLS and '#' not in href:  # type: ignore
            if href.startswith('http://') or href.startswith('https://'): # type: ignore
                links.add(href.strip()) # type: ignore
            elif not href.startswith(('mailto:', 'tel:')):  # type: ignore
                try:
                    absolute_url = urljoin(base_url, href.strip()) # type: ignore
                    if absolute_url.lower() not in IGNORED_MEDIA_URLS:
                        links.add(absolute_url)
                except Exception as e_join:
                     print(f"    Предупреждение: не удалось обработать относительную ссылку: '{href}': {e_join}")
    for img_tag in soup.find_all('img'):
        src = img_tag.get('src') # type: ignore
        data_url = img_tag.get('data-url')  # type: ignore
        if src and src.strip().lower() not in IGNORED_MEDIA_URLS and (src.startswith('http://') or src.startswith('https://')): # type: ignore
            links.add(src.strip()) # type: ignore
        elif src and not src.startswith('data:'):  # type: ignore
             try:
                absolute_url = urljoin(base_url, src.strip()) # type: ignore
                if absolute_url.lower() not in IGNORED_MEDIA_URLS:
                    links.add(absolute_url)
             except Exception as e_join_img_src:
                print(f"    Предупреждение: не удалось обработать относительную src для img '{src}': {e_join_img_src}")
        if data_url and data_url.strip().lower() not in IGNORED_MEDIA_URLS and (data_url.startswith('http://') or data_url.startswith('https://')): # type: ignore
            links.add(data_url.strip()) # type: ignore
        elif data_url and not data_url.startswith('data:'): # type: ignore
             try:
                absolute_url = urljoin(base_url, data_url.strip()) # type: ignore
                if absolute_url.lower() not in IGNORED_MEDIA_URLS:
                    links.add(absolute_url)
             except Exception as e_join_img_data_url:
                print(f"    Предупреждение: не удалось обработать относительную data-url для img '{data_url}': {e_join_img_data_url}")
    for media_wrapper in soup.find_all('div', class_='bbMediaWrapper'):
        site_id = media_wrapper.get('data-media-site-id', '').lower() # type: ignore
        media_key = media_wrapper.get('data-media-key') # type: ignore
        link_to_add = None
        if site_id == 'imgur' and media_key:
            link_to_add = f"https://imgur.com/{media_key.lstrip('/')}" # type: ignore
        elif site_id == 'youtube' and media_key:
            link_to_add = f"https://www.youtube.com/watch?v={media_key}"
        if link_to_add and link_to_add.lower() not in IGNORED_MEDIA_URLS:
            links.add(link_to_add)
    for image_wrapper in soup.find_all('div', class_='bbImageWrapper'):
        data_src = image_wrapper.get('data-src') # type: ignore
        if data_src and data_src.strip().lower() not in IGNORED_MEDIA_URLS and (data_src.startswith('http://') or data_src.startswith('https://')): # type: ignore
            links.add(data_src.strip()) # type: ignore
        elif data_src and not data_src.startswith('data:'):  # type: ignore
             try:
                absolute_url = urljoin(base_url, data_src.strip()) # type: ignore
                if absolute_url.lower() not in IGNORED_MEDIA_URLS:
                    links.add(absolute_url)
             except Exception as e_join_img_wrap:
                print(f"    Предупреждение: не удалось обработать относительную data-src для bbImageWrapper '{data_src}': {e_join_img_wrap}")
    return sorted(list(links)) 

def get_topic_details(driver, topic_url, base_url_for_links):
    print(f"  Перехожу на страницу темы: {topic_url}")
    driver.get(topic_url)
    screenshot_path = None
    publication_date_str = None 
    plain_text_content = None 
    parsed_text_details = None
    all_media_links = [] 
    try:
        first_post_article_element = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, FIRST_POST_ARTICLE_SELECTOR))
        )
        try:
            # Создаем папку, если ее нет
            screenshots_dir = 'screenshots'
            os.makedirs(screenshots_dir, exist_ok=True)
            
            # Временно сохраняем скриншот, имя файла будет уточнено позже
            temp_screenshot_path = os.path.join(screenshots_dir, 'temp_case.png')
            if first_post_article_element.screenshot(temp_screenshot_path):
                screenshot_path = temp_screenshot_path # Сохраняем временный путь
                print(f"  Скриншот первого поста сохранен во временный файл: {screenshot_path}")
        except Exception as e_screen:
            print(f"    Предупреждение: Не удалось сделать скриншот поста. Ошибка: {e_screen}")
        main_cell_element = first_post_article_element.find_element(By.CSS_SELECTOR, MESSAGE_MAIN_CELL_SELECTOR)
        try:
            date_element = main_cell_element.find_element(By.CSS_SELECTOR, POST_DATE_SELECTOR)
            raw_date_iso = date_element.get_attribute("datetime")
            if raw_date_iso:
                try:
                    if ":" == raw_date_iso[-3:-2]: 
                        raw_date_iso = raw_date_iso[:-3] + raw_date_iso[-2:]
                    dt_object = datetime.fromisoformat(raw_date_iso)
                    publication_date_str = dt_object.strftime("%H:%M - %d.%m.%Y")
                except ValueError as e_date_parse:
                    print(f"    Предупреждение: Не удалось распарсить ISO дату '{raw_date_iso}': {e_date_parse}. Использую текстовое значение.")
                    publication_date_str = date_element.text 
            else:
                publication_date_str = date_element.text 
        except Exception as e_date_find:
            print(f"    Предупреждение: Не удалось найти/обработать дату публикации для темы {topic_url}: {e_date_find}")
        try:
            text_element_container = main_cell_element.find_element(By.CSS_SELECTOR, POST_TEXT_SELECTOR)
            post_html_content = text_element_container.get_attribute('innerHTML')
            if post_html_content:
                soup_for_text = BeautifulSoup(post_html_content, 'html.parser')
                plain_text_content = soup_for_text.get_text(separator='\n', strip=True) 
                if plain_text_content: 
                    parsed_text_details = parse_post_text_details(plain_text_content) 
                all_media_links = extract_media_links_from_html(post_html_content, base_url_for_links)
        except Exception as e_text_html:
            print(f"    Предупреждение: Не удалось обработать текст/HTML публикации для темы {topic_url}: {e_text_html}")
        return publication_date_str, plain_text_content, parsed_text_details, all_media_links, screenshot_path

    except Exception as e:
        print(f"  Ошибка при сборе деталей темы {topic_url} (общая): {e}")
        # Возвращаем 5 значений, включая None для скриншота
        return None, None, None, [], None 

# --- Основной скрипт ---
if __name__ == "__main__":
    db_connection = setup_database(DB_NAME, TABLE_NAME) 
    selenium_driver = setup_driver()
    try:
        print(f"Запускаю мониторинг форума: {FORUM_URL}")
        print(f"Страница будет обновляться каждые {REFRESH_INTERVAL_SECONDS} секунд.")
        print("Для остановки скрипта нажмите Ctrl+C в консоли.")
        
        is_first_run = True 
        while True: 
            processed_topic_links_set = load_processed_topics_from_db(db_connection, TABLE_NAME) 
            
            # Обновление или переход на страницу
            current_page_url_main_loop = selenium_driver.current_url 
            if FORUM_URL not in current_page_url_main_loop:
                print(f"Обнаружено, что текущий URL ({current_page_url_main_loop}) не совпадает с целевым. Возвращаюсь на {FORUM_URL}")
                selenium_driver.get(FORUM_URL)
            elif not is_first_run: 
                print(f"\nОбновляю страницу: {FORUM_URL}")
                try:
                    selenium_driver.refresh()
                except Exception as e_refresh: 
                    print(f"Ошибка при обновлении страницы: {e_refresh}. Попробую перезагрузить страницу полностью.")
                    selenium_driver.get(FORUM_URL) 
            else: 
                print("Страница форума загружена.")
                is_first_run = False 

            # Ожидание загрузки контейнера с темами
            try:
                topic_container_element = WebDriverWait(selenium_driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, TOPIC_CONTAINER_SELECTOR))
                )
            except Exception as e: 
                print(f"Контейнер тем ({TOPIC_CONTAINER_SELECTOR}) не найден на странице. Ошибка: {e}")
                time.sleep(REFRESH_INTERVAL_SECONDS)
                continue 
            
            thread_item_elements = topic_container_element.find_elements(By.CSS_SELECTOR, INDIVIDUAL_THREAD_ITEM_SELECTOR)
            if not thread_item_elements: 
                print("Темы на странице не найдены внутри контейнера.")
                time.sleep(REFRESH_INTERVAL_SECONDS)
                continue
            
            new_topic_processed_in_this_while_iteration = False 
            for thread_element in thread_item_elements: 
                topic_title_text = "Неизвестная тема"
                try:
                    if 'is-locked' in thread_element.get_attribute("class").split():
                        continue 
                    
                    title_link_element = thread_element.find_element(By.CSS_SELECTOR, THREAD_TITLE_LINK_SELECTOR)
                    topic_title_text = title_link_element.text.strip() 
                    relative_topic_link_url = title_link_element.get_attribute("href")
                    if not relative_topic_link_url: continue

                    full_topic_link_url = urljoin(selenium_driver.current_url, relative_topic_link_url)
                    
                    if full_topic_link_url not in processed_topic_links_set:
                        print(f"\n--- Найдена новая тема для обработки: {topic_title_text} ---")
                        
                        # Получаем все детали, включая ВРЕМЕННЫЙ путь к скриншоту
                        pub_date, plain_text, parsed_details, media_links, temp_screenshot_file = get_topic_details(selenium_driver, full_topic_link_url, full_topic_link_url)
                        
                        case_num_match = CASE_NUMBER_PATTERN.search(topic_title_text)
                        extracted_case_number = case_num_match.group(1) if case_num_match else None

                        data_to_save = {
                            'topic_title': topic_title_text, 'topic_link': full_topic_link_url,
                            'publication_time': pub_date, 'full_text': plain_text, 
                            'case_num': extracted_case_number,
                            'applicant_name': parsed_details.get('applicant'),
                            'officer_name': parsed_details.get('officer'),
                            'media_references': json.dumps(media_links) if media_links else None
                        }
                        
                        # Вставляем данные и получаем ID
                        new_id = insert_topic_data(db_connection, TABLE_NAME, data_to_save)
                        
                        if new_id:
                            # Обрабатываем скриншот, если он был создан
                            if temp_screenshot_file and os.path.exists(temp_screenshot_file):
                                final_screenshot_path = os.path.join('screenshots', f'case_{new_id}.png')
                                os.rename(temp_screenshot_file, final_screenshot_path)
                                print(f"  Скриншот переименован в: {final_screenshot_path}")
                                
                                cursor = db_connection.cursor()
                                cursor.execute(f"UPDATE {TABLE_NAME} SET screen = ? WHERE id = ?", (final_screenshot_path, new_id))
                                db_connection.commit()
                                print("  Путь к скриншоту сохранен в БД.")

                            # Запускаем уведомитель
                            print(f"  -> Запускаю уведомитель для иска #{new_id}...")
                            subprocess.run(['python', 'notifier.py', topic_title_text, str(new_id)])
                        
                        print(f"  Возвращаюсь на список тем...")
                        selenium_driver.get(FORUM_URL)
                        new_topic_processed_in_this_while_iteration = True
                        break
                
                except Exception as e_thread_loop: 
                    print(f"Ошибка при обработке элемента темы '{topic_title_text}': {e_thread_loop}")
                    continue 

            if not new_topic_processed_in_this_while_iteration: 
                 print("Новых тем для обработки в этом цикле не найдено.")

            print(f"Следующая проверка через {REFRESH_INTERVAL_SECONDS} секунд...")
            time.sleep(REFRESH_INTERVAL_SECONDS) 

    except KeyboardInterrupt: 
        print("\nСкрипт остановлен пользователем.")
    finally: 
        if 'selenium_driver' in locals() and selenium_driver is not None:
            selenium_driver.quit() 
        if 'db_connection' in locals() and db_connection is not None:
            db_connection.close() 
        print("Скрипт завершил работу.")
