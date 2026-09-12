# Changelog

Notable changes to Mailchilla. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [semantic versioning](https://semver.org/lang/ru/).

The `mailchilla update` command reads the section belonging to a version out of
this file and shows it before asking for confirmation, so each entry should
read as something a person wants to know before updating.

## [0.1.3]

### Added

- The panel notices when a newer version has been released and says so in a
  banner: the version installed, the one available, a link to GitHub's
  comparison between the two, and the command to run on the server. It does not
  update anything by itself.
- The check is a setting on the General tab and can be switched off. It asks
  GitHub four times a day at most, from a background thread, so rendering a page
  never waits on the network and a server with no route to GitHub simply sees
  no banner.
- "Later" puts the banner away for that version alone — a newer one brings it
  back.

- The welcome letter carries a QR code of the subscription link, for the
  reader who opened their mail on a computer and would otherwise be copying the
  link across to their phone by hand. It is drawn on the server and travels
  inside the letter as an attachment: a QR service would be handed every
  subscriber's subscription link, and that link is the subscription. It is a
  switch on the Letters tab, standing among the texts of the letter it governs
  and saved by the same button, on by default.
- A switch control in the panel, for a setting that turns a whole thing on or
  off — where a tick reads as "one of several" and a switch as "on, or off".

### Fixed

- The setup banner and the new-version banner could stand one above the other,
  pushing the page itself off the screen — worst on a phone, where between them
  they filled it. The setup banner comes first now and the other waits: one is
  about nothing working yet, the other can keep.
- The log table came out as a ribbon of one- and two-word lines on a phone. It
  already sat in a box that scrolls sideways, but had nothing to scroll: the
  message cell may break a word anywhere, so the column squeezed down to almost
  nothing instead of overflowing. A floor under the table's width stops that,
  and the box now scrolls as it does for the client list. The same table on the
  dashboard is fixed with it.
- The buttons in the setup banner ran off the right edge on a narrow screen. A
  button cannot shrink below its own label and a translation is free to be wider
  than the English, so they wrap and take the full width instead.

### Changed

- The app buttons in the welcome letter ("Add to Happ", "Add to Incy") are
  outlined and lettered in the green the letterhead already uses. A dark button
  on a dark letter was all but invisible, and those buttons are the point of the
  block they sit in.

## [0.1.2] - 2026-09-11

### Added

- The installer offers to make the panel reachable from outside. `127.0.0.1`
  stays the default and the recommendation, and choosing `0.0.0.0` takes two
  confirmations and a warning about plain HTTP; the closing summary then shows
  the direct address instead of the SSH tunnel. `mailchilla port` switches
  between the two afterwards.

### Fixed

- `mailchilla check` reported success without checking anything when an API
  token was configured: `XuiClient.login()` returns `True` immediately in that
  mode, without a single request. It now makes a real API call and tells a
  refused token apart from an unreachable address.
- Every git call in `mailchilla` failed on an installed copy, because the
  repository belongs to the service user while the command runs as root and
  git then refuses with "detected dubious ownership". The version showed as `?`
  and `mailchilla update` could not have worked at all.
- The installer reported a live 3x-ui panel as unreachable: it used `curl -f`
  against `/`, which answers 404 whenever the panel sits under a base path.
- The hint for finding an API token pointed at the old place in 3x-ui. It is
  now Panel Settings -> Account -> API Tokens.

## [0.1.1] - 2026-09-11

### Fixed

- Every link to GitHub now points at `vogster/3x-ui-mailchilla-bot`, the
  repository's real name. They pointed at the previous name, which GitHub
  redirects only until somebody else claims it — and the installer is fetched
  over one of those links and run as root. The project itself is still called
  Mailchilla; only the repository was renamed.

## [0.1.0] - 2026-09-11

### Added

- `install.sh`: installation with one command, in English or Russian. It picks
  up the latest tag, generates `.env`, registers the systemd service and the
  `mailchilla` command.
- `mailchilla`: managing the installation — a menu when run on its own, and the
  same actions as subcommands (`status`, `restart`, `log`, `update`, `passwd`,
  `port`, `tunnel`, `check`, `backup`, `uninstall`).
- `mailchilla update`: updating to the latest tag with a backup of the
  configuration taken first and a rollback when the panel does not come up.
- A release workflow: a pushed `v*` tag is checked against `APP_VERSION` and
  published as a GitHub Release.

## [0.0.1] - 2026-09-11

The first version.
