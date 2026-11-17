"""
A test stand: the panel and the bot's handlers, with nothing real behind them.

    pip install -r requirements.txt
    python docs/stand.py                # http://127.0.0.1:8099, interface in English
    python docs/stand.py --lang ru --port 8100
    python docs/stand.py --fresh        # forget what was done on the stand before

3x-ui is an in-memory stand-in filled from docs/demo_data.py; letters are
written into an outbox folder instead of being sent; the Mail pages read two
mailboxes kept in memory, the bot's and Support's; and /stand takes a letter
"to the bot" and shows what it answered — so registration, /status and
/broadcast can be tried without a mailbox. Sign in with the login and password
docs/shots.py uses; they are printed when the stand starts.

The stand runs from a *copy* of the working tree, never from the tree itself.
settings.py, tariffs.py and the log all write beside their own code, so a panel
started here would save the demo's settings over a real installation's — the
same reason docs/shots.py gives, and the same mistake this project has made
before. The copy lives in the system's temporary directory and is rebuilt from
the tree on every start; what was saved on the stand (its settings, tariffs,
letter texts and outbox) is kept from one start to the next unless --fresh.
The clients are not kept: they live in memory and come back as the demo data.

Two modes in one file. Run from the tree it copies itself and starts the copy;
run from the copy (--serve) it is the panel. --serve refuses to run beside a
.git directory, which is what a checkout looks like and a copy does not.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(tempfile.gettempdir(), "mailchilla-stand")
DEFAULT_PORT = 8099

# What is not copied: what is not code, and above all the tree's own state.
NOT_COPIED = (".git", ".claude", "venv", ".venv", "logs", "outbox", "__pycache__",
              "3xui-docs", "screenshots", ".env", "settings.json", "email_texts.json",
              "tariffs.json", "*.json.tmp")
# What a rebuild leaves in the stand: whatever was saved on it.
KEPT = ("settings.json", "email_texts.json", "tariffs.json", "outbox", "logs")


def build(stand_dir: str, fresh: bool = False):
    """Makes `stand_dir` a current copy of the tree, keeping the stand's own state."""
    stand_dir = os.path.abspath(stand_dir)
    if os.path.isdir(os.path.join(stand_dir, ".git")) or stand_dir == ROOT:
        raise SystemExit(f"{stand_dir} is a checkout, not a place for a stand.")
    if os.path.isdir(stand_dir):
        # Emptied rather than copied over: a module deleted from the tree would
        # otherwise live on in the stand and go on being imported.
        for entry in os.listdir(stand_dir):
            if entry in KEPT and not fresh:
                continue
            path = os.path.join(stand_dir, entry)
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    shutil.copytree(ROOT, stand_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*NOT_COPIED))
    return stand_dir


def launch(args):
    """From the tree: build the copy and run it, until Ctrl+C."""
    stand_dir = build(args.dir, fresh=args.fresh)
    command = [sys.executable, os.path.join(stand_dir, "docs", "stand.py"), "--serve",
               "--port", str(args.port), "--lang", args.lang]
    process = subprocess.Popen(command, cwd=stand_dir)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return 0


def seed_tariffs(tariffs):
    """
    Three tariffs in place of the one a first start builds from the settings.

    Only when they are not there yet: a later start finds them in the stand's
    own tariffs.json, with whatever has been done to them since.
    """
    if "Standard" in [t["name"] for t in tariffs.all_tariffs()]:
        return
    tariffs._state = {"tariffs": [], "codes": []}

    def make(name, limit, days, inbounds, devices, word, uses=None, note="", renew=None):
        made = tariffs.save_tariff({"name": name, "limit_gb": limit, "expire_days": days,
                                    "inbound_ids": inbounds, "limit_hwid": devices,
                                    "renew": renew})
        tariffs.save_code({"word": word, "tariff_id": made["id"], "uses_left": uses,
                           "note": note, "enabled": True})

    # Named after the groups the demo clients carry, so the client list and the
    # tariffs agree: Family is the one with a device limit.
    # The renewals are the ones the demo clients of each group carry in the
    # stand's panel (stand_panel.RENEWALS): one of each kind worth looking at.
    make("Standard", 100, 90, [1, 2], 0, "AURORA",
         renew={"mode": "days", "value": 90, "max": 0})
    make("Family", 500, 365, [1], 3, "AURORA-FAMILY",
         renew={"mode": "monthday", "value": 1, "max": 12})
    make("Trial", 10, 7, [1], 1, "K78QYYDNSZ", uses=1, note="A personal invitation")


