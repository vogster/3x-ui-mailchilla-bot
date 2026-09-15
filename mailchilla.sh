#!/usr/bin/env bash
#
# mailchilla — managing an installed Mailchilla.
# Installed to /usr/local/bin/mailchilla by install.sh.
#
#   mailchilla              a menu (when the input is a terminal)
#   mailchilla restart      the same thing as a command
#
# The message tables below are associative arrays, so bash 4.0 or newer is
# required. The check has to stand here, above the first `declare -A` and above
# `set -o pipefail`: inside a function it would never be reached, because the
# table is built while the script is read. It is therefore written without
# arrays, without [[ ]] and without `set -o`, so that even a shell that is not
# bash gets this far and prints a sentence instead of a syntax error.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Mailchilla: run this with bash — bash mailchilla" >&2
    echo "Mailchilla: запустите через bash — bash mailchilla" >&2
    exit 1
fi
case "$BASH_VERSION" in
    [0-3].*)
        echo "Mailchilla: bash 4.0 or newer is needed, found $BASH_VERSION." >&2
        echo "Mailchilla: нужен bash 4.0 или новее, найден $BASH_VERSION." >&2
        exit 1
        ;;
esac

set -euo pipefail

CONF_FILE="/etc/mailchilla/install.conf"
INSTALL_DIR="/opt/mailchilla"
INSTALL_LANG="en"
SERVICE="mailchilla"
SERVICE_USER="mailchilla"
REPO_URL="https://github.com/vogster/3x-ui-mailchilla-bot.git"
BACKUP_DIR="/opt/mailchilla-backups"

# shellcheck disable=SC1090
[ -r "$CONF_FILE" ] && . "$CONF_FILE"
UI_LANG="$INSTALL_LANG"

ENV_FILE="$INSTALL_DIR/.env"
STATE_FILES=(".env" "settings.json" "email_texts.json" "tariffs.json")

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
    RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'
    MAG=$'\033[35m'; CYA=$'\033[36m'
else
    B=""; D=""; N=""; RED=""; GRN=""; YEL=""; MAG=""; CYA=""
fi

