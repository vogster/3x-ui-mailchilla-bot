"""
The letter texts, editable from the panel.

Every visible string is gathered here so it can be rewritten without touching
the markup. The defaults come in each language the project speaks, and the
letters go out in the language chosen on the «Letters» tab — which is separate
from the language of the panel itself.

They are kept apart from settings.json: there are a dozen settings and close to
a hundred strings, and the reset button on the settings tab has no business
wiping out a translation.

Edits are stored per language, so rewriting the English welcome letter leaves
the Russian one alone.

Substitutions in curly braces are allowed; which ones is stated at each field.
An unknown substitution is left in the text as it stands. Double asterisks make
a fragment bold: **like this**.
"""
import json
import logging
import os
import threading

import i18n

logger = logging.getLogger(__name__)

TEXTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "email_texts.json")

_lock = threading.RLock()
# {lang: {key: text}} — only the strings that differ from the default.
_overrides = {}

# A field: (key, caption in the interface, kind, hint)
# kind: "line" for a single-line field, "text" for a multi-line one,
# "switch" for a setting rather than a text — see SWITCH below.
LINE, TEXT = "line", "text"
# A switch standing among the texts. Its key is a settings.py key, not a text
# key: whether a block is sent at all is one setting for the whole
# installation, not a string that differs per language. It is described here
# rather than on the settings page so that it sits beside the texts it governs,
# which is where somebody editing the letter will look for it.
SWITCH = "switch"