def serve(args):
    """From the copy: the panel itself, on the stand-ins."""
    if os.path.isdir(os.path.join(ROOT, ".git")):
        raise SystemExit("Refusing to serve from a checkout: the panel would write its demo "
                         "settings over this tree's own. Run `python docs/stand.py` instead.")
    # Before anything of the project is imported: the log path is relative, and
    # both it and the state files have to land in the copy.
    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "docs"))

    import config
    from shots import USER, PASSWORD
    config.ADMIN_PANEL_USER, config.ADMIN_PANEL_PASSWORD = USER, PASSWORD
    config.ADMIN_PANEL_SECRET = "0" * 32
    config.PANEL_LANG = config.MAIL_LANG = args.lang
    # The one request the project makes on its own behalf; a stand has no
    # business asking GitHub anything.
    config.UPDATE_CHECK_ENABLED = False

    import demo_data as demo
    import stand_panel
    panel = stand_panel.Panel()
    stand_panel.install(panel)
    outbox = stand_panel.Outbox(os.path.join(ROOT, "outbox"))

    import mailer
    import stand_mail
    mailboxes = stand_mail.Mailboxes()
    stand_mail.install(mailboxes)

    def keep_letter(server_host, port, user, password, to_email, subject, message,
                    service_name=None, extra_headers=None, automatic=True, session=None):
        # Assembled as it would be for sending, so a letter that cannot be
        # built fails here exactly as it would in front of an SMTP server —
        # and handed back, because the caller files a copy of what it sent in
        # the Sent folder, which on the stand is the in-memory one.
        built = mailer.build_message(to_email, subject, message, smtp_user=user,
                                     service_name=service_name, extra_headers=extra_headers,
                                     automatic=automatic)
        name = outbox.keep(to_email, subject, message)
        mailer.logger.info(f"[stand] a letter for {to_email} was kept as {name}, not sent.")
        return built

    mailer.send_email_via = keep_letter

    import admin.app as appmod
    import email_bot
    import inbox
    import settings as app_settings
    import tariffs

    # email_bot took its own references when it imported the transport.
    email_bot.send_email_via = keep_letter
    email_bot.send_gotify_notification = lambda title="", message="": None
    email_bot.probe_imap = lambda host, port, user, password: 0
    email_bot.probe_smtp = lambda host, port, user, password: None
    email_bot.scan_sender_names = lambda limit=0: {}

    config.SERVICE_NAME = demo.SERVICE_NAME
    # The mailboxes' own settings were set by stand_mail.install above; saved
    # settings would have been laid over them when the panel was imported, so
    # they are set once more now that it has been.
    for key, value in stand_mail.SETTINGS.items():
        setattr(config, key, value)
    if not config.ADMIN_EMAIL:
        config.ADMIN_EMAIL = "admin@example.com"
    config.XUI_SUBSCRIPTION_BASE_URL = "https://aurora.example.com/sub"
    config.SETUP_DONE = True
    app_settings._stored["SETUP_DONE"] = True
    seed_tariffs(tariffs)
    # No poll loop runs here, and the dashboard would otherwise say that no
    # check of the mailbox has got through yet.
    inbox._last_ok_at = time.time()

    import stand_pages
    stand_pages.add_routes(appmod.app, outbox, lambda: sorted(tariffs.live_words()),
                           received=mailboxes.received_by_the_bot)

    base = f"http://127.0.0.1:{args.port}"
    print(f"\n  Test stand: {base}   (sign in: {USER} / {PASSWORD})"
          f"\n  A letter to the bot, and the outbox: {base}/stand"
          f"\n  The mailboxes, as the panel shows them: {base}/mail/bot"
          f"\n  Kept in: {ROOT}\n", flush=True)

    import uvicorn
    uvicorn.run(appmod.app, host="127.0.0.1", port=args.port, log_level="warning")


def main():
    parser = argparse.ArgumentParser(description="Run Mailchilla on stand-ins, from a copy.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--lang", default="en", help="panel and letter language (en, ru)")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="where the copy is kept")
    parser.add_argument("--fresh", action="store_true",
                        help="forget the settings, tariffs and letters saved on the stand")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return serve(args) if args.serve else launch(args)


if __name__ == "__main__":
    sys.exit(main())
