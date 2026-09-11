#!/usr/bin/env bash
#
# Mailchilla installer — https://github.com/vogster/Mailchilla
#
#   bash <(curl -Ls https://raw.githubusercontent.com/vogster/Mailchilla/main/install.sh)
#
# Installs the latest tagged release into /opt/mailchilla, generates .env,
# registers a systemd service and the `mailchilla` command.
#
# Options:
#   --lang en|ru        skip the language question
#   --version vX.Y.Z    install a particular tag
#   --branch NAME       install the tip of a branch (for testing)
#
# The message tables below are associative arrays, so bash 4.0 or newer is
# required. The check has to stand here, above the first `declare -A` and above
# `set -o pipefail`: inside a function it would never be reached, because the
# table is built while the script is read. It is therefore written without
# arrays, without [[ ]] and without `set -o`, so that even a shell that is not
# bash gets this far and prints a sentence instead of a syntax error.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "Mailchilla: run this with bash — bash install.sh" >&2
    echo "Mailchilla: запустите через bash — bash install.sh" >&2
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

REPO_URL="https://github.com/vogster/Mailchilla.git"
INSTALL_DIR="/opt/mailchilla"
CONF_DIR="/etc/mailchilla"
CONF_FILE="$CONF_DIR/install.conf"
SERVICE="mailchilla"
SERVICE_USER="mailchilla"
BIN_PATH="/usr/local/bin/mailchilla"
UNIT_PATH="/etc/systemd/system/mailchilla.service"
PY_MIN_MAJOR=3
PY_MIN_MINOR=9

UI_LANG=""
REQ_TAG=""
REQ_BRANCH=""
STEP=0
STEPS=7

# ---------------------------------------------------------------------------
# Colours. Dropped when the output is not a terminal, or NO_COLOR is set.
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; D=$'\033[2m'; N=$'\033[0m'
    RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'
    BLU=$'\033[34m'; MAG=$'\033[35m'; CYA=$'\033[36m'
else
    B=""; D=""; N=""; RED=""; GRN=""; YEL=""; BLU=""; MAG=""; CYA=""
fi

