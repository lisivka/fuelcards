#!/usr/bin/env bash
# Бекап бази SQLite «на льоту» (сервіс можна не зупиняти).
#
#   sudo bash /opt/fuelcards/deploy/backup.sh            # ручний (manual)
#   sudo bash /opt/fuelcards/deploy/backup.sh deploy     # викликає deploy.sh
#   backup.sh auto                                       # таймер fuelcards-backup (від fuelcards)
#
# Копія: backups/<тип>/db_<тип>_<РРРР-ММ-ДД_ГГХХ>.sqlite3, перевірена PRAGMA integrity_check.
# Лише для «auto»: копія на мережеву теку BACKUP_REMOTE_DIR, потім видалення копій,
# старших за BACKUP_KEEP_DAYS / BACKUP_REMOTE_KEEP_DAYS днів.
# Налаштування — у /etc/default/fuelcards-backup (зразок: deploy/fuelcards-backup.default).
set -euo pipefail

KIND="${1:-manual}"
case "$KIND" in
    manual | deploy | auto) ;;
    *)
        echo "Невідомий тип бекапу: $KIND (очікується manual, deploy або auto)" >&2
        exit 2
        ;;
esac

log() { echo "[backup $KIND] $*"; }
warn() { echo "[backup $KIND] ПОПЕРЕДЖЕННЯ: $*" >&2; }
fail() {
    echo "[backup $KIND] ПОМИЛКА: $*" >&2
    exit 1
}

BACKUP_CONFIG="${BACKUP_CONFIG:-/etc/default/fuelcards-backup}"
if [[ -r "$BACKUP_CONFIG" ]]; then
    # shellcheck source=/dev/null
    source "$BACKUP_CONFIG"
elif [[ -e "$BACKUP_CONFIG" ]]; then
    # Для auto — помилка: інакше таймер (від fuelcards) мовчки працював би без копії на шару.
    # Ручний і деплойний бекап не блокуємо — вони працюють і зі значеннями за замовчуванням.
    msg="немає доступу на читання до $BACKUP_CONFIG (потрібно: sudo chmod 644 $BACKUP_CONFIG)"
    [[ "$KIND" == "auto" ]] && fail "$msg"
    warn "$msg; беру значення за замовчуванням"
fi

APP_DIR="${APP_DIR:-/opt/fuelcards}"
APP_USER="${APP_USER:-fuelcards}"
PYTHON="${PYTHON:-$APP_DIR/venv/bin/python}"
DB="${DB:-$APP_DIR/db.sqlite3}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-15}"
BACKUP_REMOTE_DIR="${BACKUP_REMOTE_DIR:-}"
BACKUP_REMOTE_KEEP_DAYS="${BACKUP_REMOTE_KEEP_DAYS:-15}"

# Усе, що торкається файлів, — від користувача застосунку, щоб у копій був правильний власник.
if [[ $EUID -eq 0 ]]; then
    as_app() { sudo -u "$APP_USER" "$@"; }
elif [[ "$(id -un)" == "$APP_USER" ]]; then
    as_app() { "$@"; }
else
    fail "запустіть через sudo: sudo bash $0 $KIND"
fi

for days in "$BACKUP_KEEP_DAYS" "$BACKUP_REMOTE_KEEP_DAYS"; do
    [[ "$days" =~ ^[1-9][0-9]*$ ]] || fail "кількість днів має бути цілим числом ≥ 1, отримано «$days»"
done
cd "$APP_DIR" || fail "немає теки застосунку $APP_DIR"
[[ -f "$DB" ]] || fail "немає бази $DB"

TARGET_DIR="$BACKUP_DIR/$KIND"
if [[ $EUID -eq 0 ]]; then
    install -d -o "$APP_USER" -g "$APP_USER" -m 700 "$BACKUP_DIR" "$TARGET_DIR"
else
    mkdir -p "$TARGET_DIR"
    chmod 700 "$BACKUP_DIR" "$TARGET_DIR"
fi

