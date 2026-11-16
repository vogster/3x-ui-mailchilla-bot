"""
Writing the installation's own files: settings.json, tariffs.json,
email_texts.json.

One writer for all three, because all three need the same two things and each
used to get only the first.

Atomically: the json goes to a temporary file beside the real one and is moved
over it, so a write cut short leaves the old file whole rather than half a new
one.

And privately. settings.json holds the mailbox passwords and the Gotify token,
tariffs.json the words that let people in — and a file made by a plain open()
takes the process's umask, which under systemd is 022: readable by every
account on the server. install.sh was careful to chmod .env to 600 and the
panel then wrote the very same secrets next door for anybody to read.
"""
import json
import os

# Owner read and write, nobody else anything.
PRIVATE = 0o600


def _private(path, flags):
    return os.open(path, flags, PRIVATE)


def write_json(path: str, data, **dump_options):
    """Writes `data` to `path` as json, atomically, readable by its owner alone."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", opener=_private) as f:
        json.dump(data, f, ensure_ascii=False, indent=2, **dump_options)
        f.write("\n")
    # The mode given to open() applies only to a file it creates. A temporary
    # file left behind by an interrupted write keeps whatever it had, and that
    # is what would be moved into place.
    try:
        os.chmod(tmp_path, PRIVATE)
    except OSError:
        pass
    os.replace(tmp_path, path)
