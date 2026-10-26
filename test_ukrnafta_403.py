import os
from pathlib import Path
from dotenv import load_dotenv
import requests
import base64, json

# Завантажуємо .env
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')

BASE = "https://naftacards.ukrnafta.com/public-api"
token = os.environ.get('UKRNAFTA_TOKEN')
print(f"{token[:10]=}")
card_num = os.environ.get('CARD_NUMBER') #cartnumber for test, for examle = 7921000902444

headers = {
    "Accept": "application/json",
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

# Спробуємо передати структуру повнішою, якщо простий статус відхиляється
payload = {
    "status": 1
}

url = f"{BASE}/cards/{card_num}/update"
print(f"Надсилаємо POST на {url}...")

resp = requests.post(url, params={"lang": "uk"}, json=payload, headers=headers, timeout=60)

print(f"Статус відповіді: {resp.status_code}")
print(f"Тіло відповіді: {resp.text}")

p = token.split(".")[1]
p += "=" * (-len(p) % 4)
claims = json.loads(base64.urlsafe_b64decode(p))
print({k.split("/")[-1]: v for k, v in claims.items()})
print(dict(resp.headers))