# ---------------------------------------------------------------------------
# Texts. The key carries the language: ${MSG[ru.need_root]}. A key missing in
# one language falls back to English — the same rule i18n.py follows, so a
# partial translation is safe.
# ---------------------------------------------------------------------------
declare -A MSG=(
[en.tagline]="Email bot and web panel for 3x-ui"
[ru.tagline]="Почтовый бот и веб-панель для 3x-ui"

[en.ask_lang]="Language / Язык"
[ru.ask_lang]="Language / Язык"

[en.need_root]="Run the installer as root: sudo bash install.sh"
[ru.need_root]="Запустите установщик от root: sudo bash install.sh"
[en.need_systemd]="systemd was not found. This installer only supports systemd systems."
[ru.need_systemd]="systemd не найден. Установщик рассчитан только на системы с systemd."

[en.s_deps]="Packages"
[ru.s_deps]="Пакеты"
[en.s_user]="System user"
[ru.s_user]="Системный пользователь"
[en.s_code]="Source code"
[ru.s_code]="Исходный код"
[en.s_venv]="Python environment"
[ru.s_venv]="Окружение Python"
[en.s_conf]="Configuration"
[ru.s_conf]="Конфигурация"
[en.s_service]="Service"
[ru.s_service]="Служба"
[en.s_start]="Start"
[ru.s_start]="Запуск"

[en.os_unknown]="Could not identify the distribution (/etc/os-release is missing)."
[ru.os_unknown]="Не удалось определить дистрибутив (нет /etc/os-release)."
[en.os_unsupported]="Unsupported distribution: {0}. Supported: Debian, Ubuntu, CentOS, AlmaLinux, Rocky, Fedora."
[ru.os_unsupported]="Неподдерживаемый дистрибутив: {0}. Поддерживаются: Debian, Ubuntu, CentOS, AlmaLinux, Rocky, Fedora."
[en.py_missing]="python3 was not found and could not be installed."
[ru.py_missing]="python3 не найден и не может быть установлен."
[en.py_too_old]="Python {0} is too old. Mailchilla needs 3.9 or newer."
[ru.py_too_old]="Python {0} слишком старый. Mailchilla нужен 3.9 или новее."
[en.py_too_old_hint]="Ubuntu 20.04 ships Python 3.8. Upgrade the system, or install a newer Python yourself."
[ru.py_too_old_hint]="В Ubuntu 20.04 идёт Python 3.8. Обновите систему или поставьте более новый Python вручную."

[en.found_install]="Mailchilla is already installed in {0}."
[ru.found_install]="Mailchilla уже установлена в {0}."
[en.found_hint]="To update it run:  mailchilla update"
[ru.found_hint]="Для обновления выполните:  mailchilla update"
[en.found_ask]="Reinstall? The configuration and settings will be kept"
[ru.found_ask]="Переустановить? Конфигурация и настройки будут сохранены"

[en.h_xui]="Access to the 3x-ui panel"
[ru.h_xui]="Доступ к панели 3x-ui"
[en.h_panel]="Signing in to the Mailchilla panel"
[ru.h_panel]="Вход в панель Mailchilla"

[en.q_xui_url]="Address of the 3x-ui panel"
[ru.q_xui_url]="Адрес панели 3x-ui"
[en.q_xui_url_hint]="With the protocol and the port, for example http://1.2.3.4:2053"
[ru.q_xui_url_hint]="С протоколом и портом, например http://1.2.3.4:2053"
[en.q_auth]="How to authenticate"
[ru.q_auth]="Способ авторизации"
[en.q_auth_1]="Username and password"
[ru.q_auth_1]="Логин и пароль"
[en.pick]="Your choice"
[ru.pick]="Ваш выбор"
[en.q_auth_2]="API token (Settings -> Security -> API Token)"
[ru.q_auth_2]="API-токен (Настройки -> Безопасность -> API Token)"
[en.q_xui_user]="3x-ui username"
[ru.q_xui_user]="Логин 3x-ui"
[en.q_xui_pass]="3x-ui password"
[ru.q_xui_pass]="Пароль 3x-ui"
[en.q_xui_token]="API token"
[ru.q_xui_token]="API-токен"
[en.xui_checking]="Checking the connection..."
[ru.xui_checking]="Проверяем связь..."
[en.xui_ok]="The panel answers."
[ru.xui_ok]="Панель отвечает."
[en.xui_bad]="The panel did not answer. Installation continues — the address can be corrected later in .env."
[ru.xui_bad]="Панель не ответила. Установка продолжится — адрес можно поправить позже в .env."

[en.q_panel_user]="Username for the Mailchilla panel"
[ru.q_panel_user]="Логин для панели Mailchilla"
[en.q_panel_pass]="Password (empty = generate a strong one)"
[ru.q_panel_pass]="Пароль (пусто — сгенерировать надёжный)"
[en.q_panel_pass2]="Repeat the password"
[ru.q_panel_pass2]="Повторите пароль"
[en.pass_mismatch]="The passwords do not match."
[ru.pass_mismatch]="Пароли не совпадают."
[en.pass_quote]="A single quote in the password is not supported. Choose another one."
[ru.pass_quote]="Одинарная кавычка в пароле не поддерживается. Выберите другой."
[en.pass_short]="At least 8 characters, please."
[ru.pass_short]="Минимум 8 символов."
[en.pass_generated]="A password has been generated."
[ru.pass_generated]="Пароль сгенерирован."
[en.q_port]="Port for the panel"
[ru.q_port]="Порт для панели"
[en.port_bad]="A port is a number between 1 and 65535."
[ru.port_bad]="Порт — это число от 1 до 65535."
[en.port_busy]="Port {0} is occupied. Choose another one."
[ru.port_busy]="Порт {0} занят. Выберите другой."

[en.w_deps]="Installing packages, this may take a minute..."
[ru.w_deps]="Ставим пакеты, это может занять минуту..."
[en.w_clone]="Downloading version {0}..."
[ru.w_clone]="Скачиваем версию {0}..."
[en.w_venv]="Creating the virtual environment and installing dependencies..."
[ru.w_venv]="Создаём виртуальное окружение и ставим зависимости..."
[en.w_start]="Starting and waiting for the panel..."
[ru.w_start]="Запускаем и ждём панель..."

[en.start_ok]="The panel is up."
[ru.start_ok]="Панель поднялась."
[en.start_fail]="The service started but the panel does not answer on port {0}."
[ru.start_fail]="Служба запустилась, но панель не отвечает на порту {0}."
[en.start_logs]="Look at the log:  mailchilla log"
[ru.start_logs]="Посмотрите лог:  mailchilla log"

[en.done]="Mailchilla is installed"
[ru.done]="Mailchilla установлена"
[en.d_version]="Version"
[ru.d_version]="Версия"
[en.d_dir]="Directory"
[ru.d_dir]="Каталог"
[en.d_login]="Sign in"
[ru.d_login]="Вход"
[en.d_pass]="Password"
[ru.d_pass]="Пароль"
[en.d_save]="Write it down — it is stored in {0} and shown nowhere else."
[ru.d_save]="Запишите — он лежит в {0} и больше нигде не показывается."
[en.d_reach]="The panel listens on 127.0.0.1 and is not reachable from outside. Open a tunnel from your own machine:"
[ru.d_reach]="Панель слушает 127.0.0.1 и снаружи недоступна. Откройте туннель со своей машины:"
[en.d_then]="then open"
[ru.d_then]="затем откройте"
[en.d_next]="A banner in the panel will offer the setup wizard: the mailbox, the inbounds, the code word."
[ru.d_next]="Баннер в панели предложит пройти мастер настройки: почтовый ящик, inbound'ы, кодовое слово."
[en.d_lang]="The language of the letters is set separately from the language of the interface, on the Letters tab."
[ru.d_lang]="Язык писем задаётся отдельно от языка интерфейса, на вкладке «Письма»."
[en.d_cmd]="Managing it:"
[ru.d_cmd]="Управление:"
[en.d_cmd_hint]="a menu, or mailchilla restart | log | update | backup"
[ru.d_cmd_hint]="меню, либо mailchilla restart | log | update | backup"

[en.cancelled]="Cancelled."
[ru.cancelled]="Отменено."
[en.yes]="y"
[ru.yes]="д"
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

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
die() { printf '\n  %s✗%s %s\n\n' "$RED" "$N" "$1" >&2; exit 1; }
info() { printf '      %s%s%s\n' "$D" "$1" "$N"; }
warn() { printf '      %s!%s %s\n' "$YEL" "$N" "$1"; }

step() {
    STEP=$((STEP + 1))
    printf '  %s[%d/%d]%s %s%s%s ' "$D" "$STEP" "$STEPS" "$N" "$B" "$1" "$N"
}
ok() { printf '%s✓%s\n' "$GRN" "$N"; }

banner() {
    printf '\n'
    printf '  %s╭────────────────────────────────────────────────╮%s\n' "$MAG" "$N"
    printf '  %s│%s   %sM A I L C H I L L A%s                          %s│%s\n' "$MAG" "$N" "$B" "$N" "$MAG" "$N"
    printf '  %s╰────────────────────────────────────────────────╯%s\n' "$MAG" "$N"
    printf '     %s%s%s\n\n' "$D" "$(t tagline)" "$N"
}

rule() { printf '  %s────────────────────────────────────────────────%s\n' "$D" "$N"; }

heading() { printf '\n  %s%s%s\n' "$B$CYA" "$1" "$N"; }

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
ask() {
    # ask <prompt> <default> -> echoes the answer
    local prompt="$1" default="${2:-}" answer=""
    if [ -n "$default" ]; then
        printf '  %s?%s %s %s[%s]%s: ' "$CYA" "$N" "$prompt" "$D" "$default" "$N" >&2
    else
        printf '  %s?%s %s: ' "$CYA" "$N" "$prompt" >&2
    fi
    read -r answer </dev/tty
    printf '%s' "${answer:-$default}"
}

ask_secret() {
    local prompt="$1" answer=""
    printf '  %s?%s %s: ' "$CYA" "$N" "$prompt" >&2
    read -rs answer </dev/tty
    printf '\n' >&2
    printf '%s' "$answer"
}

confirm() {
    local prompt="$1" answer=""
    printf '  %s?%s %s [y/N]: ' "$CYA" "$N" "$prompt" >&2
    read -r answer </dev/tty
    case "${answer,,}" in
        y|yes|"$(t yes)"|д|да) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
choose_lang() {
    [ -n "$UI_LANG" ] && return
    printf '  %s?%s %s\n' "$CYA" "$N" "$(t ask_lang)"
    printf '      %s1%s  English\n' "$B" "$N"
    printf '      %s2%s  Русский\n' "$B" "$N"
    local answer=""
    printf '  %s>%s ' "$CYA" "$N"
    read -r answer </dev/tty || true
    case "$answer" in
        2|ru|RU|р|Р) UI_LANG="ru" ;;
        *) UI_LANG="en" ;;
    esac
    printf '\n'
}