# The captions and hints below are interface strings and go through the panel's
# own catalogue; the letter texts themselves live in DEFAULTS_BY_LANG.
GROUPS = [
    {
        "id": "common",
        "title": "General [letter group]",
        "hint": "Appears in every letter.",
        "fields": [
            ("common.footer", "Letter footer", TEXT,
             "Substitution: {service}. Every line is a paragraph of its own."),
            ("common.support", "The support line in the footer", LINE,
             "Shown in every letter, under the footer, when a support address is "
             "set on the General tab. Substitution: {support}."),
            ("common.unit_gb", "Gigabytes", LINE,
             "Substitution: {gb}. The unit beside a traffic figure."),
        ],
    },
    {
        "id": "welcome",
        "title": "Registration",
        "hint": "The letter with the subscription link: after registering, and when it is sent again.",
        "fields": [
            ("welcome.subject_new", "Subject — a new subscription", LINE, "Substitution: {service}"),
            ("welcome.subject_again", "Subject — sent again", LINE, "Substitution: {service}"),
            ("welcome.greeting", "Greeting", LINE, ""),
            ("welcome.intro_new", "Opening — a new subscription", TEXT, "Substitution: {service}"),
            ("welcome.intro_again", "Opening — sent again", TEXT, "Substitution: {service}"),
            ("welcome.label_expire", "The 'expires' line", LINE, ""),
            ("welcome.label_limit", "The 'limit' line", LINE, ""),
            ("welcome.value_forever", "The 'never expires' value", LINE, ""),
            ("welcome.value_days", "The expiry value", LINE, "Substitution: {days}"),
            ("welcome.value_unlimited", "The 'unlimited' value", LINE, ""),
            ("welcome.value_gb", "The limit value", LINE, "Substitution: {gb}"),
            ("welcome.button_main", "The main button", LINE, ""),
            ("welcome.apps_intro", "The line above the app buttons", LINE, ""),
            ("welcome.button_app", "An app button", LINE, "Substitution: {app}"),
            ("WELCOME_MANUAL_ENABLED", "Show the instructions button", SWITCH,
             "The button beside the app ones, leading to the page that explains how "
             "to connect. The link itself is set on the General tab; with no link "
             "there is no button, switch or no switch."),
            ("WELCOME_QR_ENABLED", "Send the QR code", SWITCH,
             "The subscription link as a code, for the reader who opened the letter on a "
             "computer and would otherwise be carrying the link across to their phone by "
             "hand. It is drawn on the server and travels inside the letter, so nothing "
             "is fetched from anywhere."),
            ("welcome.qr_intro", "The line above the QR code", LINE,
             "The code carries the same subscription link as the button above it."),
            ("welcome.button_manual", "The instructions button", LINE,
             "Beside the app buttons. Shown when a link to the instructions is set "
             "on the General tab and the switch above is on."),
            ("welcome.support", "The support line", LINE,
             "Its own line in this letter, where somebody is setting a connection up "
             "for the first time. Shown when a support address is set. "
             "Substitution: {support}."),
            ("welcome.manual_intro", "The line above the link", LINE, ""),
            ("welcome.commands_title", "The commands block heading", LINE, ""),
            ("welcome.commands_text", "The commands block text", TEXT, ""),
        ],
    },
    {
        "id": "status",
        "title": "Subscription status",
        "hint": "The answer to the /status command.",
        "fields": [
            ("status.subject", "Subject", LINE, "Substitution: {service}"),
            ("status.intro", "Opening", TEXT, "Substitution: {email}"),
            ("status.label_status", "The 'status' line", LINE, ""),
            ("status.value_active", "The 'active' value", LINE, ""),
            ("status.value_inactive", "The 'inactive' value", LINE, ""),
            ("status.label_tariff", "The 'tariff' line", LINE,
             "Left out for a client who is on no tariff — anybody registered "
             "before tariffs existed, or added by hand in 3x-ui."),
            ("status.label_expiry", "The 'valid until' line", LINE, ""),
            ("status.value_forever", "The 'never expires' value", LINE, ""),
            ("status.label_used", "The 'used' line", LINE, ""),
            ("status.used_of", "The value with a limit", LINE, "Substitutions: {used}, {total}"),
            ("status.used_nolimit", "The value without a limit", LINE, "Substitution: {used}"),
            ("status.meter_text", "The caption under the bar", LINE, "Substitution: {percent}"),
            ("status.outro", "Closing", TEXT, ""),
        ],
    },
    {
        "id": "help",
        "title": "Help",
        "hint": "The answer to the /help command.",
        "fields": [
            ("help.subject", "Subject", LINE, "Substitution: {service}"),
            ("help.intro", "Opening", TEXT, ""),
            ("help.cmd_status", "What /status does", LINE, ""),
            ("help.cmd_help", "What /help does", LINE, ""),
            ("help.sub_label", "The line above the link", LINE, ""),
            ("help.not_registered", "The text for people with no subscription", TEXT,
             "Substitution: {service}"),
            ("help.howto_title", "The instructions heading", LINE, ""),
            ("help.howto_steps", "The instruction steps", TEXT,
             "Every line is a step of the list. A line indented by two spaces becomes "
             "an explanation under the step above it."),
        ],
    },
    {
        "id": "broadcast",
        "title": "Broadcast",
        "hint": "A mass broadcast and a personal letter to one client.",
        "fields": [
            ("broadcast.signature", "Signature", TEXT, "Substitution: {service}"),
            ("broadcast.kind_plain", "The name of the 'plain' kind", LINE, ""),
            ("broadcast.kind_info", "The name of the 'information' kind", LINE, ""),
            ("broadcast.kind_warning", "The name of the 'warning' kind", LINE, ""),
            ("broadcast.kind_urgent", "The name of the 'urgent' kind", LINE, ""),
        ],
    },
    {
        "id": "notice",
        "title": "Service replies",
        "hint": "The bot's short answers: errors, refusals, the broadcast report.",
        "fields": [
            ("notice.unknown_subject", "Command not understood — subject", LINE, ""),
            ("notice.unknown_text", "Command not understood — text", TEXT,
             "Substitution: {subject}. Every line is a paragraph."),
            ("notice.unknown_bullets", "Command not understood — list", TEXT,
             "Every line is an item of the list."),
            ("notice.ambiguous_subject", "Several code words — subject", LINE, ""),
            ("notice.ambiguous_text", "Several code words — text", TEXT,
             "Sent when one letter carries the words of several tariffs, so that "
             "nothing is guessed. Substitution: {words}. Every line is a paragraph."),
            ("notice.not_registered_subject", "No subscription — subject", LINE, ""),
            ("notice.not_registered_text", "No subscription — text", TEXT, "Substitution: {service}"),
            ("notice.status_error_subject", "Status error — subject", LINE, ""),
            ("notice.status_error_text", "Status error — text", TEXT, ""),
            ("notice.create_error_subject", "Registration error — subject", LINE, ""),
            ("notice.create_error_text", "Registration error — text", TEXT, ""),
            ("notice.broadcast_done_subject", "Broadcast report — subject", LINE, ""),
            ("notice.broadcast_done_text", "Broadcast report — text", TEXT, "Substitution: {count}"),
            ("notice.broadcast_empty_subject", "Empty broadcast — subject", LINE, ""),
            ("notice.broadcast_empty_text", "Empty broadcast — text", TEXT, ""),
            ("notice.broadcast_default_subject", "Broadcast without a subject — subject", LINE,
             "Used when the broadcast was sent with no subject of its own."),
        ],
    },
]

