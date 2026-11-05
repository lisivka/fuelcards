---
description: Чекліст перед викладкою на сервер — що піде в прод, міграції, нові змінні .env, план відкату. Сам нічого не деплоїть.
disable-model-invocation: true
---

Готуємо викладку на сервер. Ти НЕ підключаєшся до сервера і НЕ запускаєш деплой — лише готуєш звіт.

## Що буде викладено
!`git log --oneline -20`

Спитай мене, який коміт зараз працює на сервері (на сервері: `git -C /opt/fuelcards log -1 --oneline`), і далі порівнюй з ним: `git diff --stat <той-коміт>..HEAD`.

## Склади звіт українською
1. **Що змінюється для користувачів** (з `CHANGELOG.md`, розділ «Не випущено»).
2. **Міграції:** перелік нових файлів у `fuel/migrations/`, що кожна робить, чи безпечна для наявних даних, чи можна відкотити (`python manage.py migrate fuel <попередня>`).
3. **Нові змінні оточення:** порівняй `.env_example` з попереднім релізом; що треба додати в `.env` на сервері.
4. **Нові залежності:** зміни в `requirements.txt`.
5. **Перевірки, які я маю пройти вручну:**
   - на гілці `master`, `git status` чистий;
   - `python -m pytest -q` і `python -m ruff check .` — зелені;
   - **бекап бази** робить сам `deploy.sh` першим кроком (`backups/deploy/`); якщо бекап не вдався, деплой зупиняється. Перевір: якщо на сервері ще немає `deploy/backup.sh` (коміт на сервері старший за ЗАДАЧУ-13), зроби бекап вручну перед деплоєм (у `backups/deploy/`, звідки його бере «Відновлення з бекапу»):
     `sudo -u fuelcards mkdir -p -m 700 /opt/fuelcards/backups/deploy`
     `sudo -u fuelcards /opt/fuelcards/venv/bin/python -c "import sqlite3,datetime; s=sqlite3.connect('/opt/fuelcards/db.sqlite3'); d=sqlite3.connect('/opt/fuelcards/backups/deploy/db_deploy_'+datetime.datetime.now().strftime('%F_%H%M')+'.sqlite3'); s.backup(d); d.close(); s.close()"`
     (якщо `mkdir` пише «Permission denied» — `backups/` належить root: `sudo chown fuelcards:fuelcards /opt/fuelcards/backups`)
     (екстрений ручний бекап у будь-який момент: `sudo bash /opt/fuelcards/deploy/backup.sh`);
   - деплой: `sudo bash /opt/fuelcards/deploy/deploy.sh`;
   - після деплою: відкрити сайт, сторінки «Транзакції» і «Карти», `journalctl -u fuelcards -n 50`.
6. **План відкату:** зупинити сервіс → `git checkout <попередній-коміт>` → відновити базу з `backups/deploy/` (кроки — `deploy/SERVER_SETUP.md`, «Відновлення з бекапу»; міграції окремо не відкочувати — копія зроблена до них) → запустити сервіс. Без відновлення бази (якщо дані після деплою треба зберегти) — замість нього `migrate fuel <попередня>` до `git checkout`.
7. Наприкінці: перенеси «Не випущено» в `CHANGELOG.md` під нову версію з датою (запропонуй правку, застосуй з мого дозволу).