declare -A MSG=(

[en.new_version]="Version {0} is out."
[ru.new_version]="Вышла версия {0}."
[en.new_version_how]="mailchilla update"
[ru.new_version_how]="mailchilla update"
[en.m_status]="Status"
[ru.m_status]="Статус"
[en.m_start]="Start"
[ru.m_start]="Запустить"
[en.m_stop]="Stop"
[ru.m_stop]="Остановить"
[en.m_restart]="Restart"
[ru.m_restart]="Перезапустить"
[en.m_log]="Log"
[ru.m_log]="Логи"
[en.m_update]="Update"
[ru.m_update]="Обновить"
[en.m_passwd]="Change the panel username or password"
[ru.m_passwd]="Сменить логин или пароль панели"
[en.m_port]="Change the address and port"
[ru.m_port]="Сменить адрес и порт"
[en.m_tunnel]="SSH tunnel to the panel"
[ru.m_tunnel]="SSH-туннель до панели"
[en.m_check]="Check the connection to 3x-ui"
[ru.m_check]="Проверить связь с 3x-ui"
[en.m_autostart]="Autostart"
[ru.m_autostart]="Автозапуск"
[en.m_backup]="Back up the configuration"
[ru.m_backup]="Бэкап конфигурации"
[en.m_lang]="Language of this menu"
[ru.m_lang]="Язык этого меню"
[en.m_uninstall]="Uninstall"
[ru.m_uninstall]="Удалить"
[en.m_quit]="Quit"
[ru.m_quit]="Выход"
[en.choose]="Choose"
[ru.choose]="Выберите"
[en.unknown]="No such item."
[ru.unknown]="Нет такого пункта."
[en.press]="Press Enter"
[ru.press]="Нажмите Enter"

[en.running]="running"
[ru.running]="работает"
[en.stopped]="stopped"
[ru.stopped]="остановлена"
[en.enabled]="on"
[ru.enabled]="вкл"
[en.disabled]="off"
[ru.disabled]="выкл"
[en.st_service]="Service"
[ru.st_service]="Служба"
[en.st_autostart]="Autostart"
[ru.st_autostart]="Автозапуск"
[en.st_version]="Version"
[ru.st_version]="Версия"
[en.st_panel]="Panel"
[ru.st_panel]="Панель"
[en.st_answers]="answers"
[ru.st_answers]="отвечает"
[en.autostart_on]="Autostart is on — the panel will come up after a reboot."
[ru.autostart_on]="Автозапуск включён — панель поднимется после перезагрузки."
[en.autostart_off]="Autostart is off — after a reboot it will have to be started by hand."
[ru.autostart_off]="Автозапуск выключен — после перезагрузки придётся запускать вручную."
[en.autostart_failed]="systemd did not accept the change."
[ru.autostart_failed]="systemd не принял изменение."
[en.st_silent]="does not answer"
[ru.st_silent]="не отвечает"

[en.need_root]="This needs root. Run: sudo mailchilla {0}"
[ru.need_root]="Нужны права root. Выполните: sudo mailchilla {0}"
[en.no_install]="Mailchilla is not installed in {0}."
[ru.no_install]="Mailchilla не установлена в {0}."
[en.done]="Done."
[ru.done]="Готово."
[en.cancelled]="Cancelled."
[ru.cancelled]="Отменено."

[en.u_current]="Installed"
[ru.u_current]="Установлена"
[en.u_latest]="Latest"
[ru.u_latest]="Последняя"
[en.u_uptodate]="The latest version is already installed."
[ru.u_uptodate]="Уже установлена последняя версия."
[en.u_checking]="Looking for a new version..."
[ru.u_checking]="Ищем новую версию..."
[en.u_offline]="Could not reach GitHub."
[ru.u_offline]="Не удалось связаться с GitHub."
[en.u_changes]="What has changed"
[ru.u_changes]="Что изменилось"
[en.u_ask]="Update to {0}?"
[ru.u_ask]="Обновиться до {0}?"
[en.u_local]="There are local changes to the files:"
[ru.u_local]="В файлах есть локальные изменения:"
[en.u_stash]="They will be put aside with git stash and can be brought back with: git -C {0} stash pop"
[ru.u_stash]="Они будут отложены через git stash и возвращаются командой: git -C {0} stash pop"
[en.u_pull]="Downloading {0}..."
[ru.u_pull]="Скачиваем {0}..."
[en.u_deps]="Updating dependencies..."
[ru.u_deps]="Обновляем зависимости..."
[en.u_restart]="Restarting..."
[ru.u_restart]="Перезапускаем..."
[en.u_ok]="Updated to {0}."
[ru.u_ok]="Обновлено до {0}."
[en.u_rollback]="The panel did not come up. Rolling back to {0}..."
[ru.u_rollback]="Панель не поднялась. Откатываемся на {0}..."
[en.u_rolled]="Rolled back. Look at the log: mailchilla log"
[ru.u_rolled]="Откат выполнен. Посмотрите лог: mailchilla log"

[en.p_user]="Username"
[ru.p_user]="Логин"
[en.p_pass]="New password (empty = generate)"
[ru.p_pass]="Новый пароль (пусто — сгенерировать)"
[en.p_pass2]="Repeat"
[ru.p_pass2]="Повторите"
[en.p_mismatch]="The passwords do not match."
[ru.p_mismatch]="Пароли не совпадают."
[en.p_quote]="A single quote in the password is not supported."
[ru.p_quote]="Одинарная кавычка в пароле не поддерживается."
[en.p_short]="At least 8 characters."
[ru.p_short]="Минимум 8 символов."
[en.p_new]="New password"
[ru.p_new]="Новый пароль"
[en.p_saved]="Saved. The panel has been restarted."
[ru.p_saved]="Сохранено. Панель перезапущена."

[en.host_now]="Now listening on"
[ru.host_now]="Сейчас слушает"
[en.q_expose]="Make the panel reachable from outside?"
[ru.q_expose]="Сделать панель доступной снаружи?"
[en.expose_warn]="Without a reverse proxy this is plain HTTP: the password travels the network in the clear, and the panel is open to anyone who finds the port."
[ru.expose_warn]="Без обратного прокси это обычный HTTP: пароль уйдёт по сети открытым, а панель будет доступна любому, кто найдёт порт."
[en.q_expose_sure]="Open it anyway (0.0.0.0)?"
[ru.q_expose_sure]="Всё равно открыть (0.0.0.0)?"
[en.q_close]="Close it back to 127.0.0.1?"
[ru.q_close]="Закрыть обратно на 127.0.0.1?"
[en.open_now]="The panel is open to the network:"
[ru.open_now]="Панель открыта в сеть:"
[en.open_warn]="This is HTTP without a certificate. A reverse proxy with HTTPS belongs in front of it."
[ru.open_warn]="Это HTTP без сертификата. Перед ней стоит поставить обратный прокси с HTTPS."
[en.port_new]="New port"
[ru.port_new]="Новый порт"
[en.port_bad]="A port is a number between 1 and 65535."
[ru.port_bad]="Порт — это число от 1 до 65535."
[en.port_busy]="Port {0} is occupied."
[ru.port_busy]="Порт {0} занят."

[en.tun_text]="The panel listens on 127.0.0.1 and is not reachable from outside — that is deliberate. Run this on your own machine:"
[ru.tun_text]="Панель слушает 127.0.0.1 и снаружи недоступна — так задумано. Выполните на своей машине:"
[en.tun_then]="then open"
[ru.tun_then]="затем откройте"

[en.c_checking]="Signing in to 3x-ui..."
[ru.c_checking]="Входим в 3x-ui..."
[en.c_ok]="3x-ui answers, the credentials are accepted. Inbounds: {0}."
[ru.c_ok]="3x-ui отвечает, доступы приняты. Inbound'ов: {0}."
[en.c_auth]="3x-ui refused the credentials (HTTP {0}). Check XUI_API_TOKEN, or the username and password, in {1}."
[en.c_net]="No answer from {0}. Check XUI_URL in {1}."
[en.c_odd]="3x-ui answered, but not as expected. Look at the log: mailchilla log"
[ru.c_auth]="3x-ui отклонил доступы (HTTP {0}). Проверьте XUI_API_TOKEN или логин с паролем в {1}."
[ru.c_net]="Адрес {0} не отвечает. Проверьте XUI_URL в {1}."
[ru.c_odd]="3x-ui ответил, но не так, как ожидалось. Посмотрите лог: mailchilla log"

[en.b_done]="Backup written to {0}"
[ru.b_done]="Бэкап записан в {0}"

[en.un_warn]="This removes the service, the code and the {0} command."
[ru.un_warn]="Будут удалены служба, код и команда {0}."
[en.un_keep]="Keep a backup of the configuration?"
[ru.un_keep]="Сохранить бэкап конфигурации?"
[en.un_ask]="Really uninstall Mailchilla?"
[ru.un_ask]="Точно удалить Mailchilla?"
[en.un_done]="Mailchilla has been removed."
[ru.un_done]="Mailchilla удалена."

[en.lang_saved]="The menu language has been changed. The language of the panel and of the letters is set in the panel itself."
[ru.lang_saved]="Язык меню изменён. Язык панели и писем задаётся в самой панели."

[en.help_usage]="Usage"
[ru.help_usage]="Использование"
[en.help_nomenu]="without arguments in a terminal — a menu"
[ru.help_nomenu]="без аргументов в терминале — меню"
)

