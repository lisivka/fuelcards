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
- `fuel/card_service.py`, `fuel/card_views.py`: логіка й JSON-ендпоінти сторінки «Карти»; сторінка картки `card_detail` (`/cards/<id>/`, поля й блокування залежно від провайдера — `card_service.capabilities`), журнал аудиту.
- `fuel/management/commands/`: `sync_fuel`, `sync_cards`.
- Шаблони: `base.html` (меню: Транзакції, Карти, Адмінка), `transactions.html`, `cards.html`.

## 5. Модель даних

- **Transaction**: provider, external_id (унікальна пара), kind (fuel/topup), card (FK, може бути null), card_ref, vehicle_raw, timestamp, fuel, liters, price, amount, station, canceled, raw JSON, cards.id_vehicle (FK, може бути null), cards.id_driver (FK, може бути null).
- **Card**: provider+number (унікальна пара; `number` — ключ зв'язку з `Transaction.card_ref`, для Petroline це «Ідентифікатор»), ext_number (номер на картці; `display_number` = ext_number або number), external_id (ID у провайдера), vehicle (Авто), driver_name (ПІБ), status (0 Активна, 1 Заблокована, 2 Неактивна, 3 Замінена; null якщо невідомо), status_updated_at, note.
- **ProviderConfig**: code, is_active (чи брати в синхронізацію), edit_enabled (режим редагування карток через API; типово лише UkrNafta), row_color, sync_days (глибина синхронізації, 1..366, типово 2), sync_date_from / sync_date_to (період для синхронізації «з/по»; обидві або жодна, «по» не в майбутньому).
- **AuditLog**: created_at, user (+ знімок username), action (card_create / card_info / card_status / card_delete / tx_sync / cards_sync), details JSON для синхронізацій (source: button / admin_days / admin_period / command, date_from, date_to, chunks, chunks_done, created, updated, capped), card (FK, null після видалення) + card_label (знімок), provider, changes JSON `{поле: [було, стало]}`, via_api, success, error. Пишеться в `card_service` (`record_audit`); невдалий запис в API теж логується з success=False, база при цьому не змінюється. Синхронізації пишуться через `fuel/audit.py` (`record_sync`): один запис на провайдера за запуск, неактивні не пишуться. Перегляд — сторінка «Логи» (`/logs/`, усім авторизованим) і адмінка (лише читання). Очищення старих записів поки немає.
- Синхронізація ідемпотентна: повторне завантаження оновлює записи (важливо для скасувань UkrNafta).

## 6. Провайдери (ключі лише в `.env`)

**Petroline, транзакції (новий API, основний)**: токен як для карток (див. нижче), далі `POST https://apiv2.petroline.in.ua/api/TrkTransactions/v2/list`, тіло `{"filter": {"date": {"start", "end"}}, "loadOption": {"orderBy", "skip", "take"}}`; межі періоду — київські 00:00…23:59:59 у UTC (`...Z`), пагінація по 100. Поля: `id` (= `TransactionID` старого API, тому перемикання не дублює записи), `date` (UTC), `value` (мл, ÷ `PETROLINE_VALUE_DIVISOR`), `card1Number` (= `Card.number`), `fuelName`, `azsName`, `personName` (порядок слів інший, ніж у старому API: «ім'я прізвище»). Ціни й суми немає. Спільний клієнт — `fuel/providers/petroline_api.py`. Запасний шлях: `PETROLINE_API=legacy` у `.env` вмикає старий API.

**Petroline, старий API (запасний)**: `POST http://fms.petroline.in.ua:8055/api/TransactionsGet`, тіло JSON: `login`, `pass` (SHA256 hex), `datestart`, `datefinish` (формат `dd.MM.yyyy HH:mm:ss`). У відповіді `Result[]`: TransactionID, Date, Value, AZSname, Card1, PersonName (вільний текст), Fuel. Немає ціни й суми. `Value` трактується як мілілітри (ділення на 1000, змінна `PETROLINE_VALUE_DIVISOR`), час вважається київським. Пароль у `.env` може бути як звичайним, так і готовим хешем.

**Petroline, картки (новий API)**: `POST https://apiv2.petroline.in.ua/api/Auth/token` (login, password — звичайний, не хеш) → токен; далі список `Keys/list` з пагінацією. Блокування/розблокування: `PUT /api/Keys/update` — API приймає лише повний запис картки (`KeyDto`), тому спершу `Keys/list` з фільтром `number`, змінюється лише `status` (3 активна, 5 заблокована), решта полів (зокрема `pin`, який у Keys/list завжди порожній — PIN не використовуємо) надсилається як прочитано; відповідь `{"value", "status": 200, "errors"}`, помилка в тілі теж вважається невдачею. Авто/ПІБ картки Petroline — не поля картки, а водій (`Persons`) або транспорт (`Transports`), до яких вона прив'язана (рішення 004): «Персональна» (тип 1) з водієм — перейменовуємо водія на `Авто | ПІБ` (якщо таке ім'я вже має інший водій — відмова); «Техпролив» (3) — ім'я водія = ПІБ; «Автомобільна» (5) і картка без водія — збереження лише в нашій базі, а кнопка «Записати в Petroline» робить картку «Персональною» (`Keys/update` typeId=1, personId, transportId=null) на наявному активному водієві з таким самим ім'ям (без різниці великих/малих літер; кілька таких — відмова) або на новому водієві `Авто | ПІБ` (пальне ДП; якщо прив'язка не вдалась — водія видаляємо); тип 2 і невідомий тип (ще не оновлено) — не пишемо. Режим визначається за даними останнього «Оновити карти», а перед записом картка читається з Petroline ще раз: якщо тип або водій інші — запис відхиляється. Petroline складає ім'я водія як «прізвище ім'я по батькові», тож увесь текст пишемо в `surname`. Запис — `PUT Persons/update` з повним записом (порожня відповідь = успіх); якщо ім'я в Petroline вже таке, як наше, у Petroline не пишемо. Один водій може мати кілька карток: зміна застосовується до всіх, у нашій базі оновлюються всі картки водія (сторінка «Дублі водіїв»). Звірка: якщо наші Авто/ПІБ не дають того самого імені, що в Petroline (`provider_label`), сторінка картки показує обидва варіанти і кнопки «Взяти з Petroline» (лише база) / «Записати наше в Petroline»; на «Картах» — фільтр «Розбіжності з Petroline». Читання: «Оновити карти Petroline» зберігає тип, ID водія/транспорту і `provider_label`; Авто/ПІБ заповнює лише якщо в нас обидва порожні (`Авто | ПІБ` розділяється по `|`, без `|` — усе в Авто, бо старі імена переважно «Авто_ПІБ»; для транспорту «Модель Номер» — в Авто). Транзакції з пальним «Вода» (`fuelId` 17) не зберігаються. Статуси 3 → Активна, 5 → Заблокована, інші не вгадуються (None, з попередженням у лог). Облікові дані: `PETROLINE_V2_LOGIN`/`PETROLINE_V2_PASSWORD`, якщо порожні — `PETROLINE_LOGIN`/`PETROLINE_PASS`. Ліміти в новому API — на водія (`PersonLimit`) і на транспорт (`TransportLimit`); використовуємо лише ліміти на водія (рішення 004).

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

Задачі, їхній стан і історія виконаних — лише в [`docs/TASKS.md`](docs/TASKS.md).

## 10. Безпека

- Ключі й паролі лише в `.env` (права 600), ніколи в git, чаті, скріншотах.
- `DEBUG=0` на сервері, окремий `DJANGO_SECRET_KEY`.
- Усі операції запису в API провайдерів мають бути простежувані (журнал аудиту) та підтверджуватися в інтерфейсі.
- Файрвол лише для локальної підмережі.
