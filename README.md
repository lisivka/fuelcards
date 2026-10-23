# Рух пального (Django + SQLite)

## Запуск
```
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # заповніть НОВІ (перевипущені) ключі
python manage.py makemigrations fuel
python manage.py migrate
python manage.py createsuperuser
python manage.py sync_fuel --days 2     # або --provider upg
python manage.py runserver
```
Відкрийте http://127.0.0.1:8000 . Керування картками й користувачами: /admin/ .
Автосинхронізація: Планувальник Windows / cron раз на 10-15 хв:
`python manage.py sync_fuel --days 2`
