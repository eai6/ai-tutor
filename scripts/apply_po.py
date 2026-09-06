#!/usr/bin/env python
"""Fill msgstr entries in a .po file from JSON {msgid: msgstr} maps.

    python scripts/apply_po.py ai_tutor/locale/sw/LC_MESSAGES/django.po batch*.json

Only empty msgstr entries are filled, so re-running never overwrites a
translation that has already been reviewed. Entries whose msgid carries a
%(name)s placeholder are checked: a translation that drops or renames one
would raise at render time, in front of a student, so it is refused here.
"""

import json
import pathlib
import re
import sys


def placeholders(s):
    return set(re.findall(r"%\((\w+)\)s", s)) | set(re.findall(r"\{(\w+)\}", s))


def decode(block):
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', block))


def encode(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(po_path, maps):
    table = {}
    for m in maps:
        table.update(json.loads(pathlib.Path(m).read_text()))

    text = pathlib.Path(po_path).read_text()
    filled = skipped = mismatched = 0

    pattern = re.compile(
        r'(^msgid ((?:"(?:[^"\\]|\\.)*"\s*)+))(msgstr ((?:"(?:[^"\\]|\\.)*"\s*)+))',
        re.M,
    )

    def repl(m):
        nonlocal filled, skipped, mismatched
        msgid = decode(m.group(2))
        existing = decode(m.group(4))
        if not msgid or existing:
            return m.group(0)
        if msgid not in table:
            skipped += 1
            return m.group(0)
        target = table[msgid]
        if placeholders(msgid) != placeholders(target):
            mismatched += 1
            print(f"  PLACEHOLDER MISMATCH, left untranslated: {msgid[:60]!r}")
            return m.group(0)
        filled += 1
        return m.group(1) + "msgstr " + encode(target) + "\n"

    out = pattern.sub(repl, text)
    pathlib.Path(po_path).write_text(out)
    print(f"filled {filled}, still empty {skipped}, refused {mismatched}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