t() {
    local key="$1"; shift
    local out="${MSG[$UI_LANG.$key]-${MSG[en.$key]-$key}}"
    local i=0
    for arg in "$@"; do
        out="${out//\{$i\}/$arg}"
        i=$((i + 1))
    done
    printf '%s' "$out"
}

say()  { printf '  %s\n' "$1"; }
dim()  { printf '  %s%s%s\n' "$D" "$1" "$N"; }
good() { printf '  %s✓%s %s\n' "$GRN" "$N" "$1"; }
bad()  { printf '  %s✗%s %s\n' "$RED" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$N" "$1"; }
rule() { printf '  %s────────────────────────────────────────────────%s\n' "$D" "$N"; }

# Set while the menu is up: a missing root there should refuse the one action,
# not throw the person out of the program they are working in.
IN_MENU=0

need_root() {
    [ "$(id -u)" -eq 0 ] && return 0
    bad "$(t need_root "${1:-}")"
    [ "$IN_MENU" = "1" ] && return 1
    exit 1
}

need_install() {
    [ -d "$INSTALL_DIR/.git" ] || { bad "$(t no_install "$INSTALL_DIR")"; exit 1; }
}

env_get() {
    # Reads a key out of .env, stripping the quotes install.sh writes.
    local key="$1" value
    [ -r "$ENV_FILE" ] || return 0
    value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-)"
    value="${value%\'}"; value="${value#\'}"
    value="${value%\"}"; value="${value#\"}"
    printf '%s' "$value"
}

