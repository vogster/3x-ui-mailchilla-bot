# Changelog

Notable changes to Mailchilla. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [semantic versioning](https://semver.org/lang/ru/).

The `mailchilla update` command reads the section belonging to a version out of
this file and shows it before asking for confirmation, so each entry should
read as something a person wants to know before updating.

## [0.1.4]

### Added

- The installer shows a turning bar while it works. Packages and pip take
  minutes and say nothing meanwhile, which reads as a hang; their own output is
  kept back and printed only if the step fails.
- `mailchilla` says in its header when a newer version is out. It asks once per
  run, on a four-second leash, and only ever points forward — a copy running
  ahead of the newest tag is somebody testing a branch.
- `mailchilla autostart` toggles autostart, and the menu entry shows whether it
  is on. After switching it says which way it went rather than just "done".
- The first-run wizard asks for `flow` along with the other registration
  defaults, and can send a test notification to Gotify without leaving the step.

- Name synchronisation, on the Mail tab: it reads the letters in the mailbox,
  takes the name each sender signs themselves with, and offers a table of the
  clients whose name in 3x-ui differs — tick who to update. A client with no
  name at all counts as a difference, since filling one in is the usual reason
  for doing this. The letters are read headers-only and never marked, so a
  registration waiting to be handled is not swallowed by pressing the button,
  and nothing is written until the choice is made. The panel is hidden while no
  mailbox is set up.
- The dashboard says how the mail loop is doing: when it last read the mailbox
  through, or how many checks in a row have failed and why. A loop that has
  quietly stopped — a changed password, a blocked mailbox — used to look exactly
  like a mailbox nobody writes to, and the first sign of it was somebody
  complaining that the code word does nothing.
- Tests, and a workflow that runs them on every push: the identity helpers, every
  branch of the settings coercion, both parts of every letter, the MIME shape
  with a picture and without, version comparison, and a pass that fails when a
  Russian translation is missing. `unittest` from the standard library, so
  nothing new ships to anybody.

### Changed

- The bottom bar on a phone is taller — 60px rather than 47 — with roomier icons
  and captions.
- The small buttons throughout the panel have three more pixels of height and a
  little more air either side; at 27px the text was pinched against the edges.
- The wizard no longer asks about the app schemes: `happ://add` and `incy://add`
  belong to the apps rather than to any one installation, so they are filled in
  from the start. Emptying one in the panel still removes its button.
- The Gotify notification texts follow the panel's language while they are
  untouched. A text typed into the field is left exactly as typed.
- The installer says plainly that the 3x-ui address needs the whole path,
  including the panel's base path.

### Fixed

- A letter is marked read after it has been dealt with, not before. The gap
  between the two cost whole registrations: 3x-ui unreachable, SMTP refusing, the
  process restarted — the letter was already read by then, the next cycle never
  saw it again, and somebody who wrote the code word simply got nothing back.
  Left unread it is picked up on the next pass. `/broadcast` is still marked
  first: it is the one command that must not run twice.
- In the menu, an action needing root threw the person out of the program
  instead of refusing that one action — which is why autostart looked as though
  it could not be switched.
- On a phone, a long address in the client list wrapped underneath its own
  connection dot, which read as a fault rather than as a long address. The
  column keeps the two on one line and the table scrolls sideways, as it already
  did.

## [0.1.3] - 2026-09-12

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
- Who is connected right now shows as a dot beside the address — in the client
  list, on a client's own page and in the broadcast's recipient list. Green
  breathes, red sits still, because most of a list is usually offline and a page
  of blinking would be unreadable. The panel is asked once per page and again
  every twenty seconds, and only the dots change: the search text, the filters,
  the sort order, a half-written letter and the recipients already ticked all
  stay exactly as they were. A tab nobody is looking at stops asking.
- The client list and the broadcast can be filtered by connection. On the
  broadcast it is its own control rather than another entry in the existing one,
  so "disabled and connected" is a question that can be asked.
- Where the panel cannot say who is online — an older 3x-ui has no such
  endpoint — no dots are drawn and no filter is offered. Silence is better than
  a page of red dots meaning "we did not ask".
- The roulette draws from the recipients that are ticked, not from everything on
  screen, and says so in a hint beside the button. Its slowdown is gentler and
  runs longer: the strip used to cover 99.8% of the way in three seconds and
  then creep the rest at three pixels a second, which reads as a dead stop
  rather than as suspense. The winner is now marked with a knock, a ring thrown
  off the card and a wash of its rarity colour.
- On a phone the side menu becomes a bar along the bottom, where the same links
  cost nothing horizontally and land where a thumb already is. It had been
  taking 58 of a 375-pixel screen — a sixth of the width, which the tables
  wanted. The captions come back under the icons, and signing out sits apart as
  a narrow icon so it is not tapped by mistake. Wider screens are unchanged.
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

- The Active/Disabled badge is text alone in the client list, the recipient list
  and on a client's page: the connection dot sits beside it now, and two dots on
  one row read as one thing said twice. Elsewhere — xray on the dashboard, a
  disabled inbound — the dot stays.
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
