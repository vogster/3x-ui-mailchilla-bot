"""
What the test stand runs against instead of the real thing: a 3x-ui kept in
memory, and an outbox that is a folder.

docs/stand.py starts a panel on top of these two. They live apart from it so
they can be read — and tested — without anything being started: the stand is
only worth having while it behaves like the panel it stands in for, and the
one way it stops is by a method of XuiClient gaining a request that nothing
here answers. tests/test_stand.py watches for exactly that.

The people and the inbounds are docs/demo_data.py, the same invented
installation the screenshots are taken of. Unlike the screenshot script's
stubs, this one keeps what is done to it: a client created on the stand is in
the list afterwards, a device that is forgotten stays forgotten — until the
stand is restarted, which puts everybody back.
"""
import base64
import copy
import json
import os
import time

import html as html_escape

import demo_data as demo
import mailer
from xui_client import XuiClient

GB = 1024 ** 3
DAY_MS = 86400000

# Which demo groups carry a device limit, so that the stand has clients of
# both kinds: Family is limited, Standard is not.
DEVICE_LIMITS = {"Family": 3}

# What a registered device looks like when 3x-ui lists it. A phone names its
# model; the desktop app sends only the system it runs on.
SAMPLE_DEVICES = [
    ("Pixel 9", "android", "15", "Happ/3.2"),
    ("iPhone 15", "ios", "18.1", "Happ/3.2"),
    ("", "windows", "11", "v2rayN/7.4"),
]


class Panel:
    """As much of 3x-ui as Mailchilla asks of it, in memory and changeable."""

    def __init__(self):
        self.clients = demo.clients()
        self.devices = {}
        now = int(time.time() * 1000)
        limited = 0
        for client in self.clients:
            client["limitHwid"] = DEVICE_LIMITS.get(client["group"], 0)
            if not client["limitHwid"]:
                continue
            # The first limited client has every slot taken and the second has
            # none, so both ends are on the stand without anybody arranging it.
            limited += 1
            count = {1: client["limitHwid"], 2: 0}.get(limited, 2)
            self.devices[client["email"]] = [
                {"id": client["id"] * 10 + n, "deviceModel": model, "deviceOs": system,
                 "osVersion": version, "userAgent": agent,
                 "fingerprint": f"{(client['id'] * 7919 + n * 104729) % 16 ** 12:012x}",
                 "firstSeen": now - (40 - n * 9) * DAY_MS, "lastSeen": now - n * 5400000}
                for n, (model, system, version, agent) in enumerate(SAMPLE_DEVICES[:count])]

    def find(self, key):
        """A client by uuid, by number or by its identifier — the live object."""
        key = str(key)
        return next((c for c in self.clients
                     if c["uuid"] == key or str(c["id"]) == key or c["email"] == key), None)

    def add_client(self, email, client_uuid=None, limit_gb=0, expire_days=0, inbound_ids=None,
                   comment="", flow=None, group="", limit_hwid=0):
        if self.find(email) or not inbound_ids:
            # As the real panel does: a duplicate identifier is refused, and so
            # is a client with nowhere to go.
            return None, []
        now = int(time.time() * 1000)
        number = max((c["id"] for c in self.clients), default=0) + 1
        client_uuid = client_uuid or f"c0ffee{number:02d}-1111-2222-3333-444455556666"
        self.clients.append({
            "uuid": client_uuid, "id": number, "email": email, "comment": comment or "",
            "enable": True, "totalGB": int(limit_gb or 0) * GB,
            "expiryTime": now + int(expire_days) * DAY_MS if expire_days else 0,
            "createdAt": now, "subId": f"sub{number:02d}aurora",
            "traffic": {"up": 0, "down": 0}, "inboundIds": [int(i) for i in inbound_ids],
            "group": group or "", "flow": flow or "",
            "limitHwid": max(int(limit_hwid or 0), 0)})
        return client_uuid, list(inbound_ids)

    def update_client(self, client_uuid, total_gb=None, expire_days=None, enable=None,
                      new_remark=None, new_comment=None, group=None, limit_hwid=None,
                      client_obj=None):
        client = self.find(client_uuid)
        if not client:
            return False
        if total_gb is not None:
            client["totalGB"] = int(total_gb) * GB if total_gb > 0 else 0
        if expire_days is not None:
            client["expiryTime"] = (int(time.time() * 1000) + int(expire_days) * DAY_MS
                                    if expire_days > 0 else 0)
        if enable is not None:
            client["enable"] = bool(enable)
        if new_remark is not None:
            self.devices[new_remark] = self.devices.pop(client["email"], [])
            client["email"] = new_remark
        if new_comment is not None:
            client["comment"] = new_comment
        if group is not None:
            client["group"] = group
        if limit_hwid is not None:
            client["limitHwid"] = max(int(limit_hwid), 0)
        return True

    def delete_client(self, client_uuid, keep_traffic=False, client_obj=None):
        client = self.find(client_uuid)
        if not client:
            return False, None
        self.clients.remove(client)
        self.devices.pop(client["email"], None)
        return True, None

    def attach_or_detach(self, remark, action, inbound_ids):
        client = self.find(remark)
        if not client:
            return False
        wanted = [int(i) for i in inbound_ids]
        if action == "attach":
            client["inboundIds"] = list(dict.fromkeys(client["inboundIds"] + wanted))
        else:
            client["inboundIds"] = [i for i in client["inboundIds"] if i not in wanted]
        return True

    def rename_group(self, old_name, new_name):
        moved = [c for c in self.clients if c["group"] == old_name]
        for client in moved:
            client["group"] = new_name
        # An empty group is not a row in 3x-ui, and renaming one reports failure.
        return bool(moved)

    def traffic(self, email):
        client = self.find(email)
        if not client:
            return None
        return {"up": client["traffic"]["up"], "down": client["traffic"]["down"],
                "total": client["totalGB"], "expiryTime": client["expiryTime"],
                "enable": client["enable"]}

    def forget_devices(self, email, device_id=None):
        if device_id is None:
            self.devices[email] = []
        else:
            self.devices[email] = [d for d in self.devices.get(email, [])
                                   if str(d["id"]) != str(device_id)]
        return True