env_set() {
    local key="$1" value="$2" tmp
    tmp="$(mktemp)"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # The value is written single-quoted, so python-dotenv takes it
        # literally and does not try to expand a $ inside it.
        awk -v k="$key" -v v="$value" \
            'BEGIN{FS=OFS="="} $1==k {print k "=" "\047" v "\047"; next} {print}' \
            "$ENV_FILE" > "$tmp"
    else
        cp "$ENV_FILE" "$tmp"
        printf "%s='%s'\n" "$key" "$value" >> "$tmp"
    fi
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
    chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE" 2>/dev/null || true
    chmod 600 "$ENV_FILE"
}

panel_port() { env_get ADMIN_PANEL_PORT || printf '8080'; }
panel_host() { env_get ADMIN_PANEL_HOST || printf '127.0.0.1'; }

# The repository belongs to the service user and this script runs as root, so
# a bare git call dies with "detected dubious ownership". install.sh adds the
# exception system-wide; passing it here too keeps the command working on a
# copy installed before that, and when the directory has been moved.
git_repo() { git -c safe.directory="$INSTALL_DIR" -C "$INSTALL_DIR" "$@"; }

current_version() {
    git_repo describe --tags --exact-match 2>/dev/null \
        || git_repo rev-parse --abbrev-ref HEAD 2>/dev/null \
        || printf '?'
}

panel_answers() {
    curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:$(panel_port)/login" 2>/dev/null
}

wait_for_panel() {
    local i
    for i in $(seq 1 30); do
        panel_answers && return 0
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
cmd_status() {
    local active enabled port
    active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    enabled="$(systemctl is-enabled "$SERVICE" 2>/dev/null || true)"
    port="$(panel_port)"

    printf '\n'
    if [ "$active" = "active" ]; then
        printf '  %s%-14s%s %s●%s %s\n' "$D" "$(t st_service)" "$N" "$GRN" "$N" "$(t running)"
    else
        printf '  %s%-14s%s %s●%s %s\n' "$D" "$(t st_service)" "$N" "$RED" "$N" "$(t stopped)"
    fi
    if [ "$enabled" = "enabled" ]; then
        printf '  %s%-14s%s %s\n' "$D" "$(t st_autostart)" "$N" "$(t enabled)"
    else
        printf '  %s%-14s%s %s\n' "$D" "$(t st_autostart)" "$N" "$(t disabled)"
    fi
    printf '  %s%-14s%s %s\n' "$D" "$(t st_version)" "$N" "$(current_version)"

    # is-active lies when the unit restarts in a loop, so ask the panel itself.
    if panel_answers; then
        printf '  %s%-14s%s http://127.0.0.1:%s  %s✓ %s%s\n' \
            "$D" "$(t st_panel)" "$N" "$port" "$GRN" "$(t st_answers)" "$N"
    else
        printf '  %s%-14s%s http://127.0.0.1:%s  %s✗ %s%s\n' \
            "$D" "$(t st_panel)" "$N" "$port" "$RED" "$(t st_silent)" "$N"
    fi
    printf '\n'
}

cmd_start()   { need_root start || return 1;   systemctl start "$SERVICE";   good "$(t done)"; }
cmd_stop()    { need_root stop || return 1;    systemctl stop "$SERVICE";    good "$(t done)"; }
cmd_restart() {
    need_root restart || return 1
    systemctl restart "$SERVICE"
    if wait_for_panel; then good "$(t done)"; else warn "$(t st_silent)"; fi
}

cmd_log() {
    if [ -t 1 ]; then
        journalctl -u "$SERVICE" -n 200 -f --no-pager
    else
        journalctl -u "$SERVICE" -n 200 --no-pager
    fi
}

autostart_on() { [ "$(systemctl is-enabled "$SERVICE" 2>/dev/null || true)" = "enabled" ]; }

# Say what it became, not just "done": the menu is the only place this shows,
# and a bare "done" leaves somebody wondering which way it went.
cmd_enable() {
    need_root enable || return 1
    systemctl enable "$SERVICE" >/dev/null 2>&1 || true
    if autostart_on; then good "$(t autostart_on)"; else bad "$(t autostart_failed)"; fi
}
cmd_disable() {
    need_root disable || return 1
    systemctl disable "$SERVICE" >/dev/null 2>&1 || true
    if autostart_on; then bad "$(t autostart_failed)"; else good "$(t autostart_off)"; fi
}
cmd_autostart() { if autostart_on; then cmd_disable; else cmd_enable; fi; }

# Set by cmd_backup, read by cmd_update. Returning it on stdout would mean
# silencing the message the user is meant to see.
BACKUP_PATH=""

cmd_backup() {
    need_root backup || return 1; need_install
    local dest="$BACKUP_DIR/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$dest"
    local file
    for file in "${STATE_FILES[@]}"; do
        [ -f "$INSTALL_DIR/$file" ] && cp -a "$INSTALL_DIR/$file" "$dest/"
    done
    printf '%s\n' "$(current_version)" > "$dest/VERSION"
    chmod 700 "$dest"
    BACKUP_PATH="$dest"
    good "$(t b_done "$dest")"
}

cmd_tunnel() {
    local port host addr
    port="$(panel_port)"
    host="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$host" ] || host="your-server"
    addr="$(panel_host)"
    printf '\n'
    if [ "$addr" = "0.0.0.0" ]; then
        say "$(t open_now)"
        printf '\n     %s%shttp://%s:%s%s\n\n' "$B" "$CYA" "$host" "$port" "$N"
        warn "$(t open_warn)"
        printf '\n'
        return
    fi
    say "$(t tun_text)"
    printf '\n     %s%sssh -L %s:127.0.0.1:%s root@%s%s\n\n' "$B" "$CYA" "$port" "$port" "$host" "$N"
    printf '  %s %s%shttp://localhost:%s%s\n\n' "$(t tun_then)" "$B" "$CYA" "$port" "$N"
}

