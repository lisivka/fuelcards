## 1. Мета

Веб-застосунок, що збирає дані про заправки з трьох провайдерів пального (Petroline, UPG, UkrNafta) в одну базу, показує звіти працівникам, а також дозволяє керувати картками (блокування/розблокування, редагування Авто/ПІБ, надалі ліміти) через API провайдерів.

## 2. Середовище

- Користувачів 3-5 працівників, лише локальна мережа.
- Сервер: Ubuntu, адреса `http://192.168.1.173/` (HTTP, без HTTPS).
- Хостинг: gunicorn + WhiteNoise під systemd, без nginx (порт 80, `AmbientCapabilities=CAP_NET_BIND_SERVICE`).
- Синхронізація: службова команда `manage.py sync_fuel` — `--days N` (за замовчуванням 2), `--config-days` (глибина `ProviderConfig.sync_days` кожного провайдера), `--config-period` (період `sync_date_from`..`sync_date_to`), `--provider` для вибору. Те саме з адмінки: дії «Синхронізувати вибраних за N днів» / «за період з/по» в «Конфігураціях провайдерів». Неактивні провайдери пропускаються. Період понад 30 днів ділиться на частини по 30 днів з паузою 2 с (`services.CHUNK_DAYS`, `CHUNK_PAUSE_SECONDS`), максимум 366 днів; помилка частини зупиняє лише цього провайдера.
- Синхронізація: є ручна кнопка «Синхронізувати зараз» на сторінці «Транзакції», яка синхронізує від останньої дати в базі даних. 
- Оновлення: `git pull`, `pip install -r`, `migrate`, `collectstatic`, `systemctl restart`.
- Репозиторій: https://github.com/lisivka/fuelcards (публічний; секрети лише в `.env`, у git не потрапляють).

## 3. Стек

Python 3, Django 5.x, SQLite (режим WAL, `transaction_mode=IMMEDIATE`), requests, python-dotenv, gunicorn, whitenoise. Часова зона Europe/Kyiv, мова інтерфейсу uk.

## 4. Структура коду (наявна)

- `config/` налаштування Django; `fuel/` єдиний додаток.
- `fuel/models.py`: `Provider` (TextChoices), `CardStatus`, `ProviderConfig`, `Card`, `Transaction`.
- `fuel/providers/`: адаптери `petroline.py`, `upg.py`, `ukrnafta.py` (транзакції) та `ukrnafta_cards.py`, `petroline_cards.py` (картки), спільне в `base.py` (`NormalizedTx`).
- `fuel/services.py`: upsert транзакцій, `run_sync` (збій одного провайдера не зупиняє інших, неактивні пропускаються).
- `fuel/card_service.py`, `fuel/card_views.py`: логіка й JSON-ендпоінти сторінки «Карти».
- `fuel/management/commands/`: `sync_fuel`, `sync_cards`.
- Шаблони: `base.html` (меню: Транзакції, Карти, Адмінка), `transactions.html`, `cards.html`.

## 5. Модель даних