detect_os() {
    [ -r /etc/os-release ] || die "$(t os_unknown)"
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
    OS_NAME="${PRETTY_NAME:-$OS_ID}"
    case "$OS_ID $OS_LIKE" in
        *debian*|*ubuntu*) PKG="apt" ;;
        *rhel*|*fedora*|*centos*) PKG="$(command -v dnf >/dev/null 2>&1 && echo dnf || echo yum)" ;;
        *) die "$(t os_unsupported "$OS_NAME")" ;;
    esac
}

install_packages() {
    local pkgs_apt="python3 python3-venv python3-pip git curl openssl"
    local pkgs_rpm="python3 python3-pip git curl openssl"
    if [ "$PKG" = "apt" ]; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        # shellcheck disable=SC2086
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $pkgs_apt >/dev/null
    else
        # shellcheck disable=SC2086
        "$PKG" install -y -q $pkgs_rpm >/dev/null 2>&1
    fi
}

check_python() {
    command -v python3 >/dev/null 2>&1 || die "$(t py_missing)"
    local major minor
    major="$(python3 -c 'import sys; print(sys.version_info[0])')"
    minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
    if [ "$major" -lt "$PY_MIN_MAJOR" ] || { [ "$major" -eq "$PY_MIN_MAJOR" ] && [ "$minor" -lt "$PY_MIN_MINOR" ]; }; then
        printf '\n  %s✗%s %s\n' "$RED" "$N" "$(t py_too_old "$major.$minor")" >&2
        printf '      %s%s%s\n\n' "$D" "$(t py_too_old_hint)" "$N" >&2
        exit 1
    fi
    PY_VERSION="$major.$minor"
}