def _refuse(self, method, endpoint, **kwargs):
    """
    The last line: nothing on the stand may reach a real 3x-ui.

    Every method that talks to the panel is replaced below; one that is added
    later and forgotten here ends up at this instead of at whatever XUI_URL a
    stray .env happens to name.
    """
    raise RuntimeError(f"the stand has no answer for {method} {endpoint} — "
                       f"teach docs/stand_panel.py to give one")


def install(panel: Panel) -> dict:
    """
    Puts the in-memory panel behind every XuiClient method that would otherwise
    make a request. Returns what was replaced, name to original, so that a test
    can put it back.
    """
    links = [
        {"url": "vless://c0ffee@aurora.example.com:443?security=reality&type=tcp"
                "#VLESS-REALITY-443",
         "protocol": "vless", "label": "VLESS-REALITY-443", "port": 443},
        {"url": "ss://Y2hhY2hhMjA@aurora.example.com:8388#Shadowsocks-8388",
         "protocol": "ss", "label": "Shadowsocks-8388", "port": 8388},
    ]
    stand_ins = {
        "_request": _refuse,
        "login": lambda self: True,
        "get_all_clients": lambda self: copy.deepcopy(panel.clients),
        "get_client_data": lambda self, email: copy.deepcopy(panel.find(email)),
        "get_inbounds": lambda self: [dict(i) for i in demo.INBOUNDS],
        "get_server_status": lambda self: copy.deepcopy(demo.SERVER),
        "get_online_emails": lambda self: list(demo.ONLINE),
        "get_last_online": lambda self: demo.last_online(),
        "get_client_links": lambda self, email: copy.deepcopy(links),
        "get_client_traffic": lambda self, email: panel.traffic(email),
        "get_client_devices":
            lambda self, email: copy.deepcopy(panel.devices.get(email, [])),
        "forget_client_devices":
            lambda self, email, device_id=None: panel.forget_devices(email, device_id),
        "add_client": lambda self, **kwargs: panel.add_client(**kwargs),
        "update_client":
            lambda self, client_uuid, **kwargs: panel.update_client(client_uuid, **kwargs),
        "delete_client":
            lambda self, client_uuid, **kwargs: panel.delete_client(client_uuid, **kwargs),
        "rename_group": lambda self, old_name, new_name: panel.rename_group(old_name, new_name),
        # set_client_inbounds itself is left real: its order — attach first,
        # then detach — is the part worth running, and this is all it calls.
        "_attach_or_detach":
            lambda self, remark, action, inbound_ids:
                panel.attach_or_detach(remark, action, inbound_ids),
    }
    replaced = {}
    for name, stand_in in stand_ins.items():
        replaced[name] = XuiClient.__dict__[name]
        setattr(XuiClient, name, stand_in)
    return replaced