cmd_check() {
    need_install
    printf '\n'
    dim "$(t c_checking)"

    # Not login(): with an API token set it returns True without asking the
    # panel anything, so it reports success on a wrong token or a dead host.
    # A real request is the only thing that proves the credentials work.
    local out status
    out="$(cd "$INSTALL_DIR" && "$INSTALL_DIR/venv/bin/python" -B -c '
import sys
sys.path.insert(0, ".")
import logging
logging.disable(logging.CRITICAL)
import settings
settings.load()
from xui_client import get_shared_client
try:
    r = get_shared_client()._request("GET", "/panel/api/inbounds/list/slim")
except Exception:
    print("NET"); sys.exit(0)
if r.status_code in (401, 403) or (r.status_code == 200 and "login" in r.url):
    print("AUTH", r.status_code); sys.exit(0)
if r.status_code != 200:
    print("AUTH", r.status_code); sys.exit(0)
try:
    body = r.json()
except Exception:
    print("ODD"); sys.exit(0)
if not body.get("success"):
    print("ODD"); sys.exit(0)
print("OK", len(body.get("obj") or []))
' 2>/dev/null)" || out="NET"

    status="${out%% *}"
    case "$status" in
        OK)   good "$(t c_ok "${out#OK }")" ;;
        AUTH) bad "$(t c_auth "${out#AUTH }" "$ENV_FILE")" ;;
        ODD)  bad "$(t c_odd)" ;;
        *)    bad "$(t c_net "$(env_get XUI_URL)" "$ENV_FILE")" ;;
    esac
    printf '\n'
}

cmd_passwd() {
    need_root passwd || return 1; need_install
    local user pass repeat generated=0
    printf '\n'
    printf '  %s?%s %s [%s]: ' "$CYA" "$N" "$(t p_user)" "$(env_get ADMIN_PANEL_USER)"
    read -r user </dev/tty
    [ -n "$user" ] || user="$(env_get ADMIN_PANEL_USER)"

    while :; do
        printf '  %s?%s %s: ' "$CYA" "$N" "$(t p_pass)"
        read -rs pass </dev/tty; printf '\n'
        if [ -z "$pass" ]; then
            pass="$(openssl rand -base64 18 | tr -d '/+=\n')"
            generated=1
            break
        fi
        case "$pass" in *\'*) warn "$(t p_quote)"; continue ;; esac
        [ "${#pass}" -ge 8 ] || { warn "$(t p_short)"; continue; }
        printf '  %s?%s %s: ' "$CYA" "$N" "$(t p_pass2)"
        read -rs repeat </dev/tty; printf '\n'
        [ "$pass" = "$repeat" ] || { warn "$(t p_mismatch)"; continue; }
        break
    done

    env_set ADMIN_PANEL_USER "$user"
    env_set ADMIN_PANEL_PASSWORD "$pass"
    systemctl restart "$SERVICE"
    printf '\n'
    [ "$generated" = "1" ] && printf '  %s%-14s%s %s%s%s\n' "$D" "$(t p_new)" "$N" "$B$YEL" "$pass" "$N"
    good "$(t p_saved)"
    printf '\n'
}

