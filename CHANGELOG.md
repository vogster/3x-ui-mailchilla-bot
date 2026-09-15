# Changelog

Notable changes to Mailchilla. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [semantic versioning](https://semver.org/lang/ru/).

The `mailchilla update` command reads the section belonging to a version out of
this file and shows it before asking for confirmation, so each entry should
read as something a person wants to know before updating.

## [Unreleased]

### Added

- **A support address and a link to the instructions**, both set on the General
  tab. The address goes into the footer of every letter and, in its own words,
  into the registration letter — where somebody is setting a connection up for
  the first time. The link becomes a button beside the "Add to Happ / Incy"
  ones, and the Letters tab can switch that button off the way it switches off
  the QR code. Both are empty by default: a letter should not invite anybody to
  write to a blank address. The address is a link in both places, so a reader
  presses it and their mail client opens a new letter. Every letter text can use
  `{support}` and `{manual}`, the way it already uses `{service}`.

- **Tariffs.** What a new client gets — traffic, term, inbounds — is a tariff
  now, and each tariff has its own code word. There can be as many as you like:
  a generous one for the family, a small one for a trial, a closed one with no
  word at all. The Tariffs page creates and edits them, and the create-client
  dialog offers them in a list — pick one and the fields fill themselves in,
  still editable.
- A tariff is read once, when a client is created. Editing one therefore
  changes nothing for anybody already registered, and the page says so rather
  than leaving you to wonder.
- The list of who came in through a code no longer grows without end. The
  count stays exact; the last two hundred addresses are kept, and the page says
  so when there were more. A word handed out openly could otherwise make every
  registration a little more expensive than the last, since the file is
  rewritten whole each time.
- A code can be given a **date it works until** — the last day, counted to its
  end. A word handed out for a weekend then stops on its own instead of waiting
  to be remembered on Monday. Empty means it never runs out by itself.
- **Code words are their own thing**, on a tab of their own beside the tariffs.
  A code is a word, the tariff it opens, how many activations are left in it,
  a note and a switch. Leave the activations empty and it is the word you hand
  out openly; put 1 there and it is a personal invitation, spent by whoever
  uses it. There is no second kind of object and no second form: the number is
  the whole difference.
- A tariff can therefore have several words — a seasonal one beside the
  permanent one — and any of them can be switched off on its own the moment it
  leaks, without touching the tariff or the others.
- Switching a code off keeps the record of who came in through it; removing it
  throws that away, and the confirmation says so.
- **A card for every tariff and every code.** A tariff's card shows who is on
  it, read from 3x-ui, plus its codes and what it hands out. A code's card
  shows everybody who came in through that word, each a click away from their
  own card; somebody deleted from 3x-ui since is still listed by address,
  because it happened.
- **Which tariff a client is on is kept in 3x-ui**, in the client's own group,
  named after the tariff. Nothing of ours has to be kept in step with it: the
  panel shows it, `clients/list` hands it back with everything else, and a
  group changed in 3x-ui by hand is simply the truth. The client list shows it
  beside the name and filters by it — including "without one", which is what
  anybody registered before tariffs existed will be.
- Renaming a tariff renames its group, carrying every client across. Deleting
  one leaves its clients exactly as they are — the label they carry in 3x-ui
  outlives the tariff, the client list marks it, and the tariffs page counts
  such groups under "Groups without a tariff" rather than letting them lurk.
- Two tariffs may no longer share a name, since the name is the group.
- The dashboard counts the tariffs: how many people are on each and what they
  have spent, worked out from the client list it already had rather than from
  another request.
- The broadcast's recipient picker filters by tariff, including "without one",
  so "everybody on Trial whose traffic is running out" is two clicks.
- `/status` names the client's tariff, unless they are on none.
- A client's card says which code word they came in through, linking to that
  code. It is how they arrived rather than what they have now: moving them to
  another tariff, or switching the code off, leaves it as it was.
- A client's card can move them to a tariff: pick one and the limit, the term
  (counted from today) and the inbounds fill themselves in, still editable, and
  nothing is saved until you press Save.

- `mailchilla check` no longer stops at "3x-ui answers". It also says when
  there is no tariff at all, when every code word is switched off or used up,
  when a tariff has no inbounds ticked, and when one names an inbound the panel
  does not have — the four ways registration stops working without a line in
  the log.

### Changed

- Dates are picked in a calendar of the panel's own making rather than the
  browser's. The native one paints itself, cannot be themed, and shows the date
  in the reader's locale — `mm/dd/yyyy` in a Russian interface, which is how a
  date gets read wrong. Ours speaks `дд.мм.гггг`, starts the week on Monday,
  and can be typed into as well as clicked.

- The inbound table has left the dashboard. It is 3x-ui's own list, and which
  inbounds a client gets is a property of their tariff now — the tariff's card
  says it where it matters. What stayed is the fault the table carried: a
  tariff naming an inbound the panel does not have breaks registration at that
  id in silence, and the dashboard names such a tariff.
- The single `CODEWORD` is gone from the settings, along with the traffic
  limit, the term and the inbound list. On the first start after the update
  they become a tariff called "Basic" with the same word, so nothing changes
  for anybody already registered and nobody has to do anything. The old keys
  are left in `settings.json` untouched, so a rollback to 0.1.x finds them.
- A code word is now matched as a whole word, and only in the part of a letter
  its sender actually typed. A reply quoting the welcome letter used to count
  as a fresh registration, and a word could be found inside a longer one —
  harmless with one word, a wrong subscription with several.
- A letter carrying the words of two different tariffs is no longer guessed at:
  the bot writes back asking which one is meant.
- The first-run wizard asks for the first tariff instead of "the rules for new
  clients".

## [0.1.5] - 2026-09-15

### Added

- The client list has a "Last online" column: when 3x-ui last saw each client.
  Minutes and hours while it is recent, a date once it is past a week, a dash
  for somebody who has never connected, and the exact moment in the tooltip.
  It sorts — one click gathers the people who have stopped connecting at the
  top — and keeps itself current alongside the connection dots. On a client's
  own page it is a tile of its own, right after the status, and the connection
  dot has moved there from beside the address: a dot and the moment it is about
  say more together than either did apart. A 3x-ui that does not know the
  endpoint simply leaves both out.

## [0.1.4] - 2026-09-14

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