port_free() {
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ! ss -ltn 2>/dev/null | grep -qE "[:.]$port[[:space:]]"
    else
        return 0
    fi
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
fetch_code() {
    local ref
    if [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" remote set-url origin "$REPO_URL"
        git -C "$INSTALL_DIR" fetch --tags --quiet --force
    else
        mkdir -p "$INSTALL_DIR"
        git clone --quiet "$REPO_URL" "$INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch --tags --quiet --force
    fi

    if [ -n "$REQ_BRANCH" ]; then
        ref="origin/$REQ_BRANCH"
        VERSION="$REQ_BRANCH"
    elif [ -n "$REQ_TAG" ]; then
        ref="$REQ_TAG"
        VERSION="$REQ_TAG"
    else
        ref="$(git -C "$INSTALL_DIR" tag -l 'v*' --sort=-v:refname | head -n 1)"
        if [ -z "$ref" ]; then
            # No tags yet — fall back to the default branch rather than failing.
            ref="origin/main"
            VERSION="main"
        else
            VERSION="$ref"
        fi
    fi
    info "$(t w_clone "$VERSION")"
    git -C "$INSTALL_DIR" checkout --quiet --force "$ref"
}

make_venv() {
    info "$(t w_venv)"
    if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
        python3 -m venv "$INSTALL_DIR/venv"
    fi
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" >/dev/null
}

write_env() {
    local file="$INSTALL_DIR/.env"
    local secret
    secret="$(openssl rand -hex 32)"
    umask 077
    # Values go through printf as arguments rather than into a heredoc: a
    # password is arbitrary text, and an unquoted heredoc would run whatever
    # backticks it happened to contain. Single quotes in the file itself stop
    # python-dotenv expanding a $ inside a value.
    {
        printf '%s\n' "# Written by install.sh. Only what Mailchilla needs before the panel"
        printf '%s\n' "# can come up lives here — everything else is configured in the panel"
        printf '%s\n\n' "# and kept in settings.json."
        printf "XUI_URL='%s'\n" "$XUI_URL"
        printf "XUI_USERNAME='%s'\n" "$XUI_USERNAME"
        printf "XUI_PASSWORD='%s'\n" "$XUI_PASSWORD"
        printf "XUI_API_TOKEN='%s'\n\n" "$XUI_API_TOKEN"
        printf "ADMIN_PANEL_USER='%s'\n" "$PANEL_USER"
        printf "ADMIN_PANEL_PASSWORD='%s'\n" "$PANEL_PASS"
        printf "ADMIN_PANEL_SECRET='%s'\n" "$secret"
        printf '%s\n' "ADMIN_PANEL_HOST=127.0.0.1"
        printf 'ADMIN_PANEL_PORT=%s\n\n' "$PANEL_PORT"
        printf '%s\n' "# Seeds the language on a first run; both are changed in the panel"
        printf '%s\n' "# afterwards and kept in settings.json. The interface and the letters"
        printf '%s\n' "# are set separately."
        printf 'PANEL_LANG=%s\n' "$UI_LANG"
        printf 'MAIL_LANG=%s\n\n' "$UI_LANG"
        printf '%s\n' "LOG_BUFFER_SIZE=2000"
        printf '%s\n' "LOG_FILE=logs/bot.log"
        printf '%s\n' "LOG_FILE_MAX_BYTES=5242880"
        printf '%s\n' "LOG_FILE_BACKUPS=3"
    } > "$file"
    umask 022
}

write_install_conf() {
    mkdir -p "$CONF_DIR"
    cat > "$CONF_FILE" <<CONFEOF
# State of this installation, written by install.sh and read by the
# \`mailchilla\` command. Not the application's configuration — that is .env
# and settings.json inside the install directory.
INSTALL_DIR=$INSTALL_DIR
INSTALL_LANG=$UI_LANG
SERVICE=$SERVICE
SERVICE_USER=$SERVICE_USER
REPO_URL=$REPO_URL
CONFEOF
    chmod 644 "$CONF_FILE"
}

write_unit() {
    cat > "$UNIT_PATH" <<UNITEOF
[Unit]
Description=Mailchilla — email bot and web panel for 3x-ui
Documentation=https://github.com/vogster/Mailchilla
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
# Not EnvironmentFile: the application reads .env itself through python-dotenv,
# and systemd parses such files by its own rules. Two parsers on one file
# disagree sooner or later.
ExecStart=$INSTALL_DIR/venv/bin/python run.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$INSTALL_DIR

[Install]
WantedBy=multi-user.target
UNITEOF
    systemctl daemon-reload
}

wait_for_panel() {
    local i
    for i in $(seq 1 30); do
        if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$PANEL_PORT/login" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------
ask_xui() {
    heading "$(t h_xui)"
    info "$(t q_xui_url_hint)"
    XUI_URL="$(ask "$(t q_xui_url)" "http://127.0.0.1:2053")"
    XUI_URL="${XUI_URL%/}"

    printf '\n  %s?%s %s\n' "$CYA" "$N" "$(t q_auth)"
    printf '      %s1%s  %s\n' "$B" "$N" "$(t q_auth_1)"
    printf '      %s2%s  %s\n' "$B" "$N" "$(t q_auth_2)"
    local mode
    mode="$(ask "$(t pick)" "1")"

    XUI_USERNAME=""; XUI_PASSWORD=""; XUI_API_TOKEN=""
    if [ "$mode" = "2" ]; then
        while [ -z "$XUI_API_TOKEN" ]; do
            XUI_API_TOKEN="$(ask_secret "$(t q_xui_token)")"
        done
    else
        XUI_USERNAME="$(ask "$(t q_xui_user)" "admin")"
        while [ -z "$XUI_PASSWORD" ]; do
            XUI_PASSWORD="$(ask_secret "$(t q_xui_pass)")"
        done
    fi

    printf '\n'
    info "$(t xui_checking)"
    if curl -fsSk -o /dev/null --max-time 6 "$XUI_URL/" 2>/dev/null; then
        printf '      %s✓%s %s\n' "$GRN" "$N" "$(t xui_ok)"
    else
        warn "$(t xui_bad)"
    fi
}

ask_panel() {
    heading "$(t h_panel)"
    PANEL_USER="$(ask "$(t q_panel_user)" "admin")"

    while :; do
        PANEL_PASS="$(ask_secret "$(t q_panel_pass)")"
        if [ -z "$PANEL_PASS" ]; then
            PANEL_PASS="$(openssl rand -base64 18 | tr -d '/+=\n')"
            GENERATED_PASS=1
            printf '      %s✓%s %s\n' "$GRN" "$N" "$(t pass_generated)"
            break
        fi
        case "$PANEL_PASS" in
            *\'*) warn "$(t pass_quote)"; continue ;;
        esac
        if [ "${#PANEL_PASS}" -lt 8 ]; then
            warn "$(t pass_short)"; continue
        fi
        local repeat
        repeat="$(ask_secret "$(t q_panel_pass2)")"
        if [ "$PANEL_PASS" != "$repeat" ]; then
            warn "$(t pass_mismatch)"; continue
        fi
        GENERATED_PASS=0
        break
    done

    while :; do
        PANEL_PORT="$(ask "$(t q_port)" "8080")"
        if ! [[ "$PANEL_PORT" =~ ^[0-9]+$ ]] || [ "$PANEL_PORT" -lt 1 ] || [ "$PANEL_PORT" -gt 65535 ]; then
            warn "$(t port_bad)"; continue
        fi
        if ! port_free "$PANEL_PORT"; then
            warn "$(t port_busy "$PANEL_PORT")"; continue
        fi
        break
    done
}

# ---------------------------------------------------------------------------
# Final report
# ---------------------------------------------------------------------------
summary() {
    local host
    host="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$host" ] || host="your-server"

    printf '\n'
    printf '  %s╭────────────────────────────────────────────────╮%s\n' "$GRN" "$N"
    printf '  %s│%s  %s%s%s\n' "$GRN" "$N" "$B" "$(t done)" "$N"
    printf '  %s╰────────────────────────────────────────────────╯%s\n\n' "$GRN" "$N"

    printf '     %s%-10s%s %s\n' "$D" "$(t d_version)" "$N" "$VERSION"
    printf '     %s%-10s%s %s\n' "$D" "$(t d_dir)" "$N" "$INSTALL_DIR"
    printf '     %s%-10s%s %s%s%s\n' "$D" "$(t d_login)" "$N" "$B" "$PANEL_USER" "$N"
    if [ "${GENERATED_PASS:-0}" = "1" ]; then
        printf '     %s%-10s%s %s%s%s\n' "$D" "$(t d_pass)" "$N" "$B$YEL" "$PANEL_PASS" "$N"
        printf '     %s%s%s\n' "$D" "$(t d_save "$INSTALL_DIR/.env")" "$N"
    fi

    printf '\n'
    rule
    printf '\n  %s\n\n' "$(t d_reach)"
    printf '     %s%sssh -L %s:127.0.0.1:%s root@%s%s\n\n' "$B" "$CYA" "$PANEL_PORT" "$PANEL_PORT" "$host" "$N"
    printf '  %s %shttp://localhost:%s%s\n' "$(t d_then)" "$B$CYA" "$PANEL_PORT" "$N"

    printf '\n'
    rule
    printf '\n  %s\n' "$(t d_next)"
    printf '  %s%s%s\n' "$D" "$(t d_lang)" "$N"
    printf '\n  %s %s%smailchilla%s  %s%s%s\n\n' "$(t d_cmd)" "$B" "$CYA" "$N" "$D" "$(t d_cmd_hint)" "$N"
}

# ---------------------------------------------------------------------------
main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --lang) UI_LANG="${2:-}"; shift 2 ;;
            --lang=*) UI_LANG="${1#*=}"; shift ;;
            --version) REQ_TAG="${2:-}"; shift 2 ;;
            --version=*) REQ_TAG="${1#*=}"; shift ;;
            --branch) REQ_BRANCH="${2:-}"; shift 2 ;;
            --branch=*) REQ_BRANCH="${1#*=}"; shift ;;
            *) shift ;;
        esac
    done
    case "$UI_LANG" in en|ru) ;; *) UI_LANG="" ;; esac

    banner
    choose_lang

    [ "$(id -u)" -eq 0 ] || die "$(t need_root)"
    command -v systemctl >/dev/null 2>&1 || die "$(t need_systemd)"

    if [ -d "$INSTALL_DIR/.git" ]; then
        printf '  %s!%s %s\n' "$YEL" "$N" "$(t found_install "$INSTALL_DIR")"
        printf '      %s%s%s\n\n' "$D" "$(t found_hint)" "$N"
        confirm "$(t found_ask)" || die "$(t cancelled)"
        printf '\n'
    fi

    detect_os
    ask_xui
    ask_panel

    printf '\n'
    rule
    printf '\n'

    step "$(t s_deps)"; printf '\n'; info "$(t w_deps)"
    install_packages; check_python
    printf '      %s✓%s Python %s\n' "$GRN" "$N" "$PY_VERSION"

    step "$(t s_user)"
    id -u "$SERVICE_USER" >/dev/null 2>&1 || \
        useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" "$SERVICE_USER" 2>/dev/null || \
        useradd --system --shell /sbin/nologin --home-dir "$INSTALL_DIR" "$SERVICE_USER"
    ok

    step "$(t s_code)"; printf '\n'
    fetch_code
    printf '      %s✓%s\n' "$GRN" "$N"

    step "$(t s_venv)"; printf '\n'
    make_venv
    printf '      %s✓%s\n' "$GRN" "$N"

    step "$(t s_conf)"
    [ -f "$INSTALL_DIR/.env" ] && cp -a "$INSTALL_DIR/.env" "$INSTALL_DIR/.env.bak.$(date +%Y%m%d%H%M%S)"
    write_env
    write_install_conf
    mkdir -p "$INSTALL_DIR/logs"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
    chmod 600 "$INSTALL_DIR/.env"
    ok

    step "$(t s_service)"
    write_unit
    install -m 755 "$INSTALL_DIR/mailchilla.sh" "$BIN_PATH"
    systemctl enable "$SERVICE" >/dev/null 2>&1
    ok

    step "$(t s_start)"; printf '\n'; info "$(t w_start)"
    systemctl restart "$SERVICE"
    if wait_for_panel; then
        printf '      %s✓%s %s\n' "$GRN" "$N" "$(t start_ok)"
        summary
    else
        printf '      %s✗%s %s\n' "$RED" "$N" "$(t start_fail "$PANEL_PORT")"
        printf '      %s%s%s\n\n' "$D" "$(t start_logs)" "$N"
        exit 1
    fi
}

main "$@"
