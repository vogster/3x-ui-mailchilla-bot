<h1 align="center">Mailchilla</h1>

<p align="center">
An email bot and web panel for <a href="https://github.com/MHSanaei/3x-ui">3x-ui</a>
</p>

<p align="center">
<img src="https://img.shields.io/badge/Python-3.9+-blue" alt="Python 3.9+">
<img src="https://img.shields.io/badge/License-MIT-green" alt="License: MIT">
</p>

<p align="center">
English · <a href="README.ru.md">Русский</a>
</p>

---

Someone writes a letter with the code word - and gets a working subscription back. The administrator never opens 3x-ui.

The project started with a simple need: handing out subscriptions to people you know. Doing that by hand through the 3x-ui admin panel is miserable, and a Telegram bot is not an option either - how do you hand a subscription to somebody for whom Telegram is blocked? That is the shape of the problem in Russia: to reach the bot that would give you the subscription, you first need the subscription. Email works for everybody, with no VPN and nothing to get around. So email is what it was built on.

![Dashboard](docs/screenshots/dashboard.png)

---

## Contents

- [Features](#-features)
- [Quick start](#-quick-start)
- [First run](#-first-run)
- [Settings](#-settings)
- [Reaching the panel](#-reaching-the-panel)
- [Managing it](#-managing-it)
- [Updating](#-updating)
- [How it is built](#-how-it-is-built)
- [Development](#-development)
- [License](#-license)

---

## 📬 Features

**📧 The email bot**

You write a letter with a command, in the subject or in the body - the bot handles it and answers.

| Command | What it does |
|---|---|
| the code word | creates the client in 3x-ui, sends back the subscription link |
| `/status` | traffic left, expiry, status |
| `/help` | instructions for setting up a client app |
| `/broadcast` | a letter to every active client (from the administrator only) |

The sender's name lands in the client's comment - the list then shows people rather than addresses.

**🖥️ The web panel**

![Client list](docs/screenshots/clients.png)

- A client list with live search, sorting and filters (status, mail, period, traffic)
- The dashboard opens with the machine itself: processor, memory, disk, uptime, xray's state and how many clients are connected right now. It can refresh itself every few seconds, or on the button
- A client's card: traffic, expiry, subscription link and the inbounds they belong to - including ones switched off or gone from the panel
- Each inbound also carries its own ready-made connection URL, the same string 3x-ui's copy button hands out. One click copies it
- Creating a client by hand, changing the limit, the expiry or the set of inbounds, disabling an account
- Resending the invitation and writing a personal letter, straight from the card

![Client card](docs/screenshots/client.png)

**📢 Broadcasts**

![Broadcast](docs/screenshots/broadcast.png)

A letter to chosen recipients or to every active client. Sending runs in the background, the page shows the progress, and closing the tab does not break it. The message kind sets the colour of the stripe in the letter: plain, information, warning, urgent.

The recipient list has search, filters (active, disabled, limit spent, traffic running low) and sorting. "All" and "None" act on whatever the filter left - so "everyone who ran out of traffic" is two movements.

And something completely unserious: the **Roulette** button rolls the visible recipients past like a CS:GO case opening, rarity colours and all, and leaves exactly one of them ticked. Rarity comes from traffic spent, so gold goes to the hungriest.

**💌 The letters**

![Welcome letter](docs/screenshots/email-welcome.png)

Table-based layout with inline styles, flat colours, a text version in every letter. It arrives looking the same in Gmail, on phones and in Outlook.

Every text is editable from the panel: subjects, headings, captions, the footer, the service replies. Each letter has a test-send button.

![Letter settings](docs/screenshots/settings-mail.png)

**🌍 Two languages**

The interface speaks English and Russian. So do the letters - and the two are chosen separately, so an English panel can send Russian letters. That is the usual case when you administer in one language and your users read another.

The interface strings live in `lang/`, the letter texts in the panel itself, one set per language. Editing the English welcome letter leaves the Russian one alone.

**📋 Logs and notifications**

![Logs](docs/screenshots/logs.png)

The process log in the browser: filter by level, search, exception tracebacks. Unread warnings rise to the dashboard. A push to Gotify on every registration.

---

## 🚀 Quick start

One command on a clean server with a reachable 3x-ui panel:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/vogster/3x-ui-mailchilla-bot/main/install.sh)
```

The installer asks for the language first, then for access to 3x-ui and the sign-in for its own panel, and does the rest itself: packages, a system user, the latest released version into `/opt/mailchilla`, a virtual environment, `.env`, the systemd service and the `mailchilla` command. At the end it prints the address, the password and the tunnel command.

Debian 11+, Ubuntu 22.04+, CentOS/AlmaLinux/Rocky/Fedora. Python 3.9 or newer is required — Ubuntu 20.04 ships 3.8 and the installer will say so rather than leave a half-installed copy.

Running a script straight off the internet as root deserves a look first. If you would rather:

```bash
curl -LO https://raw.githubusercontent.com/vogster/3x-ui-mailchilla-bot/main/install.sh && less install.sh && sudo bash install.sh
```

<details>
<summary><b>Installing by hand</b></summary>

Python 3.9+, and a reachable 3x-ui panel.

```bash
git clone https://github.com/vogster/3x-ui-mailchilla-bot.git mailchilla
cd mailchilla
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in XUI_URL, XUI_USERNAME, XUI_PASSWORD (or XUI_API_TOKEN)
# fill in ADMIN_PANEL_USER, ADMIN_PANEL_PASSWORD
# generate ADMIN_PANEL_SECRET: openssl rand -hex 32
python run.py
```

</details>

One command brings up both the bot and the panel. The panel is always raised - there is nowhere else to configure the project. To run the bot on its own: `python email_bot.py`.

`.env` holds only what is needed before the panel can come up: access to 3x-ui, the panel's own sign-in, its address and the logging settings. Everything else - the mailbox, the language, the code word, limits, inbounds, Gotify - is set in the panel after the first sign-in and kept in `settings.json`.

---

## ⚙️ First run

A fresh copy knows nothing about your mailbox or your inbounds. After the first sign-in a banner at the top offers to walk through the setup.

![Setup wizard](docs/screenshots/setup.png)

The wizard goes step by step: service, mail, inbounds, registration rules, then the optional notifications and app buttons. Every step saves at once - a closed tab loses nothing. The mail step has a working connection check.

"I will do it myself" dismisses the banner. The wizard is the same settings fields, laid out in order.

---

## 🔧 Settings

Everything that changes during normal use is set in the panel and applies on the fly, without a restart.

![Settings](docs/screenshots/settings.png)

| Tab | What is inside |
|---|---|
| **General** | interface language, service name, subscription base address, administrator address |
| **Mail** | IMAP and SMTP: servers, ports, logins, passwords, polling interval, connection check |
| **Registration** | inbounds for new clients, traffic limit, expiry, code word, flow |
| **Apps** | the schemes behind the "Add to Happ / Incy" buttons in the letter |
| **Gotify** | server address, token, priority, notification text |
| **Letters** | the language letters go out in, and every text of every letter |

The Mail tab has a check: the panel signs in to the mailbox over IMAP and to SMTP with whatever is in the fields (no need to save first) and reports back step by step. If an administrator address is set, a test letter goes there too.

Settings are kept in `settings.json` and the letter texts in `email_texts.json`, both beside the project. The mailbox is picked up on the next polling cycle.

**Secrets.** The mailbox passwords and the Gotify token are edited in the panel. Passwords are shown as dots and nothing else, the token keeps a few characters at either end so you can tell one from another, and either opens with the eye button. The real value never reaches the page markup, it is requested separately. `settings.json` therefore holds secrets: the file is in `.gitignore`, and it should not carry generous permissions.

---

## 🔐 Reaching the panel

By default `127.0.0.1:8080` - not reachable from outside.

```bash
ssh -L 8080:127.0.0.1:8080 user@your-server
```

Then open `http://localhost:8080`.

![Sign-in](docs/screenshots/login.png)

The alternative is a reverse proxy with HTTPS. Setting `ADMIN_PANEL_HOST=0.0.0.0` without one is a bad idea.

---

## 🖥️ Managing it

The `mailchilla` command opens a menu when run on its own, and takes the same actions as arguments — so it works by hand and from a script alike.

```bash
mailchilla
```

| | |
|---|---|
| `mailchilla status` | the service, the version, whether the panel answers |
| `mailchilla restart` | also `start`, `stop` |
| `mailchilla log` | the journal, following |
| `mailchilla update` | check for a new version and install it |
| `mailchilla passwd` | change the panel's username or password |
| `mailchilla port` | change the port |
| `mailchilla tunnel` | the ready-made SSH tunnel command |
| `mailchilla check` | sign in to 3x-ui and report back |
| `mailchilla backup` | copy the configuration to `/opt/mailchilla-backups` |
| `mailchilla uninstall` | remove everything |

`status` is not `systemctl is-active`: with `Restart=always` a service crash-looping still reads as running, so the panel is asked over HTTP instead.

Forgotten the panel password? It lives in `.env`, and without it the panel does not start at all — `mailchilla passwd` sets a new one and restarts.

<details>
<summary><b>A systemd unit for a manual installation</b></summary>

```ini
[Unit]
Description=Mailchilla
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=bot
WorkingDirectory=/opt/mailchilla
ExecStart=/opt/mailchilla/venv/bin/python run.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Two things that are easy to trip over:

- **Do not use `EnvironmentFile` for `.env`.** The application reads it itself through python-dotenv, and systemd parses such files by its own rules - two parsers on one file will disagree.
- **The user needs write access** to the project directory: `settings.json`, `email_texts.json` and `logs/` are written there.

</details>

---

## ⬆️ Updating

```bash
mailchilla update
```

Versions are tags, not the tip of a branch: the command asks GitHub for the newest `vX.Y.Z`, shows what changed from `CHANGELOG.md` and waits for a yes.

Before touching anything it copies `.env`, `settings.json` and `email_texts.json` to `/opt/mailchilla-backups/<date>/`. If the panel does not answer after the restart, it goes back to the previous version and restores them.

Files edited on the server — a template, say — are put aside with `git stash` rather than blocking the update; the command tells you how to get them back.

New settings and new letter texts need no migration: a key absent from `settings.json` falls back to the default in `config.py`, and `email_texts.json` stores only what actually differs from the shipped text.

For a manual installation the old route still works: `git pull` and `systemctl restart`.

---

## 📁 How it is built

There is no database of its own. The source of truth is the 3x-ui panel, and client state is read from it. That rules out the two drifting apart, but it does mean the panel has to be reachable.

| File | What it is for |
|---|---|
| `install.sh` | the installer: one command, English or Russian |
| `mailchilla.sh` | managing an installed copy: the menu and the subcommands |
| `run.py` | the entry point: the bot in a background thread, the panel in the main one |
| `email_bot.py` | IMAP polling, parsing letters, commands, sending |
| `xui_client.py` | the 3x-ui API client |
| `templates.py` | assembling letters with Jinja2 - HTML and a text version |
| `email_texts.py` | the editable letter texts, one set per language |
| `i18n.py` | the interface translator: catalogue lookup and the two language settings |
| `lang/` | the interface catalogues, one module per language |
| `settings.py` | settings layered over `.env` |
| `config.py` | environment variables |
| `applog.py` | log collection into a buffer and a file |
| `admin/` | the FastAPI web panel |
| `email_templates/` | the letter templates |

---

## 🛠️ Development

Forks and pull requests are welcome. The main branch is `main`.

```bash
git checkout -b feature/my-feature
# ...
git push origin feature/my-feature
# open a pull request on GitHub
```

---

## 📄 License

MIT — see [LICENSE](LICENSE).
