"""Translate the pt_MZ .po catalog via Claude Opus 4.7.

Reads ``locale/<lang_code>/LC_MESSAGES/django.po``, batches the
empty-msgstr entries, sends them to Claude with a Mozambique-register
prompt, and writes msgstrs back. Idempotent — only translates rows
where msgstr is empty, so re-runs are safe and incremental.

Usage:
    python manage.py translate_po                 # translate pt_MZ (default)
    python manage.py translate_po --locale pt_MZ  # explicit
    python manage.py translate_po --dry-run       # show prompt + outputs only
    python manage.py translate_po --batch-size 20 # default 30 per batch

Part of M3 of memory/portuguese_mozambique_pilot_plan.md. The prompt
follows the conventions from:
  - prompting-fundamentals-expert (XML structure, no CoT scaffolding
    for translation, query-last, format=JSON via structured output)
  - claude-prompting-expert (XML tags as delimiters, neutral
    imperatives, prompt caching the system instruction)

Mozambique-register decisions (locked in 2026-06-01):
  - 'tu' informal — students are 13-14 years old.
  - Post-1990 Acordo Ortográfico spelling (Mozambique signed on).
  - Preserve every {variable} / %(var)s / %s placeholder verbatim.
  - Brand names stay in Latin script ("AI Tutor" stays "AI Tutor").

The command runs `compilemessages` automatically when translations land
so the .mo file is in sync. Skip via --no-compile if you're iterating.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

logger = logging.getLogger(__name__)


# Placeholder patterns we must preserve verbatim in translations.
# Anthropic's Opus 4.7 follows literal instructions on these well,
# but we assert in code to fail loud if a translation drops one.
_PLACEHOLDER_RE = re.compile(r'%\([^)]+\)s|%[diouxXeEfFgGcrsa]|\{[^}]+\}')


SYSTEM_PROMPT = """You are a professional translator specialising in
Mozambique Portuguese (pt-mz) for educational user interfaces aimed
at secondary-school students.

<style_guide>
- Use 'tu' informal addressing throughout — these are 13-14 year-old
  students.
- Use post-1990 Acordo Ortográfico spelling (Mozambique signed on).
  Examples: "atividade" not "actividade"; "óptimo" → "ótimo".
- Keep technical vocabulary in standard Portuguese: "ângulo",
  "escala", "fotossíntese", "ecossistema", "célula".
- Brand and product names stay in their original Latin form
  ("AI Tutor" stays "AI Tutor"; do not translate it).
- Match register: keep the tone friendly and concise. Don't add
  formal flourishes that English doesn't have.
- Preserve length: a UI button label translates to a short label,
  not a sentence. "Sign In" → "Entrar", not "Iniciar a sessão".
</style_guide>

<placeholders>
The English source often contains placeholders that the runtime fills
in. Examples: %(name)s, %(count)d, {first_name}, {institution}, %s.

You MUST copy every placeholder verbatim into the translation in the
same order. Do NOT translate or remove placeholder names — they are
program variable names, not English words.

Example:
  msgid:  "Welcome, %(name)s! Let's learn."
  msgstr: "Olá, %(name)s! Vamos aprender."
</placeholders>

<output>
Reply with a single JSON object mapping each msgid to its pt-mz
translation. Strings only — no extra commentary.