# The letter texts themselves, one set per language. A language missing a key
# falls back to English, so a half-finished translation still sends a letter.
DEFAULTS_BY_LANG = {
    "en": {
        "common.footer":
            "This is an automatic letter from {service}.\n"
            "If you did not ask for it, simply ignore it.",
        "common.support": "Questions: {support}",
        "common.unit_gb": "{gb} GB",

        "welcome.subject_new": "Your {service} subscription is active",
        "welcome.subject_again": "Your {service} subscription",
        "welcome.greeting": "Hello!",
        "welcome.intro_new":
            "Your subscription to {service} is registered. Below are the terms of "
            "your plan and the link to import into your client.",
        "welcome.intro_again":
            "Here is the link to your {service} subscription once more. Below are the "
            "terms of your plan and the link to import into your client.",
        "welcome.label_expire": "Valid until",
        "welcome.label_limit": "Traffic limit",
        "welcome.value_forever": "No expiry",
        "welcome.value_days": "{days} days",
        "welcome.value_unlimited": "Unlimited",
        "welcome.value_gb": "{gb} GB",
        "welcome.button_main": "Get the settings",
        "welcome.apps_intro": "Or add the subscription straight to an app:",
        "welcome.button_app": "Add to {app}",
        "welcome.qr_intro": "Or scan the code in the app:",
        "welcome.button_manual": "How to set it up",
        "welcome.support": "Something not working? Write to us: **{support}**",
        "welcome.manual_intro": "If the button did not work, copy the subscription link by hand:",
        "welcome.commands_title": "Bot commands",
        "welcome.commands_text":
            "Write to this address with **/status** in your letter to see how much traffic "
            "is left and when the subscription ends, or **/help** for the setup instructions.",

        "status.subject": "{service} subscription status",
        "status.intro": "Here is where the subscription for **{email}** stands.",
        "status.label_status": "Status",
        "status.value_active": "Active",
        "status.value_inactive": "Blocked or expired",
        "status.label_tariff": "Tariff",
        "status.label_expiry": "Valid until",
        "status.value_forever": "No expiry",
        "status.label_used": "Used",
        "status.used_of": "{used} of {total}",
        "status.used_nolimit": "{used} (no limit)",
        "status.meter_text": "{percent}% of the limit spent",
        "status.outro":
            "To import the subscription again, send a letter with **/help** in it — "
            "we will reply with the link and the instructions.",

        "help.subject": "{service} help",
        "help.intro":
            "You can manage your subscription by writing to this address — the command "
            "can go in the subject or in the body of the letter.",
        "help.cmd_status": "the traffic left and when the subscription ends",
        "help.cmd_help": "this letter, with the instructions",
        "help.sub_label": "Your subscription link:",
        "help.not_registered":
            "You are not registered with {service} yet. Send a letter with the code word "
            "and we will reply with the settings right away.",
        "help.howto_title": "How to connect",
        "help.howto_steps":
            "Install a client for your system:\n"
            "  Windows and Linux — Nekoray, v2rayN or Clash\n"
            "  Android — v2rayNG or NekoBox\n"
            "  iOS and macOS — Streisand, Shadowrocket or V2Box\n"
            "Copy the subscription link from this letter.\n"
            "In the app open «Subscriptions» or «Import from clipboard» and paste it.\n"
            "Refresh the subscription — the server list loads by itself.",

        "broadcast.signature": "Kind regards,\nthe {service} team",
        "broadcast.kind_plain": "Plain",
        "broadcast.kind_info": "Information",
        "broadcast.kind_warning": "Warning",
        "broadcast.kind_urgent": "Urgent",

        "notice.unknown_subject": "The command was not understood",
        "notice.unknown_text":
            "We received your letter titled «{subject}», but could not tell what to do.\n"
            "Here is what you can write to this address:",
        "notice.unknown_bullets":
            "the code word — to register a subscription\n"
            "/status — how much traffic is left and when it ends\n"
            "/help — the instructions for setting up an app",
        "notice.ambiguous_subject": "Which subscription did you mean?",
        "notice.ambiguous_text":
            "Your letter carries more than one code word: {words}.\n"
            "Each opens a different subscription, so we would rather ask than guess. "
            "Write again with the one you want and nothing else.",
        "notice.not_registered_subject": "You are not registered yet",
        "notice.not_registered_text":
            "We could not find your subscription with {service}.\n"
            "Send a letter with the code word and we will reply with the settings right away.",
        "notice.status_error_subject": "Could not fetch the status",
        "notice.status_error_text":
            "Your subscription was found, but the server did not return its statistics.\n"
            "Please write to the administrator.",
        "notice.create_error_subject": "Could not create the subscription",
        "notice.create_error_text":
            "Unfortunately the subscription could not be created automatically.\n"
            "Try sending the code word again in a little while, or write to the administrator.",
        "notice.broadcast_done_subject": "The broadcast is finished",
        "notice.broadcast_done_text": "Letters sent: {count}.\nThe text of the broadcast:",
        "notice.broadcast_empty_subject": "The broadcast was not sent",
        "notice.broadcast_empty_text":
            "The text of the broadcast is empty.\n"
            "Write your message after the /broadcast command — in the subject or in the body.",
        "notice.broadcast_default_subject": "An important notice from the VPN service",
    },
    "ru": {
        "common.footer":
            "Это автоматическое письмо от сервиса {service}.\n"
            "Если вы его не запрашивали, просто проигнорируйте.",
        "common.support": "Вопросы: {support}",
        "common.unit_gb": "{gb} ГБ",

        "welcome.subject_new": "Ваша подписка {service} активирована",
        "welcome.subject_again": "Ваша подписка {service}",
        "welcome.greeting": "Здравствуйте!",
        "welcome.intro_new":
            "Ваша подписка в сервисе {service} успешно зарегистрирована. "
            "Ниже — параметры тарифа и ссылка для импорта в клиент.",
        "welcome.intro_again":
            "Высылаем ссылку на вашу подписку в сервисе {service} ещё раз. "
            "Ниже — параметры тарифа и ссылка для импорта в клиент.",
        "welcome.label_expire": "Срок действия",
        "welcome.label_limit": "Лимит трафика",
        "welcome.value_forever": "Бессрочно",
        "welcome.value_days": "{days} дней",
        "welcome.value_unlimited": "Безлимит",
        "welcome.value_gb": "{gb} ГБ",
        "welcome.button_main": "Получить настройки",
        "welcome.apps_intro": "Или добавьте подписку прямо в приложение:",
        "welcome.button_app": "Добавить в {app}",
        "welcome.qr_intro": "Или отсканируйте код в приложении:",
        "welcome.button_manual": "Как настроить",
        "welcome.support": "Что-то не работает? Напишите нам: **{support}**",
        "welcome.manual_intro": "Если кнопка не сработала, скопируйте ссылку подписки вручную:",
        "welcome.commands_title": "Команды бота",
        "welcome.commands_text":
            "Отправьте письмо на этот адрес с текстом **/status**, чтобы узнать остаток "
            "трафика и срок подписки, или **/help** — чтобы получить инструкцию по настройке.",

        "status.subject": "Статус подписки {service}",
        "status.intro": "Актуальная информация о подписке для **{email}**.",
        "status.label_status": "Статус",
        "status.value_active": "Активна",
        "status.value_inactive": "Заблокирована или истекла",
        "status.label_tariff": "Тариф",
        "status.label_expiry": "Действует до",
        "status.value_forever": "Бессрочно",
        "status.label_used": "Использовано",
        "status.used_of": "{used} из {total}",
        "status.used_nolimit": "{used} (без лимита)",
        "status.meter_text": "Израсходовано {percent}% лимита",
        "status.outro":
            "Чтобы импортировать подписку заново, отправьте письмо с текстом **/help** — "
            "пришлём ссылку и инструкцию.",

        "help.subject": "Справка {service}",
        "help.intro":
            "Подпиской можно управлять письмами на этот адрес — команду достаточно "
            "написать в теме или в тексте.",
        "help.cmd_status": "остаток трафика и срок подписки",
        "help.cmd_help": "это письмо с инструкцией",
        "help.sub_label": "Ваша ссылка подписки:",
        "help.not_registered":
            "Вы ещё не зарегистрированы в сервисе {service}. Отправьте письмо "
            "с кодовым словом, и мы сразу пришлём настройки.",
        "help.howto_title": "Как подключиться",
        "help.howto_steps":
            "Установите клиент для своей системы:\n"
            "  Windows и Linux — Nekoray, v2rayN или Clash\n"
            "  Android — v2rayNG или NekoBox\n"
            "  iOS и macOS — Streisand, Shadowrocket или V2Box\n"
            "Скопируйте ссылку подписки из этого письма.\n"
            "В приложении выберите «Подписки» или «Импорт из буфера» и вставьте её.\n"
            "Обновите подписку — список серверов загрузится автоматически.",

        "broadcast.signature": "С уважением,\nадминистрация {service}",
        "broadcast.kind_plain": "Обычное",
        "broadcast.kind_info": "Информация",
        "broadcast.kind_warning": "Предупреждение",
        "broadcast.kind_urgent": "Срочное",

        "notice.unknown_subject": "Не удалось распознать команду",
        "notice.unknown_text":
            "Мы получили ваше письмо с темой «{subject}», но не поняли, что нужно сделать.\n"
            "Вот что можно отправить письмом на этот адрес:",
        "notice.unknown_bullets":
            "кодовое слово — чтобы зарегистрировать подписку\n"
            "/status — узнать остаток трафика и срок действия\n"
            "/help — получить инструкцию по настройке приложений",
        "notice.ambiguous_subject": "Какая подписка нужна?",
        "notice.ambiguous_text":
            "В вашем письме несколько кодовых слов: {words}.\n"
            "Каждое открывает свою подписку, поэтому мы лучше переспросим, чем угадаем. "
            "Отправьте письмо ещё раз, оставив в нём только нужное слово.",
        "notice.not_registered_subject": "Вы ещё не зарегистрированы",
        "notice.not_registered_text":
            "Мы не нашли вашу подписку в сервисе {service}.\n"
            "Отправьте письмо с кодовым словом, и мы сразу пришлём настройки.",
        "notice.status_error_subject": "Не удалось получить статус",
        "notice.status_error_text":
            "Ваша подписка найдена, но сервер не отдал по ней статистику.\n"
            "Пожалуйста, напишите администратору.",
        "notice.create_error_subject": "Не удалось создать подписку",
        "notice.create_error_text":
            "К сожалению, подписку не удалось создать автоматически.\n"
            "Попробуйте отправить кодовое слово ещё раз чуть позже или напишите администратору.",
        "notice.broadcast_done_subject": "Рассылка завершена",
        "notice.broadcast_done_text": "Отправлено писем: {count}.\nТекст рассылки:",
        "notice.broadcast_empty_subject": "Рассылка не отправлена",
        "notice.broadcast_empty_text":
            "Текст рассылки пуст.\n"
            "Напишите сообщение после команды /broadcast — в теме письма или в тексте.",
        "notice.broadcast_default_subject": "Важное уведомление от VPN-сервиса",
    },
}

