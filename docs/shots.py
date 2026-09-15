"""
Retakes every screenshot in docs/screenshots/.

    pip install playwright && playwright install chromium
    python docs/shots.py

Deliberately not in requirements.txt: nobody running the bot needs a browser.

The panel is started from a *copy* of the working tree, never from the tree
itself. settings.SETTINGS_PATH is built from the module's own __file__, so a
panel started here would otherwise write the demo settings straight over a real
installation's — which is a mistake this project has made before.

3x-ui is not contacted at all: XuiClient is stubbed with docs/demo_data.py, so
the shots are reproducible and carry nobody's real address.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.path.join(ROOT, "docs", "screenshots")
USER, PASSWORD = "demo", "demo-password"

# Retina, like the screenshots that are already there.
SCALE = 2
WIDE = {"width": 1440, "height": 1150}
PHONE = {"width": 390, "height": 844}

ENTRY = '''
import sys, time
sys.path.insert(0, {root!r})
sys.path.insert(0, {docs!r})

import config
config.ADMIN_PANEL_USER = {user!r}
config.ADMIN_PANEL_PASSWORD = {password!r}
config.ADMIN_PANEL_SECRET = "0" * 32
config.PANEL_LANG = {lang!r}
config.MAIL_LANG = {lang!r}

import demo_data as demo
from xui_client import XuiClient

XuiClient.get_all_clients = lambda self: demo.clients()
XuiClient.get_online_emails = lambda self: list(demo.ONLINE)
XuiClient.get_last_online = lambda self: demo.last_online()
XuiClient.get_inbounds = lambda self: [dict(i) for i in demo.INBOUNDS]
XuiClient.get_server_status = lambda self: dict(demo.SERVER)
# The real method parses the panel's raw URLs into these dicts; _attach_links
# ties a link to an inbound by port, so the ports have to match INBOUNDS.
XuiClient.get_client_links = lambda self, email: [
    {{"url": "vless://c0ffee@aurora.example.com:443?security=reality&type=tcp"
             "#VLESS-REALITY-443",
      "protocol": "vless", "label": "VLESS-REALITY-443", "port": 443}},
    {{"url": "ss://Y2hhY2hhMjA@aurora.example.com:8388#Shadowsocks-8388",
      "protocol": "ss", "label": "Shadowsocks-8388", "port": 8388}},
]
XuiClient.find_client_by_uuid = lambda self, u: next(
    (c for c in demo.clients() if c["uuid"] == u or str(c["id"]) == str(u)), None)
XuiClient.login = lambda self: True

import admin.app as appmod
import settings as app_settings, email_bot

config.SERVICE_NAME = demo.SERVICE_NAME
config.IMAP_SERVER, config.IMAP_USER, config.IMAP_PASSWORD = "imap.example.com", "bot@example.com", "x"
config.SMTP_SERVER, config.SMTP_USER, config.SMTP_PASSWORD = "smtp.example.com", "bot@example.com", "x"
config.ADMIN_EMAIL = "admin@example.com"
config.XUI_INBOUND_IDS = [1, 2]
config.XUI_SUBSCRIPTION_BASE_URL = "https://aurora.example.com/sub"
config.GOTIFY_URL, config.GOTIFY_TOKEN = "https://push.example.com", "AbCdEf123456"
config.CODEWORD = "AURORA"
config.LIMIT_GB, config.EXPIRE_DAYS = 100, 90
config.SETUP_DONE = {setup_done!r}
app_settings._stored["SETUP_DONE"] = {setup_done!r}
email_bot._last_ok_at = time.time() - 20
# A handful of records so the log page is not empty.
import logging
log = logging.getLogger("email_bot")
for text, level in [("Bot started. Mail poll interval: 15 seconds.", logging.INFO),
                    ("Client anna.bright@example.com registered successfully.", logging.INFO),
                    ("Handling a letter from ben@example.com. Subject: '/status'", logging.INFO),
                    ("The mailbox answered slowly; the check was cut short.", logging.WARNING),
                    ("Client w.hume@example.com registered successfully.", logging.INFO)]:
    log.log(level, text)

import uvicorn
uvicorn.run(appmod.app, host="127.0.0.1", port={port}, log_level="critical")
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_panel(copy_dir, port, lang="en", setup_done=True):
    entry = os.path.join(copy_dir, "_shots_entry.py")
    with open(entry, "w", encoding="utf-8") as handle:
        handle.write(ENTRY.format(root=copy_dir, docs=os.path.join(ROOT, "docs"),
                                  user=USER, password=PASSWORD, lang=lang,
                                  port=port, setup_done=setup_done))
    process = subprocess.Popen([sys.executable, entry], cwd=copy_dir)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return process
        except OSError:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError("the panel did not come up")


def copy_tree():
    copy_dir = tempfile.mkdtemp(prefix="mailchilla-shots-")
    shutil.copytree(ROOT, copy_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", "venv", ".venv", "logs",
                                                  "settings.json", "email_texts.json",
                                                  ".env", "__pycache__"))
    return copy_dir


def sign_in(page, base):
    page.goto(f"{base}/login", wait_until="networkidle")
    page.fill("input[name=username]", USER)
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/", wait_until="networkidle")


def shoot(page, name, full_page=False):
    """
    Captures the page cropped to its own content.

    A fixed viewport gives the short pages - the wizard, the logs, a settings
    tab with two fields - a screenshot that is half empty background, which
    reads as an unfinished page rather than as a roomy one. So the height comes
    from the document, capped at the viewport so that a long page still looks
    like something you scroll rather than an endless strip.
    """
    path = os.path.join(SHOTS, f"{name}.png")
    if full_page:
        # The letter is one tall column and has no viewport of its own to fill.
        page.screenshot(path=path, full_page=True)
        print(f"  {name}.png  {os.path.getsize(path) // 1024} KB")
        return
    box = page.viewport_size
    # scrollHeight is no use here: the layout stretches to the full viewport, so
    # it reports the same number on every page. What the content actually
    # reaches is the bottom of .container - the wrapper the pages fill - and the
    # login page, which has a body of its own, keeps the whole viewport.
    height = page.evaluate("""() => {
        const c = document.querySelector('.container');
        if (!c) return document.documentElement.scrollHeight;
        const r = c.getBoundingClientRect();
        return Math.ceil(r.bottom + window.scrollY + 24);
    }""")
    height = max(300, min(height, box["height"]))
    page.screenshot(path=path, clip={"x": 0, "y": 0,
                                     "width": box["width"], "height": height})
    size = os.path.getsize(path) // 1024
    print(f"  {name}.png  {size} KB")


def letter_html(copy_dir):
    """The welcome letter as a file a browser can open, QR and all."""
    import base64
    out = os.path.join(copy_dir, "_letter.html")
    code = (
        "import sys, base64; sys.path.insert(0, %r)\n"
        "import config\n"
        "config.SERVICE_NAME = 'Aurora VPN'\n"
        "config.MAIL_LANG = 'en'\n"
        "config.HAPP_URL, config.INCY_URL = 'happ://add', 'incy://add'\n"
        "import email_texts; email_texts.load()\n"
        "import templates\n"
        "mail = templates.get_welcome_email('https://aurora.example.com/sub/8f2c41d9', 90, 100)\n"
        "html = mail.html\n"
        "for cid, data in (mail.images or {}).items():\n"
        "    html = html.replace('cid:' + cid, 'data:image/png;base64,' + base64.b64encode(data).decode())\n"
        "open(%r, 'w', encoding='utf-8').write(html)\n" % (copy_dir, out)
    )
    subprocess.run([sys.executable, "-c", code], cwd=copy_dir, check=True)
    return out


def main():
    from playwright.sync_api import sync_playwright

    os.makedirs(SHOTS, exist_ok=True)
    copy_dir = copy_tree()
    print(f"panel from {copy_dir}")
    port = free_port()
    panel = start_panel(copy_dir, port)
    base = f"http://127.0.0.1:{port}"

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()

            # --- the wide pages -------------------------------------------------
            # The sign-in page is its own shot: it fills whatever height it is
            # given with the particle canvas, and a canvas full of noise is
            # heavy - a shorter frame is both a better crop and a smaller file.
            entry = browser.new_page(viewport={"width": 1440, "height": 820},
                                     device_scale_factor=SCALE, color_scheme="dark")
            entry.goto(f"{base}/login", wait_until="networkidle")
            entry.wait_for_timeout(600)
            shoot(entry, "login")
            entry.close()

            page = browser.new_page(viewport=WIDE, device_scale_factor=SCALE,
                                    color_scheme="dark")
            sign_in(page, base)
            page.wait_for_timeout(400)
            shoot(page, "dashboard")

            page.goto(f"{base}/clients", wait_until="networkidle")
            page.wait_for_timeout(300)
            shoot(page, "clients")

            first = page.get_attribute("tbody tr[data-remark] a.row-link", "href")
            page.goto(base + first, wait_until="networkidle")
            page.wait_for_timeout(300)
            shoot(page, "client")

            page.goto(f"{base}/broadcast", wait_until="networkidle")
            page.wait_for_timeout(300)
            shoot(page, "broadcast")

            page.goto(f"{base}/logs", wait_until="networkidle")
            page.wait_for_timeout(300)
            shoot(page, "logs")

            page.goto(f"{base}/settings", wait_until="networkidle")
            page.wait_for_timeout(300)
            shoot(page, "settings")

            # The letters tab, where every text is edited.
            page.click("text=Letters")
            page.wait_for_timeout(400)
            shoot(page, "settings-mail")
            page.close()

            # --- the first-run wizard, which needs a panel that is not set up ---
            panel.terminate(); panel.wait(timeout=10)
            wizard_port = free_port()
            wizard = start_panel(copy_dir, wizard_port, setup_done=False)
            wpage = browser.new_page(viewport=WIDE, device_scale_factor=SCALE,
                                     color_scheme="dark")
            wbase = f"http://127.0.0.1:{wizard_port}"
            sign_in(wpage, wbase)
            wpage.goto(f"{wbase}/setup", wait_until="networkidle")
            wpage.wait_for_timeout(400)
            shoot(wpage, "setup")
            wpage.close()
            wizard.terminate(); wizard.wait(timeout=10)

            # --- the phone ------------------------------------------------------
            panel = start_panel(copy_dir, port)
            phone = browser.new_page(viewport=PHONE, device_scale_factor=SCALE,
                                     color_scheme="dark", is_mobile=True,
                                     has_touch=True)
            sign_in(phone, base)
            phone.goto(f"{base}/clients", wait_until="networkidle")
            phone.wait_for_timeout(400)
            shoot(phone, "mobile")
            phone.close()

            # --- the welcome letter --------------------------------------------
            letter = browser.new_page(viewport={"width": 700, "height": 900},
                                      device_scale_factor=SCALE, color_scheme="dark")
            letter.goto("file://" + letter_html(copy_dir), wait_until="networkidle")
            letter.wait_for_timeout(300)
            shoot(letter, "email-welcome", full_page=True)
            letter.close()

            browser.close()
    finally:
        panel.terminate()
        try:
            panel.wait(timeout=10)
        except Exception:
            panel.kill()
        shutil.rmtree(copy_dir, ignore_errors=True)

    print("done")


if __name__ == "__main__":
    main()