cmd_port() {
    need_root port || return 1; need_install
    local port host current
    current="$(panel_host)"
    printf '\n'
    dim "$(t host_now): $current:$(panel_port)"
    printf '\n'

    host="$current"
    if [ "$current" = "0.0.0.0" ]; then
        confirm "$(t q_close)" && host="127.0.0.1"
    else
        if confirm "$(t q_expose)"; then
            printf '\n'
            warn "$(t expose_warn)"
            printf '\n'
            confirm "$(t q_expose_sure)" && host="0.0.0.0"
        fi
    fi

    printf '\n'
    while :; do
        printf '  %s?%s %s [%s]: ' "$CYA" "$N" "$(t port_new)" "$(panel_port)"
        read -r port </dev/tty
        [ -n "$port" ] || { port="$(panel_port)"; break; }
        if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
            warn "$(t port_bad)"; continue
        fi
        if [ "$port" != "$(panel_port)" ] && command -v ss >/dev/null 2>&1 \
           && ss -ltn 2>/dev/null | grep -qE "[:.]$port[[:space:]]"; then
            warn "$(t port_busy "$port")"; continue
        fi
        break
    done

    env_set ADMIN_PANEL_HOST "$host"
    env_set ADMIN_PANEL_PORT "$port"
    systemctl restart "$SERVICE"
    wait_for_panel || true
    printf '\n'
    good "$(t done)"
    cmd_tunnel
}

cmd_lang() {
    need_root lang || return 1
    printf '\n'
    printf '      %s1%s  English\n' "$B" "$N"
    printf '      %s2%s  Русский\n' "$B" "$N"
    printf '  %s>%s ' "$CYA" "$N"
    local answer; read -r answer </dev/tty
    case "$answer" in
        1|en|EN) UI_LANG="en" ;;
        2|ru|RU) UI_LANG="ru" ;;
        *) return ;;
    esac
    if [ -w "$CONF_FILE" ] || [ "$(id -u)" -eq 0 ]; then
        sed -i "s/^INSTALL_LANG=.*/INSTALL_LANG=$UI_LANG/" "$CONF_FILE"
    fi
    printf '\n'
    good "$(t lang_saved)"
    printf '\n'
}

# --- update ---------------------------------------------------------------
latest_tag() {
    # A short leash: the menu must not sit waiting on GitHub, and a machine with
    # no route there should still get its menu.
    { if command -v timeout >/dev/null 2>&1; then
          timeout "${1:-8}" git ls-remote --tags --refs "$REPO_URL"
      else
          git ls-remote --tags --refs "$REPO_URL"
      fi; } 2>/dev/null \
        | awk -F/ '{print $NF}' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -V \
        | tail -n 1
}

# Asked once per run and remembered: the header is drawn again on every trip
# round the menu, and one question to GitHub is enough.
UPDATE_NOTE=""
UPDATE_NOTE_ASKED=0
update_note() {
    if [ "$UPDATE_NOTE_ASKED" = "0" ]; then
        UPDATE_NOTE_ASKED=1
        local latest current
        latest="$(latest_tag 4 || true)"
        current="$(current_version)"
        if [ -n "$latest" ] && [ "$latest" != "$current" ]; then
            # Only forward: a copy running ahead of the newest tag is somebody
            # testing a branch, and telling them to "update" to an older tag
            # would be wrong.
            if [ "$(printf '%s\n%s\n' "$current" "$latest" | sort -V | tail -n 1)" = "$latest" ]; then
                UPDATE_NOTE="$latest"
            fi
        fi
    fi
    printf '%s' "$UPDATE_NOTE"
}

changelog_for() {
    # The section of CHANGELOG.md belonging to one version.
    local tag="${1#v}" file="$INSTALL_DIR/CHANGELOG.md"
    [ -r "$file" ] || return 0
    awk -v v="$tag" '
        $0 ~ "^## \\[?" v "\\]?" {found=1; next}
        found && /^## / {exit}
        found {print}
    ' "$file" | sed '/^[[:space:]]*$/d' | head -n 20
}

