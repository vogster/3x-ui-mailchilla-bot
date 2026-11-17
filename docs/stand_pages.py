"""
The stand's own page, at /stand: a letter to the bot, and what it sent back.

The panel is half of Mailchilla. The other half reads a mailbox, and a stand
without a mailbox would leave the registration, /status and /broadcast paths —
which is to say most of the bot — untried. So this page plays the mailbox: the
form hands a letter straight to the handler the poll loop would have called,
and the replies turn up in the list underneath, out of the outbox folder.

It exists only on the stand. docs/stand.py adds these routes to the panel's
application after importing it; an installation never has them.
"""
import html

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

import config
import email_bot
from admin.deps import require_auth

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stand · {service}</title>
<style>
  body {{ background:#0c0c0d; color:#ededef; font:15px/1.5 system-ui,sans-serif; margin:0; }}
  main {{ max-width:860px; margin:0 auto; padding:28px 16px 60px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:15px; margin:30px 0 10px; color:#a0a0a8;
       text-transform:uppercase; letter-spacing:.06em; }}
  a {{ color:#7cc4ff; }} .muted {{ color:#8a8a93; }}
  label {{ display:block; margin:12px 0 4px; color:#a0a0a8; font-size:13px; }}
  input[type=text], textarea {{ width:100%; box-sizing:border-box; background:#161618; color:#ededef;
       border:1px solid #26262a; border-radius:8px; padding:9px 11px; font:inherit; }}
  textarea {{ min-height:90px; resize:vertical; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  button {{ margin-top:16px; background:#ededef; color:#0c0c0d; border:0; border-radius:8px;
       padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }}
  .note {{ margin:16px 0 0; padding:10px 14px; border-radius:8px; background:#161618;
       border:1px solid #26262a; }}
  table {{ width:100%; border-collapse:collapse; }}
  td, th {{ text-align:left; padding:9px 8px; border-bottom:1px solid #1d1d20; vertical-align:top; }}
  th {{ color:#8a8a93; font-weight:500; font-size:13px; }}
  .mono {{ font-family:Consolas,Menlo,monospace; font-size:13px; }}
  @media (max-width:560px) {{ .row {{ grid-template-columns:1fr; }} }}
</style></head><body><main>
<h1>Test stand</h1>
<p class="muted">3x-ui here is an in-memory stand-in and no letter leaves this machine.
<a href="/">Back to the panel</a> · <a href="/mail/bot">The mailboxes</a></p>

<h2>A letter to the bot</h2>
<form method="post" action="/stand/letter">
  <div class="row">
    <div><label for="sender">From</label>
         <input type="text" id="sender" name="sender" value="{sender}"></div>
    <div><label for="name">Sender's name</label>
         <input type="text" id="name" name="name" value="{name}"></div>
  </div>
  <label for="subject">Subject</label>
  <input type="text" id="subject" name="subject" value="{subject}"
         placeholder="a code word, /status, /help, /broadcast …">
  <label for="body">Text</label>
  <textarea id="body" name="body">{body}</textarea>
  <label><input type="checkbox" name="proven" {proven}>
    the mail server vouches for the sender (what /broadcast asks for;
    the administrator here is <span class="mono">{admin}</span>)</label>
  <button type="submit">Hand it to the bot</button>
</form>
{note}

<h2>Words that work</h2>
<p class="mono">{words}</p>

<h2>Outbox</h2>
{outbox}
</main></body></html>
"""


class _NoMailbox:
    """The handler marks the letter read when it is done; here there is nothing to mark."""

    def store(self, *args):
        return "OK", [b""]


def _outbox_table(outbox) -> str:
    letters = outbox.letters()
    if not letters:
        return '<p class="muted">Nothing has been sent yet.</p>'
    rows = "".join(
        f'<tr><td class="mono">{html.escape(l["at"])}</td>'
        f'<td class="mono">{html.escape(l["to"])}</td><td>{html.escape(l["subject"])}</td>'
        f'<td><a href="/stand/outbox/{html.escape(l["name"])}/html" target="_blank">html</a> · '
        f'<a href="/stand/outbox/{html.escape(l["name"])}/txt" target="_blank">text</a></td></tr>'
        for l in letters)
    return ("<table><thead><tr><th>When</th><th>To</th><th>Subject</th><th></th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def add_routes(app, outbox, words, received=None):
    """
    Adds /stand to the panel's application.

    `words` is a callable returning the code words that currently open a
    tariff, so the page can say what to type without anybody opening another
    tab to look. `received`, when given, is told of every letter handed to the
    bot — the stand files it in the bot's in-memory Inbox, so the Mail page
    shows the same correspondence this one does.
    """

    def render(request: Request, note: str = "", form: dict = None):
        form = form or {}
        return HTMLResponse(PAGE.format(
            service=html.escape(config.SERVICE_NAME),
            sender=html.escape(form.get("sender", "newcomer@example.com")),
            name=html.escape(form.get("name", "Newcomer")),
            subject=html.escape(form.get("subject", "")),
            body=html.escape(form.get("body", "")),
            proven="checked" if form.get("proven") else "",
            admin=html.escape(config.ADMIN_EMAIL or "not set"),
            note=f'<p class="note">{html.escape(note)}</p>' if note else "",
            words=html.escape(", ".join(words()) or "none — add a code on the Tariffs page"),
            outbox=_outbox_table(outbox)))

    @app.get("/stand", response_class=HTMLResponse)
    def stand_page(request: Request, note: str = ""):
        auth_redirect = require_auth(request)
        if auth_redirect:
            return auth_redirect
        return render(request, note)

    @app.post("/stand/letter", response_class=HTMLResponse)
    def stand_letter(request: Request, sender: str = Form(""), name: str = Form(""),
                     subject: str = Form(""), body: str = Form(""), proven: str = Form("")):
        auth_redirect = require_auth(request)
        if auth_redirect:
            return auth_redirect
        form = {"sender": sender.strip(), "name": name.strip(), "subject": subject,
                "body": body, "proven": proven == "on"}
        if "@" not in form["sender"]:
            return render(request, "The sender has to be an address.", form)
        before = len(outbox.letters(limit=1000))
        try:
            # What the poll loop does with a letter it has decided to answer:
            # the address lowered, as inbox.sender_name_from hands it over.
            email_bot.process_message(b"1", form["sender"].lower(), subject, body, _NoMailbox(),
                                      form["name"], authenticated=form["proven"])
        except Exception as e:
            # Shown rather than raised: on the stand a failing handler is the
            # thing being looked at, not a broken page.
            return render(request, f"The handler raised {type(e).__name__}: {e}", form)
        if received is not None:
            received(form["sender"].lower(), form["name"], subject, body)
        sent = len(outbox.letters(limit=1000)) - before
        return render(request, f"Handled. Letters sent in reply: {sent}.", form)

    @app.get("/stand/outbox/{name}/{part}")
    def stand_outbox(request: Request, name: str, part: str):
        auth_redirect = require_auth(request)
        if auth_redirect:
            return auth_redirect
        content = outbox.read(name, part)
        if content is None:
            return RedirectResponse("/stand?note=No+such+letter", status_code=303)
        if part == "txt":
            return PlainTextResponse(content)
        return HTMLResponse(content)