- **Transaction**: provider, external_id (унікальна пара), kind (fuel/topup), card (FK, може бути null), card_ref, vehicle_raw, timestamp, fuel, liters, price, amount, station, canceled, raw JSON, cards.id_vehicle (FK, може бути null), cards.id_driver (FK, може бути null).
- **Card**: provider+number (унікальна пара; `number` — ключ зв'язку з `Transaction.card_ref`, для Petroline це «Ідентифікатор»), ext_number (номер на картці; `display_number` = ext_number або number), external_id (ID у провайдера), vehicle (Авто), driver_name (ПІБ), status (0 Активна, 1 Заблокована, 2 Неактивна, 3 Замінена; null якщо невідомо), status_updated_at, note.
- **ProviderConfig**: code, is_active (чи брати в синхронізацію), edit_enabled (режим редагування карток через API; типово лише UkrNafta), row_color, sync_days (глибина синхронізації, 1..366, типово 2), sync_date_from / sync_date_to (період для синхронізації «з/по»; обидві або жодна, «по» не в майбутньому).
- Синхронізація ідемпотентна: повторне завантаження оновлює записи (важливо для скасувань UkrNafta).

## 6. Провайдери (ключі лише в `.env`)

**Petroline**: `POST http://fms.petroline.in.ua:8055/api/TransactionsGet`, тіло JSON: `login`, `pass` (SHA256 hex), `datestart`, `datefinish` (формат `dd.MM.yyyy HH:mm:ss`). У відповіді `Result[]`: TransactionID, Date, Value, AZSname, Card1, PersonName (вільний текст), Fuel. Немає ціни й суми. `Value` трактується як мілілітри (ділення на 1000, змінна `PETROLINE_VALUE_DIVISOR`), час вважається київським. Пароль у `.env` може бути як звичайним, так і готовим хешем.

**Petroline, картки (новий API, лише читання)**: `POST https://apiv2.petroline.in.ua/api/Auth/token` (login, password — звичайний, не хеш) → токен; далі список `Keys/list` з пагінацією. Статуси 3 → Активна, 5 → Заблокована, інші не вгадуються (None, з попередженням у лог). Облікові дані: `PETROLINE_V2_LOGIN`/`PETROLINE_V2_PASSWORD`, запасні `PETROLINE_LOGIN`/`PETROLINE_PASSWORD`.

**UPG**: спершу `POST https://paycontainer.upg.ua/api/login` (login, password) повертає Bearer-токен (брати свіжий на кожну синхронізацію). Далі `POST .../api/fuel-card-history` (form: date_from, date_to = кінець + 1 день, page), пагінація за `meta.last_page`. Номер картки в UPG часткові («031 992»). У `.env`: `UPG_LOGIN`, `UPG_PASSWORD`.

**UkrNafta** (`https://naftacards.ukrnafta.com/public-api`):
- Транзакції: `GET /common/transactions` (fromDate, toDate, holderId, with_cancelation=true). typeId 302 = заправка, 214 = поповнення; є прапорець canceled.
- Картки: `GET /accounts/{UKRNAFTA_ACCOUNT_ID}/cards?lang=uk`, у відповіді status, vin, cardInfo.firstName/lastName.
- Оновлення: `POST /cards/{cardId}/update?lang=uk`, тіло JSON: status, vin, cardInfo{firstName,lastName,...}.
- Змінні `.env`: `UKRNAFTA_TOKEN`, `UKRNAFTA_HOLDER_ID`, `UKRNAFTA_ACCOUNT_ID`.

## 7. Реалізовано

- Завантаження транзакцій з усіх трьох провайдерів, нормалізація, upsert.
- Сторінка «Транзакції»: фільтри (дата, провайдер, пошук), підсумки, пагінація, експорт CSV (`;`, UTF-8 BOM).
- Сторінка «Карти»: миттєвий фільтр за провайдером, окремі пошуки за номером/авто/ПІБ (на клієнті), окремі кнопки «Заблокувати/Розблокувати» та «Зберегти Авто/ПІБ», додавання і локальне видалення, кнопка «Оновити статуси UkrNafta».
- Для провайдерів з увімкненим режимом редагування зміни спершу йдуть в API і пишуться в базу лише після успіху. Для решти Авто/ПІБ зберігаються тільки локально, статус не змінюється.
- Права: усі авторизовані користувачі можуть редагувати картки. Пункт «Адмінка» бачать лише `is_staff`.



## 9. План далі
- **[ЗАДАЧА 1]** Додати кнопку "Синхронізувати зараз" на сторінку «Транзакції» (виклик `sync_fuel`). Шукаємо останню дату в базі й завантажує від неї включно до теперішнього часу для кожного провайдера. Після успіху показує повідомлення про кількість нових транзакцій і дату останньої синхронізації.
- **[ЗАДАЧА 2]** Налаштування та деплой конфігурації gunicorn + systemd для Ubuntu (порт 80, HTTP).
- **[ЗАДАЧА 3]** Журнал логування/аудиту: хто, коли, яку картку, що змінив, помилки синхронізації, інші службові події. Можна в окрему таблицю `AuditLog` або в існуючі моделі (через JSONField). Зробити нову сторінку Логи (останні 20 подій, пагінація).
- **[ЗАДАЧА 4]** Зміна ліміти карток через API для УкрНафта (спершу перевірити, чи підтримує це API кожного провайдера).
- **[ЗАДАЧА 5]** Написати тести для перевірки функціональності (адаптери на збережених відповідях, сервіс карток із підміненим клієнтом), PEP8/ruff.

## 10. Безпека

- Ключі й паролі лише в `.env` (права 600), ніколи в git, чаті, скріншотах.
- `DEBUG=0` на сервері, окремий `DJANGO_SECRET_KEY`.
- Усі операції запису в API провайдерів мають бути простежувані (журнал аудиту) та підтверджуватися в інтерфейсі.
- Файрвол лише для локальної підмережі.
