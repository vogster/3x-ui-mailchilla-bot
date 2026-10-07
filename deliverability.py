"""
Whether the bot's letters are likely to arrive, rather than land in Spam.

A letter with the subscription link that the client never sees is the most
expensive kind of failure there is, and the quietest: nothing errors, the
client simply decides the service does not work. Two looks:

- **DNS of the sending domain.** SPF says which servers may send for it, DMARC
  what a receiver should do with a letter that fails. Both are a single TXT
  record each and can be read from anywhere. DKIM cannot: its record sits
  under a selector only the sending server knows.
- **A letter to itself.** The bot sends a probe to its own mailbox and reads
  it back over IMAP. The receiving server writes what it concluded about SPF,
  DKIM and DMARC into Authentication-Results, and that verdict — not a guess
  from DNS — is what the client's server will have reached as well. The probe
  is from the bot's own address, so the bot's loop takes it for automatic mail
  and leaves it alone.

Every finding is a (level, text) pair: "ok", "warn", "bad" or "info", the text
in the panel's language — this is read by the administrator.
"""
import email
import imaplib
import logging
import re
import secrets
import time
from collections import namedtuple

import config
import i18n
import mailer
import templates

logger = logging.getLogger(__name__)

Finding = namedtuple("Finding", ["level", "text"])

# Mailbox providers whose own domains people send from. Their SPF, DKIM and
# DMARC are the provider's business and are set up; what is worth saying
# instead is that a free mailbox has sending limits of its own.
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yandex.ru", "yandex.com", "ya.ru", "mail.ru",
    "bk.ru", "list.ru", "inbox.ru", "internet.ru", "rambler.ru", "outlook.com",
    "hotmail.com", "live.com", "icloud.com", "me.com", "yahoo.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.de", "zoho.com",
}

# The SPF include that names a provider, by the SMTP server's domain. A domain
# whose SPF lacks it most likely does not allow the server the bot sends
# through — the commonest way a custom domain ends up in Spam.
SPF_INCLUDES = {
    "yandex.ru": "_spf.yandex.net", "yandex.com": "_spf.yandex.net",
    "gmail.com": "_spf.google.com", "google.com": "_spf.google.com",
    "mail.ru": "_spf.mail.ru",
    "office365.com": "spf.protection.outlook.com", "outlook.com": "spf.protection.outlook.com",
    "zoho.com": "zoho.com", "zoho.eu": "zoho.eu",
    "sendgrid.net": "sendgrid.net", "mailgun.org": "mailgun.org",
    "beget.com": "beget.com", "timeweb.ru": "timeweb.ru",
}

PROBE_WAIT_S = 30


def sender_address() -> str:
    for value in (config.SMTP_USER, config.IMAP_USER):
        if value and "@" in value:
            return value.strip().lower()
    return ""


def _txt(name: str) -> list:
    """The TXT records of a name, joined; [] when there are none."""
    import dns.resolver
    try:
        answer = dns.resolver.resolve(name, "TXT", lifetime=8)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]


def _smtp_domain() -> str:
    host = (config.SMTP_SERVER or "").strip().lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def check_dns() -> list:
    address = sender_address()
    if not address:
        if not (config.SMTP_USER or "").strip():
            return [Finding("bad", i18n.t("The SMTP login is not set: the bot cannot send anything yet."))]
        return [Finding("bad", i18n.t("The SMTP login is not an address, so there is no sending domain to check."))]
    domain = address.split("@", 1)[1]
    if domain in FREE_DOMAINS:
        return [Finding("info", i18n.t(
            "The bot sends from {domain}, a public mail service: its SPF, DKIM and DMARC are the "
            "service's and are in order. Its limit is volume — a free mailbox may refuse or slow "
            "down a few hundred letters a day, which a broadcast reaches quickly.", domain=domain))]

    found = []
    try:
        spf = [r for r in _txt(domain) if r.lower().startswith("v=spf1")]
        dmarc = [r for r in _txt(f"_dmarc.{domain}") if r.lower().startswith("v=dmarc1")]
    except Exception as e:
        return [Finding("warn", i18n.t("Could not ask DNS about {domain}: {error}", domain=domain, error=e))]

    if not spf:
        found.append(Finding("bad", i18n.t(
            "{domain} has no SPF record. Receivers cannot tell which servers may send for it, "
            "and many put such letters in Spam.", domain=domain)))
    elif len(spf) > 1:
        found.append(Finding("bad", i18n.t(
            "{domain} has {n} SPF records; there must be exactly one, or SPF fails altogether.",
            domain=domain, n=len(spf))))
    else:
        record = spf[0]
        found.append(Finding("ok", i18n.t("SPF: {record}", record=record)))
        # "all" bare or with "+": anybody may send. "-all", "~all" and "?all"
        # have their sign attached and do not match.
        if re.search(r"(^|\s)\+?all\b", record):
            found.append(Finding("bad", i18n.t("The SPF record ends in +all, which lets anybody send as {domain}.",
                                               domain=domain)))
        include = SPF_INCLUDES.get(_smtp_domain())
        # Any include under the provider's own domain counts: Google's own
        # record is made of _netblocks.google.com and its siblings, and a
        # domain that lists those directly allows the same servers.
        root = ".".join(include.split(".")[-2:]) if include else ""
        if include and not re.search(r"include:[\w.-]*" + re.escape(root) + r"\b", record):
            found.append(Finding("warn", i18n.t(
                "The bot sends through {server}, but the SPF record does not mention {include}. "
                "Add include:{include} to it.", server=config.SMTP_SERVER, include=include)))

    if not dmarc:
        found.append(Finding("warn", i18n.t(
            "{domain} has no DMARC record. Gmail and Yandex expect one from anybody sending in "
            "numbers; even \"v=DMARC1; p=none\" is better than nothing.", domain=domain)))
    else:
        policy = re.search(r"\bp=(\w+)", dmarc[0])
        found.append(Finding("ok", i18n.t("DMARC: {record}", record=dmarc[0])))
        if policy and policy.group(1).lower() in ("quarantine", "reject"):
            found.append(Finding("info", i18n.t(
                "DMARC asks receivers to {policy} letters that fail it — so a letter without a "
                "matching DKIM signature or SPF pass does not arrive. The probe below shows "
                "whether the bot's do.", policy=policy.group(1).lower())))
    return found


