import os
import requests
from pathlib import Path
from dotenv import load_dotenv
import os

# Знаходимо шлях до папки, де лежить поточний Python-файл
BASE_DIR = Path(__file__).resolve().parent

# Явно завантажуємо .env із цієї ж папки
load_dotenv(BASE_DIR / '.env')

# Тепер токен успішно прочитається
TOKEN = os.environ.get('UKRNAFTA_TOKEN')
print("Токен знайдено:", bool(TOKEN))

# Базова адреса публічного API Укрнафти
BASE = "https://naftacards.ukrnafta.com/public-api"



def get_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {os.environ['UKRNAFTA_TOKEN']}",
    }


def check_response(resp):
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("errorCode") not in (0, None):
        raise RuntimeError(f"Помилка API {data.get('errorCode')}: {data.get('errorDescription')}")
    return data


def set_card_status(card_number: str, status_id: int):
    """
    Змінює статус картки через API Укрнафти.
    status_id: 0 - Активна, 1 - Заблокована, 2 - Неактивна, 3 - Замінена
    """
    url = f"{BASE}/cards/{card_number}/update"

    # Відповідно до специфікації API, передаємо статус у тілі запиту (JSON)
    payload = {
        "status": status_id
    }

    print(f"Надсилаємо запит для карти {card_number} із новим статусом: {status_id}...")
    resp = requests.post(
        url,
        params={"lang": "uk"},
        json=payload,
        headers=get_headers(),
        timeout=60
    )
    print(f"Запит {resp}")
    print(f"Отримано відповідь від сервера: HTTP {resp.status_code}")

    result = check_response(resp)
    print(f"Успішно! Відповідь сервера: {result}")


if __name__ == "__main__":
    card_num = "7921000902444000"
    set_card_status(card_num, status_id=1)

    try:
        # Крок 1: Блокуємо картку (Статус 1 = Заблокована)
        print("--- КРОК 1: Блокування картки ---")
        set_card_status(card_num, status_id=1)

        # Можете за бажанням додати паузу, наприклад: time.sleep(3)

        # # Крок 2: Розблоковуємо картку (Статус 0 = Активна)
        # print("\n--- КРОК 2: Розблокування картки ---")
        # set_card_status(card_num, status_id=0)

        print("\nОперації блокування та розблокування пройшли успішно!")

    except Exception as e:
        print(f"\nСталася помилка під час виконання запиту: {e}")