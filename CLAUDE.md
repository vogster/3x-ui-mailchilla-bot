# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**The project is called Mailchilla; the GitHub repository is `vogster/3x-ui-mailchilla-bot`.** The names differ on purpose — the repository was renamed so that it turns up in searches for 3x-ui, while the product keeps its own name in `APP_NAME`, the panel, the letters and both READMEs. Do not "correct" either one to match the other. Every URL pointing at GitHub must use the repository name: `PROJECT_URL` in `config.py`, `REPO_URL` in `install.sh` and `mailchilla.sh`, and the install commands in both READMEs. GitHub still redirects the old `vogster/Mailchilla`, but only until somebody else claims that name — which would turn the README's `curl … | bash` line into somebody else's script.

Mailchilla: an email bot plus FastAPI web panel that hands out [3x-ui](https://github.com/MHSanaei/3x-ui) VPN subscriptions. A user emails a code word, the bot creates the client in 3x-ui and mails back the subscription link. Commands accepted by mail: the code word (or `/start`), `/status`, `/help`, `/broadcast` (admin address only).

## Commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # then fill XUI_*, ADMIN_PANEL_USER/PASSWORD/SECRET
python run.py                 # bot thread + web panel on 127.0.0.1:8080
python email_bot.py           # the bot alone, no panel
```

```bash
python -m unittest discover -s tests   # what CI runs
```

There is no linter config and no build step. The tests are unittest, no pytest, and they fall into two kinds. Most are pure: coercion, the code-word matching, the letter texts. `tests/test_panel_routes.py` is the other kind — it drives the panel through `fastapi.testclient` with 3x-ui stubbed out, and exists for one failure in particular: a template reading a context key the route never sent. Jinja resolves that to Undefined in silence, the page still renders, and the missing part is found by somebody clicking a week later. **A new page or a new context key wants a line in that file**; asserting on one string from the page is enough. `httpx` is what `TestClient` needs and lives in `requirements-dev.txt`, so an installation does not carry it.

Beyond the tests, verification is by running the process and watching the log (`logs/bot.log`, or the `/logs` page).

**A running panel picks up template edits but not Python ones.** Jinja re-reads a template on every render, while routes, `settings.py` and the rest are already imported — so editing both and reloading the page leaves the new markup running against the old route. The markup then reads context keys the route never sent, Jinja quietly resolves them to Undefined, and the feature disappears instead of erroring. Restart the process after any Python change, and be suspicious of "it renders but the new thing is missing".

**Run a panel you intend to poke at from a copy of the directory, not from the checkout.** `settings.SETTINGS_PATH` and `email_texts.TEXTS_PATH` are built from each module's own `__file__`, so `os.chdir` does not move them — only running the code from somewhere else does. Copy the tree to a scratch directory and start it there; then a stray save, a wizard step or a reset lands in the copy.

**Anything that exercises saving writes the installation's own files.** `settings.save()`, `email_texts.save()` and `tariffs.save_tariff()` write `settings.json`, `email_texts.json` and `tariffs.json` in the project directory itself — there is no test mode and no fixture path. A throwaway request against `/settings` or `/settings/texts` therefore overwrites a real configuration, and `email_texts.reset()` discards real edits. Run such checks against a copy of the directory, never against a checkout someone is running the panel from.

On an installed server the same work goes through `mailchilla` (`status`, `restart`, `log`, `update`, `passwd`, `check`). The shell scripts have no tests either; `bash -n install.sh mailchilla.sh` is what CI runs, and the release workflow will not publish a tag that fails it.

## Architecture

**No database.** 3x-ui is the single source of truth for client state; everything is read back from its API on each request. The only local state is `settings.json`, `email_texts.json` and `tariffs.json` (all gitignored — they hold secrets, per-installation text and the words that let people in) plus the in-memory log ring buffer. A tariff is not client state: it is a template read once, at creation, after which the values live on the client in 3x-ui like everything else.

**Two-layer configuration.** `config.py` reads `.env` into module-level attributes. `settings.py` then loads `settings.json` and **writes over those same `config` attributes** (`settings._apply()` does `setattr(config, key, ...)`). So:

- Read a setting as `config.IMAP_SERVER` at call time — never `from config import IMAP_SERVER`, which captures a stale value.
- `.env` only seeds a first run. Anything in `settings.MANAGED_KEYS` belongs to the panel and takes effect live, with no restart.
- `.env` is reserved for what must exist before the panel can start: 3x-ui access, panel login, host/port, logging.
- Adding a panel-editable setting means: a default in `config.py`, the key in `MANAGED_KEYS`, a `_coerce` branch in `settings.py`, and a field in the settings template/route.

**Client identity in 3x-ui is awkward, and the helpers exist for a reason.** The 3x-ui `email` field is an identifier with a restricted character set (`build_client_email` strips the rest); the sender's display name goes into the separate `comment` field (`build_comment`), because gluing `Name <email>` into `email` makes the panel reject the client. Lookups by address scan the whole client list (`XuiClient.find_client_by_email`, via `extract_bare_email`); updates and deletes key off the UUID (`XuiClient.client_key` prefers `uuid`, falls back to the numeric `id`). Clients created by hand in 3x-ui may carry no address at all — code that mails clients must skip rows whose `bare_email` is empty.

`xui_client.get_shared_client()` returns one process-wide `XuiClient`; it holds the login session cookies and re-authenticates on a 401/redirect, so don't construct `XuiClient()` directly.

**Tariffs are what a client gets; codes are how they got in.** `tariffs.py` holds both in `tariffs.json`: a tariff carries a name, traffic, term and inbounds, and a code carries a word, the tariff it opens, whether it is permanent or a one-shot invitation, and whether a letter using it waits for approval. Keeping them apart is what lets a leaked word be revoked without touching the tariff, and lets one tariff have several ways in.

- **Which tariff a client is on lives in 3x-ui, not here.** The client's `group` field is the tariff's *name*; `add_client` sets it in the same request that creates the client (verified against a live panel — the documented `add` body has no such field, but the panel takes it), `update_client` moves a client between groups, and `clients/list` returns `group` on every client, so the panel never pays an extra request for it. `GET clients/groups` also returns per-group client counts and traffic totals, which nothing uses yet.
- Because the name is the key, **tariff names must be unique** (`save_tariff` refuses a duplicate) and renaming a tariff must call `XuiClient.rename_group` — `routes_tariffs` and the wizard both do. An empty group is not a row in 3x-ui at all, so renaming one reports failure harmlessly.
- **The tariff is read once**, in `handle_registration`, and never again. Editing one changes nothing for anybody already registered, and the form says so — do not add code that "re-applies" a tariff.
- **A word is matched whole, and only in what the sender typed.** `email_bot.readable_text` cuts the body at the first quoted line and `contains_word` demands no word character either side. Both exist because the welcome letter carries the word back to everybody who replies to it, and because `START` lives inside `RESTART`. Never go back to `word in text`.
- **Two words in one letter are not guessed at**: the bot replies asking which is meant.
- **Words are unique across tariffs** (`tariffs.word_owner`), or what a letter gets would depend on the order the file happens to be written in.
- **A code is spent after the client exists**, not before: a code burned on a registration that then failed is worse than one used twice. An address that is already a client gets its link again and the code is *not* spent — the invitation stays for the person it was meant for.
- **There is one kind of code, not two.** `uses_left` of `None` is the word handed out openly; a number is how many registrations are left in it, and `1` is what used to be called an invitation. A `kind` field existed briefly and was removed — two fields that can disagree about the same fact are one too many. An old `tariffs.json` carrying `kind` is read fine and simply stops carrying it on the next write.
- **A generated word** (`tariffs.generate_word`) is `secrets.choice` over an alphabet with no `0/O` or `1/I/l`, because it is read off a screen and typed by hand. `enabled` is the switch; switching a code off keeps the record of who came through it, `delete_code` throws it away.
- Words are the identity of a code, so `save_code` takes `was=` to rename one, and carries `used_by` across.
- `CODEWORD`, `LIMIT_GB`, `EXPIRE_DAYS` and `XUI_INBOUND_IDS` are now only the **seed of the first tariff** — `tariffs.load()` builds it from them when `tariffs.json` is missing. They stay in `settings.MANAGED_KEYS` on purpose, so that a copy rolled back to 0.1.x finds them where it left them. Nothing else reads them.

**Two independent languages.** The panel interface language (`PANEL_LANG`) and the language letters go out in (`MAIL_LANG`) are separate settings.

- Interface: `i18n.t("English string")`, keyed on the English text itself, so English needs no catalogue. `lang/ru.py` is one flat `TEXTS` dict. A key may carry a disambiguating context in trailing brackets — `t("Active [badge]")` — which the catalogue keys on and English strips. New language = `lang/<code>.py` plus an entry in `i18n.LANGS`.
- Letters: `email_texts.py` holds `DEFAULTS_BY_LANG` and the `GROUPS` structure that drives the settings form. Overrides are stored per language in `email_texts.json`, and only strings that actually differ from the default are written. Adding a letter string means adding it to `GROUPS` **and** to every language's defaults.

**Letters are built twice.** `templates.py` renders `email_templates/<name>.html` and `email_templates/txt/<name>.txt` into an `Email(html, text)` namedtuple; every letter must have both parts. HTML is autoescaped, `.txt` is not. The only markup allowed from editable text is `**bold**` and line breaks, applied by the `emph`/`plain` filters — editable texts are text, not HTML. `t()` inside these templates is `templates.text()` (the letter texts), not `i18n.t()`.

**Entry point.** `run.py` starts the IMAP poll loop in a daemon thread and uvicorn in the main thread, with shared SIGINT/SIGTERM handling (uvicorn's own signal handlers are deliberately disabled). `admin/app.py` also calls `settings.load()`/`email_texts.load()`/`applog.install()` at import, so the panel works when imported on its own; all three are idempotent.

**Panel layout.** `admin/app.py` owns auth and the dashboard and mounts routers from `admin/routes_*.py`. `admin/deps.py` holds the shared Jinja2 environment, globals and `require_auth` — it exists to break an import cycle, so routers import from `deps`, never from `app`. Routes guard with `auth_redirect = require_auth(request); if auth_redirect: return auth_redirect`. `admin/rows.py` flattens a 3x-ui client into the row shape shared by the client list and the broadcast recipient picker.

Broadcasts run as in-memory background jobs (`admin/routes_broadcast.py`) because sending takes minutes; the browser gets a self-refreshing status page. Job state dies with the process, by design.

**No static files.** All panel CSS and JS is inline in `admin/templates/base_admin.html` (~1.4k lines); there is no `StaticFiles` mount.

**Logging.** `applog.install()` attaches a ring buffer (read by `/logs`) and a rotating file to the root logger. Use `logging.getLogger(__name__)` and log at INFO for anything an admin should see in the panel; per-poll noise goes to DEBUG.

## Installation, releases and the update path

`install.sh` and `mailchilla.sh` live at the repository root; the installer copies the latter to `/usr/local/bin/mailchilla`. Both are bilingual.

**Both need bash 4.0+** — `declare -A` for the message tables and `${var,,}` for the answer comparisons. Every supported distribution has it (CentOS 7 is already on 4.2); bash 3.2 in practice means macOS, where these scripts cannot run anyway. The version guard sits at the very top of each file, above the first `declare -A` and above `set -o pipefail`, and is deliberately written without arrays, `[[ ]]` or `set -o` so that a non-bash shell also reaches it. **Do not move it into a function** — the table is built as the file is read, so a check called from `main` never runs.

**Strings in the scripts follow the same rule as `i18n.py`.** One `declare -A MSG` table keyed `lang.key`, and `t key [args]` falls back to English when a key is missing in the other language, so a partial translation is safe. `{0}`, `{1}` are the placeholders. When adding a string, add both languages — the key balance is easy to check:

```bash
grep -c '^\[ru\.' mailchilla.sh && grep -c '^\[en\.' mailchilla.sh
```

**Versions are tags, never a branch.** `install.sh` checks out the newest `v*` tag (falling back to `origin/main` only when the repository carries no tags); `mailchilla update` finds the latest with `git ls-remote --tags`, so no GitHub API and no rate limit is involved. A GitHub Release is cosmetic — nothing breaks if one is forgotten.

Cutting a release means: bump `APP_VERSION` in `config.py`, write the section in `CHANGELOG.md`, commit, tag `vX.Y.Z`, push. `.github/workflows/release.yml` refuses a tag that disagrees with `APP_VERSION` — the version is displayed in the panel sidebar, so a mismatch is visible to every user. `mailchilla update` shows that same CHANGELOG section before asking for confirmation, so each entry should read as something a person wants to know before updating.

**Four files are the installation, and nothing may clobber them:** `.env`, `settings.json`, `email_texts.json`, `tariffs.json`. They are gitignored, which is exactly why the update is `git checkout` rather than unpacking a tarball — ignored files are left alone. `mailchilla update` still copies all of them to `/opt/mailchilla-backups/<date>/` first and restores them if the panel fails to answer after the restart — the list is `STATE_FILES` in `mailchilla.sh`, and a new state file that is not added to it is lost on the first update.

No migration step exists, and none is needed: a key absent from `settings.json` falls through to `BASE[key]` in `settings._apply()`, and `email_texts.json` stores only strings that differ from the shipped default. A *downgrade* is the lossy direction — `settings.load()` warns about unknown keys and drops them on the next save, which is what the backup is for.

Other things worth knowing before editing these scripts:

- **Liveness is an HTTP request, not `systemctl is-active`.** With `Restart=always` a service crash-looping reads as active. `wait_for_panel` polls `http://127.0.0.1:$PORT/login`.
- **The menu appears only on a tty.** `mailchilla` with no arguments and no terminal prints `status` instead of hanging on a prompt in cron or a pipe.
- **Values never go into an unquoted heredoc.** `.env` is written with `printf '%s'` per line, because a password is arbitrary text and a backtick in a heredoc would execute. In the file itself values are single-quoted so python-dotenv does not expand a `$`. `env_set` in `mailchilla.sh` keeps the same convention.
- The generated systemd unit deliberately does not use `EnvironmentFile`, for the reason `README.md` gives.
- **Every git call against the install directory needs `safe.directory`.** The repository belongs to the `mailchilla` service user while the command runs as root, and git otherwise refuses with "detected dubious ownership" — which silently breaks the version display and the whole update path. `install.sh` adds a system-wide exception and `mailchilla.sh` routes every call through its `git_repo()` wrapper; do not reintroduce a bare `git -C "$INSTALL_DIR"`.
- `/etc/mailchilla/install.conf` holds the state of the installation — directory, script language, service and user names. It is not application configuration and the app never reads it.

## The 3x-ui API

`3xui-docs/` (gitignored, not part of the project) holds the reference for the panel this talks to: `3xui-api-docs.md` — 116 endpoints, prose and example payloads — and `3xui-api-reference.json`, the same as a machine-readable collection. Read it before guessing at a request shape; it is the only description of the remote side available offline.

Two things about it are worth knowing without opening it:

**This is the newer, client-first API.** Clients have top-level endpoints — `/panel/api/clients/add`, `/update/:email`, `/del/:email`, `/:email/attach`, `/:email/detach` — rather than the classic inbound-scoped `/panel/api/inbounds/addClient` with a JSON-encoded `settings` string. A client is created once and attached to several inbounds by id in the same call, which is why `XuiClient.add_client` takes an `inbound_ids` list. Anything written against the old 3x-ui API, including most examples found online, will not match.

**`update/:email` replaces the row, it does not patch it.** Every field to be kept must be sent back — that is what `XuiClient._normalize_update_payload` is for, and why updates read the client object first.

Endpoints this project actually calls: `/login`; `clients/add`, `clients/list`, `clients/get/:email`, `clients/update/:email`, `clients/del/:email`, `clients/:email/attach|detach`, `clients/onlines`, `clients/lastOnline`, `clients/links/:email`, `clients/traffic/:email`, `clients/groups/rename`; `inbounds/list/slim`; `server/status`. The group payloads are `{"name": …}` for create and delete, `{"oldName": …, "newName": …}` for rename, `{"emails": [...], "group": …}` for bulkAdd. The docs describe much more — bulk operations, client groups, subscription links by `subId`, Xray control, per-metric history — none of which is wired up yet.

Notes that matter when reading responses: everything comes back as `{"success": bool, "msg": str, "obj": ...}`. `inbounds/list/slim` strips `settings.clients[]` down to `{email, enable, comment}` and omits uuid/subId, so it is only good for listing inbounds — per-client detail needs `clients/list` or `inbounds/get/:id`. Times are milliseconds since the epoch, traffic is bytes, and `totalGB` is bytes too despite the name. Auth works either by session cookie from `POST /login` (with `X-CSRF-Token` from `GET /csrf-token` on unsafe methods) or by `Authorization: Bearer <token>`, which skips CSRF entirely. `XuiClient` sets the bearer header at construction when `XUI_API_TOKEN` is set and short-circuits `login()`; without a token it signs in for a cookie and retries once on a 401/403 or a redirect to the login page.

**`login()` is not a connectivity check.** With a token set it returns `True` immediately without making a request, so it reports success against a wrong token, a dead host or a typo in `XUI_URL`. Anything meaning to verify that 3x-ui is actually usable must issue a real request — `mailchilla check` asks for `inbounds/list/slim` for exactly this reason, and calling `login()` there was a bug. It never sends a CSRF header on either path — so if a panel starts enforcing CSRF on cookie sessions, the token is the working route and that is the code to fix.

## Conventions

- Comments in this codebase explain *why*, often at length, and frequently record a bug that motivated the current shape. Match that when touching the same code; don't strip such comments when refactoring.
- Secrets (`IMAP_PASSWORD`, `SMTP_PASSWORD`, `GOTIFY_TOKEN`) never reach page markup: forms post back `settings.UNCHANGED` when untouched, and the real value is fetched separately by `/settings/secret`.
- User-visible strings in panel templates and Python go through `i18n.t()`; letter strings go through the editable-texts layer instead of being hardcoded.
- **The Russian catalogue is kept complete.** The captions and hints in `email_texts.GROUPS` are interface strings and need an entry in `lang/ru.py` — adding a field without one leaves English text sitting in a Russian panel. One pass over `GROUPS` finds every gap:

```bash
python -c "import email_texts;from lang.ru import TEXTS;print([s for g in email_texts.GROUPS for f in g['fields'] for s in (f[1],f[3]) if s and s not in TEXTS])"
```
- `README.md` and `README.ru.md` are parallel — a user-facing change belongs in both, and their section structure is kept identical.
- A user-visible change also belongs in `CHANGELOG.md` under `## [Unreleased]`, since that text is what `mailchilla update` shows people.
