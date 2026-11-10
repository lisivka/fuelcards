# Рух пального (Django + SQLite)

Облік транзакцій і карток пального трьох провайдерів (Petroline, UPG, UkrNafta).
Опис домену й моделі даних — [`PROJECT.md`](PROJECT.md), задачі — [`docs/TASKS.md`](docs/TASKS.md),
правила роботи — [`docs/WORKFLOW.md`](docs/WORKFLOW.md), сервер — [`deploy/SERVER_SETUP.md`](deploy/SERVER_SETUP.md).

## Перший запуск (Windows)
```
python -m venv .venv
.venv\Scripts\activate                      # Linux: source .venv/bin/activate
pip install -r requirements-dev.txt         # прод: requirements.txt
copy .env_example .env                      # заповнити ключі провайдерів
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver                  # http://127.0.0.1:8000 , адмінка: /admin/
```

## Синхронізація транзакцій — `sync_fuel`
Ходить у **реальні** API провайдерів. Повторне завантаження оновлює записи (зокрема скасування), а не дублює.
Кожен запуск пишеться в журнал аудиту (сторінка «Логи», «система (команда)»).

| Команда | Що робить |
|---|---|
| `python manage.py sync_fuel` | усі провайдери, типово `--days 2` |
| `python manage.py sync_fuel --days 7` | усі провайдери, з (сьогодні − 7 днів) по сьогодні |
| `python manage.py sync_fuel --days 90 --provider upg --provider ukrnafta` | вибрані провайдери (`--provider` можна повторювати: `petroline`, `upg`, `ukrnafta`) |
| `python manage.py sync_fuel --config-days` | кожен провайдер на свою «Глибину синхронізації, днів» з адмінки («Конфігурації провайдерів») |
| `python manage.py sync_fuel --config-period --provider petroline` | період «Синхронізувати з / по» з адмінки |
| `python manage.py sync_fuel --help` | довідка з прикладами |

- `--days`, `--config-days`, `--config-period` — взаємовиключні; `--days` від 1 до 366.
- Період довший за 30 днів вантажиться частинами по 30 днів з паузою 2 с. Помилка частини зупиняє цього провайдера, інші продовжують.
- Неактивні провайдери (прапорець «Активний» в адмінці) пропускаються.

Те саме без консолі:
- кнопка **«Синхронізувати»** на «Транзакціях» — кожен провайдер від дня своєї останньої транзакції до сьогодні, але не більше 31 дня;
- адмінка → «Конфігурації провайдерів» → вибрати провайдерів → дія «Синхронізувати вибраних за N днів» або «… за період з/по».

## Оновлення карток — `sync_cards`
Також ходить у реальні API (лише читання карток; у провайдера нічого не змінює).
```
python manage.py sync_cards                         # UkrNafta: картки й статуси
python manage.py sync_cards --provider petroline    # Petroline: картки, прив'язка до водія, ліміти водія
```
На сторінці «Карти» те саме роблять кнопки «Оновити статуси UkrNafta», «Оновити карти Petroline» і окремо «Оновити ліміти UkrNafta».

## Розробка
```
python manage.py check                              # перевірка конфігурації
python manage.py makemigrations --check --dry-run   # чи немає незроблених міграцій
python -m pytest                                    # тести (fuel/tests/, мережа заблокована)
python -m pytest fuel/tests/test_logs.py -q         # один файл
python -m ruff check .                              # лінтер
python -m ruff format .                             # форматування
python -m ruff format --check .                     # має бути без змін — перед кожним комітом
```

## Ручні проби API — `scripts/probes/`
Не тести: ходять у реальні API, запускаються з кореня проєкту лише свідомо. Приклад — знеособлені фікстури для тестів:
```
python scripts/probes/save_fixture.py                                   # вчора, UPG і UkrNafta
python scripts/probes/save_fixture.py --only ukrnafta --date 2026-10-10 --days 31 --rows 2
```
Результат (`fuel/tests/fixtures/*.json`) перед комітом переглянути очима.

## Сервер (192.168.1.173)
Деплой і бекап робить людина на сервері, докладно — `deploy/SERVER_SETUP.md`.
```
sudo bash /opt/fuelcards/deploy/deploy.sh           # оновлення: спершу бекап, потім код, залежності, міграції, перезапуск
sudo bash /opt/fuelcards/deploy/backup.sh           # ручний бекап бази → backups/manual/
systemctl list-timers fuelcards-backup.timer        # щоденний бекап о 01:00
```