def _results(raw_headers: bytes) -> dict:
    """spf/dkim/dmarc verdicts from the Authentication-Results headers."""
    msg = email.message_from_bytes(raw_headers)
    verdicts = {}
    for header in msg.get_all("Authentication-Results") or []:
        for method, verdict in re.findall(r"\b(spf|dkim|dmarc)=(\w+)", header, re.I):
            verdicts.setdefault(method.lower(), verdict.lower())
    return verdicts


def probe(wait_s: int = PROBE_WAIT_S) -> list:
    """Sends a letter to the bot's own mailbox and reads what its server made of it."""
    address = sender_address()
    if not address or not config.IMAP_USER:
        return [Finding("bad", i18n.t("The mailbox is not set up, so there is nothing to send the probe to."))]
    token = secrets.token_hex(6)
    subject = f"{config.APP_NAME} deliverability probe {token}"
    letter = templates.get_notice_email(title=subject, paragraphs=[i18n.t(
        "A check of how this mailbox's server judges the bot's letters. It can be deleted.")])
    try:
        mailer.send_email_reply(address, subject, letter)
    except Exception as e:
        return [Finding("bad", i18n.t("The probe could not be sent: {error}", error=e))]

    deadline = time.time() + wait_s
    mail = None
    try:
        mail = imaplib.IMAP4_SSL(config.IMAP_SERVER, config.IMAP_PORT, timeout=15)
        mail.login(config.IMAP_USER, config.IMAP_PASSWORD)
        while time.time() < deadline:
            mail.select("inbox")
            status, data = mail.search(None, "SUBJECT", f'"{token}"')
            ids = data[0].split() if status == "OK" and data and data[0] else []
            if ids:
                status, fetched = mail.fetch(ids[-1], "(BODY.PEEK[HEADER])")
                mail.store(ids[-1], "+FLAGS", "\\Seen")
                return _judge(_results(fetched[0][1]))
            time.sleep(3)
    except Exception as e:
        return [Finding("warn", i18n.t("The probe went out, but the mailbox could not be read back: {error}",
                                       error=e))]
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass
    return [Finding("warn", i18n.t(
        "The probe did not reach the Inbox within {n} seconds. Look for it in the Spam folder — if "
        "it is there, so are the bot's letters to some clients.", n=wait_s))]


def _judge(verdicts: dict) -> list:
    if not verdicts:
        return [Finding("info", i18n.t(
            "The probe arrived in the Inbox, but the server wrote no verdict on it — common for a "
            "letter that never left the server. Sending one to a Gmail address and opening "
            "\"Show original\" tells the same thing from outside."))]
    found = [Finding("ok", i18n.t("The probe arrived in the Inbox."))]
    for method in ("spf", "dkim", "dmarc"):
        verdict = verdicts.get(method)
        if verdict is None:
            continue
        if verdict == "pass":
            found.append(Finding("ok", f"{method.upper()}: pass"))
        else:
            found.append(Finding("bad", i18n.t(
                "{method}: {verdict} — receivers see this letter as not properly from your domain.",
                method=method.upper(), verdict=verdict)))
    if "dkim" not in verdicts:
        found.append(Finding("warn", i18n.t(
            "The letter carries no DKIM verdict. Turn DKIM signing on at your mail provider and "
            "publish the record it gives you.")))
    return found
