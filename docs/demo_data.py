"""
The made-up installation the screenshots are taken of.

Kept beside the shot script rather than inside it so the two can be read apart:
this is what the panel is meant to look like, that is how it is photographed.
Everything here is invented — no real address, no real traffic figure.
"""
import time

GB = 1024 ** 3
NOW = int(time.time() * 1000)
DAY = 86400000

SERVICE_NAME = "Aurora VPN"

# (identifier, name, enabled, used bytes, limit bytes, registered days ago,
#  expires in days or None)
PEOPLE = [
    ("anna.bright@example.com", "Anna Bright",       True,  215 * GB, 100 * GB,  44,   46),
    ("ben@example.com",         "Ben",               True,   88 * GB, 100 * GB,  66,   24),
    ("d.hollis@example.com",    "Daniel Hollis",     True,  512 * GB, 100 * GB,  15, None),
    ("ellie.k@example.com",     "Ellie",             True,    8 * GB,        0,  91,   -1),
    ("g.parry@example.com",     "Grace Parry",       True,  129 * GB, 100 * GB,  73,   17),
    ("iris@example.com",        "Iris",              True,         0,        0,  93,   -3),
    ("k.ivens@example.com",     "",                  True,   12 * GB, 100 * GB,  47, None),
    ("kitchen-tv",              "TV in the kitchen", True,  341 * GB, 100 * GB,   8,   82),
    ("lab-router",              "Router in the workshop", True, 902 * GB, 100 * GB, 5, 85),
    ("m.vaughan@example.com",   "Mia Vaughan",       True,   45 * GB, 100 * GB,  59,   32),
    ("nathan@example.com",      "Nathan",            True,  176 * GB,        0,  24, None),
    ("olive.s@example.com",     "Olive Sands",       True,   22 * GB, 100 * GB,  80,   10),
    ("paul@example.com",        "Paul",              True,  389 * GB, 100 * GB,  12,   78),
    ("r.zane@example.com",      "Rose Zane",         True,   11 * GB, 100 * GB,  87, None),
    ("s.novak@example.com",     "Sasha Novak",       True,   97 * GB,        0,  51,   39),
    ("t.okafor@example.com",    "Tom Okafor",        True,  156 * GB, 100 * GB,  33,   57),
    ("vera.l@example.com",      "Vera Lantz",        False,  63 * GB, 100 * GB,  70,   20),
    ("w.hume@example.com",      "Will Hume",         True,    3 * GB, 100 * GB,   2,   88),
    ("zoe@example.com",         "Zoe",               False,  74 * GB, 100 * GB,  62,   -8),
]

# Who the panel reports as connected at the moment of the shot.
ONLINE = ["anna.bright@example.com", "lab-router", "paul@example.com",
          "t.okafor@example.com", "w.hume@example.com"]

INBOUNDS = [
    {"id": 1, "remark": "VLESS-REALITY-443", "protocol": "vless", "port": 443,
     "enable": True, "clients": 17},
    {"id": 2, "remark": "Shadowsocks-8388", "protocol": "shadowsocks", "port": 8388,
     "enable": True, "clients": 6},
    {"id": 3, "remark": "Trojan-8443 (old)", "protocol": "trojan", "port": 8443,
     "enable": False, "clients": 2},
]

def _pair(used_gb, total_gb):
    used, total = int(used_gb * GB), int(total_gb * GB)
    return {"used": used, "total": total,
            "percent": round(used / total * 100, 1) if total else 0}


# The same shape XuiClient.get_server_status hands back — the dashboard formats
# it further, and a dict of the wrong shape shows up as a 500 rather than as a
# missing figure.
SERVER = {
    "cpu": 12.4,
    "cpu_cores": 4,
    "loads": [0.28, 0.34, 0.31],
    "mem": _pair(1.9, 4.0),
    "swap": _pair(0.1, 2.0),
    "disk": _pair(11.3, 40.0),
    "uptime": 18 * 86400 + 4 * 3600,
    "connections": 42,
    "panel_version": "2.6.2",
    "xray_state": "running",
    "xray_version": "25.8.3",
    "xray_error": "",
}


def clients():
    """The list the way 3x-ui hands it over."""
    out = []
    for i, (ident, name, enable, used, limit, ago, expires) in enumerate(PEOPLE, 1):
        out.append({
            "uuid": f"c0ffee{i:02d}-1111-2222-3333-444455556666",
            "id": i,
            "email": ident,
            "comment": name,
            "enable": enable,
            "totalGB": limit,
            "expiryTime": 0 if expires is None else NOW + expires * DAY,
            "createdAt": NOW - ago * DAY,
            "subId": f"sub{i:02d}aurora",
            "traffic": {"up": used // 4, "down": used - used // 4},
            # Everyone is on the REALITY inbound; every third one also on
            # Shadowsocks, so the client card has more than a single chip to
            # show and the leftover-link branch gets exercised too.
            "inboundIds": [1, 2] if i % 3 == 1 else [1],
        })
    return out