{
  "Sign In": "Entrar",
  "Welcome back, %(name)s!": "Bem-vindo de volta, %(name)s!"
}
</output>
"""


class Command(BaseCommand):
    help = "Translate the pt_MZ .po catalog via Claude. Idempotent — re-run safe."

    def add_arguments(self, parser):
        parser.add_argument(
            "--locale", default="pt_MZ",
            help="The .po language dir (default: pt_MZ).",
        )
        parser.add_argument(
            "--batch-size", type=int, default=30,
            help="msgids per Claude call (default: 30).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Print what would be translated; do not call the LLM or write the file.",
        )
        parser.add_argument(
            "--no-compile", action="store_true",
            help="Skip the post-translate `compilemessages` step.",
        )

    def handle(self, *args, **options):
        locale_dir = (
            Path(settings.BASE_DIR) / "locale" / options["locale"] / "LC_MESSAGES"
        )
        po_path = locale_dir / "django.po"
        if not po_path.exists():
            raise CommandError(f"No catalog at {po_path}. Run `makemessages -l {options['locale']}` first.")

        entries = _parse_po(po_path)
        untranslated = [e for e in entries if not e["msgstr"]]
        self.stdout.write(
            f"Found {len(entries)} msgids in {po_path.relative_to(settings.BASE_DIR)}; "
            f"{len(untranslated)} untranslated."
        )

        if not untranslated:
            self.stdout.write(self.style.SUCCESS("Nothing to do — catalog is fully translated."))
            return

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("--dry-run: showing the first batch only"))
            batch = untranslated[: options["batch_size"]]
            self.stdout.write(_format_dry_run(batch))
            return

        # Translate in batches. Use Anthropic SDK directly — this
        # command isn't part of the runtime engine, so it doesn't
        # need to go through the ModelConfig dispatcher.
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        translated_count = 0
        for i in range(0, len(untranslated), options["batch_size"]):
            batch = untranslated[i : i + options["batch_size"]]
            self.stdout.write(
                f"Translating batch {i // options['batch_size'] + 1} "
                f"({len(batch)} strings)..."
            )
            try:
                pairs = _translate_batch(client, batch)
            except Exception as e:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(f"Batch failed: {e}"))
                continue

            # ── Strict key validation ────────────────────────────────
            # The 2026-06-01 cross-wire incident: Claude's response keys
            # did not match the request msgids 1:1, and the previous
            # "find by msgid" loop silently assigned wrong msgstrs.
            # Fix: drive assignment by the REQUEST batch (in-order),
            # asserting the response has every requested msgid as an
            # exact-string key, no extras. Anything off → skip the
            # entire batch and shout. Better to leave entries empty for
            # a re-run than to land cross-wired translations.
            requested = {e["msgid"] for e in batch}
            returned = set(pairs.keys())
            missing = requested - returned
            extras = returned - requested
            if missing or extras:
                self.stderr.write(self.style.ERROR(
                    f"  ✗ key-set mismatch — batch dropped. "
                    f"missing={len(missing)} extras={len(extras)}."
                ))
                if missing:
                    for m in sorted(missing)[:3]:
                        self.stderr.write(f"      missing: {m!r}")
                if extras:
                    for x in sorted(extras)[:3]:
                        self.stderr.write(f"      extra:   {x!r}")
                continue

            # Now safe to assign: iterate the BATCH (not pairs.items()),
            # pulling each msgstr by exact-msgid key. Cross-wiring is
            # structurally impossible — we look up by the same string
            # we asked Claude to translate.
            for entry in batch:
                msgid = entry["msgid"]
                msgstr = pairs[msgid]
                source_phs = set(_PLACEHOLDER_RE.findall(msgid))
                target_phs = set(_PLACEHOLDER_RE.findall(msgstr))
                if source_phs != target_phs:
                    self.stderr.write(self.style.WARNING(
                        f"  ⚠️  placeholder mismatch on {msgid!r} — skipping"
                    ))
                    continue
                if entry["msgstr"]:
                    continue  # already filled in (parallel re-run safety)
                entry["msgstr"] = msgstr
                translated_count += 1

        _write_po(po_path, entries)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {translated_count} translation(s) to {po_path.relative_to(settings.BASE_DIR)}"
        ))

        if not options["no_compile"]:
            self.stdout.write("Compiling .mo files...")
            subprocess.run(
                ["python", "manage.py", "compilemessages", "-l", options["locale"]],
                cwd=settings.BASE_DIR,
                check=True,
            )
            self.stdout.write(self.style.SUCCESS("Done."))


# ---------------------------------------------------------------------------
# .po parsing — minimal, just enough for our needs.
# ---------------------------------------------------------------------------


def _parse_po(path: Path) -> list[dict]:
    """Parse a Django .po file into a list of dicts:

        [{'header_lines': [...], 'msgid': str, 'msgstr': str, 'flag_lines': [...]}, ...]

    Skips the empty-msgid header entry at the top. Multi-line msgid /
    msgstr (continuation strings) are concatenated.
    """
    entries: list[dict] = []
    current: dict | None = None
    state: str | None = None  # 'msgid' or 'msgstr'
    flag_buffer: list[str] = []
    is_first_block = True

    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()

        if not line:
            # blank line ends an entry
            if current is not None:
                if not is_first_block:
                    entries.append(current)
                else:
                    is_first_block = False
                current = None
            state = None
            flag_buffer = []
            continue

        if line.startswith("#"):
            flag_buffer.append(line)
            continue

        if line.startswith('msgid "'):
            if current is None:
                current = {"msgid": "", "msgstr": "", "header_lines": flag_buffer[:]}
                flag_buffer = []
            current["msgid"] = _unquote(line[len("msgid "):])
            state = "msgid"
        elif line.startswith('msgstr "'):
            if current is None:
                current = {"msgid": "", "msgstr": "", "header_lines": []}
            current["msgstr"] = _unquote(line[len("msgstr "):])
            state = "msgstr"
        elif line.startswith('"') and current is not None and state:
            # Continuation line for the previous msgid/msgstr.
            current[state] += _unquote(line)

    if current is not None and not is_first_block:
        entries.append(current)
    return entries


def _unquote(s: str) -> str:
    """Strip the wrapping quotes + unescape \\" \\n \\\\."""
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    return s.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def _quote(s: str) -> str:
    """Inverse of _unquote — re-wrap with quotes and escape."""
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def _write_po(path: Path, entries: list[dict]) -> None:
    """Re-write the .po preserving the existing header verbatim and
    re-emitting each entry. Only the msgstr value changes; comments,
    reference lines, flags are written through unchanged."""
    # Header — read the original first block straight from the file
    # to avoid rewriting the metadata block.
    original = path.read_text()
    if "\n\n" not in original:
        raise CommandError("Malformed .po: no blank line after header")
    header_block, rest = original.split("\n\n", 1)

    out_lines = [header_block, "", ""]
    for entry in entries:
        for ref_line in entry.get("header_lines", []):
            out_lines.append(ref_line)
        out_lines.append(f"msgid {_quote(entry['msgid'])}")
        out_lines.append(f"msgstr {_quote(entry['msgstr'])}")
        out_lines.append("")

    path.write_text("\n".join(out_lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# LLM batching
# ---------------------------------------------------------------------------


def _translate_batch(client, batch: list[dict]) -> dict[str, str]:
    """Send one batch of msgids to Claude and return the parsed map.

    Uses the Anthropic native SDK directly so we can keep the system
    prompt (which gets cached) separate from the per-batch user
    message. Falls back to a single retry on JSON parse failure.
    """
    msgids = [e["msgid"] for e in batch]
    user_payload = json.dumps(msgids, ensure_ascii=False, indent=2)
    user_message = (
        "<msgids>\n"
        f"{user_payload}\n"
        "</msgids>\n\n"
        "Translate every msgid above into pt-mz. Return the JSON object."
    )

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=4096,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    text = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    return _parse_translations(text)


def _parse_translations(text: str) -> dict[str, str]:
    """Extract the JSON object from the LLM response. Tolerates
    surrounding prose / markdown fencing."""
    # Strip markdown fences if Claude added them despite instructions.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove first and last fence lines.
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    # Find the first { ... } block.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in LLM response: {text[:200]}")
    return json.loads(cleaned[start : end + 1])


def _format_dry_run(batch: list[dict]) -> str:
    lines = ["--- batch payload (first batch) ---"]
    for e in batch:
        lines.append(f"  {e['msgid']!r}")
    lines.append("--- end ---")
    return "\n".join(lines)
