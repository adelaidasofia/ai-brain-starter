#!/usr/bin/env python3
"""
rename-contacts.py  (macOS only)

After sync.mjs runs, files for unsaved numbers land as +1234567890.md.
This script cross-references macOS Contacts to rename them to real names
and updates the frontmatter + message sender lines inside each file.

Requirements:
  - macOS (contacts are read through Contacts.app via osascript; no Xcode needed)
  - The app you run this from (Claude, Terminal, iTerm, ...) needs permission to
    control Contacts. The first run shows a one-click prompt; to change it later:
    System Settings › Privacy & Security › Automation › <that app> › Contacts

Usage:
  VAULT_ROOT=/path/to/vault python3 rename-contacts.py
  python3 rename-contacts.py --vault /path/to/vault
  python3 rename-contacts.py --vault /path/to/vault --output "Notes/WhatsApp"
"""

from __future__ import annotations

import os
import re
import sys
import subprocess
import tempfile
import argparse
from pathlib import Path

# macOS only: contacts are read through Contacts.app via osascript, which does not
# exist elsewhere. Say so in a sentence instead of dying on a FileNotFoundError
# traceback -- this repo installs on Windows too, and this step is optional.
if sys.platform != "darwin":
    print("rename-contacts.py runs on macOS only: it reads Contacts through "
          f"Contacts.app via osascript (detected platform: {sys.platform}).")
    print("The rest of the WhatsApp export is unaffected; chats just keep their "
          "phone-number names.")
    sys.exit(1)

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser()
parser.add_argument("--vault",  default=os.environ.get("VAULT_ROOT", ""),
                    help="Vault root path (or set VAULT_ROOT env var)")
parser.add_argument("--output", default="🤖 AI Chats/WhatsApp",
                    help="Subfolder within vault (default: 🤖 AI Chats/WhatsApp)")
args = parser.parse_args()

if not args.vault:
    # Auto-detect: walk up 2 levels from this script (scripts/whatsapp/ → scripts/ → vault)
    script_dir  = Path(__file__).resolve().parent
    args.vault  = str(script_dir.parent.parent)
    print(f"Auto-detected vault root: {args.vault}")

WA_DIR = Path(args.vault) / args.output
if not WA_DIR.exists():
    print(f"Error: {WA_DIR} not found. Run sync.mjs first.")
    sys.exit(1)

# ── Export contacts from macOS Contacts via Contacts.app ──────────────────────
# Ask the app that already owns the data. Contacts.app holds Contacts access
# inherently and `osascript` ships with every macOS, so this needs neither an
# Xcode toolchain nor an app bundle of its own:
#   * the previous Swift path ran `swift`, which on a Mac without Xcode is a stub
#     that triggers a multi-GB "install developer tools" prompt. Most people
#     running this are not developers, so that step simply failed for them.
#   * an ad-hoc-built binary never gets its own TCC identity (verified: `tccutil
#     reset` reports "No such bundle identifier" for one, yet it still reads
#     contacts via the host app's grant), so it can never hold Contacts access.
# The remaining gate is the standard one-click Automation prompt, "<host app>
# wants to control Contacts". JXA rather than AppleScript: same two Apple Events,
# about 6x faster on a large address book (3.3s vs 20.8s for ~3k contacts).

JXA = r"""
const app = Application('Contacts');
const names = app.people.name();
const phones = app.people.phones.value();
const rows = [];
for (let i = 0; i < names.length; i++) {
  const nm = names[i] === null ? '' : String(names[i]);
  const ps = phones[i] || [];
  for (const p of ps) if (p) rows.push(nm + '\t' + String(p));
}
rows.join('\n');
"""


def host_app() -> str:
    """The app macOS attributes this script's permissions to.

    TCC grants Automation (and Contacts) to the APP that launched the script,
    never to the script itself. Naming the right one matters: telling someone to
    enable "Terminal" when they ran this from Claude points them at a row that is
    not in their list.
    """
    bundle = os.environ.get("__CFBundleIdentifier", "")
    known = {
        "com.anthropic.claudefordesktop": "Claude",
        "com.apple.Terminal": "Terminal",
        "com.googlecode.iterm2": "iTerm",
        "com.microsoft.VSCode": "Visual Studio Code",
        "dev.warp.Warp-Stable": "Warp",
    }
    if bundle in known:
        return known[bundle]
    term = os.environ.get("TERM_PROGRAM", "")
    if term == "Apple_Terminal":
        return "Terminal"
    if term:
        return term.replace(".app", "")
    return bundle or "the app you ran this from"


