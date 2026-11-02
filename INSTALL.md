# Як встановити Claude Code і підключити цей набір до «Руху пального»



## Крок 2. PyCharm Pro
1. **Settings → Plugins → Marketplace** → `Claude Code [Beta]` → Install → перезапуск IDE.
2. **Settings → Tools → Claude Code [Beta]**: якщо плагін пише, що не може запустити Claude, вкажіть повний шлях до `claude.exe`.
3. Відкрийте проєкт, термінал PyCharm (у корені проєкту) → `claude`. Якщо працюєте в окремому терміналі — у сесії `/ide`.
4. У сесії `/config` → diff tool = `auto`, щоб зміни відкривались у diff-вікні PyCharm.

## Крок 3. Файли в репозиторій
Скопіюйте вміст папки `starter-kit/` у корінь `fuelcards`:

```
fuelcards/
├── CLAUDE.md                     ← правила для Claude (новий)
├── CHANGELOG.md                  ← журнал змін (новий)
├── pyproject.toml                ← налаштування pytest і ruff (новий)
├── requirements-dev.txt          ← pytest, pytest-django, ruff (новий)
├── PROJECT.md                    ← вже є, див. крок 4
├── .claude/
│   ├── settings.json             ← дозволи: що можна, що з питанням, що заборонено
│   ├── agents/
│   │   ├── code-reviewer.md      ← субагент-рецензент (лише читає)
│   │   └── test-writer.md        ← субагент для тестів
│   └── skills/
│       ├── task-start/SKILL.md   ← команда /task-start
│       ├── task-finish/SKILL.md  ← команда /task-finish
│       └── predeploy/SKILL.md    ← команда /predeploy
├── docs/
│   ├── TASKS.md                  ← дошка задач
│   ├── DECISIONS.md              ← журнал рішень
│   └── WORKFLOW.md               ← правила роботи (універсальні)
└── fuel/tests/
    ├── __init__.py
    └── test_services.py          ← зразкові тести (upsert, ідемпотентність, дати)
```

## Крок 4. Зміни в наявних файлах (зробіть самі або доручіть Claude першою задачею)
1. **Перенесіть `test_petroline.py`, `test_petroline_v2.py`, `test_uknafta.py`, `test_ukrnafta_403.py` з кореня в `scripts/probes/`.** Це не тести, а скрипти, що ходять у реальні API з вашими ключами; pytest не повинен їх підхоплювати. (`pyproject.toml` і так обмежує пошук тестів папкою `fuel/tests`, але так чистіше.)
2. **`PROJECT.md`:** приберіть верхній блок «Claude System Instructions» — правила тепер у `CLAUDE.md`. Блок посилається на неіснуючий `PROJECT_CONTEXT.md`. Залиште в `PROJECT.md` лише опис проєкту (розділи 1–10).
3. **`PROJECT.md`, розділ «Середовище»:** зараз там gunicorn на порту 80, а в `deploy/fuelcards.service` — waitress на порту 8000. Виправте на актуальне.
4. **`.gitignore`:** додайте рядки
   ```
   .claude/settings.local.json
   CLAUDE.local.md
   backups/
   ```
5. Встановіть інструменти розробки: `pip install -r requirements-dev.txt`, перевірте `python -m pytest -q` і `python -m ruff check .`. Ruff, імовірно, покаже зауваження до наявного коду — це нормально, їх виправлення може бути частиною ЗАДАЧІ-5.

## Крок 5. Перша сесія
```
claude
> /task-start ЗАДАЧА-0 Впровадити структуру з docs/WORKFLOW.md: перенести probe-скрипти, почистити PROJECT.md, перевірити тести і ruff
```
Claude складе план у режимі Plan, ви затвердите — і далі за циклом із `docs/WORKFLOW.md`.