python_minor() { python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0; }

cmd_update() {
    need_root update || return 1; need_install
    local current latest dirty stash_made=0 backup

    current="$(current_version)"
    printf '\n'
    dim "$(t u_checking)"
    latest="$(latest_tag || true)"
    if [ -z "$latest" ]; then bad "$(t u_offline)"; printf '\n'; return 1; fi

    printf '  %s%-12s%s %s\n' "$D" "$(t u_current)" "$N" "$current"
    printf '  %s%-12s%s %s%s%s\n' "$D" "$(t u_latest)" "$N" "$B" "$latest" "$N"
    if [ "$current" = "$latest" ]; then
        printf '\n'; good "$(t u_uptodate)"; printf '\n'; return 0
    fi

    git_repo fetch --tags --quiet --force

    local notes
    notes="$(changelog_for "$latest" || true)"
    if [ -n "$notes" ]; then
        printf '\n  %s%s%s\n' "$B" "$(t u_changes)" "$N"
        printf '%s\n' "$notes" | sed 's/^/  /'
    fi

    # Local edits are the usual reason an update fails, so say so plainly and
    # put them aside rather than dying on a git error.
    dirty="$(git_repo status --porcelain --untracked-files=no)"
    if [ -n "$dirty" ]; then
        printf '\n'
        warn "$(t u_local)"
        printf '%s\n' "$dirty" | sed 's/^/      /'
        dim "$(t u_stash "$INSTALL_DIR")"
    fi

    printf '\n  %s?%s %s [y/N]: ' "$CYA" "$N" "$(t u_ask "$latest")"
    local answer; read -r answer </dev/tty
    case "${answer,,}" in y|yes|д|да) ;; *) say "$(t cancelled)"; return 0 ;; esac

    printf '\n'
    cmd_backup
    backup="$BACKUP_PATH"

    if [ -n "$dirty" ]; then
        git_repo stash push --quiet -m "mailchilla update $(date -Iseconds)" || true
        stash_made=1
    fi

    dim "$(t u_pull "$latest")"
    git_repo checkout --quiet --force "$latest"

    dim "$(t u_deps)"
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" >/dev/null

    install -m 755 "$INSTALL_DIR/mailchilla.sh" /usr/local/bin/mailchilla 2>/dev/null || true
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    chmod 600 "$ENV_FILE"

    dim "$(t u_restart)"
    systemctl restart "$SERVICE"

    if wait_for_panel; then
        printf '\n'; good "$(t u_ok "$latest")"
        [ "$stash_made" = "1" ] && dim "$(t u_stash "$INSTALL_DIR")"
        printf '\n'
    else
        printf '\n'
        warn "$(t u_rollback "$current")"
        git_repo checkout --quiet --force "$current"
        "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" >/dev/null 2>&1 || true
        local file
        for file in "${STATE_FILES[@]}"; do
            [ -f "$backup/$file" ] && cp -a "$backup/$file" "$INSTALL_DIR/$file"
        done
        chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
        systemctl restart "$SERVICE"
        bad "$(t u_rolled)"
        printf '\n'
        return 1
    fi
}

cmd_uninstall() {
    need_root uninstall || return 1
    printf '\n'
    warn "$(t un_warn "mailchilla")"
    printf '  %s?%s %s [y/N]: ' "$CYA" "$N" "$(t un_ask)"
    local answer; read -r answer </dev/tty
    case "${answer,,}" in y|yes|д|да) ;; *) say "$(t cancelled)"; return 0 ;; esac

    printf '  %s?%s %s [Y/n]: ' "$CYA" "$N" "$(t un_keep)"
    local keep; read -r keep </dev/tty
    case "${keep,,}" in n|no|н|нет) ;; *) cmd_backup ;; esac

    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    rm -rf "$INSTALL_DIR"
    rm -rf "$(dirname "$CONF_FILE")"
    userdel "$SERVICE_USER" 2>/dev/null || true
    rm -f /usr/local/bin/mailchilla
    printf '\n'
    good "$(t un_done)"
    printf '\n'
}

cmd_version() { printf '%s\n' "$(current_version)"; }