print("Reading macOS Contacts...")
with tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False) as f:
    f.write(JXA)
    jxa_path = f.name

result = subprocess.run(
    ["osascript", "-l", "JavaScript", jxa_path], capture_output=True, text=True
)
os.unlink(jxa_path)

if result.returncode != 0:
    err = (result.stderr or "").strip()
    app_name = host_app()
    print(f"\nCould not read Contacts: {err[:300]}")
    print(f"\nmacOS grants this to the app that ran the script, which is {app_name}.")
    print("The first run shows a one-click prompt. If it was denied:")
    print("  1. Open System Settings > Privacy & Security > Automation")
    print(f"  2. Under {app_name}, enable Contacts")
    print("  3. Re-run this script.")
    print("(This step only maps phone numbers to names; skipping it keeps the")
    print("phone-number filenames and does not affect the rest of the export.)")
    sys.exit(1)

# ── Phone → name lookup ───────────────────────────────────────────────────────

def normalize(phone: str) -> str:
    return re.sub(r"\D", "", phone)

phone_map: dict[str, str] = {}
for line in result.stdout.splitlines():
    if "\t" not in line:
        continue
    name, phone = line.split("\t", 1)
    d = normalize(phone)
    if len(d) < 7:
        continue
    phone_map[d] = name
    if d.startswith("1") and len(d) == 11:
        phone_map[d[1:]] = name          # US: without country code
    if d.startswith("57") and len(d) == 12:
        phone_map[d[2:]] = name          # Colombia: without country code

print(f"Loaded {len(phone_map):,} phone entries")

# Zero entries means nothing can be renamed. Say why and stop, rather than
# scanning every file to change nothing and leaving the user to guess.
if not phone_map:
    print("\nNo usable contacts came back, so no file can be renamed.")
    print("Either the address book is empty, or Contacts returned no data. Check")
    print("System Settings > Privacy & Security > Automation >")
    print(f"{host_app()} > Contacts, then re-run.")
    sys.exit(1)

def lookup(digits: str) -> str | None:
    return (phone_map.get(digits)
         or phone_map.get(digits[1:]  if digits.startswith("1")  else "")
         or phone_map.get("1" + digits)
         or phone_map.get(digits[2:]  if digits.startswith("57") else "")
         or phone_map.get("57" + digits))

# ── Rename and update files ───────────────────────────────────────────────────

def sanitize(name: str) -> str:
    return re.sub(r'[/\\?%*:|"<>\[\]]', "-", name).strip()

matched, unmatched = 0, 0

for filepath in sorted(WA_DIR.glob("+*.md")):
    digits = normalize(filepath.stem)
    name   = lookup(digits)

    if not name:
        unmatched += 1
        continue

    new_stem = sanitize(name)
    dst      = WA_DIR / (new_stem + ".md")

    if dst.exists() and dst != filepath:
        dst = WA_DIR / f"{new_stem} ({digits[-4:]}).md"

    filepath.rename(dst)

    content = dst.read_text(encoding="utf-8")
    content = re.sub(r'^contact: "?\+?\d+"?',   f'contact: "{name}"',     content, flags=re.MULTILINE)
    content = re.sub(r'^# WhatsApp: \+\d+',      f'# WhatsApp: {name}',   content, flags=re.MULTILINE)
    content = re.sub(r'(\*\*\d+:\d+ [AP]M\*\* )\+\d+:', rf'\g<1>{name}:', content)
    dst.write_text(content, encoding="utf-8")

    matched += 1

total = matched + unmatched
print(f"\nRenamed:     {matched} / {total}")
print(f"Unmatched:   {unmatched}  (not in Contacts — international or unsaved numbers)")