def uninstall(replaced: dict):
    """Undoes install(), for the tests: XuiClient is one class for the whole process."""
    for name, original in replaced.items():
        setattr(XuiClient, name, original)


class Outbox:
    """
    Where the stand's letters go instead of to an SMTP server: a folder, one
    .html and one .txt per letter and a line of json to say whose it was.

    Nothing on the stand is ever sent. That is the point of it — every address
    in the demo data is invented, and a stand that mailed them would be mailing
    strangers the moment somebody typed a real one in.
    """

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def keep(self, to_email, subject, message) -> str:
        """Writes one letter down. Returns the name it can be read back by."""
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
        safe = "".join(ch if ch.isalnum() or ch in "@.-" else "_" for ch in str(to_email))
        name = f"{stamp}-{safe}"
        if isinstance(message, mailer.PlainText):
            # A letter that is text alone — a reply typed by a person. Its
            # "html" file is only that text made safe to open in a browser.
            text, images = str(message), None
            html = f"<pre>{html_escape.escape(text)}</pre>"
        elif isinstance(message, str):
            html, text, images = message, "", None
        else:
            html, text = message.html, message.text or ""
            images = getattr(message, "images", None)
        # A picture travels inside a real letter and is named by Content-ID; a
        # file opened in a browser has nothing to resolve that against, so the
        # picture is written into the page itself.
        for cid, data in (images or {}).items():
            if data:
                html = html.replace(f"cid:{cid}",
                                    "data:image/png;base64," + base64.b64encode(data).decode())
        base = os.path.join(self.directory, name)
        with open(base + ".html", "w", encoding="utf-8") as handle:
            handle.write(html)
        with open(base + ".txt", "w", encoding="utf-8") as handle:
            handle.write(text)
        with open(base + ".json", "w", encoding="utf-8") as handle:
            json.dump({"to": str(to_email), "subject": str(subject), "at": time.time()},
                      handle, ensure_ascii=False)
        return name

    def letters(self, limit: int = 50) -> list:
        """The letters kept, newest first."""
        out = []
        for entry in sorted(os.listdir(self.directory), reverse=True):
            if not entry.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.directory, entry), encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            out.append({"name": entry[:-5], "to": meta.get("to", ""),
                        "subject": meta.get("subject", ""),
                        "at": time.strftime("%H:%M:%S", time.localtime(meta.get("at", 0)))})
            if len(out) >= limit:
                break
        return out

    def read(self, name: str, part: str):
        """One part of a kept letter — "html" or "txt" — or None when there is no such letter."""
        if part not in ("html", "txt"):
            return None
        # Only a name this outbox handed out: the argument comes from a URL.
        path = os.path.join(self.directory, os.path.basename(str(name)) + "." + part)
        if os.path.basename(str(name)) != str(name) or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read()
