# Перше налаштування сервера (Ubuntu 22.04)

Застосунок живе в `/opt/fuelcards`, працює від користувача `fuelcards`, слухає `http://192.168.1.173:8000/`.
Odoo (порт 8069) і PostgreSQL не змінюються.

## 0. Перевірка порту

```
ss -tlnp | grep ':8000 ' || echo "порт 8000 вільний"
```

## 1. Пакети, користувач, код

```
sudo apt update && sudo apt install -y python3-venv git
sudo useradd --system --home-dir /var/lib/fuelcards --create-home --shell /usr/sbin/nologin fuelcards
sudo mkdir -p /opt/fuelcards && sudo chown fuelcards:fuelcards /opt/fuelcards
sudo -u fuelcards git clone https://github.com/lisivka/fuelcards.git /opt/fuelcards
```

## 2. Віртуальне середовище

```
sudo -u fuelcards python3 -m venv /opt/fuelcards/venv
sudo -u fuelcards /opt/fuelcards/venv/bin/pip install --upgrade pip
sudo -u fuelcards /opt/fuelcards/venv/bin/pip install -r /opt/fuelcards/requirements.txt
```

## 3. Файл .env

Згенеруй ключ:

```
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Створи файл і виставте права:

```
sudo -u fuelcards nano /opt/fuelcards/.env
sudo chmod 600 /opt/fuelcards/.env
```

Вміст:

```
DEBUG=0
DJANGO_SECRET_KEY=<згенерований ключ>
ALLOWED_HOSTS=192.168.1.173,localhost,127.0.0.1
LOG_LEVEL=INFO
UKRNAFTA_TOKEN=...
UKRNAFTA_HOLDER_ID=...
UKRNAFTA_ACCOUNT_ID=...
UPG_LOGIN=...
UPG_PASSWORD=...
# Змінні Petroline та решту скопіюй зі свого робочого .env
```

Токени й паролі не надсилай у чат і не коміть у git.

## 4. База даних

**Варіант А: перенести наявну базу** (зберігає Авто/ПІБ карток і історію).
Зупини локальний сервер, а потім на комп'ютері розробки:

```
scp db.sqlite3 <користувач>@192.168.1.173:/tmp/db.sqlite3
```

На сервері:

```
sudo mv /tmp/db.sqlite3 /opt/fuelcards/db.sqlite3
sudo chown fuelcards:fuelcards /opt/fuelcards/db.sqlite3
sudo chmod 600 /opt/fuelcards/db.sqlite3
```

**Варіант Б: нова база.** Після кроку 5 виконай `createsuperuser`, потім
`sync_cards` і `sync_fuel --days N`, але Авто/ПІБ для карток UPG і Petroline доведеться ввести заново.

## 5. Міграції та статика

```
cd /opt/fuelcards
sudo -u fuelcards venv/bin/python manage.py migrate
sudo -u fuelcards venv/bin/python manage.py collectstatic --noinput
sudo -u fuelcards venv/bin/python manage.py createsuperuser   # лише для нової бази
```

## 6. Сервіс systemd

```
sudo cp /opt/fuelcards/deploy/fuelcards.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuelcards
```

## 7. Перевірка

```
systemctl status fuelcards
curl -I http://127.0.0.1:8000/        # очікуємо 302 на сторінку входу
journalctl -u fuelcards -f            # журнал застосунку
```

У браузері: `http://192.168.1.173:8000/`.

## Оновлення

```
sudo bash /opt/fuelcards/deploy/deploy.sh
```

Перед оновленням переконайся, що файл міграції для `CardStatus` закомічений і запушений у git.

## Якщо щось не так

- `400 Bad Request`: адреси, за якою заходите, немає в `ALLOWED_HOSTS` у `.env`.
- Сторінки без стилів: не виконано `collectstatic` або сервіс не перезапущено.
- Сервіс не стартує: `journalctl -u fuelcards -n 50`. Частий випадок: `DEBUG=0` без `DJANGO_SECRET_KEY`.
- `attempt to write a readonly database`: `db.sqlite3` або каталог `/opt/fuelcards` належать не `fuelcards`.