cmd_help() {
    printf '\n  %s%s%s\n\n' "$B" "$(t help_usage)" "$N"
    printf '     mailchilla %s%s%s\n\n' "$D" "— $(t help_nomenu)" "$N"
    local c
    for c in status start stop restart log update passwd port tunnel check \
             enable disable autostart backup lang uninstall version help; do
        printf '     mailchilla %s\n' "$c"
    done
    printf '\n'
}

# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------
menu_header() {
    local active version
    active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    version="$(current_version)"
    printf '\n'
    printf '  %s╭────────────────────────────────────────────────╮%s\n' "$MAG" "$N"
    if [ "$active" = "active" ]; then
        printf '  %s│%s  %sM A I L C H I L L A%s   %s●%s %s %s%s%s\n' \
            "$MAG" "$N" "$B" "$N" "$GRN" "$N" "$(t running)" "$D" "$version" "$N"
    else
        printf '  %s│%s  %sM A I L C H I L L A%s   %s●%s %s %s%s%s\n' \
            "$MAG" "$N" "$B" "$N" "$RED" "$N" "$(t stopped)" "$D" "$version" "$N"
    fi
    printf '  %s╰────────────────────────────────────────────────╯%s\n' "$MAG" "$N"
    local note
    note="$(update_note)"
    if [ -n "$note" ]; then
        printf '     %s%s%s  %s%s%s\n' "$GRN" "$(t new_version "$note")" "$N" \
               "$D" "$(t new_version_how)" "$N"
    fi
    printf '\n'
}

item() { printf '     %s%2s%s  %s\n' "$B$CYA" "$1" "$N" "$2"; }

menu_loop() {
    IN_MENU=1
    local choice
    while :; do
        menu_header
        item 1  "$(t m_status)"
        item 2  "$(t m_start)"
        item 3  "$(t m_stop)"
        item 4  "$(t m_restart)"
        item 5  "$(t m_log)"
        printf '\n'
        item 6  "$(t m_update)"
        item 7  "$(t m_passwd)"
        item 8  "$(t m_port)"
        item 9  "$(t m_tunnel)"
        item 10 "$(t m_check)"
        printf '\n'
        item 11 "$(t m_autostart) — $(autostart_on && t enabled || t disabled)"
        item 12 "$(t m_backup)"
        item 13 "$(t m_lang)"
        item 14 "$(t m_uninstall)"
        printf '\n'
        item 0  "$(t m_quit)"
        printf '\n  %s%s%s: ' "$CYA" "$(t choose)" "$N"
        read -r choice </dev/tty || exit 0

        case "$choice" in
            1) cmd_status ;;
            2) cmd_start ;;
            3) cmd_stop ;;
            4) cmd_restart ;;
            5) cmd_log || true ;;
            6) cmd_update || true ;;
            7) cmd_passwd ;;
            8) cmd_port ;;
            9) cmd_tunnel ;;
            10) cmd_check ;;
            11) cmd_autostart ;;
            12) cmd_backup ;;
            13) cmd_lang ;;
            14) cmd_uninstall; exit 0 ;;
            0|q|quit|exit) printf '\n'; exit 0 ;;
            *) warn "$(t unknown)" ;;
        esac

        printf '  %s%s%s ' "$D" "$(t press)" "$N"
        read -r _ </dev/tty || exit 0
    done
}

# ---------------------------------------------------------------------------
# A menu only when there is somebody to show it to: without the tty check
# `mailchilla` from cron or in a pipe would hang on the prompt.
# ---------------------------------------------------------------------------
main() {
    if [ $# -eq 0 ]; then
        if [ -t 0 ] && [ -t 1 ]; then menu_loop; else cmd_status; fi
        return
    fi
    case "$1" in
        status)    cmd_status ;;
        start)     cmd_start ;;
        stop)      cmd_stop ;;
        restart)   cmd_restart ;;
        log|logs)  cmd_log ;;
        update)    cmd_update ;;
        passwd|password) cmd_passwd ;;
        port)      cmd_port ;;
        tunnel|ssh) cmd_tunnel ;;
        check)     cmd_check ;;
        enable)    cmd_enable ;;
        autostart) cmd_autostart ;;
        disable)   cmd_disable ;;
        backup)    cmd_backup ;;
        lang)      cmd_lang ;;
        uninstall|remove) cmd_uninstall ;;
        version|-v|--version) cmd_version ;;
        menu)      menu_loop ;;
        help|-h|--help) cmd_help ;;
        *) bad "$(t unknown)"; cmd_help; exit 1 ;;
    esac
}

main "$@"
