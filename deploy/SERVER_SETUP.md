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

## 8. Бекапи бази

Бекап робить `deploy/backup.sh` через SQLite backup API: копія узгоджена навіть під час роботи сервісу
(включно з даними з `db.sqlite3-wal`) і перевірена `PRAGMA integrity_check`. Простий `cp` для бази в режимі WAL ненадійний.

Копії лежать у `/opt/fuelcards/backups/<тип>/db_<тип>_<РРРР-ММ-ДД_ГГХХ>.sqlite3`:

| Тип | Коли | Як |
|---|---|---|
| `manual` | вручну, екстрено | `sudo bash /opt/fuelcards/deploy/backup.sh` |
| `deploy` | автоматично на початку `deploy.sh`; якщо бекап не вдався — деплой зупиняється | — |
| `auto` | щодня о 01:00 (таймер `fuelcards-backup.timer`); копія ще й на мережеву шару; видаляє копії всіх типів, старші за 15 днів | — |

Папку `backups/` і підпапки скрипт створює сам із власником `fuelcards` і правами 700.
Якщо її колись створено від root, виправ власника: `sudo chown -R fuelcards:fuelcards /opt/fuelcards/backups`.

### 8.1. Налаштування

```
sudo cp /opt/fuelcards/deploy/fuelcards-backup.default /etc/default/fuelcards-backup
sudo nano /etc/default/fuelcards-backup
sudo chmod 644 /etc/default/fuelcards-backup     # таймер читає файл від fuelcards
```

Тут `BACKUP_KEEP_DAYS` (локально, 15), `BACKUP_REMOTE_DIR` (тека на мережевій шарі; порожньо — не копіювати)
і `BACKUP_REMOTE_KEEP_DAYS` (15). Шлях змінюється тут, без зміни коду; перезапускати нічого не треба.
Секретів у цьому файлі немає. Якщо `fuelcards` не може його прочитати, щоденний бекап завершується помилкою
(ручний і деплойний — з попередженням і значеннями за замовчуванням).

### 8.2. Мережева шара `\\public.ua\Public` (анонімний доступ)

```
sudo apt install -y cifs-utils
getent hosts public.ua                 # ім'я має розв'язуватись; якщо ні — у fstab вкажи IP
sudo mkdir -p /mnt/public
```

Рядок у `/etc/fstab` (`sudo nano /etc/fstab`):

```
//public.ua/Public  /mnt/public  cifs  guest,uid=fuelcards,gid=fuelcards,file_mode=0600,dir_mode=0700,_netdev,nofail,x-systemd.automount  0  0
```

`nofail` — сервер завантажиться, навіть якщо шара недоступна; `x-systemd.automount` — шара підключається при першому зверненні.

```
sudo systemctl daemon-reload
ls /mnt/public                         # має показати вміст шари
sudo -u fuelcards mkdir -p /mnt/public/fuelcards
```

У `/etc/default/fuelcards-backup`: `BACKUP_REMOTE_DIR=/mnt/public/fuelcards`.

Якщо `ls /mnt/public` дає помилку: `sudo dmesg | tail`. Часто допомагає явна версія протоколу в опціях (`vers=3.0`, `vers=2.1`);
якщо гостьовий доступ на шарі вимкнено — потрібен обліковий запис від адміністратора (`credentials=/etc/fuelcards/smb-credentials`, файл з правами 600).

### 8.3. Таймер щоденного бекапу

```
sudo cp /opt/fuelcards/deploy/fuelcards-backup.service /opt/fuelcards/deploy/fuelcards-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fuelcards-backup.timer
systemd-analyze calendar "*-*-* 01:00:00 Europe/Kyiv"   # перевірити, що зона відома systemd
systemctl list-timers fuelcards-backup.timer       # наступний запуск — о 01:00
sudo systemctl start fuelcards-backup.service      # перевірити зараз
journalctl -u fuelcards-backup -n 20               # має бути «готово: …» і «скопійовано: …»
```

`deploy.sh` юніти не копіює: після змін у `fuelcards-backup.*` повтори дві перші команди.

Щоденний бекап завершується помилкою (`systemctl status fuelcards-backup` — `failed`), якщо шара недоступна
або ротація не вдалась; локальна копія при цьому вже зроблена. Причина — у `journalctl -u fuelcards-backup`.

## Оновлення

```
sudo bash /opt/fuelcards/deploy/deploy.sh
```

Бекап `deploy` робиться автоматично першим кроком. Виняток — перше оновлення, яке приносить `backup.sh`:
тоді ще працює старий `deploy.sh`, тому бекап зроби вручну перед ним (команда — у `/predeploy`).

Перед оновленням переконайся, що файл міграції для `CardStatus` закомічений і запушений у git.

## Відновлення з бекапу

```
sudo systemctl stop fuelcards
sudo bash /opt/fuelcards/deploy/backup.sh          # копія поточного стану — про всяк випадок
cd /opt/fuelcards
sudo ls -lt backups/deploy/ | head                 # вибери копію (найновіша — перша)
sudo -u fuelcards git checkout <попередній-коміт>   # лише якщо відкочується й код
sudo rm -f db.sqlite3-wal db.sqlite3-shm           # обов'язково: старий WAL зіпсує відновлену базу
sudo -u fuelcards cp backups/deploy/db_deploy_<дата>.sqlite3 db.sqlite3
sudo chmod 600 db.sqlite3
sudo systemctl start fuelcards
```

Відкат міграцій (`migrate fuel <попередня>`) з бекапом не потрібен: копія `deploy` зроблена до міграцій.
Після `git checkout` наступний `deploy.sh` не спрацює (`git pull` не на гілці) — спершу `sudo -u fuelcards git checkout master`.

## Якщо щось не так

- `400 Bad Request`: адреси, за якою заходите, немає в `ALLOWED_HOSTS` у `.env`.
- Сторінки без стилів: не виконано `collectstatic` або сервіс не перезапущено.
- Сервіс не стартує: `journalctl -u fuelcards -n 50`. Частий випадок: `DEBUG=0` без `DJANGO_SECRET_KEY`.
- `attempt to write a readonly database`: `db.sqlite3` або каталог `/opt/fuelcards` належать не `fuelcards`.