# Flat lookups over the structure itself, language aside.
FIELD_KIND = {key: kind for g in GROUPS for key, _, kind, _ in g["fields"]
              if kind != SWITCH}
GROUP_BY_ID = {g["id"]: g for g in GROUPS}
ALL_KEYS = frozenset(FIELD_KIND)


def lang(code: str = None) -> str:
    """The language a call works in: the one asked for, or the letters' own."""
    return i18n.normalise(code) if code else i18n.mail_lang()


def defaults(code: str = None) -> dict:
    """The untouched texts of one language, English filling any gap."""
    base = dict(DEFAULTS_BY_LANG[i18n.DEFAULT])
    base.update(DEFAULTS_BY_LANG.get(lang(code), {}))
    return base


def default(key: str, code: str = None) -> str:
    return defaults(code).get(key, "")


def load():
    """Reads email_texts.json. Called when the process starts."""
    global _overrides
    with _lock:
        data = {}
        if os.path.exists(TEXTS_PATH):
            try:
                with open(TEXTS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception as e:
                logger.error(f"Could not read {TEXTS_PATH}: {e}. Falling back to the default letter texts.")
                data = {}

        # A file written before the letters had languages is flat: the keys are
        # the text keys themselves. Those edits were made against the Russian
        # originals, so that is where they belong.
        if any(key in ALL_KEYS for key in data):
            logger.info("email_texts.json is in the old single-language shape; "
                        "treating the edits as Russian.")
            data = {"ru": data}

        clean = {}
        for code, values in data.items():
            code = str(code).strip().lower()
            if code not in i18n.LANGS:
                logger.warning(f"Letter texts for the unknown language {code!r} are ignored.")
                continue
            if not isinstance(values, dict):
                logger.warning(f"Letter texts for {code!r} are not an object; ignoring them.")
                continue
            base = defaults(code)
            for key, value in values.items():
                if key not in ALL_KEYS:
                    logger.warning(f"Text {key} from email_texts.json is unused; ignoring it.")
                    continue
                text = str(value)
                # An empty string is allowed: that is how an optional block is removed.
                if text != base.get(key, ""):
                    clean.setdefault(code, {})[key] = text

        _overrides = clean
        total = sum(len(v) for v in clean.values())
        if total:
            logger.info(f"Applied edited letter texts: {total} of them.")
        return {k: dict(v) for k, v in _overrides.items()}


def get(key: str, code: str = None) -> str:
    """The current value of a string: the edited one, or the default."""
    code = lang(code)
    with _lock:
        edited = _overrides.get(code, {})
        if key in edited:
            return edited[key]
    return default(key, code)


def is_changed(key: str, code: str = None) -> bool:
    with _lock:
        return key in _overrides.get(lang(code), {})


def save(values: dict, code: str = None):
    """
    Saves the edited strings of one language. Ones matching the original are not
    stored, so the file holds only what has actually been rewritten.
    """
    code = lang(code)
    base = defaults(code)
    with _lock:
        merged = {k: dict(v) for k, v in _overrides.items()}
        current = merged.setdefault(code, {})
        for key, value in values.items():
            if key not in ALL_KEYS:
                continue
            text = str(value if value is not None else "").replace("\r\n", "\n")
            if text == base.get(key, ""):
                current.pop(key, None)
            else:
                current[key] = text
        _write(merged)
        return dict(current)


def reset(group_id: str = None, code: str = None):
    """Resets the texts of one letter, or all of them at once, in one language."""
    code = lang(code)
    with _lock:
        merged = {k: dict(v) for k, v in _overrides.items()}
        if group_id is None:
            merged[code] = {}
        else:
            keys = {key for key, _, _, _ in GROUP_BY_ID[group_id]["fields"]}
            merged[code] = {k: v for k, v in merged.get(code, {}).items() if k not in keys}
        _write(merged)
        return dict(merged[code])


def _write(merged: dict):
    global _overrides
    # A language with nothing rewritten leaves no empty object behind.
    merged = {code: values for code, values in merged.items() if values}
    tmp = TEXTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, TEXTS_PATH)
    _overrides = merged
    total = sum(len(v) for v in merged.values())
    logger.info(f"Letter texts saved. Strings changed: {total}.")


def changed_count(group_id: str, code: str = None) -> int:
    keys = {key for key, _, _, _ in GROUP_BY_ID[group_id]["fields"]}
    with _lock:
        edited = _overrides.get(lang(code), {})
        return sum(1 for k in edited if k in keys)
