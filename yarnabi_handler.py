import sys
import os  # Импортируем модуль для работы с операционной системой
import requests
import json
import time
import logging


# ------------------------ ОБНОВЛЕНИЕ -------------------------------


# --- Настройка логирования ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    # --- Установка токена ---
    api_token = "yrn1.eyvG2LZEmJzjpzH9g9rRhhM1VwbiGkUj1I6yX23seH81ZvjtSjW1wfcs8IGpBZDcfxl0Qa0iSv0lO8zeWmhcaNxdMykMOFDKFAWhROw0fF_NrClMLkQ5JwR-za7qDz4mqYk7DQ_flLI238sjugvmQws0KD0MlrptddX-A3EpXA45bWzNjBVfs85g_zdU6lCXcy7Vn3_lel"

    # формирование заголовка для всех запросов
    headers = {
        'Authorization': f'Bearer {api_token}'
    }
    logger.info("Заголовок авторизации сформирован.")

    # 1. проверка аргументы
    if len(sys.argv) < 6:
        print("Ошибка: Недостаточно аргументов.", file=sys.stderr)
        sys.exit(1)

    # аргументы
    topic_link = sys.argv[1]
    officer_name = sys.argv[2]
    current_judge = sys.argv[3]
    rebuttal_type = sys.argv[4]
    yarn_judge_value = sys.argv[5]
    logger.info("Аргументы успешно получены.")

    # --- Этап GET-запроса ---
    fraction_label = "Министерство Юстиции"
    get_url = "https://yrn-api.arzmesa.ru/method/info.fraction"
    get_payload = {'nickname': officer_name, 'server_id': 7}
    logger.info("Начинаем GET-запрос для получения фракции игрока.")

    try:
        #  headers 
        get_response = requests.get(get_url, params=get_payload, headers=headers, timeout=10)
        get_response.raise_for_status()
        logger.info("GET-запрос успешно выполнен.")
        
        get_data = get_response.json()
        logger.info("Ответ от GET-запроса получен и обработан.")    

        if get_data.get("success") and get_data.get("response", {}).get("data"):
            fraction_label = get_data["response"]["data"][0]["fraction_label"]
            logger.info(f"Фракция игрока '{officer_name}' успешно получена: {fraction_label}")
        else:
            error_message = get_data.get("response", {}).get("message", "API вернуло неуспешный ответ.")
            print(f"Предупреждение (GET): {error_message}. Используется значение по умолчанию: '{fraction_label}'", file=sys.stderr)

    except (requests.exceptions.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Предупреждение (GET): Ошибка при получении фракции ({e}). Используется значение по умолчанию: '{fraction_label}'", file=sys.stderr)

    # --- Этап POST-запроса ---
    post_url = "https://yrn-api.arzmesa.ru/method/lead.rebuttal"
    logger.info("Начинаем POST-запрос для отправки данных о возражении.")
    post_payload = {
        'forum_link': topic_link,
        'player_fraction': fraction_label,
        'player_nick': officer_name,
        'rebuttal_type': rebuttal_type,
        'user_id': yarn_judge_value
    }
    logger.info(f"Данные для POST-запроса: {post_payload}")
    
    max_attempts = 3
    retry_delays = [5, 10] 

    for attempt in range(max_attempts):
        try:
            if attempt > 0:
                delay = retry_delays[attempt - 1]
                print(f"Предупреждение (POST): Ошибка. Повторная попытка через {delay} секунд...", file=sys.stderr)
                time.sleep(delay)

            # headers 
            post_response = requests.post(post_url, json=post_payload, headers=headers, timeout=15)
            post_response.raise_for_status()
            logger.info("POST-запрос успешно выполнен.")

            post_data = post_response.json()
            status_code = post_response.status_code
            message = post_data.get("response", {}).get("message", "Сообщение не найдено в ответе.")
            logger.info(f"Ответ от POST-запроса: {post_data}")

            print(json.dumps({"status_code": status_code, "message": message}))
            sys.exit(0)

        except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
            if attempt == max_attempts - 1:
                print(f"Ошибка (POST): Не удалось выполнить запрос после {max_attempts} попыток. Последняя ошибка: {e}", file=sys.stderr)
                sys.exit(1)

if __name__ == "__main__":
    main()