STAMP="$(date +%F_%H%M)"
NAME="db_${KIND}_${STAMP}.sqlite3"
# Кілька бекапів за хвилину не перезаписують один одного: _ГГХХСС, далі _ГГХХСС-2, -3…
if [[ -e "$TARGET_DIR/$NAME" ]]; then
    STAMP="$STAMP$(date +%S)"
    NAME="db_${KIND}_${STAMP}.sqlite3"
    n=2
    while [[ -e "$TARGET_DIR/$NAME" ]]; do
        NAME="db_${KIND}_${STAMP}-$n.sqlite3"
        n=$((n + 1))
    done
fi
DEST="$TARGET_DIR/$NAME"
PART="$DEST.part"
REMOTE_PART=""

# Незавершені .part (збій бекапу чи копіювання) прибираємо за будь-якого виходу.
cleanup() {
    as_app rm -f "$PART" || true
    if [[ -n "$REMOTE_PART" ]]; then
        as_app rm -f "$REMOTE_PART" || true
    fi
}
trap cleanup EXIT

# Онлайн-бекап SQLite: узгоджена копія разом з даними з WAL; потім перевірка цілісності.
# Пишемо в .part і перейменовуємо лише після успішної перевірки.
as_app "$PYTHON" - "$DB" "$PART" <<'PY'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path, timeout=60)
dst = sqlite3.connect(dst_path)
try:
    src.backup(dst)
    result = dst.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    dst.close()
    src.close()
if result != "ok":
    sys.exit(f"integrity_check: {result}")
PY
as_app chmod 600 "$PART"
as_app mv "$PART" "$DEST"
log "готово: $DEST ($(du -h "$DEST" | cut -f1))"

[[ "$KIND" == "auto" ]] || exit 0

# Далі кроки не обривають один одного: помилку записуємо в журнал і завершуємось з кодом 1 наприкінці.
ERRORS=0

copy_remote() {
    if [[ -z "$BACKUP_REMOTE_DIR" ]]; then
        log "BACKUP_REMOTE_DIR не задано — копію за межі сервера не робимо"
        return 0
    fi
    # Теку не створюємо: якщо мережева шара не змонтована, теки немає — і копія
    # не повинна непомітно лягти на локальний диск.
    if [[ ! -d "$BACKUP_REMOTE_DIR" ]]; then
        warn "немає теки $BACKUP_REMOTE_DIR (шара не змонтована?); локальна копія збережена: $DEST"
        return 1
    fi
    REMOTE_PART="$BACKUP_REMOTE_DIR/$NAME.part"
    if ! as_app cp "$DEST" "$REMOTE_PART" || ! as_app mv -f "$REMOTE_PART" "$BACKUP_REMOTE_DIR/$NAME"; then
        warn "не вдалося скопіювати в $BACKUP_REMOTE_DIR; локальна копія збережена: $DEST"
        return 1
    fi
    REMOTE_PART=""
    log "скопійовано: $BACKUP_REMOTE_DIR/$NAME"
}

# Видаляємо лише власні файли: db_*.sqlite3 старші за N днів і забуті db_*.sqlite3.part старші за добу.
prune() {
    local dir="$1" days="$2"
    as_app find "$dir" -maxdepth 1 -type f \
        \( \( -name 'db_*.sqlite3' -mmin "+$((days * 1440))" \) -o \( -name 'db_*.sqlite3.part' -mmin +1440 \) \) \
        -print -delete | sed "s/^/[backup $KIND] видалено: /"
}

copy_remote || ERRORS=1

for kind_dir in manual deploy auto; do
    if [[ -d "$BACKUP_DIR/$kind_dir" ]]; then
        prune "$BACKUP_DIR/$kind_dir" "$BACKUP_KEEP_DAYS" || {
            warn "ротація в $BACKUP_DIR/$kind_dir не вдалась"
            ERRORS=1
        }
    fi
done

if [[ -n "$BACKUP_REMOTE_DIR" && -d "$BACKUP_REMOTE_DIR" ]]; then
    prune "$BACKUP_REMOTE_DIR" "$BACKUP_REMOTE_KEEP_DAYS" || {
        warn "ротація в $BACKUP_REMOTE_DIR не вдалась"
        ERRORS=1
    }
fi

exit "$ERRORS"
