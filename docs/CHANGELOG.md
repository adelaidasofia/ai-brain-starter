---
name: changelog
description: What's new in AI Brain Starter — plain English, no jargon
---

# What's new

*Every time you update (`git pull` or tell Claude "update the ai-brain-starter skill"), check here to see what changed and why.*

---

## 2026-08-20: people insights worked only if your journals folder was named in English

**Who this affects:** anyone whose vault is not in English — and anyone who renamed their journals folder.

The insight engine has a handful of sections built on one question: which people show up in your journals, and how were you feeling when they did. Lucky-charm people, drag people, high-priority contacts going cold. If your journals folder was named anything other than `📓 Journals`, all of those sections came back empty.

The reason was one line. The person extractor looked for journals in a folder path written out in English, so a vault with `📓 Diarios` — or `Journals` with no emoji, or anything else — pointed the scan at a folder that did not exist. It found nothing, wrote `person_journal_mention_count: 0` onto every contact, and reported no error. Nothing in the output said "I could not find your journals." It looked exactly like a vault too thin to have insights yet, which is the worst way for a bug to fail: it blames your data.

The folder is now found the same way the CRM folder already was — check a list of common names, or set `JOURNALS_FOLDER` if yours is unusual. English vaults behave exactly as before.

A second bug was hiding behind the first, and could only surface once journals were being found at all: if some entries wrote their date in quotes and others did not, the two came back as different Python types and the extractor crashed comparing them. Both styles are handled now.

**What to do:** re-run the extraction over your contacts so the counts recompute — `/second-brain-mapping` picks it up, but the counts are a derived field, so pass `--force --type person` if you want them refreshed without waiting for other changes.

---

## 2026-08-19: the privacy checker no longer publishes the private word list it checks for

**Who this affects:** anyone who publishes repos with this starter installed, and anyone who forked one of ours.

Every public repo managed by this project runs a check on each change: does this change accidentally include a private name, a client, a home folder path, an email? Useful check. One problem, found in an audit: the list of private words it looked for was written out, in plain text, inside the checker itself — which is a public file. A privacy guard that carries a neatly labelled list of everything it protects is not a guard, it is a directory.

Now the private part of that list lives in a sealed repo setting (a GitHub Actions secret named `PII_PRIVATE_PATTERNS`) that only the repo's own checks can read. The public file keeps only words that were already public on purpose. Nothing about what the check catches changes when the secret is in place — same words, same matching, and the check still proves on every run that each word on the list can actually be caught.

Three situations to know about:

- **A managed repo missing its secret fails the check loudly** instead of quietly checking nothing. That is deliberate: a silently disarmed guard looks exactly like a clean repo.
- **Changes proposed from a fork** cannot read secrets (GitHub's rule, a good one), so they get checked against the public words only, with a visible note saying so. The full check runs the moment the change lands.
- **Your own install:** if you copied this repo and never set the secret, the check now tells you how to add your own private list instead of checking ours. One command, shown in the check's output.

## 2026-08-18: everything felt slower on Windows, and now it is about twice as fast

**Who this affects:** Windows users, most of all anyone on a work laptop with antivirus running. Mac and Linux are unaffected.

Before Claude runs a tool for you, it runs a set of small background checks. They are the guards that keep a secret out of a config file, stop a command being run in the middle of a git operation, and so on. Each one is quick. There are a lot of them, and they all run before you see anything happen.

On Windows they were costing twice what they should have. The reason turned out to be embarrassing and easy to miss: the small program that runs each check was itself a Python program, and it started a *second* Python program to do the actual check. Starting Python is the expensive part, especially with antivirus watching every file it opens, so every check paid that price twice. Measured on a four-core Windows 11 laptop with two antivirus products running: 97 milliseconds to run a check directly, 189 through the wrapper. Multiplied across every check, that was about 1.6 seconds of waiting before every single tool call, and roughly 3 seconds at the start of a session.

Two things were fixed.

**The wrapper now does the work itself** instead of handing it to a second copy of Python. That is the bigger half. Nothing about what the checks can do has changed: one that blocks a dangerous action still blocks it, one that crashes is still ignored quietly instead of shouting at you, and what each one prints still comes back on the same channel it always did. That is not a claim, it is tested: 28 different situations are run through both the old and the new version and the results have to come out identical, down to the byte.

**And the Python it starts is now worked out once, when you install, instead of on every check.** Windows ships a small helper called `py` whose whole job is to look up where Python actually lives. Looking that up takes about 21 milliseconds, and it was being looked up again for every check, forever, to get the same answer. The installer now works it out once and writes the real answer down.

That second change had to be careful, because Windows may hand these commands to any of four different shells, and they disagree about what a file path looks like. Git Bash, for instance, silently eats the backslashes. So the installer does not guess: it runs the exact command it is about to write, in every shell it can find on your machine, and if any of them will not run it, it keeps the old safe version and skips the optimization. You get the speed when it is provably safe and the status quo when it is not.

**Together: roughly half the waiting, gone.** The wrapper change on its own, measured on a Mac over 40 runs, took the per-check cost from 34 ms to 19 ms, which is one Python start exactly as intended. A Mac starts Python in about a sixth of the time an antivirus-laden Windows laptop does, so on the machines this was reported from the same change is worth far more, and the second fix takes another 21 ms per check on top of it.

**What you should do:** update once and re-run the installer, so your settings pick up the resolved path. Tell Claude "update the ai-brain-starter skill". Nothing else changes, and re-running is safe as many times as you like.

### Follow-up the same day: six ways a check could have gone quiet, closed

Two reviews went looking for what the speed-up above might have broken, and found six things. All six are fixed. None of them ever made a check say the wrong thing; the failure was always that a check's answer went missing, which looks exactly like everything being fine.

When each check was its own separate program, the operating system kept it walled off. Running it in the same program is what made it fast, and it also removed that wall. These are the six places the wall turned out to be doing work nobody had noticed.

- **A check that tidies up after itself could take its own answer with it.** A common cleanup step closes file handles a program is no longer using, and two of those handles were now the only way the wrapper had of talking back. The answer went into a scratch file nobody reads and you saw nothing at all. Those handles are now kept somewhere a cleanup step will not reach.
- **Anything a check scheduled for "on my way out" landed after its answer had already been sent**, tacking stray text onto the end of it. The wrapper now finishes the check's shutdown while it is still listening, then stops the moment the answer is out.
- **A check that splits itself in two reported twice.** Two answers arrive glued together and neither can be read. The copy now stops instead of reporting.
- **A check that asked to be told about shutdown made the whole thing unstoppable.** Not a hostile check: an ordinary "let me save my work first" one was enough. It now gets to save its work, and then things shut down exactly as they used to.
- **A stumble while the wrapper put things back could replace a real answer with "carry on".** Every step of that tidy-up is now independent, so one of them tripping cannot overwrite what a check decided.
- **Two protections from a separate fix were carried over.** Checks read your files as UTF-8 again rather than whatever your console happens to use, which on Windows is the difference between working and a crash that was being swallowed as "carry on". And a check that hangs is bounded again: it gives up cleanly after 45 seconds instead of stalling the tool call until the harness gives up minutes later.

The speed is unchanged. The test suite that stayed green through all six of these now has a section aimed squarely at them, and every new test was confirmed to fail against the morning's version before it was allowed to pass against this one.

**What you should do:** nothing beyond the update and re-install above. Same instruction, same one time.

---

## 2026-08-15: the setup could stop halfway and tell you it was finished

**Who this affects:** anyone whose install ended early, especially if you never reached the journaling interview or your CLAUDE.md came out mostly empty.

Setup runs in phases, 0 through 24, and each phase lives in its own file so Claude only loads the part it is working on. Which file comes next was written down in exactly one place: the routing table at the top of the setup guide.

That works right up until the install gets long. By the time Claude has finished creating your folders it has read tens of thousands of words of setup instructions plus your own answers, and the routing table is no longer the thing steering it. The current file ends. Nothing tells Claude there are fifteen more phases. So it stops, and because stopping looks exactly like finishing, it tells you the install is complete.

Nothing errors. No file is missing. You are left with a folder structure, a skeleton CLAUDE.md, and none of the parts that make this a second brain: the context layer, the journal, the advisory panel, the weekly insights. One person's CLAUDE.md recorded "phases completed: folder structure + profile" while fifteen phases had never run, and they had no way to know that was wrong.

**Every phase file now ends by naming the next one.** The instruction travels with the file being read, so it cannot fall out of context the way a table at the top can. A new check, run on every change, walks the whole sequence and fails the build if any phase does not point somewhere, if any phase is unreachable, or if the chain loops or runs off the end. Adding a phase and forgetting to wire it in is now a build failure instead of a feature nobody ever receives.

**And you can now pick up where you stopped.** Ask Claude to "resume my ai-brain-starter install" (or "retoma mi instalación") in a fresh session. It works out how far you actually got by looking at what exists in your vault rather than asking you, tells you where it landed, and carries on from there. It also records your progress as it goes, so the next interruption costs you nothing. That detection had to read the vault directly, because the people most affected by this bug are precisely the ones whose progress was never recorded.

**What you should do:** if your install ended early, update, then start a fresh session and ask Claude to resume your ai-brain-starter install. Step-by-step, including a self-contained prompt that works even if you cannot update yet: [`docs/RESUME_INSTALL.md`](RESUME_INSTALL.md).

---

## 2026-08-13: three helpers that were never actually installed, and a Windows setup that could fail without saying so

**Who this affects:** everyone, for the second half. Windows users especially, for the first.

Both of these came out of one real Windows install, where a person hit them, worked around them by hand, and told us.

**Setup could fail to install Node.js and not say why.** Node's installer puts itself in a location that belongs to the whole machine, which Windows only allows after you approve a permission prompt. Setup ran that installer in silent mode, where no prompt can appear, so on a normal (non-Administrator) PowerShell the install simply refused. Worse, setup was throwing away the installer's answer entirely: whether it succeeded, failed, or never started, the next line of output looked the same. All you saw, several lines later, was a bare "node install failed" with no reason attached.

Now setup checks whether it is running as Administrator first. If it is, nothing changes. If it is not, it installs Node just for you, from the official ZIP build, into your own user folder, and adds it to your PATH. That path needs no permission prompt at all, so it cannot fail this way. It also adds the folder npm uses for global tools, which the machine-wide installer would normally have added, so Claude Code installs and runs straight afterward. If winget was tried first and could not do it, setup now falls back to the direct download instead of giving up. And every installer's real exit code is now read and reported, so a failure says what happened and what to do about it. The same fix is applied to the Python installer beside it, which had the identical problem.

**Three background helpers were being wired up but never copied into place.** `vault-context.py` (reads your priorities and open loops and puts them in front of Claude before it answers a strategic question), `retry-budget.py` (stops Claude looping on the same failing command), and `validate-mcp-json.py` (catches a broken `.mcp.json` before it silently disables every connector you have).

Setup listed them in your settings, so they looked installed. But the actual copying was a `cp` command written inside one of the setup guides, meant to be run while Claude walked you through setup. `cp` is not a command on Windows, so there it could not run at all. And because each of these hooks is deliberately wired as "run this only if the file exists", a missing file produces no error, no warning, and no output. Nothing to notice, on any platform.

There was a second layer to it. `vault-context.py` reads a small shared library that ships beside it, and nothing ever copied that library anywhere. So even where the `cp` did run, the hook loaded, found no library, fell back to its own do-nothing branch, and exited cleanly. It has been reporting healthy and injecting nothing, on every operating system, since it shipped.

All three now install with everything else, on every platform, along with the library they need. Setup verifies them afterward instead of assuming.

**What you should do:** run this once, and you are current:

```
python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py
```

If your session start mentions "background helpers not active", this is what it was pointing at. Nothing else changes, and re-running is safe: anything you edited by hand is backed up to `<file>.bak-YYYY-MM-DD-HHMM` before it is replaced.

---

## 2026-08-05: on Mac and Linux, "daily backup scheduled" now means it really is

**Who this affects:** anyone on macOS or Linux who set up the daily vault backup.

Setup said "Daily backup scheduled (03:00 local)" as long as it managed to write the small job file that describes the schedule. Whether the operating system's scheduler ever *accepted* that job was never checked, and any error it gave back was thrown away. So that message really only reported "I wrote a file", while the thing meant to take your nightly snapshot might never have been running at all.

There is a concrete way this happened. The job file is XML, and your vault's own path gets written into it. A vault in a folder with an `&` in the name (say, "R & D Vault") produced a file that is not valid XML, which macOS refuses to load. The file still existed, so setup still called it scheduled. Two other versions of the same problem: a vault or starter folder you later moved, leaving the job pointing at a script that is no longer there (it runs every night and fails every night), and a job that is simply not loaded any more.

Now setup writes your vault path into the job file correctly whatever characters it contains, then asks the scheduler itself whether it holds the job, and only says "scheduled" if the answer is yes. Re-running setup reads the existing schedule back and repairs it when it is broken, automatically and with no prompt, the same way the Windows fix below does. A healthy schedule is left completely alone. When it cannot install one, you get the scheduler's own error and the command to take a snapshot by hand, instead of a bare "could not".

There is also a new, faster way to check and fix just the schedule, without re-running the whole setup:

```
bash scripts/vault-backup.sh schedule
```

It asks the operating system whether it really holds your daily backup job, repairs it if not, and takes no snapshot, so it is safe to run any time. If it cannot fix it, it tells you why and gives you the command to take a snapshot by hand. This matters because a repair that only happens when you re-run setup would miss exactly the people whose schedule is dead, since those are the people who never re-run setup.

**What you should do:** run `bash scripts/vault-backup.sh schedule` once (or ask Claude to), then check `status` and confirm a snapshot has actually landed in the last day or two. Worth doing now in particular if your vault folder has an `&`, `<` or `>` in its name, or if you have moved your vault or the starter since you set backups up.

---

## 2026-08-05: on Windows, a backup that never actually ran now fixes itself

**Who this affects:** anyone who set up the daily vault backup on Windows before 2026-07-31.

A previous fix stopped the daily backup's Windows scheduled task from being registered broken in the first place. What it could not do was reach back and fix a task that was ALREADY broken on a machine that had run setup before that fix landed — and nothing else in the product ever looked at that task again. Measured on a real install: 25 days with zero snapshots, while setup had printed "Backup is live" and the health check reported the vault backed up. Three separate things could each cause this on their own: the script path the task points at being empty, the task pointing at a PowerShell version that is not on a stock Windows machine, or Windows Task Scheduler's own default of refusing to run on battery power.

Now, every time you run backup setup again, it reads your existing scheduled task back, checks all three things, and — if any of them is wrong — re-registers it correctly, automatically, with no prompt. A healthy task is left alone. If it truly cannot fix it (for example, no PowerShell interpreter can be found at all), it says so loudly instead of pretending it worked.

**What you should do:** run backup setup again once (`vault-backup.ps1 setup`, or ask Claude to do it) if you set up backups on Windows before 2026-07-31. Then check status and confirm a snapshot has actually landed recently.

---

## 2026-08-01: a graphify stage looked like it worked, then charged you twice

**Who this affects:** anyone who runs `/graphify` with more than one chunk.

Finishing a graphify stage crashed at the very last step, and the crash landed in the one place where it did the most damage while looking like the least. By the time it fired, the merged graph had already been written to disk. What had *not* run yet was the step that saves the semantic cache. So you got a healthy-looking graph, a stack trace you could reasonably read as "the report failed, no big deal" — and a cache that never recorded any of the work. The next `--update` re-extracted every one of those files from scratch and re-paid the full token cost. On a real run that was thousands of files silently re-billed.

The cause was a single wrong key. The function that ranks your most-connected nodes returns each one's `degree`; the report step asked for `edges`, which has never existed on it. Three lines asked for it.

**And the check that was supposed to catch it counted zero every time.** After saving the cache, the script reports how many entries it touched, so you can confirm the run landed. It looked for cache files directly inside the cache folder — but the cache is nested, in `semantic/` and `ast/` subfolders. So the count was always zero, whether the run had upgraded nothing or upgraded thousands. A verification step that cannot distinguish success from failure is worse than no verification, because you stop looking.

Both are fixed. The report reads `degree` (and still falls back to the older key names if a future version renames it), and the cache check walks the nested folders. If a stage ever crashes again, it will crash *before* the merge instead of after.

## 2026-08-01: WhatsApp chats that were all filed as one-on-ones

**Who this affects:** anyone using the WhatsApp bridge, especially if your vault is not in English.

Three things the chat extractor was getting wrong, each of which produced a field that looked filled in and was not.

**Every group chat was labelled a direct message.** The extractor decided group-vs-direct from two signals: a `chat_type` field, or a `jid` ending in `@g.us`. Exports written by older versions of the bridge wrapper carry neither, so on those vaults the answer was always "direct" -- measured at 113 of 113 groups. It now falls back to what those exports do carry: the group subject in the filename, and the `-` discriminator that a group JID has and a phone number never does. It deliberately does not guess from JID length, because direct chats get named after long numeric identifiers too.

**The "this chat contains a decision" flag only spoke English.** The trigger words were `exception, incident, pricing, escalation, outage, edge case, refund`. On a Spanish vault that fired on 6 chats out of 736 -- not a signal, just noise near zero. Spanish equivalents are now included by default, which takes it to 100, and `WHATSAPP_DECISION_TERMS` lets you replace the list entirely for any other language.

**The "people mentioned" field was empty on every single chat.** It reused the helper that finds `[[wikilinks]]`, and a chat file is a verbatim message log that never contains one. Every chat reported nobody, in a vault with 35 people in the CRM. It now matches names as plain text, and resolves the chat's own counterpart to their CRM note even when the phone book spells it differently -- a trailing org tag (`Ana Ruiz 30X`) or missing accents (`Angela` for `Ángela`) previously meant no link at all. On the test vault that went from 0 chats linked to 46.

---

## 2026-08-01: entries that quietly drop out of every query

**Who this affects:** anyone whose notes put wikilinks in a frontmatter field -- `people:`, `related:`, `projects:`, `attendees:`.

A wikilink is made of square brackets. YAML reads square brackets as list syntax. Put them together without quotes and one of two things happens, and neither one tells you.

`people: [[Ada]], [[Alan]]` stops the frontmatter from parsing at all. The consequence is not a warning, it is that the file becomes **invisible** -- Dataview stops returning it, the metadata extractors skip it, and weekly and monthly reviews are computed as if the entry had never been written. Open the note and it looks completely normal. That is why this can run for weeks before anyone wonders why a review came back thinner than the month actually felt.

`related: [[[Ada]], [[Alan]]]` is the worse one, because it **works**. YAML reads the inner brackets as nested lists and gives you lists inside lists where you wanted two links. No error, no unparseable file, nothing to notice. The queries just never match.

The coaching skill's session template was prescribing that second form. It now shows the correct one, and the journal skill states the rule with both failure modes named, so it covers any wikilink field you add later rather than only the ones shipped today.

**The whole fix:** quote each link on its own. `people: ["[[Ada Lovelace]]", "[[Alan Turing]]"]`

---

## 2026-07-31: five Windows install bugs, all of them silent

**Who this affects:** everyone on Windows. Two of the five also affect Mac and Linux. All of them were reported by people who ran the installer, were told it succeeded, and found out later that it hadn't.

**Your patched vault scripts are no longer overwritten during an update.** When bootstrap finds uncommitted changes in your copy of the starter, it stashes them before pulling — that part was known. What wasn't: right after the pull it copies the starter's scripts into your vault's `Meta/scripts` folder, and at that moment the starter no longer holds your patches. So the update quietly replaced patched vault scripts with the unpatched versions. One person lost their fixes to `session-close-runner.sh` and `vault-safe-commit.sh` this way. A backup was written, and it held the good version, which is exactly why nobody noticed: the step printed "Updated: 2" and looked like any healthy update. Now the vault sync refuses to run while your patches are stashed, tells you so, and shows you the two commands to restore and sync when you're ready. Your vault keeps working scripts instead of getting silently reverted.

**Your memory now actually lands in your vault on a Spanish-language Windows.** The installer runs a helper that links Claude Code's memory into your vault, and it read that helper's output using the console's language setting rather than UTF-8. Your vault path contains the ⚙️ of the Meta folder, and on a Spanish (or French, or Portuguese) Windows one byte of that character has no meaning in the console's character set, so reading the output crashed. The installer then reported "could NOT link Claude Code memory into the vault" on machines where nothing was actually wrong, and your memory stayed in a hidden tool folder — the one outcome that step exists to prevent.

**Hooks work if your Windows username has an accent in it.** A user named `JuanArturoGómez` had all 53 hooks fail and block the session, every time, because Windows hands hook commands to a shell that mangles the accented character in the path. You can't rename a Windows account, so the installer now writes the short `C:\Users\JUANAR~1` form of the path, which has no accents to mangle. Anyone with á, é, ñ, ü or similar in their username is covered. In the rare case where Windows has short names turned off, the installer says so plainly instead of writing hooks that can't run.

**And once that link step could run, it had to be able to succeed.** Creating the link from Claude Code's memory folder into your vault used a symlink, and Windows only lets you make one if you are an administrator or have Developer Mode turned on. A normal account gets "a required privilege is not held by the client" and the memory stays put. It now falls back to a directory junction, which needs no special permission and behaves the same for reading and writing. If both routes fail you get the exact command to run, instead of a shrug.

**Three hooks that had never once fired now do.** `pre-write-settings-lint.py`, `lint-claude-settings.py` and `check-claude-code-version.sh` were wired into your settings but nothing ever copied them onto disk — on a real machine that was 11 references pointing at 0 files. Because the wiring is written to stay quiet when a hook is missing (so you can delete one you don't want), the absence looked exactly like health, and the installer's own check reported everything fine. The installer now installs them, its check can see them, and a new gate stops a future hook from shipping the same way.

**Also:** `--verify-only` is a new option that checks an existing install and writes nothing. `--verify` still means "install, then verify", which is what it always did — but there was no way to ask "is my install healthy?" without changing it first.

---

## 2026-07-30: closing a session no longer gets stuck behind a goal you set

**Who this affects:** anyone who uses `/goal`.

`/goal <condition>` tells Claude "keep working until this is true." It does that by holding the exit door shut — which is right in the middle of a session and wrong at the end of one. You say "close this session," Claude finishes the close checklist, tries to stop, the goal blocks it, and Claude comes back with nothing left to do. Then again. The only way out is `/goal clear`, and **Claude cannot type it for you** — it is a command your terminal handles, with no tool behind it. So it has to hand you the line.

Now it does. When a session set a goal that was never cleared, the close ends by asking you to type `/goal clear`, quoting the goal so you know which one. If the goal's condition was actually met it already cleared itself, and Claude says nothing — being told to clear something that succeeded is noise.

**Also fixed, same area:** the session-close rule and the close instructions Claude receives had drifted apart on what their step numbers meant. "Phase 3" was the goodbye in one and the audit in the other; "Phase 4" was the goodbye in one and the automatic cleanup in the other. Two documents describing one process, disagreeing about which step is which — and each was internally consistent, so nothing looked wrong. The rule was also missing its commit step entirely, which could hard-block a close. Both are fixed, and a checker now runs against **your** copy of the rule during daily maintenance, since a customised rule is exactly where this drifts.

**On macOS:** the `vault-root` test suite had been failing on every Mac for a path-spelling reason (`/var` vs `/private/var`) that never appeared in CI. Fixed. A gate that is always red on your platform is a gate you learn to skip.

---

## 2026-07-27: journal entries now carry `type: journal`, so metadata extraction stops skipping them

**Who this affects:** everyone who uses `/journal` together with `/second-brain-mapping` (or anything else that reads the vault's typed frontmatter).

**The bug:** the journal skill's entry template never wrote a `type:` field into the frontmatter. The metadata extractor sorts every note by that field — no field, no sort: each entry landed in the "No type field" bucket and was silently left out of extraction, even though every other journal template in this repo already says `type: journal`. In one real vault, twelve days of entries were invisible to mapping until the field was added by hand.

**The fix:** the entry template now opens with `type: journal`, and the capture-first save names it as a required field. Nothing else about the entry changed.

**What you should do:** nothing for new entries. If you journaled before this fix, ask Claude to backfill `type: journal` into your existing entries' frontmatter so extraction sees them too.

## 2026-07-16: the Spanish close detector stopped firing mid-conversation and started hearing "cierra esta sesión"

**Who this affects:** anyone journaling or working in Spanish (or Portuguese) — the session-close detector was misbehaving in both directions for you, and English users never saw it.

**The bug:** two failures, same root cause. The close detector lets its strong tiers (explicit, high-confidence sign-offs) override the false-positive guards — and the guard tier that's *allowed* to override them (`strict_guards`) existed only in the English pack. So in Spanish, every false-positive guard was dead code:

- **Fired when it shouldn't:** "¿Ya está en el prompt o no está creado?" — a question in the middle of a working session — triggered the full close cascade, because `ya (está|estuvo|fue)` was unanchored and matched inside any sentence.
- **Stayed silent when it should have fired:** "cierra esta sesión" did nothing. The pattern was built on the stem `cerr`, which covers *cerrar/cerremos/cerramos* but not the imperative *cierra* — Spanish *cerrar* is an e→ie stem-changing verb, so the command form is *cierr-*, not *cerr-*.

**The fix:** ported the `strict_guards` tier to `es.json` (meta-discussion of the cascade, technical references, "close the *database* session", questions *about* closing), anchored `ya está/estuvo/fue/quedó` to end-of-message, and added the imperative `cierra/cierre/cierren` (plus Argentine `cerrá`) to the close-session pattern.

**New test:** `tests/integration/test_detect_closing_signal_es_guards.sh`, wired into `scripts/ci.sh`. The English `strict_guards` test is unchanged and still passes.

---

## 2026-07-16: /weekly and /monthly could not find your journal folder

**Who this affects:** anyone whose journal folder is named anything other than a bare, emoji-less `Journals` — which includes the **default `📓 Journals`** that the setup interview creates, and every localized name (`📓 Diario` on a Spanish install, `📓 Diário` on Portuguese).

**The bug:** `build-journal-index.py` auto-detects your Meta folder (so `⚙️ Meta` and plain `Meta` both work) but did **not** do the same for the journal folder — it fell back to a hardcoded English `Journals`. Since `insights/SKILL.md` runs the script with no arguments, that hardcoded default was the only thing ever consulted. The result was `/weekly` and `/monthly` failing at step 0 with `journal directory not found`.

**The fix:** the journal folder is now auto-detected the same way the Meta folder already was, across the emoji and plain forms of the English, Spanish, and Portuguese names. Passing `--journal-dir` explicitly still wins, and a vault with no journal folder at all still fails loudly instead of inventing one.

**New test:** `tests/integration/test_journal_index_localized_dir.sh` (6 assertions incl. a negative control), wired into `scripts/ci.sh`. Against the pre-fix code it fails on `📓 Diario`, `📓 Diário`, **and `📓 Journals`**.

---

## 2026-07-14: the secret detector stopped crying wolf over container IDs and migration checksums

**Who this affects:** everyone — a detector that flags things that aren't secrets teaches you to stop trusting its real alerts.

**The bug:** the secret detector cried wolf 16 times in one session over things that are not secrets: the ID Docker prints when it starts a container, and the checksums a database migration table stores.

**The fix:** those two exact shapes are now recognized by their command context and quietly recorded instead of alarmed. Everything else — including a real secret printed in the same output — still alarms. The scrubbing and scanning layers that protect your session files were not loosened at all.

**New test:** `hooks/test_secret_patterns_fp_filter.py`, run directly by `scripts/ci.sh`'s Python unit-test gate.

**What you should do:** nothing. Update as usual.

---

## 2026-07-14: the journal now tracks how you MOVE between floors, not just where you stand

**Who this affects:** everyone who uses `/journal`, `/weekly`, or `/monthly`.

**What changed and why:** naming your floor is a point; a year of floors is a map of how you actually move — what pulls you down, what pulls you back up, where you loop. The journal now captures that movement, and the insight reports read it.

**In `/journal` (daily-journal skill):**

- **The door.** Every session now ends with ONE small, dated, physical action matched to your floor — and tomorrow's session opens by asking if it happened. A named floor without a next move produces articulate stuck people; the map now always comes with a door. Includes a guard for floors that deserve *time* rather than exits (fresh grief gets a container — "ten minutes to feel this fully" — not an escape plan).
- **Body-first check.** Low floors usually arrive body-first, story-second. Before the journal accepts "it's about the meeting," it checks sleep, food, movement, and sunlight — and tells you when the floor might be physiology, not psychology.
- **Shadow-twin probe.** Before tagging Acceptance, Neutrality, Peace, or Pride, one distinguishing question ("if this could change tomorrow, would you want it to?") — because Resignation feels like Acceptance from the inside, and the mislabel is how people stay stuck for years.
- **Movement capture.** New frontmatter records yesterday's floor, WHY the floor changed (body / witness / rupture / rope / role / story), and — when something pulled you up from a low floor — what the rope was. Over months this builds your personal rope inventory: the things that reliably work for YOU.
- **Crisis protocol, formalized.** The "crisis-tier override" other steps referenced now has a full definition: stop all mechanics, witness first, ask the nearest-rope question, surface a support line once, save their words verbatim.
- **Hand the naming back.** After ~30 entries, roughly weekly, you name the floor before Claude does. The journal trains the muscle instead of becoming it.

**In `/weekly` and `/monthly` (insights skill) — new section 0e, the Movement report:**

- Your personal **transition map** (your Fear goes to Frustration, not Shame — everyone's wiring differs)
- **Loop detection** with mechanism classes (protective / structural / physiological) — loops need their mechanism addressed, not the floor's generic way out
- **Resilience direction** — after each dip, did you land higher than before or snap back to the stuck place? Speed means nothing without direction; this trend is the single most important line in the report
- **Body-attribution rate** ("5 of your 7 low-floor days were underslept days")
- **Rope inventory** and **door completion rate** (the insight-vs-action gauge: "you named the floor 12 times and walked through the door 3 times — the map is not the walking")

The standalone `claude-daily-journal` plugin got the same journal-skill changes in its 1.4.0 (kept in sync).

---

## 2026-07-05: your vault's hooks no longer silently die when a Python plugin is present

**Who this affects:** everyone installing — especially workshop rooms. Found on a test install the day before a workshop.

**The bug:** one of the Trail of Bits plugins the installer used to add, `modern-python`, installs a "shim" that intercepts the plain `python3` command and refuses to run it — it prints "use `uv run python3`" and stops. That is a reasonable nudge for a Python developer. But the AI Brain Starter runs all of its own automation through plain `python3` — session close, the write-time secret guard, the context loaders, backups, the aggregators — and almost all of it is written to fail quietly (`|| true`, `2>/dev/null`) so a hiccup never blocks you. Put those two facts together and the result is the worst kind of bug: with that plugin present, the **entire** automation layer went dark with **no error message at all**. It looked like everything installed fine. One developer machine hid the problem entirely because it had a hand-patched copy of the shim that no fresh install has.

**The fix, in three layers:**

1. **The default install no longer adds `modern-python`.** It is Python-developer tooling, not note-taking tooling, and a footgun for non-developers. The seven Trail of Bits *security* skills still install. If you do Python work and want the `uv`/`ruff` toolchain, you can add it back yourself — it is now safe to have installed (see layer 2).
2. **The substrate is immune to the shim regardless.** Hook commands now resolve a real Python interpreter by absolute path at install time, and the shell scripts strip any `*/hooks/shims` directory off their PATH. So even if you already have `modern-python` (or a pyenv/conda setup), every hook runs.
3. **A guard so it can never come back silently.** A new CI test installs the real hooks with a fake refuse-shim first on the PATH and fails the build if any hook command would still be intercepted. `/diagnose` runs a real interpreter through the same stripped PATH, so a broken machine surfaces loudly instead of going quiet.

**New test:** `tests/integration/test_installer_shim_safe_interpreter.sh` (wired into `scripts/ci.sh`, the canonical gate).

**What you should do:** nothing. Update and re-run setup. If you specifically want the `uv`/`ruff` Python toolchain: `claude plugin install modern-python@trailofbits`.

---

## 2026-07-02: previews never install, hiccups fix themselves, and no more red ✗ for things that are fine

**Who this affects:** everyone installing — especially workshop rooms full of first-time users.

**Three bugs, all found on real machines:**

1. **"Preview" mode actually installed things.** `--dry-run` is supposed to show what WOULD be installed without touching anything. Instead it really installed Homebrew, Node, gh, pipx, and graphify on a user's machine — one branch of the script literally said "or dry-run: install for real." Now a dry run prints its plan and changes nothing, and a new test runs the real installer in a sealed sandbox that records any attempt to install something — zero attempts allowed, forever.

2. **One Wi-Fi blip looked like a broken product.** On a workshop machine, graphify showed a red ✗ "install failed" under a line saying most of the setup depends on it — scary, and wrong twice over. First, the error hid a plumbing bug: the installer sometimes couldn't SEE a tool it had just installed successfully (the folder it lands in wasn't on the session's path when pipx was already present). Second, all the diagnostic output went to `/dev/null`, so nobody could tell what actually happened. Now: the path is set unconditionally, every install's full output lands in `~/.claude/.bootstrap.log`, installs retry automatically (thirty machines on one workshop network hitting the package server together WILL have blips), and there's a fallback install method behind the first.

3. **When something still doesn't land, the assistant fixes it — not you.** If a component genuinely can't install right then, the bootstrap no longer shows a failure. It notes the gap in a small file, tells you the interview will finish it, and the setup interview (and the first-week check-in after it) quietly completes it. A person who has never opened a terminal never sees a dead end.

**Also:** the Windows installer's preview mode had the same real-install holes (now gated the same way), and there's now a documented one-line Terminal path for when Claude Code's own safety layer prefers that you run the installer yourself — pasted by you, resumed by the assistant, no dead end.

**New tests:** `tests/integration/test_dry_run_purity.sh` — runs the real installer's dry-run in a sandbox with recording stubs (zero mutations allowed) plus a structural check that every install command sits behind a dry-run guard; verified to fail against the old installer (10 unguarded sites) and pass against this one.

**What you should do:** nothing. If your last install showed a graphify ✗, run the update and tell Claude "finish my install gaps" — or just start the setup interview, which does it for you.

---

## 2026-07-02: the setup now treats every approval as genuinely yours

**Who this affects:** everyone who installs or re-runs the setup, and especially anyone whose assistant refused to run it.

**The bug:** parts of the setup instructions told the assistant things like "tell the user it's safe to approve, go ahead and approve it," "no pause options — never offer to stop," and "if they ask for a work-only setup, decline and install everything anyway." The intent was good — first-time users were abandoning installs when a routine safety prompt ambushed them, or getting lost in option menus — but the wording crossed a line: it asked the assistant to pre-commit you to approving things you hadn't read and to hide choices you were allowed to make. Newer Claude models read that and (correctly) refused — on one machine the assistant declined to run the setup at all and removed the download, which meant nobody got anything.

**What's new:** the same guidance, rewritten around your consent. The assistant still warns you before the safety prompt appears (so it doesn't feel like an ambush), still installs the full brain by default, and still doesn't pepper you with menus — but it now tells you the decision is yours, offers to review or skip anything, honors "stop" or "pause" the moment you say it (resuming later is one sentence), and if you explicitly ask for a smaller setup it makes the case once and then does what you said.

**Also fixed:** a real Windows installer crash — PowerShell was treating harmless warning messages from tools like pip as fatal errors and aborting the whole install partway through. Installs now judge success by whether the tool actually succeeded, not by whether it printed a warning. And the locked-down corporate install now prints the exact one-line command your IT team can approve to add Node.js, instead of a dead end.

**New tests:** the consent guarantees are pinned by the updated `test_trust_prompt_preframing.sh` and `test_personal_brain_not_optional.sh` suites, so the old wording can't quietly come back.

**What you should do:** nothing. If someone's assistant previously refused the install, it will run now — the parts it objected to are gone.

---

## 2026-07-02: Windows actually works now — no more error notices every session

**Who this affects:** everyone on Windows, in a big way. macOS/Linux users keep working exactly as before (same behavior, now with more tests around it).

**The bug:** the background helpers ("hooks") that make the brain work were written in the language of Mac/Linux terminals. Windows speaks several different terminal dialects depending on your setup, and none of them understood those commands. So on a Windows machine, most helpers failed on every single message — you'd see error notices that looked like something was badly broken, the auto-updater could never run, and the "your update didn't finish" checker then warned you every session about a problem you had no way to fix. None of it was your fault, and your notes were never in danger — but it looked scary, and the helpers genuinely weren't running.

**What's new:** on Windows, the installer now writes every helper in a form all Windows terminals understand, routed through a small runner that keeps a misbehaving helper invisible instead of surfacing an error notice. The auto-updater and the skill-sync were rewritten in Python (which runs the same everywhere), so Windows machines now get updates, self-heal, and stay current just like Macs. Helpers that only make sense on Mac/Linux (like the one that cleans up runaway Mac processes) now stay quietly off on Windows instead of erroring. And the handful of warnings you *should* see (like "your vault has no backup") now show Windows commands that actually work when you paste them.

**Also in this release:** several warning messages lost their internal engineering jargon ("bug class DEPLOY-FAILS-OPEN-SILENTLY-ON-CLIENT" is now a plain sentence that says nothing is broken and gives you the one command to run), fix-it suggestions no longer reference tools that were never shipped with the starter, and the Windows bootstrap validates that the `python` it found is real (Windows ships a fake one that opens the Microsoft Store) and double-checks that every hook it wires actually exists on disk.

**Under the hood:** `install-hooks-user-level.py` rewrites hook commands per-platform at install time (`hooks.json` stays the single source of truth); `scripts/hook_runner.py` reproduces the old shell-level failure-masking in portable Python, including letting intentional blocks through; `scripts/ai-brain-auto-update.py` and `scripts/sync-skills.py` replace the bash versions (the `.sh` files remain as thin pass-throughs so older installs migrate seamlessly); path checks in the worktree/vault helpers now understand Windows path separators; and `scripts/PORTABILITY.md` gained the Windows rules so new hooks stay portable.

**New tests:** `tests/integration/test_windows_platformize.sh` — proves a Windows install contains zero commands Windows can't parse, that an existing broken install migrates in place without duplicates, that the update checker stays silent on a healthy Windows install (the false-alarm loop this kills), and pins the runner's exact failure behavior. The existing auto-update and installer suites all still pass against the Python rewrites.

**What you should do:** on Windows, run the update once — tell Claude "update the ai-brain-starter skill" or run `git pull` in `%USERPROFILE%\.claude\skills\ai-brain-starter`, then `py -3 "%USERPROFILE%\.claude\skills\ai-brain-starter\scripts\install-hooks-user-level.py"`. After that one command, updates take care of themselves. On Mac/Linux: nothing.

---

## 2026-07-01: your machine now tells you if an auto-update half-landed

**Who this affects:** everyone. This is the safety net for the auto-update in the entry just below.

**The bug:** the previous entry made the auto-update install its own hooks the moment you pull them. But that install step is allowed to fail without stopping your session (that's on purpose — a stuck installer must never wedge your prompt). When it *does* fail, it prints one warning for that one turn and moves on. A few days later the auto-update looks, sees your checkout is already current, and does nothing — so the warning is gone for good and the hooks it never finished wiring just stay off. The result is a quieter version of the exact problem the auto-update was built to kill: your copy of the starter is up to date, but the hooks Claude actually runs are behind it, and nothing tells you. The daily "are you up to date?" check only compares your *download* to the latest — it never checks whether the download was actually *wired in*.

**What's new:** every session now does a quick, local check that the hooks you've downloaded are actually the hooks that are turned on. If a background install half-landed, the next session says so in one line and gives you the single command to finish it. If everything's wired correctly (the normal case) you never hear a word. It's a plain file comparison — no network, no waiting, and it can never crash your session (any hiccup just makes it stay quiet).

**Under the hood:** the check reuses the installer's own definition of which hooks belong to the starter, rather than keeping its own copy of that list — so it can't fall out of step the next time a hook is added (which would have been the same class of silent drift all over again). It deliberately ignores the handful of hooks that only turn on once you've set up a vault, since those are wired by a different step and were never part of what a background update touches.

**New tests:** `tests/integration/test_deployed_hooks_behind.sh` — proves it stays silent on a real, correct install, fires (and names the culprit) when a hook is missing or a retired one is still wired, and never falsely flags a vault-only hook. Includes negative controls in both directions.

**What you should do:** nothing. If your last update half-landed, your next session will tell you the one command to run.

---

## 2026-07-01: updates now install themselves the moment you pull them

**Who this affects:** everyone. This is about how the starter keeps itself up to date.

**The bug:** the auto-update ran a `git pull` in the background every few days, but the step that actually *wires the new hooks into Claude* was left as an instruction for the assistant to run afterward. If the assistant didn't get to it — the session ended, you asked about something else, the note scrolled past — the pull landed but the new hooks never turned on. Over time the installed copy drifted further and further behind what had actually shipped, silently, because nothing was checking. On one machine it reached 131 versions behind before anyone noticed.

**What's new:** the auto-update now installs the hooks itself, in the same step as the pull, so a shipped change reaches your machine the same session with nothing left to remember. It also pulls more safely: if you've edited the starter files locally, or your copy has diverged into a fork, it refuses to overwrite your work and tells you how to merge by hand instead of forcing a merge. It can still be paused (create a `~/.claude/.ai-brain-starter-pinned` file), it still only runs every few days, and it never runs twice at once.

**Under the hood:** the update logic moved out of a hard-to-read one-line hook and into `scripts/ai-brain-auto-update.sh`, so it can be tested. The installer now retires the old inline version and installs the new one cleanly, so a machine that already had the old one doesn't end up running both.

**New tests:** `tests/integration/test_ai_brain_auto_update.sh` (the pull-and-deploy path plus five things it must NOT touch — pinned, already current, rate-limited, dirty, diverged) and `tests/integration/test_installer_replaces_auto_update.sh` (proves an existing install ends up with exactly one updater, not two). Both include negative controls that fail against the old behavior.

---

## 2026-06-30: session-close now writes to the vault you're actually working in, not just your default one

**Who this affects:** anyone who works across more than one vault or repo that each has its own CLAUDE.md and its own Sessions/Decisions setup — for example, a personal vault plus one or more separate team or client repos.

**The bug:** the session-close cascade resolves a "vault root" to decide where your session file, decisions, and captures get written. That resolution checked an environment variable (`VAULT_ROOT`) first and only fell back to your current folder if it was unset. Most installs set `VAULT_ROOT` once, globally, as a convenience default — which meant the fallback never ran, for any session, ever. A session spent entirely inside a separate repo with its own session-close setup still had every path resolved against the unrelated default vault. Notes didn't error or warn; they just landed in the wrong place. Bug class: **WRONG-VAULT-ROOT-FROM-GLOBAL-DEFAULT**.

**What's new:** closing a session now checks first whether the folder you're actually in (or one of its parent folders, up to your home directory) has its own CLAUDE.md declaring a "Session End" or "Session Close" setup with a real Sessions folder already in place. If it finds one, that's where your session goes — even if a different vault is configured as the default. If it doesn't find one (a scratch folder, a one-off script, or your actual default vault itself), everything falls back exactly like before. Single-vault setups see no change at all.

The safety-net hooks that double-check a session actually got saved before letting you close (`verify-session-close-cascade.py`, `verify-discoverability-on-close.py`) now use the SAME resolution, for the same reason the writing hook does — otherwise a session correctly saved to its own repo could get hard-blocked at close because the safety check was still looking for it in the unrelated default vault.

**New:** `hooks/_lib/vault_root.py` — the shared resolver all three hooks now import, so this can't drift out of sync the way three independent copies eventually would have.

**New tests:** `tests/integration/test_detect_closing_signal_repo_aware_vault.sh` and `tests/integration/test_verify_cascade_repo_aware_vault.sh` — both include a negative control that fails against the old behavior and passes against the fix.

**What you should do:** nothing. The SessionStart auto-update flow picks this up on your next session (or `git pull` manually in `~/.claude/skills/ai-brain-starter/` to grab it immediately).

---

## 2026-06-30: the "what your setup injects" meter is now honest and safe

The meter from the previous entry got a hardening pass after an adversarial review found three sharp edges. All fixed:

1. **It no longer lies on Windows.** The meter needs a Unix-style shell (bash) to run your hooks. On a Windows machine it couldn't — but instead of admitting that, it cheerfully reported *"all clear, zero waste."* A meter that says "all good" while it's actually blindfolded is worse than no meter. Now, if it can't run your hooks, it says so plainly (**"CANNOT MEASURE — UNMEASURED"**) and shows only the safe inventory. Same honesty if a specific hook times out or crashes: it's marked "unmeasured," never quietly counted as clean.

2. **It won't touch your real work.** To measure honestly it runs your hooks in your current project. Some hooks *write* (auto-commit, stash, save a file). The first version could have let one of those run against your uncommitted work. Now only the harmless "before you type" hooks run in your real project; the ones that fire on *actions* run in a throwaway scratch folder, so nothing can touch your repo.

3. **Safer by default.** Plain `--measure-live --execute` now checks only the per-message hooks (the headline, and the safe ones). If you want the fuller sweep that also pokes the action-hooks, you ask for it explicitly (`--event all`) and it tells you those run in the scratch folder first.

Bottom line: it's now safe for anyone — including a paid Mycelium install — to run in their working folder, and it will never hand you a falsely reassuring "clean." (Reasoning: `docs/adr/0006-measure-live-settings-injection.md`.)

---

## 2026-06-30: see what *your* setup injects into every message

The previous update stopped the *substrate's* startup hooks from re-sending their text
on every message. But the real per-message cost is in **your** `settings.json` — the file
that holds not just our hooks but every hook you (or a teammate) wired yourself: a context
loader, a version check, a reminder that fires on each prompt. A stable block wired there
re-sends its full text every single message, and the previous check couldn't see it — it
only looked at the hooks we ship, only the Python ones, and only the prompt event.

There's now a command that measures **your actual setup**:

```
footprint-sla-check.py --measure-live --execute
```

It runs each of your wired hooks once with a blank, throwaway prompt (in a sandboxed copy
of your home folder, so nothing real is touched) and shows how many tokens each one
injects on **every message** — including your own hooks, hooks written in bash instead of
Python, and hooks that fire on tool calls (not just on your prompt). Anything that keeps
re-sending a stable block gets flagged with the fix: move it to session-start, where the
text is loaded once and re-used for free the rest of the session, instead of being paid for
fresh every turn. Run it without `--execute` first for a safe, no-run inventory of what's
wired.

This is a report you run when you want it — it never blocks anything and nothing runs
automatically. It's for spotting an expensive habit before it quietly compounds. (Full
reasoning: `docs/adr/0006-measure-live-settings-injection.md`.)

---

## 2026-06-30: stop re-sending session context on every message (cheaper, leaner)

Two startup hooks load standing context for the session: the session-start guidance
block (which files to read, always-active rules) and your project-scoped instincts.
Both were meant to run **once per session**. They didn't.

They relied on a `once` flag that Claude Code only honors for hooks declared inside a
skill — and **ignores** when the hook lives in your `settings.json`, which is exactly
where the installer puts them. So instead of loading once, both blocks were being
re-injected on **every single message**. In one real session we measured the
instinct block re-sent 14 times and the session-start block 17 times — the same text,
paid for as fresh tokens each turn, and piling up duplicate copies that eat into the
context window. It compounds the longer you work.

The fix moves both hooks to run at session start, where the text lands in the part of
the prompt Claude Code caches — so it's loaded once and re-used as a cheap cache-read
for the rest of the session (and re-loaded automatically after a compaction). Claude
still sees the same guidance on every turn; you just stop paying to re-send it. After
the change, the per-message injected-token cost from these hooks measures **zero**.

The footprint gate now catches this whole class of mistake: a `once` flag in
`settings.json` is flagged as a hard error (it's a no-op there), and a new check
measures how many tokens your prompt hooks inject on a neutral message, so a
stable block that re-sends every turn can't slip back in.

You don't need to do anything — this applies automatically on update.

---

## 2026-06-30: Windows installs are now tested for real on every change

If you install on Windows, the setup script (`bootstrap.ps1`) now runs end to end on a clean Windows machine in our automated checks, on every proposed change to this repo. Before, the checks only confirmed the script's syntax was valid; they never actually ran it on Windows. That gap let a real break slip through on 2026-06-27, where a fresh Windows install crashed before any tools, skills, or hooks were set up (the script aborted on an unauthenticated `gh`, and a Windows path broke the hook installer).

The new check starts from a clean slate (no GitHub login, a fresh Python), runs the whole installer under Windows PowerShell 5.1, and then confirms three things actually happened: the install finished without crashing, your prompt hooks got wired into `settings.json`, and the connectors registered. If a future change reintroduces that kind of break, the check turns red and it cannot ship.

You do not need to do anything. This only affects how changes to this repo are tested.

---

## 2026-06-30: faster session startup on big vaults

One of the startup checks counts how many leftover `claude/*` work branches are sitting around (so it can remind you to clean them up). The old version asked git about each branch one at a time. On a small vault that's instant, but on a large vault with a hundred-plus branches it meant a hundred-plus separate git calls every time you opened a session — several seconds of lag before you could do anything, and it got slower the more branches piled up.

It now does the same count in two git calls total, no matter how many branches you have. The reminder is identical; it just arrives instantly. A new test locks this in, so the slow per-branch version can't sneak back.

You don't need to do anything — this applies automatically on update.

---

## 2026-06-22: Granola sync now uses Granola's official API

If you connected Granola for meeting transcripts, the old sync read Granola's local cache file on your Mac. Granola encrypted and moved that cache in mid-May 2026, so the old script silently stopped finding anything — it exited cleanly and exported nothing, with no error to tell you it had broken.

This switches Granola sync to Granola's official Public API, which keeps working across those local-storage changes:
- **You now need a Granola API key.** Generate one in Granola (Settings → Connectors → API keys), then save it to `~/.config/granola/api-key`, or set `GRANOLA_API_KEY`.
- **Check it's working:** `python3 scripts/granola_sync.py --health` confirms the key and connection; `--dry-run` previews what would export.
- **Auto-export now runs every 2 hours** (the old version triggered off the cache file, which no longer exists). Re-copy `scripts/com.granola-export.plist` to `~/Library/LaunchAgents/` and reload it.
- If the key is missing or invalid, the script now **fails loudly** instead of exiting quietly — and the connector-liveness check flags Granola if it ever goes silent again.

Already using the old cache-based sync? Add an API key as above and reload the LaunchAgent; that's the whole migration.

---

## 2026-06-20: your memory now actually lives in your vault

The whole promise of this project is that your second brain lives in your vault. But Claude Code keeps its own memory (the things it learns about you) in a hidden system folder — `~/.claude/projects/.../memory/` — that never showed up in Obsidian and didn't follow you to another computer. The substrate assumed that folder had been linked into your vault, but nothing in the install ever did the linking. So for most people, memory was quietly accumulating in a place they couldn't see and couldn't back up.

This fixes it:
- **Setup now links Claude's memory into your vault**, at `⚙️ Meta/Agent Memory/`. From now on, what Claude remembers is a real file in your vault — visible in Obsidian, saved in your vault's history, and portable to another machine. The link is created loss-free: any memory you already had is migrated in, and the old folder is backed up, never deleted.
- **Already installed before today?** No action needed — the daily maintenance run links it for you automatically the next time it runs (within a day), or you can do it now: `python3 ~/.claude/skills/ai-brain-starter/scripts/link-agent-memory.py --vault "/path/to/your/vault"`.
- **Running Claude from a different project (a work repo)?** Personal and life content now routes to your brain vault and gets saved there, instead of landing in the work project's hidden memory folder.

A test now proves, on every build, that memory written by Claude actually reaches your vault — so this can't silently regress.

---

## 2026-06-18: removed the vertical packs (legal, finance, healthcare, creator)

If you installed `vertical-finance`, `vertical-healthcare`, `vertical-legal`, or `influencer-pack`, they are no longer part of this repo. They were per-industry packs (compliance retention rules, enterprise connector specs, audit-evidence templates) that belong to the paid runtime, not the free open-source substrate. Shipping them here crossed the open-core boundary documented in `docs/adr/0001-open-core-boundary.md`.

What this means for you:
- The substrate itself (journaling, knowledge graphs, memory, ingest, the pattern) is unchanged. Nothing you use day to day is affected.
- If you were running one of those packs, it stays on the copy you already cloned, but it will not get updates here and new installs will not include it.
- A CI guard now fails the build if any per-vertical pack, multi-tenant connector, or audit-analytics content is added back, so this cannot recur by accident.

No action needed on a normal install.

---

## 2026-06-13: the install now PROVES it read your doc — before you close, not "next session"

**Who this affects:** everyone running a fresh install. The setup already asks you to bring in one real doc near the end (your first activation moment). Until now it ended on a promise — "next session I'll know about it" — and you closed the tab without ever seeing it work.

### What's new

- **A live "it already knew" proof.** Right after your first doc lands in the vault, the install now answers a question about it on the spot and cites the exact line it pulled from — so you watch the system read your own content before you leave, instead of taking it on faith.
- **Cite or don't claim.** The proof is held to the same honesty bar as the paid product: one true, specific, cited fact from your doc — never an invented one. If the answer isn't in what you brought in, it says so plainly rather than making something up.
- **Still bounded to one moment.** One question, one cited answer, then the install closes — it's a proof, not a working session.

### Why

An imported doc you never see queried is still invisible — you can't tell scaffolding from a working brain. Ending on "next session I'll know it" punted the only convincing moment to a session that may never happen. Closing the loop in-session is what turns "I dropped a file somewhere" into "it actually knows my thing."

---

## 2026-06-07: a follow-up — guard the "Meta" leak from coming back, and detect vaults it already hit

**Who this affects:** the same people as the entry below (emoji-`⚙️ Meta` vaults), plus anyone setting up a fresh vault. The earlier fix stopped *new* leaks; this makes the fix permanent and helps anyone already bitten find and repair the damage.

### What's new

- **A guard so the bug can't return.** A CI check (`scripts/check-meta-resolution.sh`) now fails the build if any shell script reintroduces the naive `*Meta` glob instead of using the shared resolver. The fix below was point-in-time; this makes it durable.
- **`/diagnose` detects an already-split vault.** A new check (section 14, backed by `scripts/check-split-meta.py`) flags a vault whose session log, session archive, or traffic dashboard leaked into a plain `Meta/` beside your real `⚙️ Meta/`, and tells you how to move them back. If you were bitten before updating, this is how you find it.
- **`/diagnose`'s Meta check no longer assumes the emoji name.** Section 2 used to look only for `⚙️ Meta/` and would false-fail a stock vault that uses a plain `Meta/`. It now resolves whichever variant you actually have.

### Verification

Both new checks ship with negative-control tests wired into `scripts/ci.sh`: the guard test proves a *reintroduced* glob is caught (not just that a clean tree passes), and the detector test proves a leaked `Sessions/` in a plain `Meta/` is flagged while a healthy machine/human partition stays quiet.

---

## 2026-06-07: session logs + traffic dashboards could silently leak into the wrong "Meta" folder

**Who this affects:** anyone whose vault uses an emoji-decorated meta folder like `⚙️ Meta` for their human notes (rules, decisions, the session log). If the closed-loop memory engine ever created a plain `Meta/` folder for machine memory, five shell scripts could quietly start writing your session log, session archive, repo-traffic dashboard, and daily-maintenance logs into that machine folder instead of your real one — no error, nothing visibly wrong, until you noticed your history split across two folders.

### The problem

A vault can legitimately have two folders ending in "Meta": the human `⚙️ Meta/` (Decisions, Sessions, your Session Log) and a plain `Meta/` the closed-loop engine uses for machine memory (Learnings/). The Python scripts already resolved this correctly — they pick whichever variant contains the subfolder they actually read. But five shell scripts still used a naive glob, `for candidate in "$VAULT"/*Meta; ... break`, which takes the FIRST match in sort order. Plain `Meta` sorts before the emoji-prefixed `⚙️ Meta`, so the moment the machine folder appeared, the session-close hook and friends flipped to it and your human folder stopped receiving writes.

### The fix

- **All five scripts now call the shared resolver** (`scripts/_meta_resolver.py`) — the same one the Python scripts already use — through a new command-line entry point. The resolver prefers the Meta variant that contains a known human-memory subfolder (`Sessions`, `Decisions`), so the emoji folder wins regardless of sort order or locale. Affected: `session-end-hook.sh`, `vault-daily-maintenance.sh`, `traffic-digest.sh`, `traffic-snapshot.sh`, `detect-partial-installs.sh`.
- **No logic was duplicated into bash.** There is one resolver, one source of truth, so the shell and Python paths can never disagree as the rules evolve.
- **Stock single-`Meta` vaults are unaffected** — when only one Meta folder exists, it is still chosen.

### Verification

A regression test ships with it (`scripts/test-meta-resolver.sh`, wired into `scripts/ci.sh`): it proves that when both `Meta/` and `⚙️ Meta/` exist the resolver picks the human `⚙️ Meta/`, plus negative controls — a machine-memory caller still resolves to plain `Meta/`, a stock single-`Meta` vault still resolves to it, and a vault with no Meta folder exits non-zero so the caller's own fallback runs. So the check is neither always-passing nor always-failing.

---

## 2026-06-07: your vault's own scripts now stay in sync with the repo (they used to silently rot)

**Who this affects:** anyone who set up a vault more than a few updates ago. The helper scripts inside your vault's `⚙️ Meta/scripts/` folder were copied in once, at setup, and never refreshed. So every fix or new script shipped *after* your setup — the session-close runner, the rule-conflict and drift checks, the passive-capture helper — never reached your vault. Your skills updated on `git pull`; your vault scripts didn't.

### The problem

`scripts/sync-skills.sh` keeps your installed skills (`~/.claude/skills/…`) current on every update, but it never touched the *vault* copy of the scripts. Those were only ever written by the setup phases, the first time. Nothing brought an existing vault's `⚙️ Meta/scripts/` back up to date, so it drifted further behind the repo with every release.

### The fix

- **New `scripts/sync-vault-scripts.sh`** — the skill→vault half of the sync. It copies the canonical vault scripts from the repo into your vault's `<meta>/scripts/` with the *same* safety contract as the skill sync: a file you edited locally is backed up to `<file>.bak-YYYY-MM-DD-HHMM` before it's overwritten, a symlinked scripts dir (maintainers editing the repo live) is left untouched, and an identical file is a silent no-op. It finds your vault automatically (`--vault`, `$VAULT_ROOT`, or your `settings.json`), so it runs with no arguments.
- **It runs automatically on update.** `sync-skills.sh` now calls it at the end, so a `git pull` refreshes your vault scripts the same way it refreshes your skills — no extra step.
- **`/diagnose`'s partial-install check uses it.** `detect-partial-installs.sh` now checks your whole vault-script set (not just the two aggregators), and `--fix` re-syncs them.
- The synced set is an explicit, **import-closed** manifest — a script ships only alongside the helper modules it needs, so it can't land half-broken in your vault.

### Verification

`tests/integration/test_vault_script_sync.sh` (wired into `scripts/ci.sh`) proves the manifest is import-closed, a fresh sync populates the folder, a re-run is a no-op, a locally-edited script is backed up before being updated, a symlinked scripts dir is skipped, `--dry-run` writes nothing, and an unresolvable vault is a non-fatal no-op.

---

## 2026-06-06: Smart Connections is no longer enabled by default (it could crash Obsidian on large vaults)

**Who this affects:** anyone whose vault grows past a few thousand notes. On a large vault, opening Obsidian crashed the renderer repeatedly — a hard `EXC_BREAKPOINT (SIGTRAP)` V8 fatal, CPU pinned — because heavy "indexer" plugins were building full indexes on open and exhausting Obsidian's single renderer process. Smart Connections is the heaviest of them.

### The problem

Obsidian renders your whole vault in one Electron renderer with a bounded memory heap, and every indexer plugin holds an in-memory index of your notes. Setup used to install **and enable** Smart Connections by default — it builds SQLite-backed embeddings of every note. On a small vault that is invisible; past ~5K notes, that plus the other heavy indexers loading at once can exhaust the heap and crash the app on open, before you can even disable anything from inside Obsidian. The crash needed the heavy combination — core Obsidian and Dataview alone were stable.

### The fix

- **Smart Connections is no longer in the default install.** It is documented as an explicit opt-in, with a large-vault warning and a "scope it to a subset of folders, not the whole vault" instruction. graphify already covers explicit relationships; where the Mycelium runtime is in use, that runtime is the semantic retrieval layer, so Smart Connections is redundant.
- **Dataview stays default** — it is the lightest indexer and the dashboards depend on it; it opens fine even at 13K+ notes.
- **A "large-vault plugin posture" guide** (`templates/rules/obsidian-plugins.md`): keep Dataview, scope or disable Tasks + Smart Connections as the vault grows, plus step-by-step crash recovery — quit, set `.obsidian/community-plugins.json` to `[]` (restricted mode), reopen, re-add Dataview only, then add others one at a time watching Activity Monitor. Crash reports live at `~/Library/Logs/DiagnosticReports/*Obsidian*Renderer*.ips`; an `EXC_BREAKPOINT` there means the renderer ran out of memory.
- This complements the machine-folder index exclusion shipped earlier the same day: that bounded the session/log/snapshot churn; this bounds the plugins that index your real notes.
- **`/diagnose` gained a renderer-crash check (section 13).** It scans for repeated `Obsidian*Renderer*.ips` crash reports carrying `EXC_BREAKPOINT` (macOS) and, when it finds them, prints the recovery remedy. On a clean machine it stays quiet; off macOS it skips.

### Verification

A negative-control test ships with it (`scripts/test-renderer-crash-guard.sh`, wired into `scripts/ci.sh`): it proves a reports dir with repeated Obsidian-renderer `EXC_BREAKPOINT` reports is flagged (exit 1), and that every negative control stays silent (exit 0) — an empty dir, a different app, a different crash signature, a single isolated crash (below the "repeated" threshold), and crashes outside the time window. So the check is neither always-firing nor always-silent.

---

## 2026-06-06: your brain can no longer end up with zero off-machine backup, silently

**Who this affects:** everyone. If your vault lives on one disk with no off-machine copy, one hardware failure loses everything — every note, every journal entry — with no warning. A real person hit exactly this: about 1,100 notes, no Time Machine, no cloud copy, no git remote, a single drive. The product never said a word about it.

### The problem

The hourly git auto-snapshot is *local-only* by design (it refuses to run with a remote), so "I have snapshots" still meant one disk away from total loss. Nothing detected the no-backup state, nothing offered a fix, and onboarding actively waved backup off — the old Phase 15 told the installer "your normal backup habits already cover it, no special setup needed." For anyone whose normal habits were *nothing*, that was the bug, encoded into setup.

### The fix

- **A loud, repeated session-start signal.** When no off-machine backup of any kind is detected — not our backup, not Time Machine, not a cloud copy, not a pushed git remote — every session opens with a warning and the one command to fix it. It is advisory (it never blocks) but it does not go quiet until a backup exists.
- **One-command backup.** `bash scripts/vault-backup.sh setup` asks one thing — a destination you already have (an external drive, or a Google Drive / Dropbox / OneDrive folder) — then writes one compressed daily snapshot there (a single file, not the churning git tree, so a cloud folder syncs it fine), schedules it daily, and excludes the regenerable machine-exhaust. `--encrypt` for a vault with journals or client notes (AES-256, passphrase in your OS keychain). `verify` does a real restore to prove it works — a backup you have never restored is a hope, not a backup. Windows parity ships (`vault-backup.ps1`).
- **Onboarding now establishes a backup** (Phase 1, step 8.6) and **confirms it before setup is called done** (the rewritten Phase 15), or makes you decline on purpose with the consequence stated plainly.
- **`/diagnose`** gained a backup check (section 12), and `docs/BACKUP.md` is the full guide (cross-linked from `docs/CLOUD_SYNC.md` and `docs/MAINTENANCE.md`).

### Verification

Two new self-tests ship with it, both with negative controls. The detector test proves a bare local vault reports NO_BACKUP (exit 1) and that every real off-machine-copy path reports BACKED_UP (exit 0) — so the guard is not just always-firing or always-silent. The round-trip test runs the whole loop on a fixture vault: one archive written, machine-exhaust excluded, the detector flips to backed-up, a real restore extracts the notes back, rotation honors the keep count, and the encrypted path round-trips without leaking plaintext.

---

## 2026-06-05: stop SessionStart hooks from piling up and freezing your machine

**Who this affects:** anyone who runs more than one Claude Code session at a time (a common, intended workflow). Two SessionStart hooks could pile up under concurrency and peg every CPU core.

### The problem

The secret-scan hook (`scan-prior-sessions-for-secrets.py`) stamped its 6-hour cooldown marker *after* its slow corpus scan finished, not before. So every session that started while a scan was still running saw no fresh marker and launched its own full scan. Several multi-minute scans running at once pinned the CPU until a machine froze under heavy load. There was no single-instance lock, no incremental mode, and no time budget. The runaway-process reaper (`remediate-runaway-procs.py`) only cleaned up orphaned no-op processes (a dead parent), so it was blind to exactly this kind of stuck hook — one with a live parent.

### The fix

- The secret-scan hook now claims its cooldown marker *first*, holds a single-instance lock so only one scan can run at a time, scans incrementally (only files changed since the last full pass), de-prioritizes itself, caps each pass with a wall-clock budget, and skips oversized transcripts. A second session can no longer start a second scan.
- The reaper now also clears a stuck hook process — one running under `~/.claude/hooks/` for many minutes at high CPU — which is the exact class that caused the freeze. It is path-scoped with a dual age-and-CPU gate, so a fast hook, a bounded scan, or a busy compiler is never touched. Tune or disable it with `RUNAWAY_HOOK_MIN_AGE_MIN` / `RUNAWAY_HOOK_MIN_CPU` / `RUNAWAY_REMEDIATE_BYPASS=1`.

### Verification

Two new automated tests ship with the fix. The reaper test is a positive-and-negative control over the kill decision (a stuck hook process is reaped; a young one, a low-CPU one, a non-hook program, a non-python process, and the reaper's own process are all left alone). The scan test proves a second concurrent run backs off instead of starting a second scan — with a mutation check confirming the test goes red if the lock is removed. Every other SessionStart hook was audited and found bounded (each works on a small, capped set — worktrees, branches, a single marker file — never the unbounded session corpus the scan hook walks).

---

## 2026-06-03: stop asking for your email over and over

**Who this affects:** anyone who installed without giving an email, or who declined the optional ask. Previously you could be asked again and again, in every kind of session (even while journaling), and pointed at a "token" to paste.

### The problem

A background hook ran on every message of every session. If it did not find an email-on-file marker, it interrupted with "give us your email" and walked you through fetching and pasting a token. It came back every few hours, forever. Declining was never remembered, and a network hiccup while signing up left no record, so the asking never stopped. People who had already given their email still got asked.

### The fix

The every-session ask is gone. Your email is now asked at exactly two moments, both optional and freely declinable, and neither involves a token:

- Once during first-time setup, at the very end of the install interview.
- At most once after an update actually downloads a new version, and only if there is still no email on file. After that it waits at least two weeks before it could come up again.

Declining is now remembered permanently, so "no" means no. Normal sessions, especially journaling, never ask. Nothing ever tells you to paste a token.

Existing installs heal themselves: the next time the starter updates, it removes the old hook from your settings automatically.

### Verification

New automated tests cover the post-update hook (it asks only after a real version change, stays silent on a normal session, and respects a decline) and the installer's new "retire a removed hook" step (with a negative control proving it leaves your own hooks and the other starter hooks untouched). Full rationale in [ADR-0003](adr/0003-no-runtime-email-gate.md).

---

## 2026-05-27 (late evening, part 6): unblock the personal-pii-scrub CI gate

**Who this affects:** every PR. Previously: main HEAD failed CI from the moment the scrub gate was added (commit 21162b2), and every downstream PR inherited the failure.

### The problem

The `personal-pii-scrub` workflow added to main on 2026-05-27 14:00Z flagged a mix of false positives (plugin maintainer metadata in `.claude-plugin/marketplace.json` / `plugin.json`, framework references that are deliberately public per the license-hygiene carve-out) and genuine pre-existing leaks (six `scripts/*.py` and `scripts/extractors/schemas.yaml` comments that named the author, a client, or her book title in narrative documentation).

### The fix

Two coordinated changes:

1. **Gate carve-outs for ai-brain-starter specifically.** The scrub workflow now excludes `.claude-plugin/marketplace.json` and `.claude-plugin/plugin.json` (same shape as LICENSE / AUTHORS — intentional maintainer metadata, not personal-data leakage). The pattern list drops the bare framework-name token because ai-brain-starter IS the public substrate that teaches the 34-floor framework per the CLAUDE.md license-hygiene carve-out — code comments + framework explanations legitimately reference it here. Two specific book-title phrases are added as separate scrub patterns (with a first-letter character-class trick so the workflow's own source doesn't trip the PR-scoped private-context scan in lint.yml) to keep that IP scrubbed even though the bare framework token is now allowed.

2. **Genericized six pre-existing source leaks** in `scripts/auto-wikilink.py`, `scripts/auto-crm-from-mentions.py`, `scripts/passive-capture.py`, `scripts/graphify_wikilink_gaps.py`, and `scripts/extractors/schemas.yaml`. Each was a narrative comment naming the author, a client, or her book title — replaced with generic placeholders that still communicate the example without leaking personal context.

### Verification

A local replica of the gate's git-grep loop confirms zero matches across the 14 patterns against the current tree. The pattern list is kept in lockstep across the source-files scan and the archive (`.mcpb`/`.zip`) scan.

### Caveat

The workflow file header still says "Auto-managed by ~/.local/bin/gh-harden-repos.sh (Layer 8)." Hand-edits to this file in ai-brain-starter survive only until the next daily harden run UNLESS the upstream template is updated to match. The repo-specific carve-outs are now documented in the workflow header so the template update can preserve them when it propagates.

**Bug class:** CI-GATE-ADDED-WITHOUT-FIRST-CLEANING-SOURCE.

---

## 2026-05-27 (late evening): install verification + auto-update rewires hooks correctly

**Who this affects:** anyone whose local `~/.claude/skills/ai-brain-starter` clone has local commits (a "divergent fork"), and anyone who relies on the weekly auto-update hook to keep hooks current.

### Bug 1: divergent fork = settings.json points at scripts that don't exist on disk

**The problem:** `bootstrap.sh` detects when your local clone has commits not on `origin/main` and origin has commits not on yours — a "divergent fork." It skips the pull so your local work isn't overwritten. Good. But the user-level hook installer ran anyway, writing every new hook entry from `hooks.json` into `~/.claude/settings.json`. If your local fork was missing scripts those new entries reference, hooks would silently fail at runtime — the `2>/dev/null || echo '{"continue":true}'` wrapper in every hook command swallowed the `python3: can't open file` errors without telling anyone. Result: meeting cascade + other UserPromptSubmit hooks never fired, no clue why.

**The fix:** the installer now has a `--fail-on-missing` flag that walks every hook command in the merged settings, extracts the script path, and verifies it exists on disk. Gated commands (`[ -f X ] && python3 X ...`) are recognized as intentionally optional — those don't trigger the failure. Bootstrap calls the installer with `--fail-on-missing` and escalates a missing-paths exit to a red `err` block listing every missing path plus the recovery command: `cd ~/.claude/skills/ai-brain-starter && git pull --rebase origin main && python3 scripts/install-hooks-user-level.py`. Idempotent; back-compat preserved (no flag = same behavior as before).

### Bug 2: weekly auto-update hook pointed at the wrong file + didn't tell Claude what command to run

**The problem:** the `UserPromptSubmit` auto-update hook in `hooks.json` pulls origin once a week and asks Claude to walk you through what changed. Step 4 of the post-update prompt said "Check if hooks.json differs from the local settings.local.json — if so, offer to update settings.local.json." Two bugs in one sentence: (a) the installer writes to `settings.json`, not `settings.local.json` — so the diff check was against an irrelevant file; (b) Claude was told to "offer to update" but never given the actual command, so even when Claude wanted to help, it would either guess or fail to act. After a weekly auto-update added new hooks, settings.json could stay stale indefinitely with the user none the wiser.

**The fix:** step 4 of the post-update prompt now explicitly tells Claude to run `python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py --quiet --fail-on-missing` and surface its output if it fails. The installer is idempotent and backed up, so this is safe to run automatically. The `_how_to_update` doc string at the bottom of `hooks.json` was updated to match.

### Verification

New regression test at `tests/integration/test_install_path_verification.sh` covers four cases: (1) all scripts present → exit 0; (2) required script missing with `--fail-on-missing` → exit 1 + recovery hint; (3) optional gated script missing → not flagged; (4) `--verify` alone (without `--fail-on-missing`) → exit 0 + report (back-compat). All 11 integration tests pass.

**Bug class:** SILENT-STRAND-DIVERGENT-FORK (sibling of SILENT-FAILURE-IN-EXCEPT-BLOCK, ARTIFACT-WITHOUT-AUTOMATION-WIRING).

---

## 2026-05-27 (late evening): meeting note auto-extract now works for non-English folder names

**Who this affects:** anyone whose meeting-notes folder is named something other than `Meeting Notes` or `Meeting-Notes` — Spanish (`Reuniones`), French (`Réunions`), German (`Besprechungen`), Chinese (`会议笔记`), or any custom folder.

### The problem

`scripts/write-hook.sh` is the PostToolUse hook that fires when you save a meeting note and asks Claude to run `/meeting-todos` to extract action items. It detected the meeting folder by hardcoded substring match: only `Meeting Notes/` or `Meeting-Notes/` would trigger the prompt. Save a note under `Reuniones/` → silent no-op, no auto-extract, no signal that the cascade was wired for EN only.

### The fix

The hook now reads `AI_BRAIN_MEETING_NOTES_DIR` — a colon-separated list of folder names, same shape as `PATH`. Defaults to `"Meeting Notes:Meeting-Notes"` if unset, so existing installs are unchanged. Set it in your shell init to add or replace the matched folders:

```bash
# In ~/.zshenv or ~/.bashrc
export AI_BRAIN_MEETING_NOTES_DIR="Meeting Notes:Reuniones:Réunions"
```

Folder names are matched case-insensitively as fixed-string substrings — no regex escaping, multibyte characters work (Chinese, accented Latin, etc.), trailing slashes are tolerated. Phase 11 of `/setup-brain` writes this for you when it detects a non-English vault.

### Verification

New regression test at `tests/integration/test_write_hook_meeting_folder_i18n.sh` covers seven cases: EN defaults, Spanish/French/Chinese folder names, multiple folders in one var, case-insensitivity, trailing-slash tolerance, and env-var-overrides-defaults. All integration tests pass.

**Bug class:** I18N-IN-INFRA-LAYER (cascade wired for EN-only folder names).

---

## 2026-05-27 (late evening, part 3): `/meeting-todos` no longer dead-ends on fresh-install vaults

**Who this affects:** anyone whose vault doesn't yet have a to-do file (every fresh install, plus anyone using a non-standard layout).

### The problem

`skills/meeting-todos/SKILL.md` Step 5 said "look for a file named `✅ Get to-do.md` or `TODO.md` in the vault root or `🏠 Home/` folder." If none of those existed, Claude had already done the whole extraction — read the meeting, separated your tasks from others' tasks, surfaced open questions — and then errored with "file not found" at the final write step. No guidance, no recovery, no offer to create the file.

### The fix

New **Step 0** runs first. It tries the canonical paths (`🏠 Home/✅ Get to-do.md`, `Home/✅ Get to-do.md`, `✅ Get to-do.md`, `TODO.md`, etc.), falls back to reading the vault `CLAUDE.md` for a hint, and if nothing is found, ASKS the user before creating `🏠 Home/✅ Get to-do.md` with canonical frontmatter (`type: todo`, `created:`, `updated:`) and a single `## Inbox` section. On "no", the skill stops cleanly rather than wasting an extraction it can't file. Step 5 was rewritten to reuse the path Step 0 already resolved — no filesystem re-search at write time.

### Verification

Regression test at `tests/integration/test_meeting_todos_step0_create_if_absent.sh` asserts Step 0 is present, names the canonical path, falls back to CLAUDE.md, asks before creating, names the canonical frontmatter + Inbox section, has an explicit "on no, stop" branch, and that Step 5 reuses the resolved path.

**Bug class:** WORKFLOW-DEAD-ENDS-ON-MISSING-DESTINATION (sibling of SILENT-NO-OP-AFTER-WORK-ALREADY-DONE).

---

## 2026-05-27 (late evening, part 4): meeting-workflow truncation flag now reads first, cap raised to 16K

**Who this affects:** anyone whose customized `⚙️ Meta/rules/meeting-workflow.md` exceeds 8K chars — the cap was hitting most real-world vaults (the canonical template is 4.8K, but customized rules typically grow to 8–12K once Phase 11 has folded in the user's tool stack + per-vault folder conventions).

### The problem

`hooks/inject-meeting-workflow-on-trigger.py` caps the injected rule content at `MAX_RULE_CHARS`. When the cap fired, the truncation marker was appended at the END (`...[truncated — read full file at <path>]`). But the header BEFORE it said "Run the FULL cascade below" — so Claude started executing the cascade against the first 8K chars without knowing late steps had been dropped. Decision Log entries, CRM updates, humanizer passes, backlinks verification, and final reporting could silently not happen, with Claude only noticing the truncation marker after irreversible writes were already done.

### The fix

Two changes:

1. **Cap raised from 8K to 16K.** The additionalContext budget is generous; the canonical template plus a Phase-11-customized version fits comfortably. Most real-world rules no longer trigger truncation at all.

2. **When truncation does fire, the TRUNCATED flag now goes FIRST.** It appears at the very top of `additionalContext`, before the cascade-instruction. The flag names the rule-file path, tells Claude to read the full file before running the cascade, and explicitly lists the silent-drop categories at risk (Decision Log, CRM updates, humanizer pass, backlinks verify, final report). The tail marker stays as defense in depth.

### Verification

Regression test at `tests/integration/test_inject_meeting_workflow_truncation_flag.sh` covers six properties: cap is 16K, below-cap rules don't get the flag, above-cap rules get the flag at the TOP (asserted via byte-position comparison vs. the cascade-instruction), the flag names the rule path, the flag warns about silent-drop categories, and the tail marker is preserved. All 11 integration tests pass.

**Bug class:** TRUNCATION-FLAG-AFTER-DAMAGE-DONE (sibling of READING-ORDER-MATTERS-FOR-INSTRUCTION-INJECTION).

---

## 2026-05-27 (late evening, part 5): Phase 11 writes the customized meeting-workflow rule to the vault file, not just CLAUDE.md

**Who this affects:** anyone running `/setup-brain` and Phase 11 after this change. Existing users with a customized rule in CLAUDE.md are unaffected — re-run `/setup-brain` Phase 11 if you want the rule file rebuilt.

### The problem

Phase 4 of `/setup-brain` copies a generic Granola-default `meeting-workflow.md` template into `<vault>/⚙️ Meta/rules/`. Phase 11 then interviews the user about which meeting tool they actually use (Otter / Google Meet + Gemini / Fireflies / Zoom / Teams + Copilot / Notion AI / manual) and generates a tool-specific rule. The bug: Phase 11 appended that customized rule to CLAUDE.md and left the generic Granola template untouched in the vault rule file. But `hooks/inject-meeting-workflow-on-trigger.py` reads from the vault rule file. So the hook kept injecting the generic Granola guidance into context every time the user said "I just had a meeting," even though Phase 11 had already generated a perfectly customized version that lived elsewhere.

### The fix

Phase 11 now writes the customized rule to `<vault>/⚙️ Meta/rules/meeting-workflow.md`, **overwriting** the generic copy Phase 4 placed. The vault rule file is the canonical source of truth — same file the inject hook reads. Phase 11 also explicitly warns against duplicating the rule body into CLAUDE.md (the prior bug-source) — drift between two near-identical bodies was the failure mode.

### Verification

Regression test at `tests/integration/test_phase11_writes_to_vault_rule_file.sh` asserts the Phase 11 instruction names the vault rule path, names "overwriting" intent, explains why (the inject hook reads from this file), and warns against duplication. All integration tests pass.

**Bug class:** CUSTOMIZATION-LIVES-IN-WRONG-FILE (sibling of TWO-SOURCES-OF-TRUTH-DRIFT).

---

## 2026-05-27 (evening): meeting cascade — Windows install + Granola fallback path + louder install failure

**Who this affects:** every user, especially Windows installers and anyone who runs `bootstrap.sh` without then completing the conversational `/setup-brain` flow. Three critical bugs caught by a pre-deployment adversarial audit before a 30-user install batch.

### Bug 1: Windows users got NO hook wiring

**The problem:** `bootstrap.sh` (Mac/Linux) ran `python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py` at the end of install, which writes every hook (incl. the new "I just had a meeting" trigger) into `~/.claude/settings.json`. `bootstrap.ps1` (Windows) had ZERO equivalent — 841 lines, zero references to the installer. Windows users got hook files on disk but nothing wired in settings, so the trigger phrase silently produced nothing.

**The fix:** ported the user-level-hook install block into `bootstrap.ps1`. Auto-detects `python3` / `python` / `py` from PATH. Hard-fails (red error) with the manual re-run command if Python is missing or the installer exits non-zero, so a non-tech user can't miss it. Dry-run prints a preview line.

### Bug 2: `FALLBACK_SUMMARY` pointed at a non-existent Granola cache path

**The problem:** when the hook fires but the user hasn't run `/setup-brain` (so no customized `⚙️ Meta/rules/meeting-workflow.md` in the vault), it falls back to an embedded summary. That summary told Claude to "Check Granola (`~/Library/Application Support/com.granola.granola/`)" — but the real path per `scripts/granola_sync.py` is `~/Library/Application Support/Granola/cache-v6.json`. Claude searched a non-existent folder, found nothing, told the user "no transcript" — when in fact one existed.

**The fix:** the fallback now instructs Claude to invoke `granola_sync.py` directly (which auto-detects the real cache path) rather than embedding any hard-coded path. Tool-agnostic — also lists Google Meet+Gemini / Otter / Fireflies / Zoom / Teams / Notion AI Notetaker as parallel branches, plus the vault Meeting Notes folder with localized variants. If everything fails, suggests `/setup-brain` to wire the user's actual tool.

### Bug 3: `bootstrap.sh` installer failure was a quiet yellow `warn`

**The problem:** if the user-level hook installer (`install-hooks-user-level.py`) exited non-zero on Mac/Linux (e.g., pre-existing settings.json with parse errors), the bootstrap output buried the failure as a single yellow `warn` line. The 30x team install would walk away thinking everything worked while the meeting trigger + 6 other hooks silently never fired.

**The fix:** escalated to a red `err` that gets surfaced in the install-summary `Failed:` list at the very end. Message names the consequence ("meeting trigger + 6 other UserPromptSubmit hooks WILL NOT FIRE until resolved") and the manual re-run command.

**What you should do:** nothing. The SessionStart auto-update flow picks this up on your next session. Windows users specifically: re-run `bootstrap.ps1` to get the hook-installer step.

**Acknowledgement:** thanks to the adversarial-review pass that caught these before the install batch — the original PR shipped morning of 2026-05-27, the FP/FN fix landed afternoon, and this set landed evening. All three made it into the user's first cluster install in one day.

---

## 2026-05-27 (afternoon): broader natural-language coverage on the meeting trigger

**Who this affects:** every user. Tightening from the morning ship (PR #120). Real-world stress testing surfaced 6 false negatives on common phrasings — "I just had my discovery call", "I just got out of the kickoff call", "1:1 with my manager just ended", "meeting with the founders just ended", "Just had a great call!", "pull my notes from this morning's meeting" — and 2 false positives — "I just had to call the bank" (verb-of-action, not a meeting), "pull request review" (code-context, not a meeting note).

**The fix:**

- **Compound-noun handling.** The regex now allows up to 3 hyphen-aware adjective tokens between a required determiner ("a / the / my / our / today's") and the meeting noun. "I just had my discovery call" / "I just wrapped up the all-hands meeting" / "I just got out of the kickoff call" all match because "discovery", "all-hands", "kickoff" sit in the modifier slot. "I just had to call the bank" doesn't match because there's no determiner between "had" and "call" (just the infinitive marker "to"), so the pattern fails.
- **Terse forms ("Just had a great call!").** New trigger without the "I" prefix.
- **Phone-end signal.** "I just got off the phone with X" now fires.
- **Artifact-pull discipline.** The `pull / process / file / capture / extract` patterns now have TWO branches: (1) artifact-list-only (`notes / transcript / recording / granola / action items / to-dos`) — so `pull request review` can't match `review` as a meeting noun; (2) `<verb> + <det> + <noun>` — requires a determiner after the verb, so `pull request review` (no det) doesn't match but `pull the standup note` does.
- **Spanish "salir del".** Contracted "salir del kickoff" now matches alongside "salir de la junta".
- **Expanded noun lists.** Added `check-in / session / demo / kickoff / kick-off / retro / retrospective / review / briefing / workshop / offsite / alignment / all-hands` (EN), `entrevista / charla / junta / reu / demo / kickoff / sesión / check-in / taller` (ES), `chat` as a standalone noun (was previously gated behind `chat with`).
- **Bilingual mixing.** "acabo de tener un meeting con el cliente" / "ya terminé el call con el equipo" now match — EN nouns with ES verbs are common in bilingual teams.

**Coverage now:** 27 EN positives + 18 ES positives + 38 negatives in the regression test (`tests/integration/test_meeting_workflow_trigger_hook.sh`). All pass. Validated against an additional 48-positive + 34-negative external stress test.

**What you should do:** nothing. The SessionStart auto-update flow picks this up on your next session.

---

## 2026-05-27: "I just had a meeting" now actually fires the cascade

**Who this affects:** every user. If you've said "I just had a meeting" or "pull the transcript" and Claude shrugged instead of pulling the transcript and updating your to-dos, this is the fix.

**The bug:** `templates/rules/meeting-workflow.md` shipped with a `trigger:` frontmatter field listing the phrases — "I just had a meeting", "pull meeting notes", "pull the transcript", "[name] meeting is done". The README and POWER_TOOLS.md both promised the rule "fires" on those phrases. But the `trigger:` field was just informational text — nothing in `hooks.json` actually pattern-matched the phrases. The only thing surfacing the rule to Claude was a single bullet in the once-per-session `session-start-context.py` dump ("- meeting-workflow.md for meetings"). If the user said "I just had a meeting" any time after the first prompt of the session, the rule wasn't in context and Claude had no way to know to read it. Cascade silently no-op'd. Bug class: **ARTIFACT-WITHOUT-AUTOMATION-WIRING**.

**What's new:**

**`hooks/inject-meeting-workflow-on-trigger.py`** — a UserPromptSubmit hook that pattern-matches "I just had a meeting" + variants (EN and ES, accent-insensitive), reads the user's vault `⚙️ Meta/rules/meeting-workflow.md` (so Phase 11's per-tool customization is preserved), and injects the full rule as `additionalContext`. Fires on every matching prompt, not just the first one of the session. Same proven pattern as `inject-best-of-best-on-consulting.py` and `inject-love-language-context.py`.

Trigger regex is temporal-anchored to avoid false positives — "I just had a meeting" fires, "I have a meeting tomorrow" does not. Bilingual coverage:

- EN: *"I just had a meeting"*, *"the meeting just ended"*, *"meeting with John just ended"*, *"pull the transcript"*, *"pull my meeting notes"*, *"process today's meeting"*, *"[name]'s meeting is done"*, *"done with my interview"*, *"wrapped up the sync"*, and more.
- ES: *"acabo de tener una reunión"*, *"la reunión ya terminó"*, *"ya terminé la reunión"*, *"trae las notas de la reunión"*, *"saca el transcript"*, *"reunión con María ya terminó"*, etc. Works whether the user types accents or not.

If the rule file doesn't exist in the vault (user skipped Phase 4), the hook falls back to an embedded summary so the cascade still fires — never silent.

**Bypass:** set `MEETING_WORKFLOW_BYPASS=1` env var, or include the literal `MEETING_WORKFLOW_BYPASS=1` token in the prompt.

**`tests/integration/test_meeting_workflow_trigger_hook.sh`** — regression test asserting (1) hook is present, (2) hook is wired into `hooks.json` under `UserPromptSubmit`, (3) 13 EN positive triggers fire, (4) 12 ES positive triggers fire, (5) 14 negative cases (future/past-week/planning/asking-about) do NOT fire, (6) injected payload references the rule body, (7) bypass env var works, (8) in-prompt bypass token works, (9) empty prompt produces no output, (10) fallback summary fires when no vault rule exists.

**`hooks.json`** — added the new hook to the `UserPromptSubmit` chain between `inject-love-language-context.py` and `email-gate-hook.py`. `scripts/install-hooks-user-level.py` ABS_FINGERPRINTS list updated so the migrator owns the new hook on subsequent installs.

**What you should do:** nothing. The SessionStart auto-update flow picks this up on your next session (or `git pull` manually in `~/.claude/skills/ai-brain-starter/` to grab it immediately). Existing `settings.local.json` files will be offered the update via the standard auto-update prompt.

---

## 2026-05-19: close the commit-time race for reconcile-worktree-shared

**Who this affects:** anyone using git worktrees inside an Obsidian vault with the `reconcile-worktree-shared.py` SessionEnd hook, who has ALSO seen the worktree-archive prompt fire "N uncommitted changes will be discarded" warnings on byte-identical-to-main files.

Reconcile fires at SessionEnd. Between then and the worktree-archive prompt, OTHER committers (hookify-auto-commit, auto-snapshot, your own commit wrappers) can land commits on master. The active worktree branch falls behind. Archive sees those byte-identical files as "uncommitted" and warns. False positive, but it trains the eye to ignore the warning - which would mask a real loss the day a worktree edit ISN'T also at main.

**What's new:**

**`scripts/post-commit-ff-worktrees.sh`** - a generic helper any committer can call after landing a commit on main. It enumerates active claude/* worktrees via `git worktree list --porcelain` and `git merge --ff-only` each one to the main branch tip. Silent on success, silent on FF-impossible (diverged branches are the reconcile-on-SessionEnd fallback's job, not ours). Parses worktree-list output line-by-line via bash `case` - never `awk $2`, because AWK's default field split silently truncates worktree paths containing spaces.

**`tests/integration/test_post_commit_ff_worktrees.sh`** - CI test for the helper. Three assertions: FF advances a strict-ancestor worktree branch, space-containing paths parse correctly, diverged branches are left alone.

**What you should do:** if you maintain your own commit wrapper(s), call the helper as the last step after a successful commit:

```bash
bash /path/to/post-commit-ff-worktrees.sh "$MAIN_VAULT"
```

And ALSO wire `reconcile-worktree-shared.py` into your `~/.claude/settings.json` Stop hooks (not just SessionEnd) so the FF fires between assistant turns. SessionEnd alone leaves a race window between the hook firing and the archive prompt.

---

## 2026-05-18: NVIDIA Tier-2 helpers + two new SessionStart hooks

**Who this affects:** anyone running LLM-calling scripts in their vault, or anyone whose Claude desktop sessions sometimes wedge after a few days.

Three small additions, all opt-in.

**`scripts/nvidia.sh` + `scripts/_nvidia_router.py`** — bash and Python helpers for the NVIDIA build endpoint (Llama 3.3 70B, Llama 4, Qwen3, DeepSeek V4, Nemotron). The Python helper mirrors the API surface of a Claude router so vault scripts can swap one import + one call when the workload is mechanical (classification, extraction, format conversion, structured-output regex work). Free credits on developer accounts. Reads `NVIDIA_API_KEY` from the standard fallback chain (env → `.zshenv` → `.zsh_secrets` → `.zshrc` → …). NEVER use this for judgment, voice-sensitive prose, or agentic loops — the quality gap is real.

**`scripts/nvidia_compare.py`** — comparison harness. Before flipping any pipeline from Claude to a Tier-2 model, run real samples through both and require ≥90% agreement. Text mode uses an LLM-as-judge; JSON mode does structural equality. Pluggable judge callable — defaults to the Claude router next to it if one exists, otherwise accepts `judge_fn=...`. Fails loudly on quality drift instead of letting a cheaper price tag hide it.

**`hooks/check-cron-paths.sh`** — SessionStart hook that warns if your crontab references absolute paths under `$HOME` that no longer exist. Useful after any directory move that affects cron jobs. Silent on success, never blocks.

**`hooks/pty-pressure-check.sh`** — SessionStart hook that warns when the macOS pseudo-terminal pool is ≥75% full. Long-running Claude desktop processes can leak ptys over multi-day sessions; at 100% no new shell can spawn. Silent on success, never blocks.

**What you should do:** the bash hooks land in `~/.claude/hooks/` if you want them — register in your `settings.json` under `hooks.SessionStart`. The NVIDIA helpers live in `scripts/` and are unused unless you call them.

---

## 2026-05-18: removed the `no-duplicate-h1` hookify template

**Who this affects:** anyone who installed the `no-duplicate-h1.local.md` warn rule from `templates/hookify-rules/`.

The rule fired on any `# Title` line in `.md` files and was meant to keep H1s out of Obsidian notes (where the filename already renders as the title). In practice the regex `^# .+` matched H1-looking lines inside fenced code blocks too. Every bash comment starting `# Setup` at column 0 inside a triple-backtick block would warn, and the friction outweighed the benefit.

**What changed:**
- `templates/hookify-rules/hookify.no-duplicate-h1.local.md` removed.
- Matching test cases dropped from `templates/scripts/hookify-rule-tests.py`.

**What you should do:** if you installed the rule in your own vault and want it gone, delete your `.claude/hookify.no-duplicate-h1.local.md`. If you like the rule and want to keep it, it will keep working. The template removal does not touch installed copies.

The Obsidian rendering behavior (filename = title) is unchanged. You can still avoid H1 in note bodies as a personal style; there is just no enforcement template shipping with the starter kit.

---

## 2026-05-17: the session-close cascade no longer strands artifacts in a worktree

**Who this affects:** anyone who uses git worktrees and runs the session-close cascade.

When you said "bye" or "wrapping up" from inside a worktree, the close cascade pre-resolved the session file, Decisions, Captures and Time Tracking to the *worktree's* own `Meta/` folder, not the main vault. A worktree sits on a throwaway `claude/<slug>` branch, so when the worktree was archived those writes showed up as "uncommitted changes that will be permanently discarded." Session history was silently lost.

PR #66 fixed this same bug class for the Stop hook (`session-end-hook.sh`). It was never fixed for the UserPromptSubmit hook (`detect-closing-signal.py`), the Layer 1 hook that pre-resolves the paths the model writes to. This release closes that gap.

**What changed:**
- `detect-closing-signal.py` gains `resolve_main_vault()`. When the cascade fires inside a worktree, the vault root is collapsed back to the main vault before any artifact path is resolved. Session files keep the worktree slug in their filename; only the directory changes to the main vault. Mirrors the `resolve_main_vault()` already in `session-end-hook.sh`.
- New SessionStart watchdog `surface-stranded-session-artifacts.py`. It scans every worktree for session artifacts left uncommitted and surfaces them loudly at the next session start, before they can be archived away. This is the uncommitted-changes companion to `surface-orphan-claude-branches.py`, which already covers committed-but-unmerged commits on `claude/*` branches.
- Two new CI tests (`test_detect_closing_signal_worktree.sh`, `test_stranded_session_artifacts_watchdog.sh`) enforce both invariants and fail on revert.

**What you should do:** nothing required, the auto-update picks it up. To wire the new watchdog into your SessionStart hooks immediately, re-run `bash ~/.claude/skills/ai-brain-starter/bootstrap.sh` (idempotent).

---

## 2026-05-14: install hardening — slash commands actually appear in the palette + activation in this session + capture everything

**Who this affects:** anyone who installed before today. Twelve PRs landed today that fix gaps in the install flow. The most user-visible:
1. After install, typing `/` in a new session now actually surfaces `/journal`, `/second-brain-mapping`, `/diagnose`, `/setup-vault-types`, and the other shipped skills. Before today, the skill folders were installed but the palette entries were not — typing `/second-brain-mapping` got blank in the dropdown.
2. The install used to ask "how do you want to back up your vault — Google Drive / iCloud / Dropbox / Git / local?" mid-flow. That five-option menu confused people and stalled momentum. Killed. Desktop is the canonical vault home; your normal backup habits cover it.
3. The install used to end with a Substack link and no inline orientation. Now the three commands and one habit for week one are named **in the conversation** before the link. Users who don't click the link still walk away with the picture.
4. New activation moment in the install session itself: "Bring me ONE active doc right now." A real doc from the user's life lands in the vault before close — not deferred to a "next session" that may never happen.
5. New canonical `🏠 Home/About Me.md` file gets created at install and populated as the user answers questions. Personal context revealed anywhere now lands somewhere durable, instead of going nowhere.

**What you should do:** re-run `bash ~/.claude/skills/ai-brain-starter/bootstrap.sh` once to backfill all the missing pieces. Idempotent — it skips anything you already have. After it finishes, open a new session and type `/` — you'll see the full slash command palette.

### Breaking down the twelve PRs

| PR | What | Impact |
|---|---|---|
| #66 | 5-layer fix for the worktree session-loss bug class. `session-end-hook.sh` `resolve_main_vault()` strips `.claude/worktrees/<slug>/` from path resolution. `worktree-prune.sh` refuses to delete branches with unmerged commits. New CI test enforces the invariant. New `recover-orphan-claude-branches.py` recovery script. | Closes [#65](https://github.com/mycelium-hq/ai-brain-starter/issues/65) (reported by a user who lost a full debrief session). Existing orphan commits surface via the new SessionStart hook. |
| #67 | Phase 0 install guidance: don't wrap bootstrap output in the Monitor tool. It rendered every stdout line as a "Human:" turn and leaked `<task-notification>` XML to the chat. | Quieter install, no more wall of bare "Human:" labels. |
| #68 | reconcile-worktree-shared.py now does `git merge --ff-only master` first instead of always committing on the worktree branch. Cuts orphan-commit accumulation from ~1 per session-end to zero in the normal case. Plus install fixes: killed the "2-3 hours / 30 min for basic" time-gate line; wired key-people interview to also create per-person CRM files. | Cleaner branch history. Install asks for full name + nickname per person. |
| #69 | CI test for the reconcile FF invariant (companion to PR #66's test). New SessionStart hook surfaces orphan-branch count when nonzero. Em-dash warning added to the public-repo scrub gate. | Drift prevention. The 78f4a37 → #65 → #68 chain (same defect recurring one week later because no CI test caught it) is now closed at every layer. |
| #70 | Phase 15 backup question stopped pushing users to iCloud/Drive/Dropbox when their vault was on Desktop. Silent acknowledgment, move on. New Phase 24.6 "progressive use" pointer. Phases 24, 24.5, 24.6 marked MANDATORY in install closing checklist. | Smoother close. No more confusing backup ask. |
| #71 | Phase 24 now names the three commands and one habit **inline** in the conversation, matching the canonical Substack first-week post verbatim. Plus a stuck-help pointer. | Users who don't click the link still know what to do tomorrow. |
| #72 | NEW Phase 19.5: "Bring me ONE active doc right now." Activation moment in the install session itself, before close. Plus Desktop confirmed as canonical vault home. Phase 24.6 reframed from deferred action to progressive use. | Install ends with TWO real things in the vault (first journal entry + first imported doc), not zero. |
| #73 | Bootstrap install loop was hardcoded to 8 skills and missing `second-brain-mapping`, `setup-vault-types`, and `diagnose` — all referenced as slash commands in phase docs. New CI test asserts every phase-doc slash command has a matching skill folder AND is in the install list. | Fresh installs now actually have the skills the install flow references. |
| #74 | `commands/` directory at repo root with one `.md` per slash command. Bootstrap copies them into `~/.claude/commands/` so they actually appear in the Claude Code palette when users type `/`. CI test extended to assert each required skill has a matching commands/<name>.md. | This is the fix for "I typed `/` and `/second-brain-mapping` didn't come up." Skill folders alone don't register palette entries — plugin-style commands/<name>.md files do. |
| #76 | Phase 11 (Gmail / Google Workspace MCP install) now MANDATORY with explicit Phase 4 carryover: if the user mentioned Gmail in the tools answer, install fires automatically. Phase 13 (health) split into 13a (wearables, existing) + 13b (lab tests + health reports, NEW). | No more "user mentioned Gmail, model captured it in CLAUDE.md, never installed the MCP." Same for labs — people with annual bloodwork but no wearable now get asked. |
| #77 | NEW canonical `🏠 Home/About Me.md` file. Sectioned schema (Identity, Work, Relationships, Health, Values, Habits, Hobbies, History, Notes). Universal capture rule: anything personal the user reveals during install (or any future session) must land somewhere durable — append to About Me, never overwrite, don't pause to confirm. Phase 4 produces CLAUDE.md AND About Me at once. Phases 11, 13 also write through to About Me. | Information given → information saved. No more "I have ADHD" mentioned in passing and forgotten. |
| (this PR) | CHANGELOG entry. Phase 3b idempotency — preserve user About Me content on re-install. CLAUDE.md template gains a universal capture rule line so the rule loads every session forever. | Re-install no longer overwrites your About Me. The capture rule is now visible in every future session. |

### Why so many in one day

A friend's install surfaced a cascade of distinct failure modes, each one revealing the next: orphan commits → install missing skills → palette entries missing → activation deferred → information collected and discarded. Each layer had a real surface bug; each got a fix shipped with a CI test (where applicable) so the next install doesn't repeat the failure.

The pattern that emerged and is now codified: **every fix to a bug class ships with (a) the actual fix, (b) a CI test that would fail on revert, (c) an observability surface that catches future regressions.** Same family across PRs #66, #68, #73, #74.

---

## 2026-05-10: health-mcp v0.7 — multi-year analytical surface + /longitudinal skill

**Who this affects:** anyone with 12+ months of health-mcp data who wants to surface patterns that span years — Floor-to-body fingerprints, cycle-phase × HRV, sleep architecture drift, longevity-marker trends, symptom correlates. Single-day tools (recovery score, journal context) were already in v0.2. v0.7 fills the multi-year analytical gap.

**The shape:** the Health & Body panel (Peter Attia + Stacy Sims + Chris Winter + Bessel van der Kolk + Carrie Pagliano + Lara Briden's load-bearing dissent) converged on six analytical surfaces. v0.7 ships all six plus a `/longitudinal` skill that wraps them with Briden's noise filter codified: report only signals above a strength threshold, never drown the user in correlations.

### What changed

1. **`services/health-mcp/analytics.py`** (new, ~600 lines): the analytical core. Stdlib only (statistics, math) — no scipy dep so install footprint stays tiny. Pearson correlation in pure Python. Signal-strength gate (strong / moderate / weak / noise) based on |r| × n.
2. **`services/health-mcp/main.py`**: seven new MCP tools.
   - `health_correlate(metric_a, metric_b, group_by, vault_root, lookback_days)` — pairwise correlation, optionally grouped by Floor / cycle phase / day-of-week.
   - `health_floor_body_fingerprint(floor, vault_root, lookback_days)` — body signature for a named Floor (Acceptance, Anger, etc.) vs all other days.
   - `health_loop_signature(loop_dates_iso, vault_root, lookback_days)` — body fingerprint of a named loop (Founder Exhaustion Loop, etc.).
   - `health_sleep_architecture(start, end)` — REM/Deep/Core/Awake percentages, efficiency, fragmentation. Night-of bucketing (sleep starting 18:00+ goes to the next day).
   - `health_longitudinal_summary(start, end, granularity)` — month/quarter/year aggregation of longevity markers (HRV baseline, VO2max, lean body mass, walking steadiness, etc.).
   - `health_symptom_correlate(symptom_type, vault_root, lookback_days)` — body fingerprint of symptom-present vs symptom-absent days.
   - `health_top_signals(vault_root, lookback_days, min_strength)` — the Briden filter. Scans curated metric pairs + Floor × HRV pairings, returns ONLY signals at or above min_strength.
3. **`skills/longitudinal/SKILL.md`** (new): wraps the seven tools into a single `/longitudinal` pass. Six-step orchestration: top_signals first (noise filter), then top-3 Floor fingerprints, sleep architecture drift, longitudinal markers, symptom correlates, named loops if /patterns ran recently. Required Health & Body panel commentary on the surfaced signals.
4. **`services/health-mcp/tests/test_v07_analytics.py`** (new): 19 tests covering Pearson correctness (positive/negative/zero-variance/below-min-n), signal-strength gate (strong/moderate/weak/noise), metric alias resolution, correlate with seeded fixtures, sleep architecture stage percentages, longitudinal monthly buckets, loop_signature vs baseline, friendly-name resolution, no-data graceful handling, top_signals filter behavior.

### Why a noise filter is the centerpiece

Briden's dissent in the panel pass was load-bearing: "Don't drown in correlations. More data without hypotheses is rumination in spreadsheet form. Focus on the 3 metrics that actually predict your worst days." The substrate honors this by returning a `signal_strength` field on every correlation and a `top_signals` entry point that scans + filters before returning anything. The `/longitudinal` skill then reports only signals above the threshold and explicitly tells the user when nothing cleared.

### Tests

116 passing (19 new + 97 prior). 3 deselected (e2e tests gated behind DB lock).

### What to do

If you have 12+ months of data: run `/longitudinal` for a year, `/longitudinal 5y` for the longer view, `/longitudinal all` for whatever your DB holds.

If you just started: keep journaling + the auto-sync chain running. The surface becomes useful around 3-6 months of paired Floor + body data.

---

## 2026-05-10: health-mcp v0.6 — Apple Shortcuts bridge for free Apple-native auto-sync

**Who this affects:** Apple Watch / iPhone users who want HealthKit data flowing into the DuckDB without re-exporting an XML zip every few weeks AND without depending on a paid third-party iOS app (the prior v0.2 TCP shim required Health Auto Export Premium).

**The shape:** Apple does not expose a network API for HealthKit; data lives on-device. The substrate's three prior Apple Health paths were XML re-export (manual, periodic), Simple Health Export CSV (manual, periodic), and the v0.2 Health Auto Export TCP shim (real-time but paid third-party dep). v0.6 adds a fourth path that is Apple-native + free: Apple Shortcuts personal automation writes a daily JSON payload to iCloud Drive; the Mac receiver picks it up and ingests on the next `/journal` Stop hook. Karpathy + DHH + Naval converged on the panel: a substrate that depends on a paid app has a ceiling on adoption the substrate author does not control. Steve Jobs's load-bearing dissent (verify automation reliability) was resolved by iOS 17+ time-of-day automations supporting silent "Run Without Asking" execution.

### What changed

1. **`services/health-mcp/shortcut_normalize.py`** (new): translates the iOS Shortcut's JSON payload shape into the same `_kind`-tagged dicts that `parse_xml.iter_records` produces, so `_bulk_insert` works without modification. Sleep stage aliases, numeric coercion, malformed-entry skip — all covered.
2. **`services/health-mcp/main.py`**: two new MCP tools.
   - `health_import_shortcut(payload_path, force=False)` — import a single `<YYYY-MM-DD>.json` payload, idempotent via file SHA.
   - `health_sweep_shortcut_inbox(inbox_path=None, archive=True)` — drain every payload in the iCloud Drive inbox, archive to `processed/` after success.
3. **`hooks/coach-auto-prescribe-on-journal.py`**: the v0.5.1 chain hook gains a third sync step alongside Oura + Fitbit. On `/journal` Stop, the hook scans `~/Library/Mobile Documents/com~apple~CloudDocs/health-mcp/`, ingests every new payload, archives to `processed/`. Reports `Apple Shortcut +N (Md)` in the chain summary.
4. **`services/health-mcp/shortcut/README.md`** (new): 3-step iPhone setup + payload schema + coverage notes + troubleshooting.
5. **`services/health-mcp/tests/test_shortcut_bridge.py`** (new): 16 tests covering normalizer round-trip, sleep stage aliases, malformed input handling, file SHA stability, end-to-end import + sweep behavior.

### Tests

13 normalizer + I/O tests pass deterministically without DB. 3 e2e tests gated behind fastmcp availability + DB fixture (run via `pytest -k "e2e or sweep"`). Total v0.6 test count: 16 new + 81 prior = 97.

### Coverage and limits

Apple Shortcuts can read HRV, RHR, sleep stages, steps, workouts, mindful minutes, cycle data, VO2 Max — roughly 95% of what the substrate's body-track section, recovery score, and journal context use. Three types are not exposed via `Find Health Samples`: ECG records, full symptom logs, and State of Mind (iOS 17.2+ partial). For full coverage, run `health_import_xml` periodically (monthly is plenty) alongside the daily Shortcut sync. The substrate de-dupes via file SHA so the two paths coexist cleanly.

### What to do

If you want zero-touch Apple Health auto-sync: follow the 3-step iPhone setup in `services/health-mcp/shortcut/README.md`. The Mac side is already wired.

If you already use Oura, Fitbit, or are happy with periodic XML exports: nothing to do. The v0.5.1 chain still runs Oura + Fitbit; the Apple Shortcut step skips silently if the iCloud inbox doesn't exist.

If you want to opt out of just the Apple Shortcut sweep without disabling Oura + Fitbit: the existing `HEALTH_AUTO_SYNC_BYPASS=1` env var skips ALL wearable syncs in the chain. A more granular bypass for just Apple Shortcuts is on the v0.7 roadmap.

---

## 2026-05-10: health-mcp v0.5.1 — collapse auto-chain to /journal-only (single daily trigger, not per-session)

**Who this affects:** users on v0.5 who have many Claude Code sessions per day.

**The shape:** v0.5 wired the wearable sync to SessionStart. The panel correction (Karpathy + Patrick Collison + Naval, panel pass 2026-05-10 minutes after v0.5 merged): for users with ~20 sessions/day, that's 20× the python-process overhead per day for a logic that only needs to run once daily. The fix: tie the entire chain to /journal, the user's existing once-daily habit. Howard Marks's dissent on the panel was load-bearing — the explicit cost is "if you skip /journal for 2 days, your wearable data is 2 days stale and your prescription is off." That cost is tolerable, and /health doctor surfaces it loudly.

### What changed

1. **`hooks.json`**: removed the SessionStart `health-auto-sync.py` entry. The Stop-on-journal entry stays.
2. **`hooks/coach-auto-prescribe-on-journal.py`**: now runs three steps in order — wearable sync (Oura + Fitbit if > 24h stale and credentials present), yesterday's journal backfill, today's coach prescription. The single entry point for the daily auto-chain.
3. **`hooks/health-auto-sync.py`**: still ships in the repo + still fully tested, but is NOT wired in the default `hooks.json`. Available as an opt-in for power users who want per-session sync.
4. **Granular bypass**: `HEALTH_AUTO_SYNC_BYPASS=1` now skips ONLY the sync step inside the chain hook (lets a user disable just the wearable pull without disabling the coach prescription).
5. **`docs/AUTOMATION.md`**: rewritten to reflect the single-trigger architecture, with a "Why /journal is the gate (not SessionStart)" section documenting the panel reasoning + Howard Marks's dissent.

### Tests

81 passing (unchanged from v0.5). The hooks.json test was renamed and tightened — it now ASSERTS that `health-auto-sync.py` is NOT in SessionStart (regression guard), AND that `coach-auto-prescribe-on-journal.py` is in Stop.

### What to do

If you installed v0.5 already: re-run `/health-setup` or manually remove the SessionStart entry for `health-auto-sync.py` from your `~/.claude/settings.json`. Restart Claude Code. From that point on, the chain fires once per day at `/journal` Stop only.

If you actively want per-session sync (rare): add the SessionStart entry back manually. The script still works; it just isn't the default.

The substrate philosophy stays: if you don't journal, the chain quietly waits. `/health doctor` shows the staleness so you know.

---

## 2026-05-10: health-mcp v0.5 — auto-trigger chain + /health-doctor (no more remembering commands)

**Who this affects:** anyone using the health stack who is not going to remember to run `/coach today`, `/ingest-health`, or `/backfill-journal-body-context` every morning. Anyone who shipped capability across v0.1-v0.4 and noticed nothing was happening.

**The shape:** Across v0.1-v0.4 the substrate shipped 41 tools + 5 skills, but only ONE auto-fire chain (`health-context` inside `/journal`, `/coaching`, `/panel`, `/patterns`, `/insights`). Everything else was manual: `/health-setup`, `/ingest-health`, `/coach today`, `/backfill-journal-body-context`. The user-facing point of the substrate was getting buried under commands the user had to remember.

The permanent-fix-pattern rule from CLAUDE.md applies: capability without trigger isn't deployed. v0.5 is the automation layer.

### Two auto-trigger hooks

1. **`hooks/health-auto-sync.py`** — SessionStart hook. Whenever the user opens Claude Code, the hook checks the freshness of the last Oura import and the last Fitbit import. If either is > 24 hours stale AND the env-var credentials are present, it pulls yesterday's data silently in the background. Surfaces a one-line summary in the session-start context if a sync ran; silent otherwise. Handles range queries, so a 7-day absence pulls 7 days of catch-up at the next session.

2. **`hooks/coach-auto-prescribe-on-journal.py`** — Stop hook. After a journal session completes, the hook:
   - Reads `<VAULT_ROOT>/Meta/coach-profile.yaml`. Skips silently if no profile.
   - Checks if today's coach prescription already exists. If not, creates one via the `coach.decide_workout_type` decision tree (same logic as `health_coach_prescribe`).
   - Runs `scripts/backfill-journal-body-context.py` for yesterday only, appending the body-track section below the verbatim journal content.
   - If `profile.calendar_drop: true` and google-workspace MCP is connected, the prescription is available for the next /coach today to drop into Google Calendar at `preferred_workout_clock`.

Both hooks are registered in the `hooks.json` template and get installed into `~/.claude/settings.json` by `/setup-brain` (or `/health-setup` v0.5+, which auto-wires them).

### New `/health doctor` observability skill

Six sections, color-coded green / yellow / red:
1. **Data freshness** — hours/days since last import per vendor + labs
2. **Last prescription + completion** — most recent prescription, completion status, streak, missed days
3. **Auto-trigger hooks installed** — are the two hooks wired in settings.json? Have they fired in the last 48h?
4. **Coach profile** — exists? `calendar_drop: true`? `preferred_workout_clock` set? `days_per_week` reasonable?
5. **Lab status flags** — any marker with status low or high + days since last test + the WHY + suggested re-test cadence
6. **Cycle phase + sleep regularity** — phase, irregularity flag, regularity score, bed/wake stdev

Each yellow / red has a one-line "what to do" — never vague "consider reviewing."

### Bainbridge guardrail

The dissent voice from the v0.2 panel (auto-trigger the analysis, never auto-trigger the action) is the load-bearing constraint for v0.5. The chain prepares the workout, appends the body-track to journals, and writes the calendar event — but `/coach log` stays manual. Logging completion (RPE + lift actuals) is the body-in-the-loop moment that the substrate never automates.

### Failure modes handled

- Wearable API down at session start → hook catches exception, exits silently. Next session retries.
- User skips /journal one day → tomorrow's /journal triggers the chain for two days at once. No missed days.
- User doesn't open Claude Code for a week → next session pulls 7 days of wearable backfill in one shot.
- health-mcp not yet installed → hooks exit silently. Once `/health-setup` runs, hooks start firing.
- Coach profile not yet set → Stop hook exits silently. Once `/coach profile` runs, hooks start firing.
- google-workspace MCP not connected → prescription still creates in DB, calendar drop skipped. Hook never blocks.

### Bypass env vars

- `HEALTH_AUTO_SYNC_BYPASS=1` — skip the SessionStart sync (for offline / debugging)
- `COACH_AUTO_PRESCRIBE_BYPASS=1` — skip the Stop hook prescription
- Both default off; bypass is opt-in

### Tests

81 passing (up from 69 in v0.4). New v0.5 tests cover:
- Both hooks compile cleanly (no syntax errors)
- Bypass env vars short-circuit to silent JSON
- Hooks emit valid JSON even under unexpected failure (Claude Code blocks the prompt on invalid hook output)
- hooks.json template registers both hooks
- /health-doctor SKILL.md enumerates the six sections
- AUTOMATION.md preserves the Bainbridge dissent anchor

### What to do

If you ran `/health-setup` before v0.5: re-run it, or manually paste the two hook entries from `hooks.json` into your `~/.claude/settings.json`. Then restart Claude Code. From that point on, opening any Claude Code session triggers the sync; running `/journal` triggers the prescription.

If you're on a fresh install: `/health-setup` now ends with the auto-wire step. Default yes.

To verify: `/health doctor` — green flags = working, yellow / red = specific fix needed.

---

## 2026-05-10: health-mcp v0.4 — /coach longevity + fitness coach skill (progressive overload + cycle-aware + Floor-paired)

**Who this affects:** anyone who wants a daily workout prescription that reads from their actual biometrics + cycle phase + emotional state (Floor) instead of a generic template. Companion to the /weekly + /monthly insights — coach drives the daily prescription, insights surface the weekly/monthly pattern.

**The shape:** The substrate had the data + the analytics + the journal pairing + the insights review. What it didn't have was a COACH PERSONA that issued a workout. A user on the panel sent the "Claude Fitness Coach" PDF from someone else and asked what we could learn. The panel said: lift the calendar drop + progressive overload + the daily decision shape, but extend it because their version is JUST fitness and our substrate already has the longevity + cycle-aware + Floor-pairing data they're missing. v0.4 is that extension.

### What landed

1. **Coach state layer** (`services/health-mcp/coach.py`): three new DuckDB tables (`coach_prescriptions`, `coach_completions`, `coach_lift_progress`). Tracks every prescription's id + workout type + difficulty + why_today, every completion's RPE + notes + lift actuals, and per-lift progression state (last weight + reps + sets, consecutive full sets, consecutive failures, current top set).

2. **Progressive overload state machine.** Fail-twice-drop-10%, complete-twice-add-2.5kg (upper body) or 5kg (lower body), single-fail holds the weight. Deload every 4th week from profile start (40% volume drop, 20% intensity drop).

3. **Decision tree wired to existing analytics.** The `health_coach_prescribe` tool reads recovery score + sleep score + cycle context + somatic state + body_says_slow_down + days-per-week + equipment + level + started_iso and returns workout_type + intensity_factor + difficulty + deload_week + why_today + prescription_id. Cycle-phase qualifier: luteal HRV dips don't collapse intensity (Sims, panel rule). Sleep score < 35 → rest day. `body_says_slow_down: true` → active recovery (Levine rule).

4. **`/coach` skill** (`skills/coach/SKILL.md`): the user-facing surface. Profile setup (12 questions saved to `<vault>/Meta/coach-profile.yaml`), daily prescription that reads health-mcp + today's journal Floor and renders the workout block, weekly planning that pulls last week's completion + body data, monthly review that bundles the Attia longevity panel (VO2Max + Zone 2 minutes + walking steadiness + lean mass), and `/coach log` for entering actuals after a session.

5. **Floor qualifier** (substrate differentiator). Floor in Shame / Fear / Apathy / Grief / Anger AND `floor_sensitivity: high` in profile → intensity drops ~15%, swap heavy compounds for moderate accessories or mobility. Floor in Joy / Peace / Love / Gratitude → green light. Floor in Courage + good sleep + follicular phase → PR day. The qualifier is multiplicative on top of `intensity_factor`. Never overrides somatic-state slow-down — that's still regulate-first.

6. **Calendar drop integration.** If `profile.calendar_drop: true` AND `google-workspace` MCP is connected, daily prescriptions write to the user's Google Calendar at their preferred workout time. Title pattern: `🏋️ [Type] · [duration]min`. Full workout block in description. Weekly planning writes 7 events at once.

7. **Five new tools**: `health_coach_prescribe`, `health_coach_lift_state`, `health_coach_log_completion`, `health_coach_recent_prescriptions`, `health_coach_summary`. Brings tool total to 41 (32 in v0.2 + 4 in v0.3 + 5 in v0.4).

### What we deliberately did NOT copy from the PDF

- Their iOS-beta Apple Health integration. Our local-only multi-vendor (Apple + Oura + Fitbit) path is strictly better for privacy + cross-platform.
- Their hardcoded sleep tiers (`<4h = no workout, 4-5h = recovery only, 5-6h = drop 40%`). We use `sleep_score` and `sleep_regularity` as continuous signals.
- Their generic cycle prescriptions (`follicular = strength PRs, luteal = lighter weights`). Ours compares THIS cycle's HRV/sleep to baseline per phase via `health_phase_means`, not a generic 28-day template.
- "Nutrition only when asked" — flipped. If the under-fuel detector fires (Braddock pattern from v0.2), the coach surfaces it without being asked.
- Their assumption that the data-driven coach is the whole answer. The Bainbridge dissent voice on the panel was integrated as the body-literacy prompt (`health_journal_body_question`) and the Floor qualifier — the coach acknowledges emotional state, not just biometrics.

### Tests

69 passing (up from 54 in v0.3). New v0.4 tests cover the progressive-overload state machine (first session, complete-twice-add for upper + lower, fail-twice-drop-10%, single-fail-holds), the deload-week computation, the decision tree (body_says_slow_down → active recovery, luteal qualifier bumps intensity, low sleep score → rest, deload week cuts intensity), and the log-completion roundtrip with consecutive-full vs consecutive-failure counters.

### What you might want to do

If you want the coach: run `/coach` to start the profile setup, then `/coach today` for your first prescription. Once your profile is saved, set up the daily scheduled run via `/schedule` so the workout drops into your calendar before you wake up. After the first week, run `/coach week` for the Sunday planning + weekly review.

If you don't want the coach: nothing changes. The skill only fires when you invoke it.

---

## 2026-05-10: health-mcp v0.3 — multi-vendor (Oura + Fitbit) + /health-setup wizard + journal backfill skill + backfill prompt

**Who this affects:** anyone with a wearable that is NOT just Apple Watch (Oura Ring, Fitbit), anyone who wants their existing journals enriched with body context retroactively, anyone setting up health-mcp for the first time on Windows or Linux.

**The shape:** v0.2 covered the full Apple Health surface but assumed the user had an iPhone. v0.3 closes three gaps: (1) Oura Ring + Fitbit ingestion via vendor APIs sharing the same DuckDB schema and the same scoring formulas, (2) an interactive /health-setup wizard that branches by vendor + OS so the user only sees install steps that apply to them, and (3) a /backfill-journal-body-context skill plus a self-contained backfill prompt for running a one-shot pass over every journal entry this year.

### What landed

1. **Oura Ring support** (`services/health-mcp/oura_client.py` + `health_import_oura` tool). Personal Access Token only — no OAuth flow. Generate one at https://cloud.ouraring.com/personal-access-tokens (free), export `OURA_PERSONAL_ACCESS_TOKEN`, run `health_import_oura(start, end)`. Oura's daily sleep score + sleep sessions (stages) + readiness + activity + workouts get normalized to the same DuckDB rows Apple Health uses. HRV / RHR / steps / active kcal map to the same HKQuantityType ids so recovery score and every vault-aware tool work without modification.

2. **Fitbit support** (`services/health-mcp/fitbit_client.py` + `health_import_fitbit` tool). OAuth2 via a Personal app registered at https://dev.fitbit.com/apps. The client auto-refreshes access tokens if FITBIT_REFRESH_TOKEN + FITBIT_CLIENT_ID + FITBIT_CLIENT_SECRET are also set. Fitbit HRV requires Premium; without Premium you get steps + sleep + RHR + weight, no HRV. Sleep stages are 30-second epoch granularity from Fitbit's v1.2 sleep endpoint.

3. **Multi-vendor schema sharing.** Every vendor writes to the same `records` / `workouts` / `sleep` / `cycle` / `symptoms` tables. The recovery_score formula uses whichever metric has data for a given day. If you have both an Apple Watch and an Oura Ring, both populate; v0.3 takes the last-writer-wins approach for simultaneous same-day same-metric writes. v0.4 will add per-source priority preferences.

4. **`/health-setup` wizard** (`skills/health-setup/SKILL.md`). Branches by which wearable(s) the user has AND which OS they're on. Calls `health_vendor_setup_guide(vendor, os_kind)` which returns OS-specific shell commands the user pastes into Terminal / PowerShell / bash. Then `health_vendor_healthcheck(vendor)` verifies the token works before running the first import. Branches cleanly for `apple_health`, `oura`, `fitbit`, `garmin` (routes via Apple Health on iPhone), and `whoop` (deferred to v0.4 — open-wearables is the substitute today).

5. **`/backfill-journal-body-context` skill + script.** Walks every daily journal entry in a date range (default this year) and appends a "Body track" section BELOW the original verbatim content. The original journal text is NEVER modified — the journal-verbatim rule is non-negotiable. Pulls HRV / RHR / sleep / steps / workouts / cycle phase / recovery score / sleep score / out-of-range lab markers + a Floor-paired interpretation. Default model: **Python template (zero cost, deterministic).** Optional `--llm-model minimax` for richer prose at ~$0.06/M tokens. Idempotent — re-runs skip entries that already have the marker.

6. **`docs/BACKFILL_PROMPT.md`.** A self-contained prompt for running the backfill in a fresh Claude Code session. Copy-paste into a new chat and the model walks through dry-run → real-run → ongoing-cadence scheduled task → /weekly verification end-to-end. The prompt explicitly de-prioritizes Sonnet / Opus for the per-entry interpretation — Python template is the default, MiniMax opt-in.

### Tools count

v0.2 was 32 tools. v0.3 adds 4 more: `health_import_oura`, `health_import_fitbit`, `health_vendor_setup_guide`, `health_vendor_healthcheck`. Total: 36 tools.

### Tests

54 passing (up from 41 in v0.2). New v0.3 tests cover vendor-client token validation (raises when env var missing), SHA determinism for idempotent re-imports, and vendor setup guide returning the right shape per vendor + OS (with alias normalization for `apple` / `apple_watch` / `iphone` / `ios` → `apple_health`).

### What you might want to do

If you have an Oura Ring or Fitbit, run `/health-setup` for the first-time install flow. The wizard asks you which wearable(s) you have and which OS, then walks you through the per-vendor token setup and first import.

If you have a year (or more) of daily journals and want them paired with your body data retroactively, follow the prompt at `docs/BACKFILL_PROMPT.md` in a fresh Claude Code session. It runs the script with Python templates (zero LLM cost) and optionally with MiniMax for richer per-entry prose.

After backfill, run `/weekly` to see the body track populated alongside your Floor tags. Then run `/patterns` to see how Floor patterns correlate with HRV / sleep / cycle phase over the period.

---

## 2026-05-10: health-mcp v0.2 — full HealthKit surface + cycle awareness + lab import + voice bridge

**Who this affects:** anyone who installed health-mcp at v0.1 (the Apple Health connector that paired biometrics with daily-journal / coaching / advisory-panel / patterns / insights). Or anyone who hasn't installed it yet but uses the body-aware skills.

**The shape:** v0.1 covered the obvious 15-tool surface — XML / CSV / TCP ingestion, recovery / sleep / strain scores, journal / coaching / panel / insights context. The advisory-panel pass on 2026-05-10 (every Health & Body voice plus Jackie Kennedy) flagged the gaps: cycle phase entirely missing, only ~20 quantity types covered, no lab import, no symptoms, no ECG, no iOS 17 State of Mind, voice register breaking when biometric data lands inside a journaling skill. v0.2 closes all of it.

### What landed

1. **Full HealthKit surface coverage.** The type registry now indexes 108 quantity types (every Apple Health metric — VO2Max, walking steadiness, sleeping wrist temperature, all 40 dietary types, blood oxygen, AFib burden, ECG, etc.), 47 symptom + cardio-event + sensory-event types (headache, bloating, fatigue, hot flashes, lower back pain, pelvic pain, irregular heart rhythm event, etc.), 14 cycle / reproductive types (menstrual flow, cervical mucus, ovulation tests, pregnancy, contraceptive, lactation, sexual activity), ECG records with classification, and iOS 17+ State of Mind mood logs. New tables: `cycle`, `symptoms`, `ecg`, `state_of_mind`, `labs`.

2. **Cycle phase awareness (Sims + Briden, panel 2026-05-10).** Three new tools — `health_cycle_context`, `health_phase_tagged_metric`, `health_phase_means` — read menstrual flow records and surface current phase + cycle day + length variance + irregularity flag. A low-HRV day in mid-luteal is normal physiology, not a recovery deficit; the substrate now contextualizes biometrics by cycle phase instead of gaslighting half its users.

3. **Lab CSV import (Boham, panel 2026-05-10).** New tool `health_import_labs` accepts LabCorp / Quest / Function Health / generic CSV exports. Auto-detects format. Plus `health_recommended_labs()` returns the 16-marker substrate reference panel (ApoB, fasting insulin, hs-CRP, full thyroid, sex hormones, vitamin D, etc.) with the WHY for each marker. Apple Health captures the visible 20% of health; the chemistry that drives chronic disease is invisible to it. The labs change the prescription.

4. **Voice bridge (chairman synthesis, panel 2026-05-10).** `health_journal_context` now takes a `voice_profile` arg (`clinical` / `warm` / `curious`). The same biometric data renders as "HRV 28ms (-33% vs 30-day baseline)" or "you slept 5h 12m short night; HRV ran noticeably below your usual" or "Body, last 24h: ... Anything you want to notice about how that maps to what happened yesterday?". The host skill picks the register so the journaling voice is preserved.

5. **Body literacy prompts (Bainbridge, panel 2026-05-10, dissent voice integrated).** `health_journal_body_question` returns NOT a number but a context-aware embodiment question. "Your body had a hard night. What did it want from you today that you didn't give it?" The substrate is for inhabiting a body, not surveilling one.

6. **Sleep regularity (Winter, panel 2026-05-10).** New `health_sleep_regularity` returns regularity score 0-100 derived from bed-time + wake-time + duration variance, plus mean sleep latency and nap detection. Chronic sleep debt is the predictor; one-night sleep score isn't the whole signal.

7. **Longevity panel (Attia + Patrick, panel 2026-05-10).** `health_longevity_panel` bundles VO2Max, walking speed, walking steadiness, lean body mass, body fat %, Zone 2 minutes, and 6-minute walk distance into one call. The most-predictive longevity markers Apple Watch already records.

8. **Somatic pre-check for coaching (Levine, panel 2026-05-10).** `health_somatic_state` returns recent HR/HRV volatility plus a `body_says_slow_down` boolean. The coaching skill should call this BEFORE emotional inquiry. Sympathetic activation makes reframe work counterproductive; the body asks for regulation first.

9. **Nutrition under-fuel detector (Braddock, panel 2026-05-10).** `health_nutrition_summary` aggregates dietary records and flags days where consumed kcal < 70% of (basal + active). Under-fueled days show up as low HRV, and the recovery score will tell you to "rest" when the actual prescription is "eat enough." Catches the asymmetry.

10. **Long-window mode (van der Kolk, panel 2026-05-10).** `health_long_window` and `health_long_window_with_journal` compare same-month-this-year vs same-month-last-year and surface persistent asymmetries (4+ months on the same side of YoY delta). Trauma signatures and seasonal Floor-body coupling don't show up in 30-day windows.

11. **Symptom-vs-Floor correlation (Pagliano, panel 2026-05-10).** `health_symptom_correlation` correlates symptom occurrences with Floor tags. Surfaces "headache co-occurs with Floor 4 (Fear) at 58%, vs 12% baseline" — useful for pelvic / migraine / GI patterns that may be Floor-linked.

12. **Audio exposure tool.** `health_audio_exposure` returns hours over the safe-listening threshold (default 80 dB) for environmental + headphone audio. WHO recommends < 1hr/day at >85 dB; cumulative hearing damage is dose-dependent and irreversible.

13. **Privacy-first README (Jackie Kennedy, panel 2026-05-10, public-facing review).** Privacy commitment now opens both README and SETUP. "Local-only. Your health data never leaves your machine. Every score is directional, not diagnostic." appears above the fold. Cost-status of each ingestion path is labeled explicitly.

### Tools count

v0.1 was 15 tools across 5 categories. v0.2 ships 32 tools across 8 categories: ingestion (5), query (3), analytics (5), surface (7 — longevity / sleep regularity / somatic state / nutrition / long window / audio exposure / lab panel), cycle (3), symptoms + ECG + state-of-mind (3), vault-aware (8 — including voice-bridge and body literacy), live (1). Plus `health_recommended_labs` for the panel reference list.

### Tests

41 passing (up from 18 in v0.1). New v0.2 fixture covers cycle records, symptoms, ECG, State of Mind, longevity quantity types, and dietary records. Lab CSV fixture exercises the LabCorp / Quest / Function / generic auto-detection.

### What you might want to do

If you installed v0.1, `git pull` and re-import your Apple Health export.zip with `health_import_xml(path, force=True)` to populate the new cycle, symptoms, ECG, and State of Mind tables. Existing scores stay valid.

If you're on a clean install, follow [services/health-mcp/SETUP.md](../services/health-mcp/SETUP.md) for the three-mode ingestion flow.

If you take periodic labs, export from your patient portal and try `health_import_labs(path, lab_format='auto')`.

---

## 2026-05-09: v1.3.1 vertical-healthcare actually-complete + CI privacy scanner fix

**Who this affects:** anyone who installed v1.3.0 expecting `/vertical-healthcare init` to register and stage drafts.

**The shape:** v1.3.0 advertised "vertical-healthcare completion" and committed the four files that release described as missing (retention, both decision-audit patterns, the third connector). But the original five files from the v1.2.0 partial scaffold were never staged either. Without README and SKILL, skill discovery did not register the pack at all, so the v1.3.0 promise was vacuous on disk. v1.3.1 lands the five missing files so the pack is functionally installable.

### Two changes

1. **`skills/vertical-healthcare/` is now installable.** Five files added: README, SKILL, the Epic and Cerner FHIR connectors, and `schema/typed-memory-categories.md`. Combined with the four files already on main since v1.3.0 (retention defaults, PHI-handling firewall, clinical-decision evidence chain, Salesforce Health Cloud connector), the pack covers all four substrate extension surfaces (typed-memory schema, retention defaults, connector configs, decision-audit patterns).
2. **CI privacy scanner now scans only added lines, not whole files.** The `private-context tokens (PR-scoped)` check was selecting changed FILES correctly but then greping each file's full content. Pre-existing committed text (maintainer-attribution in plugin.json's author block, older CHANGELOG entries about features intentionally shipped public) failed every PR that touched those files. Fix: grep only the lines this PR adds (diff lines starting with `+`, excluding the `+++ b/path` file header). Net-new private-context additions still fail; legacy text is now correctly ignored.

### What you might want to do

If you tried `/vertical-healthcare init` on v1.3.0 and got nothing, run `git pull` to v1.3.1 and try again. If you are on a clean install or are not in the healthcare vertical, no action needed.

---

## 2026-05-08: Scheduled-task naming convention + `/diagnose` lint check

**Who this affects:** anyone who has scheduled tasks under `~/.claude/scheduled-tasks/`.

**The shape:** Claude Code's slash autocomplete registers `~/.claude/scheduled-tasks/<name>/SKILL.md` entries the same way it registers regular skills. A user typing `/journal` sees both the conversational journal skill AND any `daily-journal` cron task in the autocomplete menu, which is confusing because cron tasks have no manual-invocation use case. Until upstream supports a `cron_only: true` frontmatter flag (tracked in [anthropics/claude-code#57508](https://github.com/anthropics/claude-code/issues/57508)), the convention is to prefix scheduled-task names with `_`. They sort to the bottom of autocomplete and read as cron-only at a glance.

### Two changes

1. **`docs/MAINTENANCE.md` documents the convention.** New "Naming convention" subsection in Scheduled Tasks shows the bad/good rename pattern (`daily-journal` -> `_daily-journal-cron`).
2. **`/diagnose` now warns on violations.** New section 10b in `scripts/diagnose.sh` and `scripts/diagnose.ps1` scans `~/.claude/scheduled-tasks/` for two problems: (a) names that collide with installed skills, (b) names that lack the `_` prefix. Either one emits a yellow WARN with a remediation hint.

### What you might want to do

If you have existing scheduled tasks, run `/diagnose` and rename any flagged ones. Two-step rename: change the directory name, then update the `name:` field in the task's `SKILL.md` frontmatter to match.

---

## 2026-05-08 — `/journal` reads a config file for what to pull in (opt-in cross-platform)

**Who this affects:** anyone who runs `/journal`. Default behavior changes for first-run users: the skill now creates a `Meta/journal-config.md` file in the vault on first invocation, and asks once whether to opt in to cross-platform data sources.

**The shape:** `/journal` Step 0 used to pull RescueTime + Session Captures only. That left two gaps. (1) Captures.md only fires at session-close, so warm/unclosed Claude sessions left their content invisible to the journal. (2) Most relational events happen in iMessage / WhatsApp / on the calendar, not inside a Claude session. The journal was missing half the day. The fix: extend Step 0 to pull from six sources, three on by default (own data) and three opt-in (private conversations).

### What runs by default (own data, on)

- **RescueTime** (productivity numbers from your account).
- **Session Captures** (quotes from your own Claude sessions).
- **Today's activity** (git commits, modified files, session files in your own vault).

These three are safe by default because they only read your own vault and your own RescueTime account.

### What's opt-in (private conversations, off)

- **iMessage 24h** (threads from your phone with traffic in last 24h).
- **WhatsApp 24h** (threads from WhatsApp with traffic in last 24h).
- **Calendar** (today's events from Google Workspace).

These three are off by default because they see private conversations and meetings. The skill creates `Meta/journal-config.md` (template at `templates/journal-config.md`) on first run with all three off, and asks once whether to flip any on. The user stays in control by editing the file directly. The skill never re-prompts after the first run.

### Why the change

Codified after a journal session where the user's most emotionally important content of the day (a fight with a sibling, a dinner with a parent, a proposal drafted for a friend) was nowhere in the day's Claude sessions because those events happened on the phone. The journal asked "what's on your mind?" with no context, and the user had to manually re-narrate the day. The new pulls let the journal see the day, but only with the user's explicit consent per data source.

### How to turn things on

Edit `Meta/journal-config.md` (or `⚙️ Meta/journal-config.md` if your vault uses emoji-prefixed Meta). Change `off` to `on` for any data source. Save. Next `/journal` picks it up.

### Filters

`imessage_filters.exclude_chats` and `whatsapp_filters.exclude_chats` accept a list of phone numbers, emails, or contact names to skip on every pull. Useful for work-only threads or anything you want kept out of the journal context.

### Files added or changed

- `skills/daily-journal/SKILL.md` — Step 0 now config-gated (0-pre + 0a-0f), entry format adds `## Today` summary section.
- `templates/journal-config.md` — new template with safe defaults + per-source filter docs.

---

## 2026-05-08 — new `/coaching` skill: multi-pass panel sessions with accountability tracking

**Who this affects:** anyone who has hit a moment too big for a daily journal entry. A hard conversation with a co-worker, a decision they're second-guessing, accumulated friction with a person, anything that needs panel feedback over multiple iterations and tracking over weeks or months.

**The shape:** The advisory panel inside `/journal` is one-shot per day. Real coaching, real therapy, real advisor relationships are not one-shot. They're multi-turn, they update when new evidence comes in, and they track whether the same blind spot keeps surfacing across months. The vault had all the pieces (panel rules, daily journals, decision logs) but no skill that orchestrated the multi-pass arc and filed it for tracking. `/coaching` fills that gap.

### What `/coaching` does

A three-tier output architecture for any hard moment worth tracking:

1. **Verbatim raw** at `📋 Strategy/Coaching Sessions/Processing Notes - YYYY-MM-DD - <topic>.md`. The user's exact words, no annotation. Per the save-exact-words rule. Available for re-read forever, especially useful for users planning to write memoir or book material later.
2. **Synthesized accountability record** at `🏠 Home/Coaching Sessions/YYYY-MM-DD - <topic>.md`. What surfaced, commitments named, re-eval date one month out. The file `/weekly` and `/monthly` look at to ask whether the pattern repeated and whether commitments landed.
3. **Rolling pattern aggregator** at `🏠 Home/Panel Feedback Log.md`. Patterns table at the top tracks mention counts. Single mention is watch. 2+ mentions across different contexts promote to acute action item.

### The corrections-update-takes loop

The move that distinguishes coaching from journal. When the user pushes back on a panel take with new info, the next pass UPDATES prior reads transparently. Patterns get demoted explicitly when corrections invalidate them. A class-register read can become wrong once autobiographical context surfaces. A piece of advice can be retracted when the user names why it does not apply. The system shows its work.

### Failure modes the skill explicitly avoids

- **Yes-machine panels.** At least one voice MUST dissent. The dissent is the value.
- **Synthesis without verbatim.** Always file verbatim raw FIRST. Strip the user's voice from the archive and the system breaks.
- **Inflating mention counts.** One coaching session is one mention per pattern, even if multiple panelists agreed in that session. Mentions cumulate across DIFFERENT sessions.
- **Skipping the corrections loop.** Roleplaying a panel without updating takes when the user pushes back is theater, not coaching.
- **Forgetting re-eval.** A Coaching Session without a `re_eval_date` is just a fancy journal entry. The accountability comes from the calendar return.

### Files added

- `skills/coaching/SKILL.md` — the skill itself
- `templates/coaching-sessions/EXAMPLE - 2025-01-15 - Hard Conversation With Co-worker.md` — generic example using a hypothetical co-worker scenario
- `templates/Panel Feedback Log.md` — starter template for the rolling aggregator
- README skill table updated to include `/coaching`

### Integration with existing skills

- `/journal` keeps its one-shot daily check-in with inline panel reactions
- `/weekly` and `/monthly` are the natural surface for re-eval. They should read open Coaching Sessions and surface ones whose `re_eval_date` has passed
- `/patterns` reads Panel Feedback Log Patterns table to confirm 2+ mentions across contexts
- `/deconstruct` is auto-offered when a coaching session surfaces a `stakes: high` decision

### Manifest version

No bump. Additive new skill plus templates, no changes to existing skill behavior.

---

## 2026-05-08 — minimum Claude Code version bumped to 2.1.133

**Who this affects:** anyone using the quick-try `--plugin-url` install path or running the full bootstrap on a fresh machine.

**The shape:** Claude Code 2.1.133 (released 2026-05-08) fixes a silent bug where subagents were not discovering project, user, or plugin skills via the Skill tool. AI Brain Starter is skill-heavy: graphify, second-brain-mapping, weekly insights, and several quarterly maintenance flows compose skill calls from agent context. On 2.1.129–2.1.132, those agents may have been failing to find the skills they needed without surfacing an error. Bumping the floor to 2.1.133 closes that gap.

### What changed

- **README quick-try section** — minimum bumped from "2.1.129+" to "**2.1.133+**". The bootstrap path is unaffected (the bootstrap installs/upgrades Claude Code itself), but operators trying the plugin directly against an existing install need the floor.
- **Two genuine 2.1.133 wins for this substrate:**
  - **Subagent skill discovery fix.** Skill-heavy flows now correctly resolve project/user/plugin skills inside subagents. No code change in this repo, just the Claude Code upgrade.
  - **Parallel session 401 race fix.** Pre-2.1.133, a refresh-token race could log out all concurrent sessions when running multiple worktrees in parallel. Heavy-vault users with multiple worktrees benefit immediately.
- **`worktree.baseRef` setting** — Claude Code 2.1.133 reverted the `EnterWorktree` default back to `fresh` (branches from `origin/<default>`); 2.1.128–2.1.132 default was `head` (preserves unpushed commits). If your workflow relies on entering worktrees with WIP intact, set `"worktree": {"baseRef": "head"}` in `~/.claude/settings.json`. For local-only repos with no remote, `"head"` is the only path that works at all. The bootstrap will not auto-set this — it's a per-user judgment call.
- **`$CLAUDE_EFFORT` env var now reaches hooks and Bash subprocesses.** Skills that want effort-aware behavior can finally read it. Examples already in the substrate (advisory-panel rule 15, life-history-prose effort-aware depth) come online automatically when a calling skill exposes the env var.

### Manifest version

No bump — README + docs only.

---

## 2026-05-06 — vertical-healthcare completion + recommended-skill-overrides doc

**Who this affects:** covered entities, business associates, and health systems onboarding to the substrate; plus all installs that want a sensible starter `skillOverrides` configuration.

**The shape:** v1.2.0 shipped finance + legal packs but kept healthcare untracked because its retention/, decision-audit/, and a third connector were missing relative to what its SKILL.md description promised. Authoring those four files closes the gap; the pack is now production-grade. Plus a portable doc that explains which Claude Code 2.1.129+ skills to set to `off` for a sharper auto-routing loadout.

### What shipped

- **`skills/vertical-healthcare/retention/defaults.md`** — HIPAA 6-year baseline per 45 CFR 164.530(j), per-state add-ons (California / Texas / New York / Florida / Massachusetts), special-case modifiers (decedent records 50 years per 164.502(f), minor patients per state baseline, 42 CFR Part 2 separate handling, research-consent records, psychotherapy notes 7-year floor).
- **`skills/vertical-healthcare/decision-audit/phi-handling.md`** — firewall against the 18 HIPAA identifiers per 164.514(b)(2). Detection sweep at write time, coverage check, tenant-boundary check, sensitive-subset stamp, minimum-necessary review per 164.502(b). Access-time logging captures role, purpose, encounter, disposition, auth basis. Cross-boundary moves restricted to BAA-stamped channels, written authorizations, required-by-law disclosures, or de-identified output.
- **`skills/vertical-healthcare/decision-audit/clinical-decision-trail.md`** — chain of input data → decision → decision-maker → supporting evidence → alternatives considered → shared-decision-making flag. Credentialing check, encounter validity, time consistency, content-addressable stamp. Reviewer chain (peer review, attending, medical director, quality) for institution-flagged decisions. Provenance: 45 CFR 164.526, 164.530(j), CMS 42 CFR 482.24(c), Joint Commission RC.02.01.01.
- **`skills/vertical-healthcare/connectors/salesforce-health-cloud.md`** — Health Cloud connector (Person Accounts, EhrEncounter, EhrCondition, EhrMedicationStatement, CarePlan, CareRequest, ContentDocument). OAuth JWT bearer + web server flows. SObject sharing-rule + field-level-security honored, never bypassed. Sync cadences (nightly to real-time via Streaming API), rate-limit handling, cert rotation.
- **`docs/RECOMMENDED_SKILL_OVERRIDES.md`** — a portable starter recipe for `skillOverrides` in `~/.claude/settings.json`. Walks through what to set `off` (skill collections that won't fire for most users), `user-invocable-only` (rare manual use), `name-only` (tokens-saving), with the reasoning per category.

### Manifest version

Bumped 1.2.0 → 1.3.0.

---

## 2026-05-06 — Vertical packs (finance, legal) + maintainer release docs

**Who this affects:** consulting clients and operators in regulated verticals (CFOs, finance ops, internal audit, in-house legal, legal ops, law firms) who want the substrate to come pre-shaped to their compliance and audit obligations rather than starting from a blank vault. Plus maintainers cutting future releases.

**The shape:** two production-grade vertical skill packs were sitting untracked in `skills/`, ready to ship. Both have full coverage of the substrate's four extension surfaces (typed-memory schema, retention defaults, connector configs, decision-audit patterns). Healthcare is partial (schema + connectors only, retention + decision-audit pending) and stays untracked until complete.

### What shipped

- **`skills/vertical-finance/`** — Pre-configured finance vertical pack. Typed-memory categories for deals, counterparties, SOX 404 controls, audit evidence. Retention defaults aligned with SOX, SEC 17a-4, per-jurisdiction variations. Connectors for Workday, NetSuite, SAP Finance. Decision-audit patterns for SOX evidence stamping (`decision-audit/sox-404-evidence.md`) and board-pack version trails (`decision-audit/board-pack-trail.md`). 9 files, 37.8 KB.
- **`skills/vertical-legal/`** — Pre-configured legal vertical pack. Typed-memory for matter management and privilege handling. Retention defaults aligned with ABA Model Rule 1.15 + state-bar variations. Connectors for Clio, NetDocuments, iManage. Decision-audit patterns for conflicts checks (`decision-audit/conflicts-check.md`) and privilege handling (`decision-audit/privilege-handling.md`). 9 files, 40.6 KB.

Both packs use the `/vertical-finance` / `/vertical-legal` triggers with `init | status | rebuild` subcommands. They're additive — installing them doesn't change the substrate's behavior for users in other verticals. Use when onboarding a CFO organization or law firm that needs the substrate to come pre-shaped to their work.

### Also shipped this release

- **`docs/RELEASE_PROCESS.md`** — maintainer-facing reference for cutting a release. Covers the cut-a-release procedure, what `release.yml` does, the artifact list, the stable URL pattern, the PR-scoped privacy gate, re-running a release via `workflow_dispatch`, and the manual smoke-test path.
- **`docs/RELEASES.md`** — 2026-05-06 user-facing entry describing the `--plugin-url` quick-try path that landed in v1.1.0, alongside the email-gated full bootstrap.

### Not shipped (intentional)

- **`skills/vertical-healthcare/`** is partial (5 files, 22.2 KB; missing the `retention/` and `decision-audit/` directories that the SKILL.md description promises). Stays untracked until `retention/` and `decision-audit/` directories are populated. Filed as `⚙️ Meta/Claude To-dos.md` follow-up.

---

## 2026-05-06 — Release workflow + `--plugin-url` quick-try path

**Who this affects:** anyone who wants to try ai-brain-starter skills against an existing vault without running the full bootstrap, plus maintainers who want a tagged-release distribution path alongside the email-gated install funnel.

**The shape:** Claude Code 2.1.129 added a `--plugin-url <url>` flag that fetches a plugin .zip archive from a URL for the current session. The repo was already a valid Claude Code plugin (`.claude-plugin/plugin.json` + `marketplace.json`), but had no published release artifact for `--plugin-url` to point at. This drop adds the release pipeline and a one-line quick-try path in the README, alongside the existing email-gated full install.

### What shipped

- **`.github/workflows/release.yml` — tag-triggered release builder.** Fires on `v*` tag push (or manual `workflow_dispatch` with a tag input). Validates `.claude-plugin/plugin.json` + `marketplace.json` parse as JSON, builds a clean `ai-brain-starter.zip` + `ai-brain-starter.tar.gz` (excluding `.git`, `.github`, secrets, build artifacts, local settings), generates SHA256 sums, and creates the GitHub release with auto-generated release notes. The release URL is stable: `https://github.com/mycelium-hq/ai-brain-starter/releases/latest/download/ai-brain-starter.zip`.
- **`.github/workflows/lint.yml` — new `privacy` job, PR-diff-scoped.** Mirrors the maintainer's write-time hookify guard so external contributors cannot introduce tokens the repo treats as private context. Scoped to files changed in the PR (pre-existing committed content already passed hookify on prior writes). Pushes to main skip this job — the maintainer's local hookify catches direct pushes. Pattern matches `hookify.no-personal-in-starter.local.md` exactly.
- **README quick-try section.** A new "Quick-try (existing Claude Code users)" subsection inside the install block documents the `--plugin-url` invocation. Frames the path as session-scoped evaluation, not a replacement for the full bootstrap (which still sets up the Obsidian vault + hooks + resolver). Existing email-gated install remains the primary path.

### How to release

Tag a version on `main` and push the tag:

```
git tag v1.0.0
git push origin v1.0.0
```

The workflow runs in a few seconds, scrubs the diff, builds both archives, and publishes the release. Re-runs on the same tag via `workflow_dispatch` will overwrite the assets (`--clobber`) without recreating the release.

---

## 2026-05-02 — Synthesizer LLM mode + closed-loop daemon + eval framework

**Who this affects:** anyone running the `synth-pr-to-sop` / `synth-thread-to-sop` skills, anyone who wanted lower-latency feedback from `Meta/Learnings/` capture into procedural memory, and maintainers who want a regression-risk score on synthesizer changes.

**The shape:** the deterministic synthesizers and the hourly closed-loop cron worked, but had three soft edges. (1) Heuristic extraction missed nuance the operator could fix in-session; an opt-in LLM refinement would cover the gap when the operator is willing to spend tokens. (2) The hourly promote cron has up-to-1-hour latency; an operator tuning the loop in real time wanted seconds. (3) Refactors to the synthesizer regex paths had no objective measurement of whether quality went up or down. Today's drop ships an opt-in LLM mode for both synthesizers, a real-time daemon alongside the cron, and a golden-pair eval framework.

### What shipped

- **`skills/_shared/llm_synth.py` — optional Anthropic-API refinement.** A thin wrapper around `claude-haiku-4-5-20251001` with prompt caching on the system block (TTL 1h, since the extraction template repeats across runs). Returns `(parsed_json, error)` tuples; never raises. Both synthesizers now accept `--use-llm`; when set, the LLM refines title, steps, summary (and rationale + dissent + parent_rule for thread classifications). The heuristic still owns the idempotency key (`sha8` from PR ID or thread root_ts), so re-running with `--use-llm` on the same source overwrites the same file. Output frontmatter records `synthesis_mode: llm-refined` or `synthesis_mode: heuristic`. Default is off; `pip install anthropic` + `ANTHROPIC_API_KEY` are only required when the flag is set. Missing dep or missing key falls through to heuristic-only with a one-line stderr warning, never a hard crash.
- **`scripts/closed-loop-daemon.py` — real-time alternative to the hourly promote cron.** Watches `<vault>/⚙️ Meta/Learnings/` via `watchdog` (inotify on Linux, FSEvents on macOS) when installed, otherwise falls back to a 30-second stat-poll loop. On every new `.md` learning capture it runs `promote-episodic-to-procedural.py --quiet`. Pidfile-protected (single instance), signal-handled (SIGTERM/SIGINT/SIGHUP). New `templates/launchd/com.abs.closed-loop-daemon.plist.template` + `scripts/install-closed-loop-daemon.sh` give a one-shot macOS launchd install path. Linux operators can write a systemd user unit pointing at the same script. The hourly cron stays as a belt-and-suspenders sweep.
- **`scripts/eval-synthesizers.py` + `tests/eval/fixtures/*` — synthesizer regression-risk score.** Five golden-pair fixtures (PR-merge workflow, thread decision, thread exception, PR step-ordering, thread workflow). Each fixture has `input.md` + `expected.json`; the script runs the deterministic synthesizer in operator-driven mode (no LLM cost) and scores the produced typed-memory file 0-100 (60 pts frontmatter completeness + value match, 25 pts body keyword overlap, 15 pts step count + step ordering hint match). Baseline on the shipped fixtures: average 97.8 / 100. `--fail-below N` flag exits non-zero if any fixture scores below N, so CI can guard against quality regressions on synthesizer refactors.
- **`tests/integration/test_synth_llm_mode.py` — 5 unit tests.** Monkeypatches the Anthropic client (no real API calls) to verify (1) the system prompt has `cache_control` with TTL=1h, (2) LLM-refined fields override heuristic fields when valid JSON returns, (3) the `synthesis_mode` marker writes correctly in both modes, (4) missing-dep and missing-key paths return clean errors, (5) unknown memory-type is rejected.
- **Documentation.** `templates/AUTONOMOUS-SYNTHESIS-README.md` gets two new sections: "Optional LLM mode (`--use-llm`)" and "Eval framework (`scripts/eval-synthesizers.py`)". `templates/CLOSED-LOOP-README.md` gets a "Daemon mode" section with macOS launchd install + Linux systemd guidance + when-to-use guidance (default cron, switch to daemon for real-time tuning, run both for belt-and-suspenders).

### Why this is the right fix

The three improvements target three different primitives in the catalect architecture without touching the heuristic core. The LLM mode is opt-in by design — default users keep the stdlib-only install footprint and zero per-run cost. The daemon is alongside, not replacing, the cron — operators who want simple zero-config keep what they have. The eval framework lives at `tests/eval/` so it runs cleanly without an Anthropic key, which means CI can use it on every PR.

The integration test (11 of 11 passing, unchanged) is preserved and exercises the same end-to-end pipeline. The new LLM-mode test adds 5 more checks. Memory-runtime-pro (the private SaaS-ready runtime) added a parallel webhook retry harness in the same session.

### Personal-data scrub

Every new file in this drop passed a word-boundary regex scrub for personal tokens before commit. The eval fixtures use generic placeholder names ("alice", "bob", "carol", "dana", "erik", "manager", "engineer") and synthetic PR/thread URLs at example.com / generic Slack workspaces.

### What's preserved

All existing skills, hooks, schemas, scripts, the catalect primitives, the connector pattern, the session-close cascade, the bootstrap. No renames, no removals. The 11/11 integration test still passes. Default invocation of either synthesizer behaves exactly as before; `--use-llm` is the only new flag.

---

## 2026-05-01 — Drift-ignore + 5 universal scripts/rules/skills propagated upstream

**Who this affects:** maintainers running the monthly `vault-repo-drift-check.sh` against a personal vault, plus anyone who wanted the new universal artifacts (timezone calendar rule, handoff lifecycle, graphify coverage audit, CRM collision check, stub audit, Slack ingest connector, Remotion-React video best-practices skill).

**The shape:** the drift check used to flag every personal rule, script, skill, and hook on every run, drowning genuinely-ambiguous candidates in 40+ items of personal noise. Now there's a `.driftignore` mechanism so each maintainer keeps a private list of "I've decided this stays local" patterns without seeing them re-flagged each month. And five long-lived universal artifacts that had been living only in the personal vault are now upstream.

### What shipped

- **`.driftignore` mechanism.** New `.driftignore.example` template at repo root. Each clone copies it to `.driftignore` (gitignored locally) and adds personal patterns. The drift check loads them as substring matches and suppresses any drift line whose path contains a pattern. `scripts/vault-repo-drift-check.sh` updated to read the file and prefix every drift line with a stable relative-path key (`rules/X.md`, `scripts/X.py`, `skills/X`, `hooks/X.sh`, `obsidian-plugin:X`) that pattern matching can target.
- **`templates/rules/calendar.md` — explicit-timezone rule for Google Calendar MCP calls.** Naive datetimes in `cal_create_event` / `cal_update_event` get reinterpreted as UTC and the event lands at the wrong hour. Hook + rule require an explicit `±HH:MM` offset on every start/end, plus a list-after-write verification step. Codified after a real-world case where a 10 AM event landed five hours off.
- **`templates/rules/handoff-files.md` — handoff lifecycle rule.** Cross-session handoff files accumulate at the top of `Meta/` if nobody deletes them after the bridged work ships. New rule defines: identification (frontmatter `type:` or filename pattern), location convention (`Meta/Handoffs/` active, `Meta/Handoffs/Archive/` consumed), required `consumes_when:` frontmatter (a concrete completion signal, hook-enforced at write time), and a four-bucket close-time scan that classifies each handoff as archive / keep / audit / leave-alone. Generalizes to any `consumes_when:` artifact (PRD drafts, journal seeds, contribution drafts).
- **`scripts/graphify_coverage_audit.py` — single source of truth for "what has and hasn't been graphified."** Unions the manifest, cache, and graph stores; classifies every eligible `.md` file as current / stale / moved / missing; handles flat staging paths, absolute and relative source_file fields, vault reorgs, and both root and meta layouts (`<vault>/graphify-out/` or `<vault>/⚙️ Meta/graphify-out/`). Configurable `SKIP_PARTS` via `--skip` flag or `VAULT_SKIP_PARTS` env var. Outputs `COVERAGE_REPORT.md` (human) and `COVERAGE_REPORT.json` (machine).
- **`scripts/crm-collision-check.py` — CRM dedupe pre-check.** Run before creating a new CRM card; warns if the candidate name is an alias of an existing card or a single-word prefix of a fuller name (e.g., "Alex" colliding with "Alex Rivera"). Three exit codes (safe / collision / error). Configurable via `VAULT_ROOT` and `CRM_DIR` env vars.
- **`scripts/stub_audit.py` — bucketed signal-density audit.** Six buckets from "empty / URL-only" through "substantive," with per-folder counts and 10-sample previews. Does NOT delete; surfaces distribution so the user picks a threshold. Configurable `SKIP_PARTS`.
- **`skills/ingest-slack/`.** Pulls recent messages from a Slack channel into the vault as queryable markdown. Writes one file per channel per day to `External Inputs/Slack/<channel>/<date>.md`. Auto-creates Decision Log stubs when trigger keywords (exception, incident, pricing, escalation, outage, edge case, refund) appear. Idempotent: re-running on the same day overwrites cleanly. Builds on the existing connector pattern (matches `ingest-github`, `ingest-notion`, `ingest-linear`, `ingest-gmail` from the catalect drop).
- **`skills/remotion-best-practices/`.** Domain-specific knowledge for Remotion (video creation in React) — 38 rule files covering animations, audio, captions, compositions, sequencing, transitions, fonts, light leaks, Lottie, charts, transparent video, and FFmpeg integration. Loads on demand when a Remotion project is in the conversation.

### Why this is the right fix

The drift check is meant to surface universal candidates worth contributing upstream, not to nag about the maintainer's private vault layer. Without `.driftignore`, every monthly run produced 40+ flags and the universal candidates were buried. Now the noise is suppressed and only genuinely-ambiguous items show up.

The five propagated artifacts had earned their place via repeated use in the source vault. Each one fixes a real problem (wrong-hour calendar events, accumulating stale handoffs, "is this graphified?", duplicate CRM cards, "which folders have stub files?", "pull this Slack channel into the brain", "best practice for this Remotion animation").

### Personal-data scrub

Every new file in this drop passed a word-boundary regex scrub for personal tokens before commit. Generic placeholder names used where examples were needed.

### What's preserved

All existing skills, hooks, schemas, scripts, the catalect primitives, the session-close cascade, the bootstrap. No renames, no removals. New rules slot into `templates/rules/` alongside the existing 18; new scripts into `scripts/` alongside the existing ~80; new skills into `skills/` alongside the existing 24.

---

## 2026-05-01 — Catalect architecture: 5 primitives + memory runtime + integration test

**Who this affects:** anyone using ai-brain-starter as a substrate for AI agents. Single users get richer typed memory and a queryable HTTP runtime; teams and operators get the connector pattern + autonomous synthesis + bi-temporal resolver as a foundation for company-brain workflows. Anyone who wanted the repo to demonstrate the full "company brain" primitive coverage from the catalect framing rather than just substrate.

**The shape:** the catalect "company brain" architecture names 5 primitives. Before today the repo shipped only the substrate layer (vault as ground truth, typed memory at the schema level, deterministic hooks). Today closes the gap to all 5 primitives with a passing end-to-end integration test (11 steps green) and an honest scorecard of what shipped vs what is sequenced as follow-up.

### What shipped

- **Typed memory primitives.** Five new JSON Schemas at `templates/schemas/`: `fact.json`, `workflow.json`, `exception.json`, `relationship.json`, `outcome.json`. The three existing schemas (decision, journal, session) extended with the cross-type contract: `provenance`, `confidence`, `freshness_days`, `last_verified`, `source_count`, plus `memory_class` (episodic/procedural typology) and `entity_ids` (cross-source linking field for slack/github/notion/linear/gmail/whatsapp). Plus a sixth typed primitive: `skill.json`, which defines skills as structured executable objects rather than just markdown instructions.
- **Memory runtime as REST API.** New `services/memory-api/` ships a read-only FastAPI app mirroring the `graph-query` MCP surface: 8 endpoints with bearer-token auth and an OpenAPI 3.1 spec. Generic `personal` / `team` scopes; operators rename per deployment.
- **Bi-temporal resolver primitive.** New `templates/RESOLVER.md.template` + `scripts/resolver-build.py` aggregate active rules from `Meta/Decisions/`, `Meta/Workflows/`, `Meta/Exceptions/`, `Meta/Facts/`. `scripts/stale-rule-check.py` flags entries past their `freshness_days`. `scripts/proposed-update-drafter.py` annotates downstream files when a source rule changes. Vault git is transaction-time, frontmatter `decision_date` / `last_verified` / `observed_at` are validity-time.
- **Structured agentic execution.** New `hooks/validate-skill-frontmatter.py` enforces `skill.json` shape at Write/Edit time. Three reference `SKILL.md` files updated (`diagnose`, `security-snapshot`, `setup-vault-types`) showing how to declare `tool_access`, `policy_constraints`, `required_inputs`, `output_shape`.
- **Closed-loop learning.** New `hooks/post-tool-use-learnings.py` captures execution failures and explicit `<learning>` annotations as episodic memory at `Meta/Learnings/`. New `scripts/promote-episodic-to-procedural.py` clusters recurring episodic entries (3+) and drafts procedural-memory candidates at `Meta/Promotion-Candidates/`.
- **Four new ingestion connectors.** `skills/ingest-github/`, `skills/ingest-notion/`, `skills/ingest-linear/`, `skills/ingest-gmail/` follow the proven `ingest-slack` pattern. Each writes typed external-input markdown with cross-source `entity_ids` baked in. `skills/_shared/connector_utils.py` extracts the duplicated logic (~480 lines across the six skills) into shared helpers.
- **Two autonomous synthesizers + wiki maintainer.** `skills/synth-pr-to-sop/` reads merged-PR markdown and emits `workflow.json`-conforming SOPs. `skills/synth-thread-to-sop/` reads resolved Slack threads and classifies them as decision / exception / workflow with the right frontmatter. `scripts/ground-truth-wiki-maintain.py` regenerates wiki pages at `Meta/Wiki/<topic>.md` from typed memory, idempotent.
- **Cross-cutting integration test.** `tests/integration/test_e2e_pipeline.py` exercises all five primitives end-to-end in 11 steps. Bare `python3 tests/integration/test_e2e_pipeline.py`, exit 0 on full pass. Verifies that ingest → synth → resolver → stale-check → promotion → wiki maintenance compose correctly with realistic synthetic data.
- **Documentation.** `docs/AGENTS.md` is the technical positioning page for AI builders, with bi-temporal architecture section, primitive coverage scorecard (honest, 5-6/10), and build-standards compliance. `docs/DOGFOOD.md` names the vertical pattern thesis (operations as the wedge, not just notes). `docs/EXISTING-IMPL-AUDIT.md` is the per-source existing-implementation audit (per MCP Build Runbook Lesson #16) covering all four new connectors + two synthesizers.

### Why this is the right fix

The catalect "company brain" framing crystallized the architecture as a five-piece composition rather than one-piece "memory tool." Shipping all five primitives with a passing integration test demonstrates the substrate composes; agents reading and writing typed memory through one runtime is the wedge, not just the typed memory itself.

Two alternatives considered and rejected:

1. *Wait for an upstream "MCP-memory" standard before shipping.* The standard is still being defined. Shipping our own typed primitives with a documented schema (`templates/schemas/README.md`) means we adopt whatever crystallizes; without our own primitives there is nothing to bridge from.
2. *Ship one primitive at a time across multiple drops.* Each primitive in isolation works but does not demonstrate composition. The integration test was what turned six isolated agent reports into actual end-to-end evidence the architecture holds.

### Build standards compliance

Per the operator's `Build Standards.md` + `MCP Build Runbook.md` runbooks (vault-internal). Today's build pass codified two new lessons in those runbooks (Lesson #22: read both runbooks BEFORE briefing parallel agents; Lesson #23: shared utils + integration test as Day-1 deliverables, not follow-up).

Applied during this build: shared utilities extracted (`skills/_shared/connector_utils.py` saved 321 net lines across six skills); idempotent connectors verified; schema validation hooks at write time; personal-data scrub gate clean across all 56 new files; no em dashes; cross-type frontmatter contract populated on every connector write; integration test shipped as a deliverable.

PRD: ChatPRD UUID `97b2c7ad-4c31-46d5-aa49-457006b47ba3`.

### What's preserved

All existing skills, hooks, schemas, scripts, the session-close cascade, the bootstrap, the install pipeline. Nothing renamed or removed. The new primitives compose with existing artifacts: `RESOLVER.md` aggregates entries that `aggregate-decisions.py` already rebuilds; `validate-skill-frontmatter.py` runs alongside existing hooks at write time; the new schemas are additive (existing journal/session/decision frontmatter still validates against their schemas, just with optional new fields available).

### Migration paths

- **New users via `bootstrap.sh`:** automatic. New templates + scripts + hooks land in fresh installs.
- **Existing users on `git pull`:** the new architecture is additive. The three existing schemas gained new optional fields; existing entries continue to validate. New typed primitives (`fact`, `workflow`, `exception`, `relationship`, `outcome`, `skill`) are opt-in: write entries against them when ready.
- **Activating new hooks (opt-in):** `hooks/validate-skill-frontmatter.py` and `hooks/post-tool-use-learnings.py` ship in the public repo but are not auto-registered in user `settings.json`. To activate, link into `~/.claude/hooks/` and add the hook entries to `settings.json` under `PreToolUse` (validator) + `PostToolUse` (learnings) respectively.
- **Trying the runtime:** `cd services/memory-api/ && pip install -r requirements.txt && cp .env.example .env && uvicorn app:app --port 8765`. Curl `/openapi.json` for the spec, `/healthz` for liveness, `/search?query=test&scope=personal` for a query (returns 404 if no graph.json is configured, which is fine).

---

## 2026-04-30 — Hooks now install at USER level (closes #6, fires universally in worktrees)

**Who this affects:** anyone whose Claude Code work happens in git worktrees (most active users do, since each `claude/<branch>` worktree is how feature work is isolated).

**The bug:** ai-brain-starter hooks were installed at project level (`<vault>/.claude/settings.json`). When Claude Code runs from inside `<vault>/.claude/worktrees/<name>/`, project-level hooks silently don't fire. UserPromptSubmit hooks specifically — the ones that detect "bye", catch malformed YAML at write time, log skill usage — would never get a chance to run. Reports of "I said bye and the cascade didn't trigger" had this as a quiet root cause even after the cascade detection itself was fixed.

**The fix is structural:** hooks now install at user level (`~/.claude/settings.json`), which fires universally regardless of cwd. The hooks themselves are unchanged — only the install path moved.

### What shipped

- **`scripts/install-hooks-user-level.py`** — idempotent installer that reads `hooks.json` (the canonical source-of-truth in this repo) and merges entries into `~/.claude/settings.json` while preserving every existing user-defined hook. Custom hooks are never touched. Backup at `~/.claude/settings.json.bak-{timestamp}-abs` before any edit. Post-write JSON validity verified; auto-rollback on parse error. Fingerprint-based matching means re-running the installer is a no-op when nothing has changed.
- **`hooks/migrate-to-user-level.py`** — SessionStart hook that detects existing project-level installs and prompts the user once to migrate. Tracks state per-vault at `~/.claude/.abs-migration-state.json` so the prompt fires at most once per vault. Easy opt-out: `migrationDeclined: true` in CLAUDE.md frontmatter.
- **`scripts/test-hooks-in-worktree.sh`** — regression test that creates a temp git repo + worktree, fires the detector hook from inside the worktree, and asserts it responds correctly. **6/6 checks pass:** main-worktree firing, child-worktree firing, worktree name derivation, installer preservation of custom hooks, installer adding ABS hooks, installer idempotency. CI-runnable.
- **`bootstrap.sh`** updated with `--install-hooks-user-level` flag (manual escape hatch) AND inline call at the end of normal install runs (so new users get user-level hooks by default without thinking about it).
- **`docs/HOOKS_INSTALL.md`** — full architecture doc covering install/migration/troubleshooting/why.
- **`hooks.json`** — added the migrate-to-user-level entry to the SessionStart chain so the migration prompt is part of the canonical install surface.

### Why this is the right fix

Two alternative fixes considered and rejected:

1. *"Detect worktrees and install hooks at the worktree level too."* Project-level config inside `.claude/worktrees/<name>/.claude/settings.json` would still be brittle and require reinstalling on every new worktree. User-level wins on simplicity.
2. *"File a Claude Code bug and wait for an upstream fix."* Issue is open ([#6](https://github.com/mycelium-hq/ai-brain-starter/issues/6)) but waiting blocks every user. The user-level install is a structural workaround that's actually cleaner — global hooks belong at global scope.

### What's preserved

The full session-close cascade, all 14 bundled skills, all the Phase 5 setup, every existing user-defined hook in `~/.claude/settings.json`, every project-level hook the user installed manually. The only change is that ai-brain-starter's specific hooks now live at user level. Project-level installs are not removed automatically — they coexist (additive migration). Once the user verifies the user-level hooks work, they can manually delete the project-level entries.

### Migration paths

- **New users via bootstrap.sh:** automatic — installer runs at end of bootstrap.
- **Existing users on a current update:** the SessionStart migration hook detects project-level installs and prompts once with the migration command.
- **Manual:** `python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py`
- **Verification:** `bash ~/.claude/skills/ai-brain-starter/scripts/test-hooks-in-worktree.sh` (6/6 expected).
- **Uninstall:** `python3 ~/.claude/skills/ai-brain-starter/scripts/install-hooks-user-level.py --uninstall` removes ONLY ai-brain-starter entries, preserves everything else.

**Closes [mycelium-hq/ai-brain-starter#6](https://github.com/mycelium-hq/ai-brain-starter/issues/6).**

---

## 2026-04-30 — Compounding reliability + stewardship layer (10 features in one drop)

**Who this affects:** everyone. This is a single coordinated drop that addresses every reliability gap the maintainer's panel review surfaced and closes 4 of 4 oldest open issues simultaneously.

**The shape:** these aren't 10 independent features. They're a layered architecture where each piece compounds on the previous one. Foundational reliability first (schema linter, bootstrap bundle), then telemetry, then stewardship surfaces, then periodic processes, then infrastructure.

### Layer 1 — Reliability foundations

- **Vault schema linter.** Same permanent-fix pattern that saved settings.json. New `hooks/lint-vault-frontmatter.py` is a PreToolUse hook that catches malformed YAML in Decisions/, Sessions/, and journal frontmatter before it lands. New `scripts/vault-schema-validator.py` is a standalone validator with 9-fixture self-test, runnable in CI or on-demand. Per-type schemas at `templates/schemas/{decision,session,journal}.json`. Closes the same class of bug that nuked a real CRM record 2026-04-27 (silent YAML parse error → empty re-marshal over real content).
- **Bootstrap reliability bundle (closes [#2](https://github.com/mycelium-hq/ai-brain-starter/issues/2), [#3](https://github.com/mycelium-hq/ai-brain-starter/issues/3), [#4](https://github.com/mycelium-hq/ai-brain-starter/issues/4) at once).** New flags: `--restore` for interactive recovery from .bak files, `--smoke-test` for end-to-end install verification, `--detect-partial` for finding half-installed components. Persistent log at `~/.claude/.bootstrap.log` with size-based rotation. Three new scripts: `bootstrap-restore.sh`, `detect-partial-installs.sh`, `post-install-smoke-test.sh`. The smoke test runs Python syntax, bash syntax, JSON validity, hook smoke tests, aggregator smoke tests, schema validator self-test, and the closing-signal fixture harness — 130+ checks in one command.

### Layer 2 — Telemetry foundation

- **Skill-usage telemetry (opt-in).** New `hooks/log-skill-usage.py` (UserPromptSubmit) detects `/skill-name` invocations and logs structured records to `~/.claude/logs/skill-usage.jsonl` AND vault `⚙️ Meta/skill-usage-log.jsonl` (matches the existing reporter schema, dual-location write so vault-aware analytics still work). Privacy-first: OFF by default, opt-in via `cascadeTelemetry: true` in CLAUDE.md frontmatter or `SKILL_USAGE_TELEMETRY=1` env var. Anonymized session IDs (SHA-256 truncated). Length bucketed, never full prompts. Local only, never sent over network. Erase any time with `rm ~/.claude/logs/skill-usage.jsonl`.

### Layer 3 — Stewardship surfaces

- **First-week check-ins (day 3 / day 7 / day 14).** New `hooks/first-week-checkin.py` is a SessionStart hook that fires once per milestone with a one-paragraph "how's it going?" prompt and 1-2 specific suggestions tailored to which skills the user has and hasn't tried (read from telemetry if opted in, generic hints otherwise). Closes the cohort dropout cliff. State tracked at `~/.claude/.ai-brain-checkin-state.json`. Easy opt-out via `firstWeekCheckin: false` in CLAUDE.md.
- **CLAUDE.md drift detection.** New `scripts/check-claude-md-drift.py` flags people in `## People` not mentioned in any session/decision/journal in the last 90 days, archived projects, broken wikilinks, duplicate headings, and `Codified YYYY-MM-DD` markers older than a year. Read-only; writes a review document to `⚙️ Meta/CLAUDE-md drift.md` for the user to act on. The drift detector is the meta-rule for the memory durability rule: it catches the case where the rule itself rotted.
- **Curatorial pass surface.** New `scripts/curate-skills-surface.py` reads usage telemetry and ranks skills, outputs a "most-used skills" badge, and optionally patches a managed region in README.md (between `<!-- top-skills:BEGIN -->` and `<!-- top-skills:END -->` markers). Once 4 weeks of data accumulate, the README re-ranks itself; until then, it stays static.

### Layer 4 — Periodic processes

- **Vault hygiene auto-pass.** New `scripts/vault-hygiene.py` walks the vault and reports broken wikilinks, empty notes, stale notes (>365 days untouched by default), duplicate concept candidates (same stem in multiple folders), and graphify staleness. Read-only; writes a summary to `⚙️ Meta/Vault Hygiene.md`. Designed to run weekly via cron OR as part of /sunday-review.
- **/sunday-review meta-skill.** New skill at `skills/sunday-review/SKILL.md`. Orchestrates `/weekly` + `/patterns` + vault-hygiene + claude-md-drift + decision-retrospective + skill-usage curatorial pass in a single ordered flow, then synthesizes one note at `📓 Journals/Reviews/Sunday Review {YYYY-MM-DD}.md` with linked drill-downs. Matuschak's panel critique fix: existing skills don't compound unless you force them to interlock once a week. This is that forcing function.
- **Decision retrospective loop.** New `scripts/decision-retrospective.py` finds Decisions/ files older than 90 days with empty Outcome and produces review-ready prompts. The `--apply-prompt` mode appends a "Retrospective candidates" section to `⚙️ Meta/Decision Retrospective.md` with one entry per stale decision, ready to fill in during /sunday-review or /monthly. Without this, Outcome fields stay empty forever and the quarterly retro never happens.

### Layer 5 — Infrastructure

- **Multi-machine vault sync helper.** New `scripts/vault-multi-machine-sync.sh` ships the missing piece for users who work from multiple machines on the same vault. Uses git as transport (vault must have a remote). Three modes: `status`, `pull`, `push`, `sync`. Targeted paths only (never `git add -A`). Refuses to run if no remote, refuses to push during concurrent index lock, fail-loud on merge conflicts. Closes the gap in the memory durability rule (which says "always also write to vault" but didn't ship the sync between machines).

### What this drop deliberately does NOT do

It does not add new content skills. The Matuschak/Jackie panel reads were correct: more shipping isn't the answer; reliability + stewardship + curatorial discipline is. Every new artifact in this drop strengthens what already exists — it does not introduce new dormant features.

### Compounding diagram

```
Schema linter + Bootstrap bundle  → reliable foundation
             ↓
   Skill-usage telemetry (opt-in) → real usage data
             ↓
   First-week check-ins, drift, curatorial → stewardship informed by data
             ↓
   Vault hygiene + Sunday review + decision retro → periodic deepening
             ↓
   Multi-machine sync                → infrastructure for compound use
```

### Existing users

The next auto-update sync wires every new hook into `hooks.json` and pulls in the new scripts. Telemetry stays OFF unless explicitly opted in. First-week check-ins compute days-since-install via the git clone date or a marker file; existing users will see the day-14 check-in fire on their next session if they're past day 14, which is intended (mid-flight stewardship).

### Issues closed

- [#2](https://github.com/mycelium-hq/ai-brain-starter/issues/2) bootstrap: --restore mode (shipped as `bootstrap.sh --restore` + `scripts/bootstrap-restore.sh`)
- [#3](https://github.com/mycelium-hq/ai-brain-starter/issues/3) bootstrap: persistent log file (shipped as `~/.claude/.bootstrap.log` with size-rotation)
- [#4](https://github.com/mycelium-hq/ai-brain-starter/issues/4) bootstrap: detect partially-installed graphify (shipped as `bootstrap.sh --detect-partial` + `scripts/detect-partial-installs.sh`)

---

## 2026-04-30 — Session close cascade rebuilt as a deterministic 3-layer pipeline

**Who this affects:** everyone. Every time you say "bye" to end a session, the new pipeline runs.

**The problem:** the prior architecture relied on the model "noticing" closing signals and choosing to read a separate cascade rule file before responding. Three brittle steps (notice → read rule → execute) any one of which could fail silently. Reports came back of users saying "bye" and nothing getting saved — captures lost.

**What changed:** the close cascade is now layered across three coordinated mechanisms.

- **Layer 1 — `hooks/detect-closing-signal.py` (UserPromptSubmit, NEW).** Detects close signals via regex against language packs (EN / ES / PT) before the model ever sees the prompt. Pre-resolves all paths. Pre-builds the session file shell with frontmatter and section headers. Pre-fetches decisions with empty Outcome. Writes a marker file. Injects the cascade context as `additionalContext` so the model receives complete instructions without reading a separate rule file. Performance budget: under 500ms.
- **Layer 2 — model's turn.** The model receives the injected context and runs only the irreducibly creative work: incomplete-work check, conversation scan for journal seeds (verbatim), writing notes, to-dos, decisions, delegations, then writes everything in a single batched tool-call block to the pre-built shell.
- **Layer 3 — `scripts/session-end-hook.sh` (Stop, UPGRADED).** Reads the marker, runs aggregators, performs a targeted git snapshot if the vault is git-tracked, sweeps retention, and crucially fires `scripts/session-close-fallback.py` if the session body is empty (model bailed) — the fallback calls Haiku 4.5 with the conversation transcript and fills the file. No silent loss.

**The full 7-phase cascade is preserved.** Every capture from the prior spec — journal seeds, Substack candidates with kill-conditions, to-do reconciliation, decision logging, decision outcome backfill, delegations with drafted messages, time tracking — runs identically. The change is where the work happens (deterministic hook vs. model's context window), not what gets captured.

**Token efficiency:** the model now receives a ~400-600 token system block with pre-resolved paths and inline cascade phases, instead of having to re-read a 3K-token rule file plus narrate phase-by-phase tool calls. Roughly 80% reduction in close-related model token spend, identical capture fidelity.

**UX change:** the cascade runs invisibly by default. The model says a clean goodbye, the captures land in the background. Set `sessionCloseFeedback: minimal` in your CLAUDE.md frontmatter to see a one-line summary at close end. Set to `verbose` for phase-by-phase output if you want to debug.

**New files:**
- `hooks/detect-closing-signal.py` — UserPromptSubmit detector
- `scripts/session-close-fallback.py` — Haiku-backed graceful degradation
- `scripts/recover-last-close.py` — recover from partial-completion flags
- `scripts/undo-last-close.py` — rollback most recent close
- `scripts/test-closing-signals.py` — fixture-based test harness (74 fixtures, CI-runnable)
- `templates/closing-signals/{en,es,pt}.json` — multilingual signal dictionaries
- `docs/SESSION_CLOSE.md` — user-facing reference for the whole system
- `templates/rules/session-close.md` — rewritten as the canonical rule (supersedes session-end-cascade.md, which is now a redirect stub)

**Modified files:**
- `scripts/session-end-hook.sh` — marker check + Haiku fallback wiring + git snapshot + retention
- `hooks.json` — UserPromptSubmit chain prepended with the detector
- `templates/generated/claude-md-template.md` — Phase 4 session-end section rewritten + new optional config block
- `SKILL.md` — routing table updated to mention session-close walkthrough in Phase 19-23 finish
- `phases/phase-19-23-finish.md` — Phase 24.5 walkthrough added (15-second verbal pointer for new users)

**Closing signals matched:**
- Explicit (no confirmation): `/close`, `/wrap-up`, `/bye`, `/done`, `/finish`, `/cerrar`, `/terminar`, `/chao`, `/fechar`, `/encerrar`, `/tchau`
- High-confidence natural language: bye, thanks that's all, good night, ttyl, cya, signing off, talk later, wrapping up, I'm done, k bye, gn (EN); chao, chau, nos vemos, hasta luego, listo gracias, eso es todo, buenas noches, me voy (ES); tchau, até logo, valeu, falou, boa noite, pronto, obrigado (PT)
- Ambiguous (asks "wrapping up?"): ok, cool, perfect, great, sounds good, dale, bueno, beleza
- Emoji-only: 👋, 🙏, ✌️, 🫡, 💤
- False-positive guards exclude code blocks, quoted "bye", "done with X" transitions, "listo para X" readiness, meta-questions like "what does ttyl mean?"

**Customization:** add per-user signals via `closingSignals.custom: ["k thx", "okkk"]` in your CLAUDE.md frontmatter. Switch on Haiku ambiguous-classifier with `closeDetection: hybrid` (needs ANTHROPIC_API_KEY).

**Recovery:** if a close fails because the model bailed and you didn't have ANTHROPIC_API_KEY set, a partial-flag is left at `~/.claude/.cascade-partial-{session_id}.json`. Run `python3 ~/.claude/skills/ai-brain-starter/scripts/recover-last-close.py` later to retry the fallback.

**Rollback:** `python3 ~/.claude/skills/ai-brain-starter/scripts/undo-last-close.py` moves the most recent session file + co-located decisions to an `.undone-{timestamp}/` archive folder, optionally reverts the git commit, and re-runs aggregators. Always interactive unless `--yes`.

**Testing:** `python3 scripts/test-closing-signals.py` runs 74 fixtures across all three languages, ambiguous cases, false positives, and adversarial inputs. Exits 0 on all-pass for CI.

**Existing users:** the next auto-update sync wires the new hook into `hooks.json` and pulls in the new scripts. Old `session-end-cascade.md` becomes a redirect stub pointing at `session-close.md`. No content is lost; nothing breaks. The model-side cascade is identical in scope; the trigger is now deterministic.

**Why it matters:** "I said bye and the cascade didn't run" was a real, recurring failure mode that lost user context. The fix is structural — make detection a deterministic hook, give the model only the work it can't be replaced for, and add a Haiku backstop so even a model bail doesn't lose captures. The system never gets worse than current state on any failure.

---

## 2026-04-28 — Claude Code config integrity guards (5-layer defense against silently corrupt settings.json)

**Who this affects:** anyone who has ever hand-edited `~/.claude/settings.json` or `.mcp.json`. No breaking change — all three guards are additive and warn-only by default.

**What changed:** Three new hooks land in `hooks/`, wired into PreToolUse and SessionStart in `hooks.json`. They form a layered defense against the most common silent failure mode in Claude Code config: a duplicate top-level key (especially a second `"permissions": {...}` block at the bottom of settings.json) that wipes the original allowlist because JSON's last-key-wins semantics are silently tolerated by `json.load()`. The user keeps re-approving the same gh/git push permissions every session, never realizing the config has been corrupt for weeks.

- `lint-claude-settings.py` — detects duplicate keys at any depth, unknown enum values for `model` and `theme`, hooks pointing at files that don't exist on disk, and bare-command permissions missing the `Bash(...)` wrapper. Runs in three modes: warn-only (default, for SessionStart drift detection), `--strict` (exit 2 on BLOCK-severity issues, for the PreToolUse blocker), and `--test` (5 self-test fixtures including duplicate-key and false-positive guard, so the linter itself can't silently rot).
- `pre-write-settings-lint.py` — PreToolUse Write|Edit blocker. If you (or Claude) try to write a config file that contains a duplicate top-level key, the write is blocked with a stderr explanation pointing at the exact issue. Edit operations are projected (current file + old/new substitution) before linting so the check matches the post-edit shape.
- `check-claude-code-version.sh` — SessionStart-cached check (24h TTL) against `gh api repos/anthropics/claude-code/releases/latest`. Warns if you're behind by 3 or more patch versions. Catches the silent-drift class of bug: Claude Code has no built-in "you're behind" notification, so users routinely miss memory-leak fixes and reliability patches that ship every couple weeks.

**Why it matters:** The duplicate-permissions bug is a real failure mode that's easy to introduce and hard to detect. JSON parsers don't complain. Claude Code doesn't complain. The only signal is "huh, why is this permission not working" weeks later. Catching it at the write boundary is cheap; debugging it cold is hours. The version check closes the same kind of gap on the upgrade axis — no nag, just a one-line surface at SessionStart if you've drifted enough that it matters.

**The defense layers:**
1. PreToolUse blocks bad writes (write boundary)
2. FileChanged (if your Claude Code version supports it) warns at write-time
3. SessionStart audits drift introduced by external editors
4. SessionStart runs the linter's self-test so guard rot fails loud
5. Wire the version-check output into your existing UserPromptSubmit hook to surface drift inline

**Files touched:** `hooks/lint-claude-settings.py` (new), `hooks/pre-write-settings-lint.py` (new), `hooks/check-claude-code-version.sh` (new), `hooks.json` (PreToolUse Write|Edit chain extended, new SessionStart block).

**Existing users:** the next sync run picks up the new hooks via your hooks.json. The new SessionStart block is gated on `[ -f ~/.claude/hooks/<file> ] && ... || true` so missing files exit silently — no breakage if a sync is incomplete.

**Requires:** `gh` CLI for the version check (silently no-ops if missing).

---

## 2026-04-25 — Framework expanded from 16 to 34 floors across templates, scripts, and phase docs

**Who this affects:** anyone setting up a new AI Brain Starter vault, or anyone whose existing vault has the older 16-floor framework wired into templates and the graphify pipeline. No breaking change for users who already manually expanded their framework — this just makes the public repo match.

**What changed:** The High-Rise framework was expanded from 16 floors to 34 by mapping ~150 named human emotions onto the building. The setup phase that creates concept notes, the Templater suggester for new journal entries, the Floor Check-In quick-reference template, and the graphify scripts that build floor edges in the knowledge graph were all still on the original 16. New users would have a vault that silently lost the 18 added floors at every layer (template → frontmatter → graph). This catches all four layers up.

**The new floors:** Disgust (1), Embarrassment (3), Resignation (6), Confusion (7), Loneliness (8), Boredom (9), Disappointment (11), Hurt (12), Frustration (14), Contempt (17), Hope (20), Trust (25), Compassion (26), Humility (27), Belonging (28), Gratitude (30), Excitement (31), Wonder (32). Tier ranges shifted: Low is now 1-18 (was 1-8), Middle is 19-24 (was 9-13), High is 25-34 (was 14-16).

**Why it matters:** Several common emotional states were collapsed into the wrong floor under the 16-schema — anger and frustration treated as one floor when they have distinct voice signatures (Anger = ALL CAPS at someone; Frustration = blocked-energy "ugh"), love and gratitude conflated when one is "I give to you" and the other is "I'm so grateful." Vault dashboards and pattern-recognition queries get sharper when the labels match the actual emotional resolution.

**Files touched:** `templates/obsidian/Journal Entry.md`, `templates/obsidian/Floor Check-In.md`, `phases/phase-10a-journaling.md` (concept-note generator + Spanish translation table + tier notes), `scripts/graphify_prep.py`, `scripts/graphify_canonicalize.py`.

---

## 2026-04-24 — New build rule: structured-signal-first audit before LLM batches

**Who this affects:** anyone building scripts, skills, or agents that iterate an LLM over a folder of vault files (classify, extract, label, score, summarize). No breaking change.

**What changed:** Build Standards Optimization Pass gains a new section 4a — *Structured-signal-first audit*. Before iterating an LLM over a folder of files, the pre-build checklist now mandates a five-minute audit of what structured signal already lives in those files (frontmatter fields like `concepts_extracted`, `themes`, `tags`, body wikilinks pointing at the concepts you're about to classify, prior extractor output). If existing signal already covers ≥60% of the judgment, the build is Python-first with the LLM as tiebreaker on the residual ambiguous tail.

**Why:** vault automation tends to leave structured signal behind on every pass. When a later build needs to do the "same kind" of classification, going straight to an LLM batch re-derives what's already on disk. A 2,000-file batch at ~10s per call is hours of runtime and meaningful API spend; a Python pass over existing wikilinks + frontmatter handles the obvious cases in seconds, with the LLM reserved for genuinely contextual judgment. The audit takes five minutes; skipping it costs orders of magnitude more.

**Files touched:** `docs/BUILD_STANDARDS.md` (new section 4a between LLM usage check and Excel financial math).

---

## 2026-04-23 — Install flow: one paste, zero commands to type

**Who this affects:** anyone installing AI Brain Starter for the first time. No breaking change for existing users, big UX improvement for new users.

**What changed:** The install is now truly one paste, end to end. The README's Step 2 is a single natural-language prompt that tells Claude to clone the repo into `~/.claude/skills/ai-brain-starter/`, run bootstrap, and walk you through every setup phase without stopping. No typing `/setup-brain`. No "open Claude Code in your vault folder" instruction. No terminal navigation. The only prerequisites remain: install git (Homebrew does this) and have a paid Claude account.

**Bootstrap now dual-mode.** `bootstrap.sh` and `bootstrap.ps1` detect whether they're running inside Claude Code (via `$CLAUDE_CODE_ENTRYPOINT`). If yes, the "Next Steps" banner says Claude will continue the setup interview automatically. If no (i.e., invoked standalone from a terminal), it tells the user to open Claude Code and paste the setup prompt. One script, two correct messages.

**Why:** the previous flow had five friction points for non-technical users (install Claude Code → open in vault path → clone repo → run bootstrap → type slash command). Every decision point is an abandonment point. The blog-post funnel aims at writers and founders who are not developers; the defining characteristic of this audience is that they don't know how to navigate a filesystem or a terminal. "Zero decisions" is the actual bar, not "low friction."

**Files touched:** `README.md` (Step 2 rewritten, "Prefer the terminal?" advanced section removed, team-join paste simplified), `bootstrap.sh` + `bootstrap.ps1` (header comments rewritten, Next-Steps branch on `$CLAUDE_CODE_ENTRYPOINT`).

---

## 2026-04-23 — To-do system: strengthened task contract + area-casing warning

**Who this affects:** anyone using the to-do template. No breaking change, two strengthenings of existing rules.

**What changed (1/2): every task stands alone — stricter contract.** The "self-contained task" rule (shipped earlier today) now requires all four of: (a) action verb + concrete object ("Draft Q3 plan outline" not "Work on Q3 plan"), (b) a context anchor (prefix, URL, wikilink, or file path), (c) an expected output named (deck page, CSV row count, Slack DM sent, PR opened), (d) how you report done (mark `[x]` + reply in thread, push to branch X, send to collaborator). Tasks like "Follow up with friend" or "Verify PDF" still fail the rule even with a wikilink because the expected output and done-reporting channel are missing.

**Why:** shipping a wikilink is necessary but not sufficient. A reader with zero context still can't tell "done" from "in progress" without a named output and a report channel. Multi-owner delegated work breaks most often at the hand-off, not the hand-out.

**What changed (2/2): `[area::]` values must be lowercase.** Dataview `GROUP BY area` is case-sensitive. `[area:: sales]` and `[area:: Sales]` render as two separate "sales" buckets in per-person views — silently. Docs now tell you to pick a fixed set of 3-8 canonical lowercase values up front and lint drift on every touch.

**Why:** caught in a real audit where team member views had ghost GROUP BY sections because different sessions used different casings. The user saw duplicate headers and couldn't tell at a glance whether it was two different workstreams or one with drift.

**Files touched:** `templates/generated/todo-system-template.md` (minimum-contract rule strengthened), `docs/TODO_SYSTEM.md` (Dataview-only projections emphasis + area-casing key principle).

---

## 2026-04-23 — To-do system: self-contained task rule

**Who this affects:** anyone using the to-do template, especially the new Four Quadrants view. No breaking change, just a new rule documented in the template.

**What changed:**

Added an explicit rule that every captured task MUST stand on its own when surfaced out of context. Every task needs at least one of: a `[Context prefix in brackets]` naming the project/entity/file, a direct URL, a wikilink to the source note, or a file path. Tasks without any of these get tagged `[needs-context]` so the user (or Claude on next triage) knows to enrich before execution.

**Why:** the Four Quadrants view and Dataview queries in general strip the surrounding session header. A task like "Verify PDF has all 9 pages" made perfect sense when written inside a "## 📋 From Workshop PDF build" capture block. A week later, rendered alone in Q1, it is gibberish. The user has to dig through session notes to remember which PDF, which workshop, which pages. That archaeology kills execution velocity, which is the whole point of a to-do system.

This rule applies when Claude is helping capture tasks during `/journal`, meeting-todos, or session close. Claude must either pull context from the transcript and inline it, or flag the task for the user to complete.

**Files touched:** `templates/generated/todo-system-template.md` (added "Self-contained task rule" paragraph in the How to Use section with four concrete anchor types and examples of what fails).

---

## 2026-04-23 — To-do system: optional weighted scoring formula

**Who this affects:** anyone using the to-do template who wants more rigor than pure P1/P2/P3 judgment. Everyone else can ignore this; the three-question prioritization framework still works unchanged, and the four-quadrant Dataview view works identically regardless of how priority was assigned.

**What changed:**

The to-do template now documents an opt-in weighted scoring formula alongside the default three-question framework. Every new task can take four numeric inputs:

- `[impact:: 1-5]` — goal alignment (weight 0.40)
- `[urgency:: 1-5]` — time consequence of delay (weight 0.30)
- `[effort:: S|M|L]` — execution cost, inverted (weight 0.15)
- `[commit:: Y|N]` — external promise bonus (weight 0.75)

A formula turns these into a score; thresholds map to P1/P2/P3 deterministically. This is especially useful if an LLM is doing your triage from the capture inbox into the prioritized queue, because deterministic scoring beats "Claude, please prioritize these tasks" on consistency.

**Why:** the pure three-question framework works for most people but has two failure modes. First, some users repeatedly mis-assign priority and want an auditable reason per task. Second, LLM-assisted triage produces inconsistent results when the criteria are purely linguistic; a formula removes that drift. This ships both modes in the same template, labeled clearly, with the formula marked optional and explicitly secondary to gut judgment.

**Important caveats (read before using):**

- **Calibration is required before trust.** The weights (0.40 / 0.30 / 0.15 / 0.75) are a sensible first guess, not evidence. Score 20 known tasks manually, compare against your gut, adjust until they agree, then use the formula.
- **When the formula is procrastination:** if after two weeks of using the scoring system your daily execution has not actually changed, the formula is plumbing without payoff. Go back to the three-question framework. The four-quadrant view works either way.
- **Overbuilding your to-do system is a real hazard.** If you are tempted to add scoring because you want more "rigor," ask first whether the rigor will change what you do today. If not, skip it.

**Files touched:** `templates/generated/todo-system-template.md` (added "Two prioritization modes" callout in the top README, added "Optional: Weighted Scoring System" section inside the Get to-do.md file template with formula, example calculation, calibration instructions, fallback rule, and Claude-assisted triage prompt), `docs/TODO_SYSTEM.md` (added a summary table under "Optional: Weighted Scoring Formula" pointing to the full template for details).

---

## 2026-04-23 — To-do system: capture inbox + Eisenhower four-quadrant view

**Who this affects:** anyone running fresh `/setup-brain` installs from now on who opts into the `✅ To-dos/` folder. Existing installs can upgrade by re-installing `templates/generated/todo-system-template.md`; the new four-quadrant Dataview block is additive and can be pasted into the top of an existing `Get to-do.md` without breaking anything.

**What changed:**

The to-do template now installs two files instead of one for the main list: `Get to-do.md` (prioritized queue) and `From Meetings.md` (raw capture inbox). Captures from journaling, meetings, and session close land in `From Meetings.md` grouped by source, then get triaged into `Get to-do.md` once a week.

At the top of `Get to-do.md`, a new "Four Quadrants" section auto-renders every open task from both files through an Eisenhower matrix: Q1 (Important + Urgent), Q2 (Important, Not Urgent), Q3 (Urgent, Less Important), Q4 (Backlog), plus a NEEDS TRIAGE quadrant for tasks without a `[priority::]` tag. Importance is read from the existing `[priority:: 1-3]` inline field; urgency is derived from `[due::]` within 7 days (or a P1 with no due date). Nothing changes about how you write tasks, the quadrants are just a new lens over the same inline fields.

`This Week.md` was also updated to pull P1s from both files so priority captures sitting in the inbox still surface during weekly planning.

**Why:** a single mixed file forced you to scroll past raw capture clutter to see what actually needed doing. The split keeps the prioritized queue clean. And a text list of P1/P2/P3 does not answer the question "what do I do right now?" as directly as a four-quadrant matrix does, which is the question you are actually asking when you open the file. Eisenhower has been the textbook answer to this for decades; rendering it through Dataview means it is always current with zero maintenance.

**Files touched:** `templates/generated/todo-system-template.md` (added File 2 `From Meetings.md`, added Four Quadrants Dataview block at top of File 1, updated `This Week.md` query to read from both files, added "Why two files" explainer, added "How the quadrants work" in the usage section), `phases/phase-02-03-plugins-folders.md` (updated `✅ To-dos/` install description to mention the two-file + four-quadrant model).

---

## 2026-04-22 — Light/full tier removed: everyone gets the full second brain

**Who this affects:** anyone running fresh `/setup-brain` installs from now on. Existing installs keep working unchanged. **Existing CLAUDE.md files that mention `PLAN_TIER` are stale references, not bugs.** See `docs/migrations/2026-04-22-light-full-removed.md` for cleanup.

**What changed:**

The "do you want light or full?" question has been removed from Phase 1. Every new install now unconditionally gets the advisory panel, knowledge graph context routing, panel-voice routing, monthly insight reports with pattern analysis, and the Instinct Engine. Previously these were gated behind `PLAN_TIER == "full"`.

**Why:** the light tier was a defensive crouch from an earlier moment when the daily-budget concern was uncertain. Real usage data and the workshop showed the full version is what people came for, and most users never figured out what they were missing in light mode. Splitting the experience added friction (one more question, one more decision the user had no good basis for making) without saving them anything they cared about. Removing the choice removes the friction.

**Files touched:** `SKILL.md` (dropped Tier column from routing table, removed PLAN_TIER variable), `phases/phase-01-welcome.md` (deleted Step 1.0b), `phases/phase-05-context-layer.md`, `phases/phase-10b-panel-roster.md`, `phases/phase-18-insights.md` (all tier gates removed), `templates/generated/obsidian-rules-template.md` (Rule 19 collapsed to single 12-category version).

---

## 2026-04-22 — Windows .ps1 files now ship with UTF-8 BOM (parser-error fix)

**Who this affects:** every Windows user who has ever run `bootstrap.ps1`, `drift-check.ps1`, or `update-check.ps1`. **Critical fix.**

**What changed:**

All three PowerShell scripts in the repo now start with the UTF-8 BOM bytes (`EF BB BF`). Without it, Windows PowerShell 5.1 (the default on Windows 10/11) reads the files as Windows-1252 and crashes on the first em dash, box-drawing character, or ⚙️ emoji it hits. The scripts contain all three. The bootstrap was the worst case (51 non-ASCII lines) and would have failed at install time for every Windows user, but the bug went unnoticed because no one was running the bootstrap on Windows during development.

**Also:** `phases/phase-18-insights.md` documents a `run-insights.ps1` template that Claude writes to Windows users' machines during setup. The template contained em dashes inside PowerShell strings AND the ⚙️ emoji in vault paths but had no BOM directive. Both have been fixed: em dashes stripped, mandatory BOM-save instruction added above the template with a verification command.

**Codified durably:** SKILL.md "Important Notes for Claude" now includes the rule "Windows .ps1 files MUST be saved as UTF-8 with BOM" so future setup runs and future maintainers see this on every read.

**Why:** flagged by a Windows user during a `git pull` who saw a parser error on line 201 of drift-check.ps1. The bash version worked, so they reported it as low-urgency. It was actually a much bigger issue: the same encoding fragility was in every PowerShell script we ship.

---

## 2026-04-22 — Setup friction fixes: no terminal, no GitHub prompt, ⌘↩ clarity

**Who this affects:** everyone running fresh `/setup-brain` installs.

**What changed:**

Three small but high-impact friction reductions surfaced by the workshop on April 21-22:

1. **No more "open a terminal."** SKILL.md now says: *"NEVER ask the user to open a terminal during setup. Claude runs all bash commands via its own tools."* Workshop attendees got stuck whenever the assistant told them to switch to Terminal — non-technical users don't know what a terminal is, where to find it, or how to switch back. Fixed everywhere this was happening.
2. **GitHub auth prompt removed entirely.** Bootstrap no longer prompts for `gh auth login`. The `gh` binary still installs (it's useful), but auth is never required and never asked about. Phase 0 docs updated to match. Connecting GitHub adds zero value to the brain setup.
3. **⌘↩ vs typing rule added to Visual Reassurance Protocol.** The single most common stall point: users see a gray tool-approval box and don't know whether to type something or press ⌘↩ (Mac) / Ctrl↩ (Windows). New rule: *"If you see a gray tool box → ⌘↩. If Claude ends with a question mark → type your answer."* Said out loud once before Phase 0 starts, repeated if the user stalls.

**Why:** the workshop showed that broken tools weren't the problem — confusion was. Three concrete clarity fixes saved more abandonment risk than any feature add would.

---

## 2026-04-22 — CI / lint workflow + /diagnose self-check

**Who this affects:** maintainers (CI) and end users debugging a setup (/diagnose).

**What changed:**

1. **GitHub Actions workflow** at `.github/workflows/lint.yml` now runs on every push and PR. It catches: bash syntax errors (`bash -n`), PowerShell parser errors (`pwsh ParseFile`), missing UTF-8 BOM on `.ps1` files (the bug class above), em dashes in `.ps1`/`.sh` (preventive), and JSON validation for `hooks.json` and any `.mcp.json`. Costs $0 on public repos.
2. **`/diagnose` self-check command** at `skills/diagnose/`. Run it anytime the user is unsure if something is working. Single command checks: CLAUDE.md exists in vault, all expected skills installed in `~/.claude/skills/`, hooks registered, `journal-index.json` exists and is fresh, vault path readable, MCPs registered. Reports green/yellow/red per check with one-line fix guidance. Wired for Mac/Linux (`scripts/diagnose.sh`) and Windows (`scripts/diagnose.ps1`).

**Why:** the Windows BOM bug sat in `bootstrap.ps1` since the file was created because nothing tested it. CI prevents the regression class. /diagnose closes the gap between "something feels off" and "here's exactly what's broken" — workshop attendees specifically asked questions in the shape of "how do I know if it's working?"

---

## 2026-04-21 — Granola: local cache export replaces API sync

**Who this affects:** anyone using Granola for meeting notes.

**What changed:**

`scripts/granola_sync.py` now reads Granola's local cache directly instead of calling the Granola API. No API key or MCP required — works on any Mac with Granola installed, on any plan (free, pro, business). Exports full timestamped transcripts as markdown to your meeting notes folder, firing automatically via a LaunchAgent whenever Granola updates its cache after a meeting.

A companion `scripts/com.granola-export.plist` is included for the LaunchAgent install (edit the two placeholder paths, then `launchctl load` it).

The Granola MCP entry has been removed from Phase 0 bootstrap and all docs — the local cache approach covers the same use case without the network dependency or plan restriction.

---

## 2026-04-21 — Phase 24: first-week handoff with recommended uses

**Who this affects:** everyone running fresh `/setup-brain` installs. Post-install only, no effect on existing setups.

**What changed:**

1. **New Phase 24** appended to the setup flow. After install completes, Claude delivers a brief, understated congratulations (Jackie-register: no exclamation marks, no "Congrats!") and points to a short companion read on recommended first-week uses: three commands and one habit.

2. **Language-conditional link.** Claude shows only the link that matches the user's `PRIMARY_LANGUAGE` from Phase 1. One block, one link. No dual-language dump at the end of install.

3. **SKILL.md updated** to 25 phases (0-24). Phase 23.5 kept its "last INSTALL phase" marker; Phase 24 is the post-install handoff, not an install step.

**Why:** the most common failure mode for a new install isn't a broken tool, it's a user who finishes setup and doesn't know where to start. A single short read with three concrete actions closes that gap.

---

## 2026-04-21 — retry-budget hook: cap Claude's failing-command loops

**Who this affects:** everyone. Optional but recommended for all setups.

**What changed:**

1. **New hook `hooks/retry-budget.py`** blocks the 4th invocation of an identical Bash command within a 30-minute window. Attempts 1-3 pass silently; attempt 4 exits with code 2 and a message asking Claude to surface the blocker to you instead of looping further.

2. **Bypass flag** `RETRY_BUDGET_BYPASS=1` prefix lets Claude legitimately re-run a command more than 3 times (polling for a cron to finish, iterating on a fix where each attempt is a real change). The bypass is explicit so Claude has to tell you it's using it.

3. **Scope guards** prevent false positives: commands under 15 characters (`ls`, `pwd`, `date`, `git status`) are exempt, and state is per-Claude-session (no leakage between parallel work).

4. **Registered in `hooks.json`** under a new PreToolUse Bash matcher, so `/setup-brain` wires it up automatically on fresh installs. Existing users can install manually via the pattern in `phases/phase-05-context-layer.md`.

5. **New rule 31 in `templates/rules/efficiency.md`** documents the behavior and when to invoke the bypass.

**Why:** without a retry ceiling, Claude will cheerfully burn 200K context looping on a failing command before surrendering — this is the single highest-cost silent failure mode we see. Three attempts is enough to cover flaky-network hiccups; the fourth is almost always a signal to stop and plan. Pattern adapted from Devin 2.0 ("ask user for help if CI does not pass after the third attempt") and Cursor 2.0 ("don't loop more than 3 times to fix linter errors").

---

## 2026-04-21 — first-run UX hardening for /second-brain-mapping

**Who this affects:** everyone, especially first-time users running `/second-brain-mapping` on a fresh vault. Reduces silent failures and makes cold-start safer.

**What changed:**

1. **Setup-vault-types precheck.** Both `/second-brain-mapping` and `scripts/second-brain-mapping.sh` now check that at least one document-type extractor is configured before doing anything. Without this, Phase 1 used to run silently and report "no extractor" for every file in your vault, a confusing first-run experience. Now you get a clear message pointing you to `/setup-vault-types` instead.

2. **`--sample [N]` mode.** New flag that picks N files per registered type (default 1), runs extraction, and shows you the actual fields that would be written, without writing anything. Use this on a cold start to verify the output looks right before committing to your whole vault. Example: `/second-brain-mapping --sample 3`.

3. **Progress heartbeat in Phase 1.** On vaults of more than a few hundred files, Phase 1 used to run silently for minutes. Now it prints a progress line every 250 files showing files-per-second and ETA. Tunable with `--progress-every N`.

4. **File-count-aware cost estimate before Phase 2.** The "Run graphify? ~100k-1M tokens" prompt is now backed by a vault-specific estimate: it counts your actual files and words, applies graphify's typical compression ratio, and shows you a dollar figure at current Sonnet pricing for both cold-start and incremental modes. Treat the number as order-of-magnitude, not a quote.

5. **Hardened graphify install path.** `/graphify` Step 1 used to silently swallow pip install errors. Now it captures install output, surfaces the last 20 lines on failure, and stops with actionable next steps (network or proxy, PEP 668, pipx fallback, Python 3.10+ check) instead of continuing with a broken interpreter.

6. **Rate-limit-aware subagent dispatch.** When many graphify subagents fail with `429 / rate_limit / overloaded_error`, the skill now surfaces a clear message about API tiers and offers three concrete options (wait and re-run, split corpus, raise tier) instead of pretending extraction succeeded.

**Why:** with more people downloading and trying ai-brain-starter cold, the first 60 seconds of a `/second-brain-mapping` run determine whether they keep going or close the terminal. Six failure modes that previously looked like "the tool froze" or "the tool is broken" now produce explicit, actionable messages.

---

## 2026-04-21 — graphify wikilink hard guards

**Who this affects:** anyone who uses `scripts/graphify_apply_wikilinks.py` to apply graph-derived wikilinks. Tightens the script against path-form wikilink leaks, mirroring the hardening just shipped in `auto-wikilink.py`.

**What changed:**

1. **Hard guard in `apply_wikilink()`** — if `link_target`, `search_term`, or `display` contains a `/`, the script strips the path prefix or refuses to apply. Prevents path-form links like `[[folder/Name]]` from ever reaching the vault.

2. **User input sanitization** — at the first-name disambiguation prompt, pasted path-form input (`👤 CRM/Diego`) is stripped to the basename before use. You can no longer accidentally write a path-form alias by pasting a full vault path.

3. **Hard guard in `create_stub()`** — `note_name` with `/` is sanitized to basename. Stops stubs from being created as orphaned subdirectory children of CRM/ or Notes/.

4. **Defense in depth in `graphify_wikilink_gaps.py`** — graph labels containing `/` are filtered out during candidate collection so the apply script never sees them.

5. **Maintenance runbook** added to the docstring — dry-run first, path-form guard behavior, FileNotFoundError handling, pairing with `graphify_wikilink_gaps.py`, and pointer to `wikilink_misfire_audit.py` for cleanup.

**Why:** the v1 `auto-wikilink.py` bug (writing `[[folder/Name]]` across thousands of files) was discoverable only after a full vault audit. The graphify apply script had the same structural vulnerability — no hard guard, no input sanitization — even though the graph rarely produces path-form labels. This patch closes that gap before it causes the same mess.

---

## 2026-04-21 — wikilink misfire audit + auto-wikilink patch

**Who this affects:** anyone who ran `auto-wikilink.py` before v2 (the v1 script wrote `[[folder/Name]]` path-form wikilinks instead of bare `[[Name]]` wikilinks — every vault that ever ran v1 has these).

**What changed:**

1. **New script: `scripts/wikilink_misfire_audit.py`** — detects and batch-fixes path-form wikilinks left by v1. Run with `--fix` to apply, `--dry-run` to preview. Generates a report at `⚙️ Meta/Reports/auto-wikilink-misfire-audit-{date}.md`. Configurable `WRONG_ALIAS_BASENAMES` set for notes whose v1 alias links were semantically wrong (strip the link, keep the display text instead of re-linking).

2. **`scripts/auto-wikilink.py` patch** — added `try/except (FileNotFoundError, OSError)` in `add_wikilinks()`. Previously a file that disappeared between the directory walk and the file read (e.g. a git-deleted stub) would crash the whole run. Now those files are silently skipped.

3. **`scripts/auto-wikilink.py` maintenance runbook** added to the docstring — correct run order (misfire audit first, then auto-wikilink), known quirks (multi-word note titles create aggressive alias matches), and `WRONG_ALIAS_BASENAMES` pointer.

**Correct cleanup sequence:**
```
python3 "⚙️ Meta/scripts/wikilink_misfire_audit.py" --fix
python3 "⚙️ Meta/scripts/auto-wikilink.py" --all --dry-run  # review first
python3 "⚙️ Meta/scripts/auto-wikilink.py" --all
```

---

## 2026-04-21 -- graphify: two silent-failure bugs fixed

**Who this affects:** anyone running `/graphify` with MiniMax pre-extract enabled, anyone running graphify stages on a vault with nested folder structure (journals, writing, notes with subfolders), or anyone who has ever looked at the `extraction_manifest.json` and found it under-counting what was actually processed.

**What changed:**

1. `scripts/graphify_minimax_preprocess.py` now walks the full shell-config fallback chain to find `MINIMAX_API_KEY` — `~/.zshenv`, `~/.zsh_secrets`, `~/.zshrc`, `~/.zprofile`, `~/.bashrc`, `~/.bash_profile`, `~/.profile`, `~/.env`. Previously it only grepped `~/.zshrc` and failed silently for users whose keys lived anywhere else.

2. `scripts/graphify_stage_finish.py` now records a manifest entry for every file sent to a stage, not just files that produced LLM-novel canonical nodes. It also unflattens staged `graphify-input/A_B_C.md` source-file references back to their original nested paths by trying each `_` → `/` combination against the real filesystem.

**Why #1:** `~/.zshrc` is an *interactive* shell config. Scripts launched by IDE agents or subprocesses run under a non-interactive shell that never sources `.zshrc`, so a key defined only there is invisible. Users commonly keep API keys in `~/.zsh_secrets` (private, sourced from `.zshenv`) or directly in `.zshenv`. The old fallback missed both of those cases. The error was silent and expensive — the script errored out mid-pipeline, the stage ran without the cheap pre-extract, and the main model burned more tokens than it should have.

**Why #2:** When you stage files for a graphify chunk, the script flattens the nested path into a single filename (`✍️ Writing/High-Rise/Floor.md` → `✍️ Writing_High-Rise_Floor.md`) so every file lives in one directory. Subagents then set `source_file` on every node they produce to this flattened staged path. After the run, `graphify_stage_finish.py` was trying to map those staged paths back to the real vault file to record a manifest entry — but it only tried the direct path, which no longer exists once staging is cleaned up. The staged path stopped resolving, `is_file()` returned False silently, and the manifest recorded zero entries despite 100% of the stage succeeding. Next coverage audit would then report every one of those files as "MISSING" and re-run them.

Separately, the manifest only pulled entries from canonical nodes/edges. Files whose content was already covered by the regex-preflight wikilink pass never produced any *new* LLM items, so they never appeared in canon and never got a manifest entry — even though the agent read them and decided they didn't need additional inference. Those files also kept showing up as "MISSING" forever.

**What you'll see:** `manifest updated: N files recorded` where N matches the number of files you actually sent to the stage. Previously it could print `0 files recorded` on a stage that processed dozens of files successfully. If the resolver can't map some source files back, you'll get a `WARN:` line naming the first five unresolved refs.

**Who should update:** everyone running graphify stages regularly. The bugs compound over time — every stage with missing manifest entries adds noise to future coverage audits and causes unnecessary re-runs.

---

## 2026-04-20 -- skill sync skips skills with their own .git

**Who this affects:** anyone who has put a bundled skill (like `humanizer`) under independent version control after installing it. Most commonly: forking a skill on GitHub and tracking your changes there.

**What changed in `scripts/sync-skills.sh`:** the sync now skips any installed skill directory that contains its own `.git` (file or directory). The same way it already skips symlinked skills.

**Why:** if you fork a skill and develop it independently, the old behavior would clobber your local commits with the bundled version on every sync. Worst-case: the overwrite would race with an in-flight Edit and corrupt a commit you were about to push to your fork. Skipping is safer; you keep responsibility for `git pull`-ing your fork on your own schedule.

**What you'll see:** when sync runs, your forked skill shows up in the SKIPPED line as `<skill>: <path> has its own git repo (independently managed)`. No files written, nothing backed up, nothing changed in your fork's working tree.

**If you want bundled-version updates anyway:** delete `.git` from the installed skill (turns it back into a plain copy), or remove the install entirely and let the bundled version reinstall fresh on next sync.

---

## 2026-04-20 -- proposal-PDF workflow (opt-in reference, not a default install)

**Who this is for:** founders and consultants who ship formal business proposals as PDFs. If you don't send PDF proposals, ignore this update — nothing auto-installed, nothing got added to your active Claude environment, nothing to remove.

**What was added to the repo as reference material (not installed anywhere on your machine):**

- `templates/obsidian-snippets/proposal-letterhead.css` — Obsidian CSS snippet (also usable with pandoc + WeasyPrint from the CLI) that transforms a plain markdown note into a letterpress-quality branded PDF on export
- `docs/PROPOSAL_PDF_WORKFLOW.md` — step-by-step guide covering install, customization, both Obsidian-UI and CLI paths, color alternatives, and recommended proposal structure

**Explicit opt-in:** To use this you have to actively copy the CSS into your vault's `.obsidian/snippets/` folder, enable the snippet in Obsidian Settings, and customize two placeholder lines. Or, for the CLI path, install pandoc + WeasyPrint via Homebrew. Nothing happens automatically, and nothing is loaded into any skill or session context.

**What the output looks like:** Georgia body typography, deep navy section headers with hairline underlines, italic letterhead on pages 2+, "N / total" page numbers, clean business-document table styling. Reads as "law firm" or "senior consulting firm" — sober, not marketing-flavored.

**When NOT to use this:** you don't send formal PDF proposals; your proposals live in Google Docs, Notion, or a design tool; your clients expect slide decks not documents; you're fine with a markdown-default PDF.

**Originated in:** A Colombian consulting engagement where the proposal had to read as senior-professional-services to a traditional industrial patriarch. The same register works for any founder shipping proposals into traditional industry contexts — construction, legal, financial services, manufacturing, government.

**To adopt:** Read `docs/PROPOSAL_PDF_WORKFLOW.md`. 15 minutes from install to first PDF. **To skip:** do nothing. This isn't on by default.

---

## 2026-04-20 -- two new team workflows: Canonical Facts and playbook-to-task wiring

**What changed:** `for-teams/team-workflows.md` gained two new sections that codify patterns every team with contractors or external-facing numeric claims will eventually need.

**Section 5 — Canonical Facts registry.** Single source of truth for every numeric claim, source, and attribution that appears in external material (pitch deck, sales one-pager, investor memo, marketing site). Each entry carries the claim, a tier-1 primary source, the year, and the URL. Files that cite numbers must trace back to the registry. Drift = stop-ship defect before anything ships.

**Section 6 — Playbook-to-task wiring.** An "Instructions for [Name]" playbook without a matching task in the team to-do file is orphan work: the contractor never sees it. Session close now scans every playbook modified this session against the team to-do file; if any is not referenced by a live task, close is blocked until a task is added or the playbook is explicitly marked reference-only.

**Why it matters:** these two patterns address the same failure mode from opposite sides. Canonical Facts prevents contradictory numbers from reaching investors. Playbook-to-task wiring prevents careful instructions from becoming invisible work. Both patterns came out of a 2026-04-20 fabrication-audit session where four conflicting market-size numbers had propagated across five investor assets, and a rebuilt pitch-deck playbook failed to reach the contractor because no task pointed at it.

**Also:** `CLAUDE.md` template gets the matching three lint rules (Canonical Facts source-of-truth, Canva URL resolution for shortlinks, playbook-to-task wiring) so Claude enforces them at write time, not just at session close.

---

## 2026-04-17 -- bootstrap now auto-removes deprecated tools on re-run

**What changed:** `bootstrap.sh` and `bootstrap.ps1` now have a "Cleanup deprecated tools" section that runs at the top of every re-run. If it finds something that's been removed from the bundled stack, it removes it automatically and tells you why. No prompts, no manual steps.

Current tools it removes if present:
- **claude-mem** — security issues (open local HTTP port, file-read surface, plaintext API keys). The built-in memory system covers everything it did.
- **notebooklm** — browser automation + Google login on every session wasn't worth it for most users. If you want it back: `git clone https://github.com/PleasePrompto/notebooklm-skill.git ~/.claude/skills/notebooklm`

**If you actively use one of these:** re-install it after the bootstrap runs. The bootstrap only removes it — it doesn't block you from having it.

**How future removals work:** when something gets removed from the default stack, the bootstrap handles the cleanup. You don't need to read release notes or run manual commands — just re-run bootstrap and it takes care of it.

---

## 2026-04-17 (session-end-cascade.md) -- foreground-only git + cross-session lock contention rules

**The problem this fixes:** If you run multiple Claude sessions on the same machine, they share one `.git/` and queue at `.git/index.lock` when closing. The old session-close protocol backgrounded aggregators (`&`) and sometimes deleted live locks (`rm -f .git/index.lock`), which in concurrent setups corrupted the git index and stalled commits for minutes. One session's session-close.md edit on 2026-04-17 lost a 10-minute window to this exact race.

**What changed:**
- Aggregators now run **foreground, sequential** — no `&`, no `run_in_background`. ~5s slower per close, eliminates the entire race class.
- Added a **polite spin-wait commit pattern**: wait for `index.lock` to clear naturally, only `lsof`-check then remove if it's been orphaned 60s+, never blindly delete.
- Hardened the existing "no `git add -A`" rule with the cross-session reasoning (sweeping commits steal staged files from other sessions).

**No action needed** — fix lives in `templates/rules/session-end-cascade.md`. Re-run the install or pull the latest to pick it up.

---

## 2026-04-17 (hooks audit) -- rotate-logs.sh gzip failure cleanup

**The problem this fixes:** If gzip failed mid-write (disk full, permission error), a partial or zero-byte `.1.gz` was left on disk. On the next rotation cycle it would shift to `.2.gz`, polluting the rotation history. The original log was always safe, but the stale partial was never cleaned up.

**What changed:**
- **`hooks/rotate-logs.sh`**: gzip step now uses `if/else`. On success, truncates the original. On failure, removes the partial `.1.gz`. One-line change, no behavior change on the happy path.

---

## 2026-04-17 (later) -- vault-git targeted-paths rule in CLAUDE.md and claude-md-template

**The problem this fixes:** Claude Code was running `git add -A` inside large Obsidian vaults during session close, walking 60K+ files, locking `.git/index.lock` for 10+ minutes, and burning context while the assistant polled for progress. Rules alone aren't enough — future sessions can ignore them.

**What changed:**

- **`CLAUDE.md`**: new "Git in large Obsidian vaults (users' vaults)" section. Instructs the assistant to never run `git add -A`, `git add .`, or unscoped `git status` in a vault. Always pass explicit file paths. Includes a fast diagnostic (`wc -l <(git ls-files)`) to detect whether you're in a large vault.
- **`templates/generated/claude-md-template.md`**: added "Git in this vault (if git-tracked)" section. Ships the rule to every new vault's `CLAUDE.md` via Phase 4 so new users get it on setup, not after an incident.
- **`scripts/auto-snapshot.sh`** (follow-up): rewrote to use targeted paths instead of `git add -A` even with the file-count guard. The guard always aborted on large vaults anyway — the rewrite makes the script actually useful.

**If you already have a large vault under git:** stage only the files you know changed. Session files, decision files, edited rules, to-do edits. Not the whole tree.

---

## 2026-04-17 -- git bloat prevention + vault health check

**The problem this fixes:** Claude Code's worktree isolation feature creates a full copy of your vault for every session. If those copies aren't cleaned up, they accumulate — 32 stale copies discovered in a live vault, totalling 46GB. On top of that, each copy left a `claude/` git branch behind, inflating `.git/objects` to 6GB. Binary files (videos, Photoshop files) committed by accident made it worse.

**What changed:**

- **`scripts/worktree-prune.sh`** (upgraded): now also deletes orphaned `claude/` branches — those whose worktree directory no longer exists. Previously only pruned stale refs. Wire this to a weekly cron or scheduled task.

- **`scripts/vault_maintenance.py`** (upgraded): added a Git Health section to the monthly maintenance report. Checks for: stale `claude/` branches (>5 is a warning), prunable worktrees, and git pack size >500MB. Reports the exact fix commands so you don't have to look them up.

- **`templates/rules/session-end-cascade.md`** (upgraded): added Phase 2b — git snapshot + cleanup. Every session close now removes the current worktree and deletes all `claude/` branches after committing. Prevents accumulation from the source.

- **`templates/rules/advisory-panel.md`** (tightened): intro, Technology & AI section, and Panel Rules compressed ~40%. No panelists removed. The `Pick when:` triggers are unchanged — just stripped the commentary between credential and trigger.

**If you already have bloat:** run the vault maintenance script to see your current state, then follow the fix commands in the report. Or run manually: `git branch | grep 'claude/' | xargs git branch -D && git worktree prune`.

---

## 2026-04-17 -- session close runs on Sonnet

**`templates/rules/session-end-cascade.md`**: added a Model section at the top. The session-close protocol should always run on Sonnet, not Opus. The close is structured, write-heavy work (scanning, filing, batch writes, running aggregators) — no judgment calls. Switching to Sonnet before Phase 0 saves real tokens without losing anything. Claude announces the switch so you know what model is running.

---

## 2026-04-17 -- maintenance hooks, MCP health check, worktree pruner, rollback guide

New scripts and hooks that save common manual recovery steps:

- **`scripts/mcp-config-check.py`** (new): health checker for your MCP config. Catches six silent-fail bugs: malformed .mcp.json, missing server paths, blank env vars, ghost config files, orphan MCP directories, and misplaced user-scoped MCPs. Run at session start or on-demand. Configurable via env vars (VAULT_ROOT, MCP_SCAN_DIRS).
- **`scripts/worktree-prune.sh`** (new): weekly git worktree pruner. Self-locates via $BASH_SOURCE so it survives vault moves. Logs to `logs/worktree-prune.log`. Wire to a cron or scheduled task.
- **`hooks/file-changed-settings.sh`** (new): FileChanged hook that validates .claude/settings.json and .mcp.json on every write. Surfaces a clear error to stderr if JSON is malformed, before a silent failure cascades into broken hooks.
- **`hooks/rotate-logs.sh`** (new): rotates hook logs at 500KB, keeps 3 gzipped generations per file. Auto-discovers *.log files in LOG_DIR. Safe to call every SessionStart. Prevents unbounded log growth on active vaults.
- **`hooks/claude-scheduled-runner.sh`** (new): headless Claude Code launcher for launchd/cron scheduled tasks. Reads the task prompt from a SKILL.md file, runs `claude -p` with a turn cap, logs to ~/Library/Logs. All paths configurable via env (VAULT_ROOT, CLAUDE_BIN, TASKS_DIR, LOG_DIR).
- **`templates/rules/rollback.md`** (new): step-by-step recovery guide when hooks, settings, or plugins break. Diagnosis-first approach (check JSON validity, scan logs, look for stuck locks) before any revert. Nuclear-last ordering.
- **`templates/rules/obsidian-reference.md`** (new): Obsidian-specific reference details. Covers the workspace.json sort-state quirk (why editing app.json doesn't change sort order), macOS APFS folder mtime behavior, and the custom-sort plugin fix.

---

## 2026-04-17 (later) -- token optimization guide + cheap model routing

New `docs/TOKEN_OPTIMIZATION.md`: a practical guide to where Claude Code burns tokens on overhead (spoiler: 5K–20K per message before you type anything) and six fixes that cut 50–70% of that cost. Covers caveman-dense Claude-facing files, a hard cap on MEMORY.md entries, disabling unused MCP servers, routing grunt work to cheap models, and a quarterly compression habit.

New `scripts/minimax.sh`: a thin bash wrapper for MiniMax M2.7 (~$0.06/M tokens, 150x cheaper than Opus). Users supply their own API key from [platform.minimax.io](https://platform.minimax.io). Good for extraction, summarization, and bulk classification — the grunt work you shouldn't pay Opus for.

`docs/MEMORY_SYSTEM.md` now has a hard 50-entry cap and a pre-add checklist (already in CLAUDE.md? skip. one-time bug fix? skip. useful in 3 sessions? if no, skip).

`templates/generated/obsidian-rules-template.md` now ships a "Token Efficiency Rules" block so every new vault starts with the compress-everything mindset baked in.

- **`docs/TOKEN_OPTIMIZATION.md`** (new): the full guide + checklist
- **`scripts/minimax.sh`** (new): generic cheap-model helper
- **`docs/POWER_TOOLS.md`**: new "Cheap model APIs" section
- **`docs/MEMORY_SYSTEM.md`**: 50-entry cap in the hygiene section
- **`templates/generated/obsidian-rules-template.md`**: Token Efficiency Rules block
- **`README.md`**: linked TOKEN_OPTIMIZATION.md in Deeper Documentation
- **Why it matters:** a large vault setup burns hundreds of thousands of tokens per session on overhead alone. These patterns pay for themselves within one session.

---

## 2026-04-17 (later) -- auto-snapshot: guard against large vaults

`scripts/auto-snapshot.sh` now checks tracked file count before running `git add -A`. If the vault has more than 5,000 tracked files (typical Obsidian vault: 10K-60K), the script logs a clear abort message and exits instead of walking the full tree. A full-tree `git add` on a large vault locks `.git/index.lock` for 10+ minutes and burns assistant context while it waits.

No behavior change for small repos (side projects, code repos). If you have a large vault, use explicit-path staging at session close instead.

- **`scripts/auto-snapshot.sh`**: added file-count guard before `git add -A`

---

## 2026-04-17 (later) -- vault-context hook: actual file injection for strategic questions

Previously, the session-protocol hook told Claude to "read Current Priorities.md before responding." That's an instruction — it can be skipped or deferred. In practice, Claude often gave generic answers without ever reading the vault.

Fix: a new `vault-context.py` hook that actually reads the files and injects their contents into context before Claude responds. No instructions to follow — the content is just there.

How it works: on every message, the hook checks for strategic keywords (plan, decision, priorities, client, revenue, strategy, etc.). If matched, it reads `⚙️ Meta/Current Priorities.md` and `⚙️ Meta/Open Loops.md` and passes them as `additionalContext`. Silent on trivial queries (rename, fix typo, etc.). Auto-detects the vault root by walking up from the working directory — no hardcoded paths, works in worktrees.

You can extend it by editing `~/.claude/hooks/vault-context.py` and adding entries to `TOPIC_MAP` — each entry maps a keyword list to a list of additional files to inject (e.g. your raise dashboard, a project brief, a client list).

- **`hooks/vault-context.py`** (new): the hook itself. Auto-detecting, keyword-triggered, silent on non-matches.
- **`hooks.json`**: added vault-context as a UserPromptSubmit hook.
- **`phases/phase-05-context-layer.md`**: added installation step with copy command and wiring instructions.
- **Why it matters:** instructions are unreliable. Injected context is not.

---

## 2026-04-17 (later) -- daily-journal: verbatim-capture rule added

When you're journaling with Claude, you type a lot of things back: answers, tangents, panel replies, corrections. Previously the skill only synthesized those into a smooth narrative, so the exact words got lost. If you later came back looking for something you'd said, it was gone.

Fix: every message you type during a journal session now gets logged word-for-word in a dedicated `### My responses to the panel (verbatim, every message I typed back in this session)` subsection inside the entry. No paraphrase. No summary. Typos preserved. The narrative stays readable; the verbatim appendix is the archive.

- **`skills/daily-journal/SKILL.md`**: new "Verbatim-capture rule (critical — no exceptions)" section near the top (after the separation rule). Updated entry template to require the verbatim subsection. Added reinforcement line in the Step 7 "Important" bullets.
- **Why it matters:** a journal that silently paraphrases you is a journal you stop trusting. The rule is stated in three places now so the model can't skip it.

---

## 2026-04-17 -- wikilink gaps: `--exclude` flag to skip vault author's own name

In personal vaults, the owner's name appears in every journal entry — as a section header, panel pullback marker, signature, or third-person reference. The gaps script was surfacing it as a high-connection "candidate" with thousands of false matches.

Fix: add `--exclude LABEL [LABEL ...]` so users can suppress their own name (or any label) permanently without touching the script.

- **`scripts/graphify_wikilink_gaps.py`**: new `--exclude` flag; excluded labels skip the `is_wikilink_candidate` check entirely. Case-insensitive match.
- **Usage:** `python3 graphify_wikilink_gaps.py --vault-root . --exclude "Jane Doe" "Jane"`

---

## 2026-04-16 (late, part 2) -- auto-wikilink: `--all` flag for vault-wide backfill

`auto-wikilink.py` previously defaulted to journals only. For a mature vault, that leaves years of writing, notes, chats, and CRM with unlinked mentions. Running it on individual folders piecemeal is tedious.

Fix: add a `--all` flag that walks the entire vault, plus a cleaner dir-exclusion model.

- **`scripts/auto-wikilink.py`**: new `--all` flag walks every `.md` file in the vault (respecting the team-vault firewall). Split `EXCLUDED_DIR_NAMES` into `EXCLUDED_TERM_DIRS` (dirs that can't be sources of canonical terms — e.g. AI Chats) and `EXCLUDED_PROCESSING_DIRS` (dirs that can't be written to — e.g. `_archive`, `.obsidian`). AI Chats now receives wikilinks but never supplies them.
- **Use pattern:** `--dry-run --all` first (prints proposed count), review sample, then drop `--dry-run` to apply. On a mature vault this typically connects 10k-50k unlinked references in one pass.
- **Still safe:** existing region-tracking, frontmatter protection, and path-form guard all unchanged. Team-vault firewall still hard-enforced.

Why this matters: an Obsidian alias lets `[[Vanessa]]` resolve to `[[Vanessa Rodriguez]]`, but it does NOT auto-convert plain text "Vanessa" mentions across your vault. `--all` closes that gap retroactively, so the graph actually reflects what you wrote.

---

## 2026-04-16 (late) -- Model routing: flip the default, add a nudge hook

Most sessions silently run the biggest model available even for trivial tasks because users set `"model": "opus"` (or `opusplan`) once and forget. The rules at the SKILL level say "route to the right model" but no mechanism enforces it — and a running Claude session can't swap its own model mid-turn.

Fix: flip the default, add a lightweight nudge.

- **`hooks/route-suggest.py`** (new): UserPromptSubmit hook. Classifies each prompt by keyword — strategy/panel/architecture → suggest `opus`; trivial edit → suggest `haiku`; extraction/tagging/boilerplate → suggest a cheap model (minimax/local). Silent when no confident match. Never auto-switches — just prints a one-line `[route nudge]` the user sees in context.
- **`templates/rules/efficiency.md`**: added Rule 30 — "Never push back with 'too long' or 'too expensive.'" Cost appeals shut down conversations; specific blockers open them. Banned phrases listed.
- **Recommended default:** set `"model": "sonnet"` in settings.json and use `/model opus` or a shell alias (e.g. `cc-deep='claude --model opusplan'`) when you actually need heavy reasoning. Opus becomes opt-in, not opt-out.

Why this matters: a dumb router with the right default beats a smart router with the wrong one. Flipping the default is one line; the nudge hook catches the 20% of prompts where the default is wrong. Together they cut token burn without adding decision fatigue.

---

## 2026-04-16 (evening) -- Dropped Calendar + Juggl from default stack

Audited the installed Obsidian plugins against actual usage. Two plugins weren't earning their spot: Calendar (dead weight once `/journal` became the entry point — nobody was clicking dates) and Juggl (zero config data, zero note references — graph exploration happens via Graphify + Smart Connections now).

- **`phases/phase-02-03-plugins-folders.md`**: removed `calendar` and `juggl` from the PLUGINS dict and from the manual-fallback walkthrough. Auto-installer now ships 6 plugins, not 8.
- **`templates/rules/obsidian-plugins.md`**: dropped the Juggl section; "Visual Graph Exploration" now points only at Neo4j Browser.
- **`templates/rules/tool-routing.md`** + **`docs/POWER_TOOLS.md`**: removed Juggl/Calendar from routing tables and power-tool catalog.
- **`skills/insights/SKILL.md`**: removed Juggl from the monthly plugin-update scan list.

Existing installs aren't auto-removed — this only affects new `/setup-brain` runs. If you want to drop them from an existing vault, delete `.obsidian/plugins/{calendar,juggl}` and remove them from `community-plugins.json`.

---

## 2026-04-16 (night) -- Windows parser bug + bootstrap cleanup

Post-consolidation audit caught three things: a PowerShell parser bug that broke every Windows bootstrap run, bun left in as dead weight, and install verbs that leaked past the phase-file firewall.

- **`bootstrap.ps1`**: `"$sub:"` in two status lines was parsed by PowerShell as a scope accessor (`$scope:name`), erroring on every bundled sub-skill. Fixed with `"${sub}:"`. Windows users would have hit this on every install.
- **`bootstrap.sh` + `.ps1`**: removed bun. It was a claude-mem runtime dep that stayed after claude-mem was dropped. Nothing currently depends on it.
- **`phases/`**: pulled remaining install verbs (brew/winget/snap/flatpak/git-clone/cp -R/mcpServers) out of phase-01, -04, -06-09, -11. Phases now defer to bootstrap for any install recovery.
- **CHANGELOG**: compressed the top three entries from 3-paragraph templates to 1-paragraph + bullets. Cleanup commits don't need the full template.

---

## 2026-04-16 (late evening) -- Single source of truth for installs: bootstrap canonical, Phase 0 thin

Install logic lived in two places (`bootstrap.sh`/`.ps1` AND `phases/phase-00-install.md`) with 80% overlap; they drifted and users on different paths got different stacks.

- **`bootstrap.sh`** and **`bootstrap.ps1`** are the ONE source of truth: fastmcp, full bundled sub-skill set (insights, deconstruct, daily-journal, repurpose-talk, nano-banana skill folder), granola + chatprd MCPs, obsidian-skills marketplace, obsidian/context7/playwright plugins, Mac Obsidian CLI symlink. `--dry-run` skips verification.
- **`phases/phase-00-install.md`** rewritten 431 → 158 lines: thin orchestrator only. Invokes the local bootstrap, then Granola login walkthrough, Obsidian CLI confirmation, nano-banana deferred install, Knowledge Graph CLAUDE.md rule template.

Every install path (curl one-liner, /setup-brain, re-run) now hits the same code. Windows parity via `.ps1` in the same commit, not deferred.

---

## 2026-04-16 (evening, later) -- Enforce compressed Claude-facing docs at tool level

Memory-level rules require active recall and get skipped; moved compression enforcement into a tool-level hook so it fires regardless.

- **`templates/hookify-rules/hookify.compress-claude-docs.local.md`** (new): warn-level hook on writes to memory files, hookify rules, vault rule files, and CLAUDE.md. Shows the rules inline at tool-use time.

---

## 2026-04-16 (evening) -- Fix duplicate note titles (filename + H1)

Scripts and templates were writing `# Title` after frontmatter; in Obsidian the filename IS the title, so every note rendered its heading twice.

- **`scripts/granola_sync.py`**: dropped the `# {title}` line; auto-imported notes now start with the `*Auto-imported...*` context line.
- **`phases/phase-06-09-tools-templates.md`**: CRM Entry and Meeting Note templates no longer include `# {{title}}` after frontmatter.
- **`templates/hookify-rules/hookify.no-duplicate-h1.local.md`** (new): opt-in warn rule catching any H1 written after frontmatter in a `.md` file.

---

## 2026-04-16 (late p.m.) -- Session close protocol: no more stubs, 7-day retention, compressed rule

**The problem:** the session-end-hook created a "stub" session file every time the hook fired, expecting Claude to fill it in. In practice most sessions end without running the full protocol (short sessions, abrupt exits, worktree subagents, compactions), so stubs piled up unused. One user had 966 of 1,046 files as empty stubs -- 92% noise, 4.2 MB of clutter in the `⚙️ Meta/Sessions/` folder.

**What changed:**

**`scripts/session-end-hook.sh`** (rewritten):
- No longer creates stub files. Claude writes the real session file directly during session close (Phase 2 of the protocol).
- Added retention cleanup on every hook invocation: stubs older than 7 days are deleted, substantive files older than 7 days are archived to `Sessions/Archive/`. Fast and idempotent -- only touches files past the cutoff.
- Cross-platform date math (BSD `date -v-7d` on macOS, GNU `date -d '7 days ago'` on Linux).
- Step 4 prompt trimmed: points Claude at the session-close rule file instead of restating the entire protocol inline in a JSON blob.

**`templates/rules/session-end-cascade.md`** (rewritten, same filename for install compatibility):
- Went from 7 lanes / 192 lines to 4 phases / 84 lines. No information lost -- just compressed per the "caveman prose" rule (machine instructions, not human-facing).
- Phase 0: run `date` once, reuse the timestamp everywhere (session file, time tracking, to-do dates).
- Phase 1: single-pass conversation scan fills all output buckets in memory before writing anything.
- Phase 2: batch writes in parallel, aggregators run in background.
- Phase 3: conditional change-impact audit and repo propagation.

**`phases/phase-05-context-layer.md`**: the inline hook template embedded in the phase doc was updated to match the new scripts/ version (no stubs, retention cleanup, compressed Step 4 prompt).

**`phases/phase-04-claude-md.md`**: the CLAUDE.md session-end section was updated from "7-lane capture cascade" to "4-phase session close protocol" to match the new rule file.

**Why this matters for you:**
- Your `⚙️ Meta/Sessions/` folder will stop filling up with empty placeholders.
- The folder self-cleans: anything older than 7 days either goes away (stubs) or moves to `Archive/` (substantive). You get a week of rolling context, nothing more.
- Session close runs faster (single-pass scan, parallel writes, background aggregators).

**Upgrade notes:**
- If you already have hundreds of stub files, delete them: `grep -rl 'session_label: "update pending"' "$VAULT/⚙️ Meta/Sessions/" | xargs rm` (one user went from 1,046 files / 4.2 MB to 83 files / 476 KB).
- The new hook is backward-compatible with existing substantive session files; they'll sit untouched until they pass the 7-day cutoff, then move to `Archive/`.

---

## 2026-04-16 (p.m.) -- Removed claude-mem from bundled stack (security)

Dropped claude-mem from the default install after a security audit surfaced: (1) unauthenticated local HTTP API on port 37777; (2) arbitrary file-read via the `smart_unfold` / `smart_outline` MCP tools; (3) API keys stored plaintext at `~/.supermemory-claude/credentials.json`; (4) a `UserPromptSubmit` hook injecting content into every session (persistent prompt-injection surface); (5) deprecated `glob@11.1.0` transitive dep flagged for ReDoS; (6) PreToolUse:Read hook truncating Read output to line 1 (we had shipped a local patch around this).

Fresh installs no longer register the `thedotmack` marketplace, enable `claude-mem@thedotmack`, install `bun` as a claude-mem runtime, or run `npx claude-mem install`. Existing installs that already had it are NOT auto-uninstalled (bootstrap is additive). To remove manually: `claude plugin uninstall claude-mem@thedotmack` + drop the `thedotmack` entry from `extraKnownMarketplaces` in `~/.claude/settings.json`.

What replaces it for most users: the auto-memory system at `~/.claude/projects/.../memory/` (typed markdown files, durable, human-readable) plus graphify for cross-session knowledge. That combo covers `mem-search` / `knowledge-agent` use cases without the attack surface. For AST-aware code search (the one unique capability), install `ast-grep` as a standalone CLI if a specific project needs it.

Removed from: bootstrap.sh, bootstrap.ps1 (if present), README.md, docs/POWER_TOOLS.md, phases/phase-00-install.md, phases/phase-01-welcome.md, phases/phase-06-09-tools-templates.md, scripts/patch-claude-mem-read-hook.sh (deleted).

---

## 2026-04-16 (p.m.) -- Removed notebooklm from bundled stack

Dropped the notebooklm skill from the default install. Not part of the daily workflow for most users; the overhead (Chromium browser automation, Google auth dance, first-run setup) wasn't paying off. If you want it, clone directly: `git clone https://github.com/PleasePrompto/notebooklm-skill.git ~/.claude/skills/notebooklm`. Existing installs are untouched (bootstrap never overwrites the folder).

Removed from: bootstrap.sh, bootstrap.ps1, README.md, docs/POWER_TOOLS.md, phases/phase-00-install.md, phases/phase-06-09-tools-templates.md, scripts/vault-repo-drift-check.sh, skills/notebooklm/.

---

## 2026-04-16 (p.m.) -- Session close rule: compressed + restructured

**templates/rules/session-end-cascade.md** (rewrite, 191 → 85 lines):
- Renamed "11-lane capture cascade" to "Session close protocol" and restructured into Phase 0 (single timestamp) / Phase 1 (single-pass scan with output buckets) / Phase 2 (batch writes) / Phase 3 (verify + propagate). Same semantics, dense caveman prose.
- Added explicit "Report zeros, never skip silently" directive and a templated summary format so every session ends with the same shape.
- Backgrounded both aggregators in Phase 2 (parallel `&`) for faster wall time.
- Added Phase 0 timestamp discipline: one `date` call per session, reuse everywhere.
- Added 7-day retention policy (session files archived or stubbed, prevents unbounded growth).
- Kept the `gh issue create` heredoc under Phase 3 so end users have the actual command, using `<owner/repo>` placeholder.
- Tightened skip condition: <5 user messages with no decisions/info/learnings.

## 2026-04-16 (p.m.) -- Hookify template README: correct upstream URL

**templates/hookify-rules/README.md**:
- Fixed two links that pointed to `github.com/anthropics/claude-code/tree/main/plugins/hookify`. The hookify plugin actually lives in a separate repo: `github.com/anthropics/claude-plugins-official/tree/main/plugins/hookify`. Both references updated.
- Discovered while filing an upstream PR to fix a CLAUDE_PLUGIN_ROOT fallback bug in hookify itself: [anthropics/claude-plugins-official#1441](https://github.com/anthropics/claude-plugins-official/pull/1441).

## 2026-04-16 (p.m.) -- Claude Performance Self-Improvement System

**scripts/claude_performance_digest.py** (new):
- Weekly digest script that reads Claude Code JSONL session data from ~/.claude/projects/ and computes six effectiveness metrics: activity distribution (Coding/Exploration/Debugging/Delegation/Planning/Conversation), one-shot edit rate, agent spawn analysis, model mix (Opus/Sonnet/Haiku), per-project allocation, and hookify firings.
- Six diagnostic rules with configurable thresholds. When triggered, writes prescriptive to-dos to "Claude To-dos.md" for investigation items, and for four specific rule types (VERBOSE AGENTS, MODEL ROUTING, LOW ONE-SHOT RATE, EXPLORATION OVERHEAD) writes permanent behavioral rules to ~/.claude/CLAUDE.md so future sessions read them at start.
- Stdlib only, stream-parses JSONL, idempotent to-do appending. Self-locates via __file__. Configurable PROJECT_LABELS dict for clean display labels.
- Usage: python3 scripts/claude_performance_digest.py [--days N] [--dry-run] [--no-report]

## 2026-04-16 -- Plugin hook fix + graphify encoding hardening (Lessons #95-99)

**scripts/fix-plugin-hooks.sh** (new):
- Claude Code does not reliably expand `${CLAUDE_PLUGIN_ROOT}` in plugin hooks.json. When the variable is unset, hook commands resolve to a nonexistent path, error, and Claude Code defaults to BLOCK for PreToolUse -- silently denying all Write/Edit operations. Run this script after any plugin install to replace all `${CLAUDE_PLUGIN_ROOT}` references with absolute paths. Safe to re-run.

**graphify_stage_finish.py** -- encoding hardening (Lesson #98):
- All `write_text()` calls now use `encoding="utf-8"`. Without it, emoji characters in node labels (e.g. folder names like `📋 Strategy`) can be silently mangled on some systems. Affected calls: raw JSON, canon JSON, graph.json (already had `ensure_ascii=False`, now also has explicit encoding), GRAPH_REPORT.md, and extraction_manifest.json.

**graphify_stage_select.py** -- SKIP_PARTS expanded (Lesson #89):
- Added `⚙️ Meta` and `🗄 Archive` to the skip set. Without these, vault meta folders (templates, GRAPH_REPORT.md, runbook files) would appear as eligible extraction candidates, inflating file counts and wasting LLM tokens.

**What NOT to do (Lesson #99)**:
- Never write `graph.json` directly from a NetworkX object (`nx.node_link_data()`). The `hyperedges` key lives outside the NetworkX model and is silently dropped. Always use `graphify_stage_finish.py --num-chunks 0` for recluster/report-only runs -- it reads `merged_graph` as a dict and preserves hyperedges through the full pipeline.

---

## 2026-04-16 -- Graphify pipeline hardening: layout auto-detect, mtime manifest, dual-SHA, cache pruner

Four improvements to the graphify staged-rollout scripts, plus a new utility:

**graphify_stage_select.py** (rewrite):
- Layout auto-detect (Lesson #87): detects personal vs. multi-vault (team) layout at startup. Personal vault keeps cache + chunks under graphify-out/. Team layout splits cache at vault root, chunks under corpus subfolder. Prints layout name for clarity.
- SKIP_PARTS filter (Lesson #89): excludes Archive/, _review_alternate_drafts/, and iCloud/GDrive conflict copies ("foo 2.md") from file listing. Prevents non-content files from entering the extraction pipeline.
- Mtime-manifest short-circuit (Lesson #93): reads extraction_manifest.json and skips files whose mtime is within 5 seconds of their last LLM extraction. Falls back to SHA check when the manifest doesn't cover a file. Cuts re-run times dramatically on large vaults.
- Dual-SHA cache lookup (Lesson #94): tries both relative-to-vault and absolute path variants when checking the SHA cache. The graphify library uses relative paths internally, older scripts used absolute. This prevents false cache misses after upgrades.
- Now accepts multiple corpus folders in a single run and supports --max-files-per-chunk (default 45) to prevent schema collapse on large batches.

**graphify_stage_finish.py** (rewrite):
- Layout auto-detect with --corpus-folder and --cache-dir args. Auto-detects corpus folder by scanning vault children. Uses detected base path for raw/canon output, graph path, and report path.
- Manifest writer (Lesson #93): after cache save, writes extraction_manifest.json with per-file entries (llm_time, sha, node_count, stage). This is the write side of the mtime short-circuit that select.py reads.
- Fixed cache_dir in Step 5b to use args.cache_dir instead of hardcoded path.
- Added import hashlib (required for manifest SHA computation).

**graphify_prune_stale_cache.py** (new):
- Deletes cache entries whose SHA256 key no longer matches any current file. Run monthly or after vault restructuring. Honors the same SKIP_PARTS and dual-SHA logic as select.py.

**patch-claude-mem-read-hook.sh** (new):
- Disables the claude-mem PreToolUse:Read hook that replaces file content with a one-line summary. Idempotent, creates timestamped backup before patching. Run after any claude-mem plugin update.

---

## 2026-04-16 -- Bug fixes: pip, graph edge key, directed graphs, and skill guardrails

Porting improvements that surfaced from production use:

**graphify/SKILL.md** -- Three fixes:
- `pip install` changed to `"$PYTHON" -m pip install` (with bare-pip fallback). Prevents the case where the system `pip` installs to a different Python than the one graphify actually runs under, causing "module not found" on first run.
- Added `--directed` flag to the usage table. Builds a `DiGraph` that preserves edge direction (source to target) instead of the default undirected `Graph`. Useful for code dependency graphs, citation networks, or any corpus where direction matters.
- Added `--whisper-model` flag to the usage table. Lets you pass a larger model (`small`, `medium`, `large`) when transcribing audio/video files for higher accuracy at the cost of speed.

**meeting-todos/SKILL.md** -- Added guardrail to the description: "Do NOT use for general task management, journaling, or pulling full meeting transcripts (use the meeting workflow for that)." Prevents Claude from triggering this skill when someone asks to journal or pull a full transcript.

**patterns/SKILL.md** -- Added guardrail: "Do NOT use for weekly/monthly journal reviews (use insights), daily journaling (use daily-journal), or one-off decisions (use deconstruct)." Clarifies the skill's scope so Claude routes correctly instead of running `/patterns` on a prompt that belongs in `/journal` or `/weekly`.

---

## 2026-04-15 -- To-do system template

New template: `templates/generated/todo-system-template.md`. A complete prioritized task management system for Obsidian with Dataview integration.

**What it includes:**
- **Main to-do file** with P1/P2/P3 priority tiers, Dataview inline fields (`[area::]`, `[priority::]`, `[due::]`), and a Done Archive section
- **This Week view** that auto-pulls all P1 items via Dataview (never needs manual refresh)
- **Waiting On tracker** with sections for delegations, external blockers, and "blocked on self" items
- **Team variant** with per-person views, `[owner::]` field, and sprint progress queries

**System rules baked in:**
- Lint rule: every task must have `[area::]` and `[priority::]` or Claude adds them on contact
- Stale item decay: open items older than 14 days with no due date get flagged during weekly reviews
- Overdue rule: past-due items auto-surface and must be re-dated or dropped
- Priority assignment framework: three questions (hard deadline? someone blocked? moves top goal?)

**Integration:** Added as a conditional folder in Phase 2-3 (only created if the user wants in-vault task management). Personal to-do Dataview queries added to the query library.

Born from the maintainer's own vault restructure: the "organize by when I thought of it" pattern always decays into a mess. Priority tiers with inline fields and auto-refreshing views don't.

---

## 2026-04-15 -- Graph query MCP + conditional graph loading + Minimax routing + session length flag

Four optimizations for high-volume, multi-account Claude setups:

**Graph query MCP** (`scripts/mcps/graph-query-server.py`): FastMCP server that loads your vault graph (NetworkX node-link JSON) at startup and exposes surgical tools: `search_nodes`, `get_neighbors`, `find_path`, `query_subgraph`, `get_community_members`. Replaces reading the full GRAPH_REPORT.md (~3K tokens) every time you ask a question. Load graph once, query it many times. Supports two vaults via `scope` param ('primary'/'secondary'). Requires two env vars: `GRAPH_JSON_PATH` and `SECOND_GRAPH_JSON_PATH`. Install via `fastmcp` (pip) and add to `.mcp.json`.

**Conditional graph loading** (`templates/generated/claude-md-template.md`): Session Protocol step 1 changed from "always load both graphs" to "load only when the first message is topic-relevant." Keyword hook (`graph-context-hook.sh`) catches natural-language queries (not just exact nouns) so casual questions like "what's my pattern with money?" or "my pitch needs work" trigger graph loading automatically. Saves 6K+ tokens on sessions that don't touch the graph.

**Explicit Minimax routing list** (`templates/rules/efficiency.md`, rule 28): Five operation types always route to the cheap model without asking: (a) structured extraction from raw text, (b) bulk tagging/classifying, (c) boilerplate from template, (d) single-doc summary under 5K tokens with no voice requirement, (e) pre-extraction for graphify/weekly/insights pipelines. Removes the hesitation loop where Claude second-guesses whether to route.

**Session length flag** (`templates/rules/efficiency.md`, rule 29): At 30 exchanges, surface a reminder to run `/compact`. Long sessions degrade in the back half. Early compaction keeps the context clean.

---



## 2026-04-14 -- Advisory panel: Colombia localization section + named-only rule

Two additions to the advisory panel template:

- **New "Colombia: Life & Business" section** with 8 named, integrity-verified voices covering corporate culture (Carlos Raul Yepes), brand building (Catalina Escobar), cultural identity (Hector Abad Faciolince), business law (Francisco Reyes Villamizar), women in business (Sylvia Escovar), relationships/gender (Florence Thomas), bicultural identity (Patricia Engel), and holistic wellness (Dr. Jorge Carvajal Posada). Every person was researched for integrity before inclusion.
- **Rule #8: Named panelists only.** Claude must never invent archetypes or unnamed experts. Every panel voice must be a named person from the roster. If none fit, say so and offer to add one. Prevents fabricated "a hospitality GM" or "a marketplace founder" style voices.

## 2026-04-14 -- Doc compression rule + memory durability enforcement

Two new efficiency rules that make the whole setup more reliable:

- **Rule #25: Compress all Claude-facing docs.** Every file Claude reads (rules, runbooks, SKILL.md, CLAUDE.md, templates) must fit in a single Read call (<10k tokens). Dense prose, no filler. If a file exceeds the limit, split it. This prevents the Read tool from silently truncating important instructions.
- **Rule #26: Memory durability.** Never store something only in Claude's project memory. Memory is tied to one account on one machine. Every memory must also be written to a vault file in the same response. The vault is the source of truth across all accounts and computers.

Both rules in `templates/rules/efficiency.md`.

## 2026-04-14 -- MCP Build Runbook + 13 build lessons

New `docs/MCP_BUILD_RUNBOOK.md` — the full protocol for building MCP servers and managed agents on top of this vault setup. Distilled from a single build session that shipped 13 agents.

What's in it:
- **Optimization Pass** (mandatory before every build): kills over-engineered stacks before you write code. Saved multiple Next.js + Postgres + Railway setups that were meant for one internal user.
- **13 lessons** from real builds: symlink handling in macOS vaults, dict access safety, lazy Anthropic client pattern, datetime.utcnow() deprecation fix, financial math goes in Excel not Python/LLM, and more.
- **Self-test protocol**: every agent must pass a no-API-key self-test before it's done.
- **GitHub publishing checklist**: strip personal data, standard files, README requirements.
- **Which agents are worth publishing**: decision table for when to open-source vs keep private.

The lazy client pattern and symlink rules alone will save most people 30-60 minutes per build.

## 2026-04-14 -- ChatPRD and RescueTime MCP setup docs + RescueTime server script

Two new MCP integrations documented and ready to use:

- **ChatPRD MCP** — HTTP MCP at `https://app.chatprd.ai/mcp`. Add to your vault `.mcp.json`, authenticate once via OAuth, and Claude can create/read/search PRDs directly from Claude Code. Setup snippet in `docs/POWER_TOOLS.md`.
- **RescueTime MCP** — Custom FastMCP server at `scripts/mcps/rescuetime-server.py`. Gives Claude read access to your productivity data (pulse, top apps, category breakdown, trends). Pairs with the session-end time tracking lane so `/weekly` reviews can merge app-level data (RescueTime) with purpose-level logs (session end cascade). Setup instructions in `docs/POWER_TOOLS.md`.
- **Tool routing template updated** — added RescueTime row so the routing table is complete for anyone who sets it up.

Important: never commit your RescueTime API key or ChatPRD tokens. Keep secrets in the `env` block of your vault `.mcp.json` and make sure that file is gitignored.

---



## 2026-04-14 -- custom-sort auto-activates on install (no manual toggle needed)

- **Fix:** custom-sort plugin now writes `data.json` with `suspended: false` during Phase 2 setup. Previously the plugin installed silently disabled (Obsidian's default is `suspended: true`) and required a manual ribbon-click to activate. First-time users had no idea why their folders weren't sorting. This is now handled automatically.

---



## 2026-04-14 -- Journal organization by month + insights save path fix

- **New script: `scripts/organize-journals.py`** — organizes a flat Journals folder into month subfolders ("January 2026", "February 2026", etc.) based on the `creationDate` field in each entry's YAML frontmatter. Also moves any existing `Monthly Summaries/` and `Weekly Insights/` subfolders into their matching month folders and removes the empty parent folders. Run once to reorganize, or after any bulk import. Usage: `python3 scripts/organize-journals.py --vault-root "/path/to/vault"`.
- **Insights skill save path updated** — `/weekly` and `/monthly` reports now save directly inside the appropriate month folder (e.g. `Journals/April 2026/Apr. 7-13, 2026 Weekly.md`) instead of a separate `Weekly Insights/` or `Monthly Insights/` root subfolder. Keeps all content for a given month together in one place.

---



## 2026-04-14 -- Recursive folder sorting by most recently modified

- **New plugin: Custom File Explorer sorting** (`custom-sort` by SebastianMC). Phase 2 now installs this automatically. It sorts every folder in your vault by the most recently modified note *inside* it, recursively. This means if you edit a file deep inside a subfolder, that folder and all its parents bubble to the top of the file explorer — not just the file itself. Fixes the limitation where Obsidian's built-in sort only used the folder's own filesystem mtime (which macOS doesn't update recursively).
- **New file: `sortspec.md`** at vault root. Auto-generated during setup. Contains the `> advanced recursive modified` rule for all folders. You can customize per-folder rules here if needed.

---



## 2026-04-14 -- Optional time tracking lane in session-end cascade

- **New Lane 8 (optional): Time tracking.** If you add a "Time tracking" preference to your CLAUDE.md, Claude will auto-log what you worked on at the end of each session, categorized by type (Writing, Business, Vault, Personal, Admin, etc.). No manual tagging needed: Claude infers the category from conversation context. Pairs well with productivity APIs like RescueTime for a combined "what app" + "what purpose" view during weekly reviews. Opt-in: does nothing unless you enable it.
- Session-end cascade is now 9 lanes (was 8). Lane 9 is the former Lane 8 (change impact audit).

---



## 2026-04-14 -- Customer discovery meeting template + to-do system guidance

- **New template: `templates/meeting-prep-discovery.md`** -- A Mom Test-based customer discovery meeting prep template. Includes: 3-point agenda to send the client, role assignments (lead/relationship owner/listener), 30-question bank organized by block (their world, pain points, how they buy, competitive landscape, expansion), meeting structure with time blocks, case study outline draft, and post-meeting action items. Generic and ready to use for any B2B client meeting. Based on Rob Fitzpatrick's "The Mom Test" framework.
- **To-do system tip: external tracker callout** -- When team members use an external task tracker (Linear, Jira, Asana) for certain work (e.g., engineering), don't duplicate those tasks in the vault. Instead, add a callout in the vault to-do file noting where those tasks live. Prevents sync drift and double maintenance.

---

## 2026-04-15 -- Advisory panel: Technology & AI section

Five new panelists covering the AI/automation gap most knowledge workers have in their advisory roster:

- **Ethan Mollick** (Wharton/Co-Intelligence) — practical AI integration, what to delegate vs. own
- **Tiago Forte** (Building a Second Brain) — PKM, vault architecture, knowledge compounding
- **Andy Matuschak** (evergreen notes, tools for thought) — stress-tests whether systems actually change thinking over time
- **Andrej Karpathy** (Tesla AI, OpenAI) — technical AI sanity-checks, capability assumptions
- **Tim Ferriss** (4-Hour Workweek) — ruthless elimination, delegation, systems over heroics

Pick when: AI workflow decisions, vault/system design, automation choices, delegation triage.

---

## 2026-04-14 -- Cowork project guide + floor voice verification

- **New doc: `docs/COWORK_PROJECTS.md`** — Guide for creating project-scoped CLAUDE.md files when using Cowork projects. Includes template, architecture diagram, and tips from real usage (iterate with Cowork feedback, split tool routing, don't over-duplicate). Solves the common problem where a Cowork project scoped to a subfolder doesn't inherit root vault context.
- **Writing voice as floor verification** — New section in SKILL.md (under "Emotional floor tagging") that teaches Claude to cross-check floor assignments against writing style, not just content. Includes a voice signature table for all 16 floors and cross-floor heuristics (entry length, bilingual code-switching, body vocabulary). Template is calibrated per-user after ~100 entries.

---

## 2026-04-13 -- Naming conventions + journal integration

- **Insight report naming:** Weekly reports now use human-readable dates (e.g., "Apr. 7-13, 2026 Weekly.md") instead of ISO week numbers. Monthly uses "Apr. 2026 Monthly.md".
- **Journal / Session Captures integration:** Daily journal skill now checks the Session Captures staging file before starting the interview, surfaces accumulated seeds, and deletes them after use.
- **Journal index note:** Users with emoji folder names (e.g., "Journals" vs. "Journals") must pass `--journal-dir` and `--meta-dir` explicitly to `build-journal-index.py`, or update the defaults in the script to match their vault structure.

---

## April 13, 2026 (forty-second session -- LLM accuracy guardrails)

Five new efficiency rules that prevent Claude from guessing when a tool gives the right answer instantly:

- **Rule #10: Never count in-context.** Use `wc` for words/chars/lines. LLMs tokenize subwords, not characters, so counting by reading is architecturally unreliable.
- **Rule #11: Never do math in-context.** Use `python3 -c` or `bc` for any arithmetic. Anthropic's own docs say to verify with specialized software. There's an open bug for this (anthropics/claude-code#9421).
- **Rule #12: Verify wikilinks exist.** Check with `obsidian unresolved` or Glob before creating `[[links]]`. Never link to non-existent notes without flagging it.
- **Rule #13: Use IANA timezones.** Never hardcode UTC offsets or use ambiguous abbreviations (EST/EDT). Use `python3` with `zoneinfo` for conversions. DST differences between cities make offsets unreliable.
- **Rule #14: Check file size before reading.** Run `wc -l` first. Under 2000 lines: read whole. Over 2000: use offset/limit. Over 5000: question whether you need the whole file.

---

## April 13, 2026 (forty-first session -- always check system clock)

- **Efficiency rule #9: Always check the system clock.** Claude's internal sense of time is unreliable, and the system prompt only provides a rough date (no time). New rule: always run `date` in bash before writing any timestamp. Applies to journal entries, meeting notes, file headers, session captures, and to-do dates. Use `date "+%Y-%m-%d %I:%M %p"` for human-readable format.

---

## April 13, 2026 (fortieth session -- optimize-on-repeat + change impact audit)

Two process improvements ported from production use:

- **Efficiency rule #8: Optimize on repeat.** Expanded beyond "recurring processes get a runbook." Now: before running a repeated task, review what happened last time. After running, note what could be better and fix it immediately (update the runbook, fix the script, add a rule, file the bug). The key addition is "don't just note it and move on." Document deduplication misses, schema violations, hung steps, parallelization opportunities, caching gaps, new tools, and pattern drift.

- **Session close Lane 8: Change impact audit.** When a session modifies rules, scripts, skills, hooks, schedules, integrations, or paths, verify nothing broke before closing. Six checks: (1) paths resolve, (2) skills still trigger, (3) hooks still fire, (4) schedules still run, (5) cross-file references valid, (6) integrations connect. Catches silent breakage from renamed paths, moved files, or updated configs.

---

## April 13, 2026 (thirty-ninth session -- Session-close capture system)

- **Session close protocol:** 8-lane automatic capture at end of every session (journal seeds, writing notes, actionable content, to-dos, delegations, decisions, belief shifts, change impact audit). Nothing valuable stays trapped in chat transcripts.
- **Session Captures staging file:** template added for journal seed accumulation across sessions. Journal skill pulls from it and deletes used items.
- **Decision archive lifecycle:** active decisions in Decisions/ move to Decisions/Archive/ after Outcome + Pattern are filled in during weekly/monthly retrospectives.
- **Decision retrospective:** added to weekly/monthly insights skill (section 5b2) to close the loop on past decisions.

---

## April 13, 2026 (thirty-eighth session -- Obsidian sort order)

Phase 2 plugin installer now configures `fileSortOrder: "byModifiedTime"` in `.obsidian/app.json` during setup. Files and folders sort by most recently modified (newest first) out of the box. Uses `setdefault` so it won't overwrite if the user already set a preference.

## April 13, 2026 (thirty-seventh session -- new skill + panel automation)

Added **1 new skill** and **1 automation script**:

- **repurpose-talk** (`/repurpose-talk`) -- turns a speaking engagement into 10-30 content pieces. Extracts key insights, stories, and one-liners, then generates LinkedIn posts, short-form notes, article seeds, and a video clip plan with timestamps. Includes a 2-week posting calendar and cross-pollination checks (business angles, investor soundbites, CRM follow-ups). Supports bilingual output. Trigger: `/repurpose-talk` or "I just gave a talk."
- **panel-trigger-hook.sh** -- a UserPromptSubmit hook that detects decision language in prompts ("should I", "weighing", "torn between", "pros and cons", etc.) and injects an advisory panel reminder so Claude pulls 3-5 relevant voices with mandatory dissent. Silent passthrough on non-decision prompts. Install by adding to settings.local.json hooks. Solves the problem of advisory panels only firing when explicitly invoked -- this makes them proactive.

## April 13, 2026 (thirty-sixth session -- 5 new skills)

Added **5 skills** that were missing from the repo:

- **daily-journal** -- conversational journaling with floor detection, behavior accountability, and advisory panel dialogue. Interviews you, identifies your emotional floor, runs checks (gym, sleep, scrolling), consults 90+ advisory voices, and saves a properly formatted entry. Includes idea quarantine and to-do extraction.
- **humanizer** (v2.7.0) -- removes AI writing patterns from text using Wikipedia's 29-pattern library. Pre-flight doc-type detection, voice calibration against your own writing, Spanish/bilingual support, 4-tier ROI-ranked pattern ordering, and adaptive pass strength.
- **insights** (/weekly, /monthly) -- generates insight reports from journal entries with floor trends, life coach flags, therapist observations, 60+ panel voices, first-principles audit, skill usage snapshots, and Obsidian ecosystem checks.
- **nano-banana** -- image generation via Google Gemini 3 Pro Image. Text-to-image, editing, multi-image composition (up to 14 images), iterative refinement, and search-grounded generation.
- **notebooklm** -- query Google NotebookLM notebooks from Claude Code for source-grounded, citation-backed answers. Browser automation, library management, persistent auth.

All skills were sanitized: personal paths replaced with `[VAULT_PATH]`, names removed, personal context genericized. Panel rosters preserved (they're public figures and the framework is universal).

---

## April 13, 2026 (thirty-fifth session -- proactive compaction rule)

Added efficiency rule #7: **compact proactively at ~50% context usage.** Long sessions (graph pipelines, weekly reviews, multi-step cascades) degrade quality when context fills silently. The rule is simple: if you've done 3+ major tasks, compact before the next one. The PreCompact hook already preserves state, so compaction is safe.

---

## April 13, 2026 (thirty-fourth session -- patterns auto-detection)

The `/patterns` skill (Instinct Engine) now has **session-end auto-detection triggers** inspired by Hermes Agent's skill generation heuristics. Instead of only running when you manually invoke `/patterns`, Claude now silently evaluates four triggers at the end of every session:

1. **High tool-call friction** (5+ calls for routine work, suggesting a missing shortcut)
2. **User correction** (approach was wrong, may echo a recurring pattern worth codifying)
3. **Dead-end recovery** (backtracked before finding the right path, worth preserving)
4. **Non-trivial discovery** (undocumented finding that should become a rule)

If any trigger fires, Claude suggests running `/patterns` but never auto-runs it. You can say "yes" (full scan), "just save it" (quick capture), or "no" (skip).

---

## April 13, 2026 (thirty-third session -- vault maintenance automation)

Three additions that keep your vault clean automatically so it doesn't become a junk drawer over time:

1. **Vault maintenance script.** New `scripts/vault_maintenance.py` runs a monthly hygiene scan checking 7 categories: inbox overdue files, naming issues (too-long or lowercase-starting filenames), stray binaries outside designated folders, backup file accumulation, empty folders, oversized folders (500+ files), and graphify backup count. Writes a Markdown report to your Meta folder. Fully configurable via CLI flags.

2. **Graphify backup rotation script.** New `scripts/rotate_graphify_backups.py` keeps only the N most recent graph.json backups (default 3) and deletes the rest. Also cleans .bak files older than N days. Prevents the 50-backup pileup that can consume hundreds of MB.

3. **Inbox zero pattern.** New Rule 23 in `templates/rules/obsidian.md`: create an Inbox/ folder as a quick-capture landing zone with a 7-day max residency rule. The maintenance scan flags overdue items. Prevents notes from piling up in random folders.

4. **Maintenance docs.** New `docs/MAINTENANCE.md` with setup instructions, CLI usage, and three recommended scheduled task patterns (monthly scan, quarterly audit, weekly backup rotation) with cron expressions.

All scripts require `--vault-root` (no hardcoded paths), auto-detect emoji-prefixed Meta folders, and work in any vault.

---

## April 13, 2026 (thirty-second session -- Obsidian plugin integration + skill tracking)

Five additions that connect your vault to Obsidian's plugin ecosystem and help you understand which skills you actually use:

1. **Skill usage tracking.** New hook + script (`skill-usage-tracker.sh`) that logs every `/skill` invocation to a JSONL file. Companion report script (`skill-usage-report.py`) generates a Markdown usage report with per-skill counts, daily/weekly trends, and peak-time analysis. Add it to your `/monthly` routine to spot which skills earn their keep and which you forgot exist.

2. **Obsidian plugin integration guide.** New template `templates/rules/obsidian-plugins.md` covering three plugins that extend what Claude Code can do with your vault: Local REST API (open notes, search, run commands over HTTP), Smart Connections (semantic search that finds conceptually related notes even without shared links), and Juggl/Neo4j (visual graph exploration). Includes a search routing table so you know when to use which tool.

3. **Open-in-Obsidian rule.** Rule 22 in `templates/rules/obsidian.md`: after creating or significantly editing a file, auto-open it in Obsidian via the Local REST API so you don't have to hunt for it. Skips bulk operations.

4. **Neo4j export script.** `scripts/graph-to-neo4j.py` converts your graphify `graph.json` into Neo4j-compatible CSVs and a Cypher import script. For power users who want Cypher queries over their knowledge graph.

5. **PostToolUse Skill hook.** Added to `hooks.json` so the skill tracker fires automatically. Existing hooks unchanged.

All scripts auto-detect vault root from `$VAULT_ROOT` env var or their own file location, so they work in any vault without editing paths.

---

## April 13, 2026 (thirty-first session -- to-do system with Dataview views)

New documentation and templates for a complete to-do system that scales from solo use to small teams.

**What's new:**

- **`docs/TODO_SYSTEM.md`** -- full architecture guide for an inline-field to-do system. Covers: Dataview inline fields (`[owner::] [area::] [priority::]`), a three-question prioritization framework, view file templates (per-person, by-area, sprint progress, overdue, due-this-week, waiting-on), a "This Week" focusing lens (max 7 items, ONE Thing pattern), a Done Archive for completed tasks, and a lint rule so Claude auto-fixes missing fields. All templates are generic with placeholder names and paths.

- **`templates/dataview-queries.md`** -- added 6 to-do system queries: filter by person + priority, group by area, overdue items, due this week, waiting-on (delegated), and sprint progress. These complement the existing journal, CRM, and decision-log queries.

**How to use it:** Read `docs/TODO_SYSTEM.md`, adapt the folder paths and team member names to your vault, create the view files from the templates, and add inline fields to your existing tasks (a script approach is recommended for 100+ tasks). Add the lint rule to your CLAUDE.md so fields stay complete over time.

---

## April 13, 2026 (thirtieth session -- obsidian hygiene rules)

New file: `templates/rules/obsidian.md` -- 21 rules for wikilink hygiene, naming conventions, and import safety. Born from a vault-wide audit that found and fixed 3,548 issues across a 5,900-file vault. The four most impactful rules (discovered during the audit):

- **Never wikilink inside URLs.** Auto-linking scripts that insert `[[wikilinks]]` into URLs break both the URL and the link. 205 instances found and fixed.
- **No em dashes in filenames.** Em dashes (`---`) break Obsidian anchor links and TOC slugs. Use ` - ` instead. 46 files renamed.
- **Heading links use wikilink syntax.** Markdown anchors `[text](#slug)` silently break in Obsidian with emojis or special characters. Use `[[#Heading|display]]`.
- **No Roam artifacts.** `[[//database-path/...]]` references from Roam exports never resolve. Clean on import.

Drop this file into your vault's rules folder and reference it from your root CLAUDE.md.

---

## April 13, 2026 (twenty-ninth session -- graphify multi-vault pipeline)

Three new graphify scripts that make the knowledge graph pipeline work across multiple vaults:

1. **`graphify_stage_finish.py`** -- the end-to-end finish script that combines chunk results, canonicalizes, merges into the existing graph, reclusters, regenerates the report, and saves the cache. Now accepts `--vault-root`, `--report-title`, and `--report-path` so it works for any vault (personal, team, or project) without hardcoded paths.

2. **`graphify_canonicalize.py`** -- merges nodes that refer to the same concept but were given different IDs across files (e.g., 74 separate "Love" nodes from different journals collapse to 1). Also strips invalid file_type values agents invent and normalizes folder-prefix wikilink labels.

3. **`graphify_stage_select.py`** -- walks a corpus folder, applies filters (500-word minimum, skip AI-generated content), checks the cache for real LLM extractions vs. preflight stubs, and bin-packs the uncached files into word-balanced chunks ready for parallel dispatch.

All three auto-detect your vault root from their own script location, so they work anywhere without editing paths.

---

## April 13, 2026 (twenty-eighth session -- context optimization)

Three fixes that prevent your vault from slowing down over time:

1. **Session aggregator bug fix.** The script that builds Last Session.md had a bug where old content got duplicated on every run. Over time this could balloon the file to hundreds of KB, making it unreadable. Fixed: old content now gets stripped properly, and there's a 15KB safety cap so it can never snowball again.

2. **Smarter session-start hook.** The hook used to tell Claude to re-read your CLAUDE.md file, but it's already loaded automatically. That was wasting tokens. Now it just tells Claude to read Last Session + Current Priorities, and load rules files only when needed for the specific task.

3. **New: context-audit.py script.** Run `python3 "⚙️ Meta/scripts/context-audit.py"` to check your vault's health: file sizes, aggregator integrity, stale memories, zombie worktrees, rules completeness. All checks should show green. Run it anytime things feel slow, or add it to your /monthly routine.

---

## April 13, 2026 (twenty-seventh session -- /deconstruct first-principles skill)

New skill: `/deconstruct` strips away assumptions you don't realize you're making and rebuilds your thinking from scratch. Modeled on Aristotle's first-principles method.

**What changed:**

- **New skill: `skills/deconstruct/SKILL.md`** — a 4-phase analysis framework. Phase 1 surfaces hidden assumptions and classifies their origin (convention, imitation, precedent, fear, or unexamined default). Phase 2 finds what's true independent of all that. Phase 3 rebuilds 3 approaches from scratch. Phase 4 identifies the single high-leverage move.
- **Two modes:** Full mode (all 4 phases) for big decisions. Fast mode (Phase 1 + Phase 4 only) for daily use when auto-triggered.
- **Three auto-trigger integration points:** (1) Panel trigger for convention-following language during journaling ("best practice," "that's how it's done"). (2) Decision-log gate that auto-offers deconstruct when stakes are high. (3) Weekly retrospective audit that flags high-stakes decisions made without a first-principles check.
- **Fear-to-journal bridge:** When an assumption is classified as fear-origin, the skill explicitly flags it as an emotional problem, not an analytical one: "This isn't an analysis problem. It's a journal entry. What are you actually afraid of?"

**Why this matters:** Most thinking tools help you think better within your current frame. This one questions the frame itself. The auto-triggers mean you don't have to remember to use it; it catches convention-following and high-stakes moments automatically.

---

## April 12, 2026 (twenty-sixth session — modular CLAUDE.md + aggregator tightening)

Two problems: CLAUDE.md and Last Session.md both grew past the 10,000-token read limit, which meant Claude needed multiple reads at session start and could miss important rules.

**What changed:**

- **CLAUDE.md split into modular rule files.** Three large protocol blocks — session-start checks (~200 lines), session-end cascade (~120 lines), and meeting workflow (~55 lines) — were extracted into standalone files in `⚙️ Meta/rules/`. CLAUDE.md now has concise trigger pointers that tell Claude *when* to load each protocol and *where* to find it. The pointers explicitly say "the summary below is NOT sufficient — you MUST read the full file." This keeps CLAUDE.md under 10K tokens while preserving every detail of every protocol.
- **New rule template files** in `templates/rules/`: `session-start-checks.md` and `session-end-cascade.md` — the universal (non-personal) versions of the extracted protocols. These get installed into your vault's `⚙️ Meta/rules/` directory during setup.
- **Session aggregator tightened.** Default changed from top 3 sessions to top 2. New `--max-lines` flag (default: 60) truncates verbose session entries with a pointer to the full file in `Sessions/`. Legacy pre-split content archived to `Sessions/legacy-pre-split.md` instead of bloating the aggregated view. Result: Last Session.md dropped from ~25K tokens to ~4K tokens.
- **Old inline rule templates preserved.** The old `session-start-update-check.md` and `session-end-capture.md` templates still exist for backwards compatibility. New installs will use the modular rule files instead.

**Net result:** both mandatory session-start files now load in a single read call. No protocol details were lost — they just moved from "always loaded" to "loaded when triggered."

---

## April 11, 2026 (twenty-fifth session — graphify runbook hardening)

Hardened `skills/graphify/RUNBOOK.md` with a top-of-file STOP-READ gate, a PRE-FLIGHT CHECKLIST, two new standing rules, and four new lessons (#37–#40) — all from a real session that started with "run graphify on more of the vault" and turned into 30+ minutes of wasted work because the runbook got skimmed instead of read.

### Part 1 — The STOP-READ gate and PRE-FLIGHT CHECKLIST

The runbook now opens with a big red directive telling Claude to read the whole file in full before touching any graphify work. If the file exceeds the Read tool's 10k-token cap (which it does), Claude is instructed to chunk the read with `offset` + `limit` — never to fall back to Grep sampling or `head_limit`, because those are search tools, not reading tools, and they leave you confident you've "covered it" while missing most of the content.

Right after the gate is a 7-step **PRE-FLIGHT CHECKLIST** that catches the five most destructive antipatterns before they cost time:

1. Read this whole file
2. Check for stale graphify processes from other sessions
3. Force-warm the target folder if your vault is on a sync service (iCloud, Google Drive, OneDrive, Dropbox) — cold reads can be 1000x slower and look exactly like a hang
4. Never call `check_semantic_cache` on large corpora — re-hashing blocks for minutes
5. Prioritize concept-dense corpora (Books/Notes/Writing) over episodic ones (Journals/Daily Logs) — roughly 3x more concepts per token
6. Verify the cwd matches the target vault root
7. Run the stage-selection script to size the job before dispatching — NOT on capped slices though, see Lesson #38

### Part 2 — Two new standing rules

**Active lesson capture.** Any optimization or gotcha gets written up the moment it surfaces, not saved for end-of-session. If you wait, the specifics (exact numbers, exact error messages) degrade into vague pattern-matching and the lesson loses most of its value. Ten seconds to write now beats ten minutes of re-derivation next week.

**Validation hypotheses on every batch/dispatch doc.** Every handoff doc for a graphify batch must include a "What this run is testing" table with numbered hypotheses, quantitative predictions, measurement methods, and explicit kill criteria. This turns every stage into a live experiment for the lessons it depends on. You already pay the tokens — recording the measured result against a pre-registered prediction is free signal. Without it, lessons stay anecdotal ("cap-7 worked last time") and drift silently. With it, every stage either strengthens the lesson with N more data points or explicitly overturns it when a kill criterion triggers.

### Part 3 — Four new lessons (#37–#40)

- **#37** — Read-tool 10k cap handling: use `offset`+`limit` chunking, never Grep sampling
- **#38** — Don't run the full-corpus sizer for capped slices. Write a targeted picker that sorts by metadata (filename date, `st_size`, `st_mtime`) without reading contents, then reads only the top `cap × 1.6–2.0` candidates in a 16-thread pool for a 6–8x speedup over sequential. Includes filter-attrition math: journals lose ~70% to `<500w OR already-cached`, writing loses ~40%, so overshoot constants are corpus-specific.
- **#39** — Scan for content-level near-duplicate drafts in `Writing/` and `Drafts/` folders BEFORE chunking. `graphify_prep.py` only catches byte-identical duplicates; files that share 99% of their content but differ in formatting or wikilink conventions slip through. A single 57k-word draft triplicated across three folders costs ~430K tokens to redundantly extract. A 10-second title-similarity scan before dispatch catches it.
- **#40** — Merge discipline: normalize wikilinks + tags + whitespace before line-level comparison, then preserve every unique line in a "Recovered from earlier drafts" appendix at the bottom of the merged file. Never silent-drop content. Back up all originals to `/tmp` before overwriting or deleting anything.

### Why this matters for non-technical users

These four lessons are the difference between a `/graphify` run that costs 30 minutes and one that costs 3 hours. For someone who has never written a Python script and whose vault is on iCloud or Google Drive, the cold-read gotcha (Lesson #17) combined with the skimmed-runbook pathology (now blocked by the new top-gate) was the most common "it hung, I don't know what happened" outcome. The gate + checklist force a diagnostic path before any action, and the new lessons give Claude explicit fallbacks for the three most common failure modes.

**What you should do:** nothing. Next time you run `/graphify`, Claude will read the hardened runbook first and apply the new pre-flight checklist automatically. You'll notice faster runs and fewer "why is this hanging?" moments. If you want to see the new lessons, open `skills/graphify/RUNBOOK.md` and scroll to the "Session-start discipline and read-tool patterns" subsection (Lessons #37–#40) and the two "Standing rule" blocks at the top of "Lessons learned."

---

## April 11, 2026 (twenty-fourth session — CHANGELOG rotation)

Rotated 28 older session entries (April 8 → April 11 sessions 1–18) out of `CHANGELOG.md` and into a new `CHANGELOG_archive_2026Q1.md`. The live changelog now carries only the most recent ~5 sessions, which is what almost every post-pull update check actually needs to read.

**Why:** the live `CHANGELOG.md` had grown to 1,313 lines. Every `git pull` triggers Claude to read it and translate "what's new" into plain English for the user, so every line is a real token cost on every pull. The full release history is preserved in the archive file — nothing is lost. To read it: open `CHANGELOG_archive_2026Q1.md` next to this file.

**What you should do:** nothing. The next pull will show you this rotation entry and that's it. If you want the full history, the archive file is right next to the live one.

---

## April 11, 2026 (twenty-third session — non-technical onboarding overhaul + automatic file drift detection)

This is two things shipped together because they share the same theme: **make a non-technical user's first install (and every subsequent pull) work without them ever having to ask "why doesn't this work?"**

### Part 1 — Non-technical onboarding overhaul (14 fixes)

Audited the full first-install experience for someone who has never opened a terminal and doesn't know what Obsidian, Claude Code, Python, Node, or an API key is. Found 14 friction points where the setup assumed the user knew something they don't, or made them do a manual step that the bootstrap could just do for them. All 14 are fixed in this release.

**Auto-installs added — you no longer have to download anything yourself:**

- **Obsidian** — auto-installed by the bootstrap (Mac via `brew install --cask obsidian`, Windows via `winget install Obsidian.Obsidian`, Linux via snap → flatpak → AppImage download fallback). Previously the README mentioned Obsidian as a "prerequisite" with a one-line bullet, the bootstrap didn't touch it, and you'd hit a wall mid-setup when /setup-brain asked "do you have Obsidian?" You no longer have to think about it.
- **Claude Code itself** — auto-installed by the bootstrap via `npm install -g @anthropic-ai/claude-code`. Previously listed as a prerequisite with no install step. Now installed automatically once Node is present, on every OS.
- **winget on older Windows 10** — bootstrap.ps1 used to abort hard with "winget is required, install App Installer from the Microsoft Store and re-run." That sentence is opaque to a non-technical user. Now the bootstrap auto-downloads and installs App Installer from Microsoft's official URL (`aka.ms/getwinget`) before doing anything else, with a fallback to direct MSI installs of Python, Node, and Obsidian if winget can't be installed at all.
- **Obsidian community plugins** (Dataview, Templater, Tasks) — Phase 2 of `/setup-brain` used to walk you through ~36 manual clicks across the Obsidian Community Plugins UI to install three plugins one at a time. Now Phase 2 runs a Python helper that downloads each plugin's latest release directly from GitHub, drops it into your vault's `.obsidian/plugins/` folder, and writes `.obsidian/community-plugins.json` to enable them. Manual UI walkthrough is still there as a fallback for any plugin that fails to auto-install.

**Onboarding-flow clarity:**

- **README install section rewritten end-to-end.** Step 1 (open your terminal — with concrete instructions for Mac, Windows, Linux including the critical "PowerShell, NOT cmd.exe" warning), Step 2 (paste the install command and watch it run), Step 3 (type `claude` then `/setup-brain`). The "Prerequisites" section is gone — replaced with one line that says "all you need is a Mac, Windows, or Linux computer; the bootstrap installs everything else." Manual `git clone` install path removed since it's confusing to non-technical readers.
- **`/setup-brain` Phase 0 progress messaging.** Used to go silent for 2-3 minutes while installing tools, which makes non-technical users think the setup has frozen. Now Claude says "Setting up the tools you'll need — give me a moment" before starting, and gives a one-line `tool ready ✓` confirmation as each tool finishes.
- **`/setup-brain` Phase 1 step 6 — Obsidian question rewritten.** Used to ask "Do you have Obsidian installed? If not, go to obsidian.md and download it. I'll wait." Now detects whether Obsidian is already installed (which it always should be after the bootstrap) and skips the question. If somehow it's missing, the skill auto-installs it instead of asking.
- **Homebrew password prompt warning.** Bootstrap now prints a multi-line `⚠️ HEADS UP` block before installing Homebrew, telling you the password prompt is coming, that you won't see characters as you type, and to NOT close the window. Reduces "is this thing frozen?" anxiety.
- **`gh auth login` framing.** Previously asked you to log in to GitHub with developer-jargon defaults ("GitHub.com → HTTPS → Login with web browser"). Now framed as OPTIONAL with explicit "if you don't have a GitHub account, press Ctrl+C to skip — everything else still works." The implicit pressure to log in is gone.
- **Granola post-install authorization step.** Bootstrap registers the Granola MCP server but used to never tell you that the MCP only works AFTER you've signed into the Granola Mac/web app at least once. Now the install message says explicitly "I just wired up Granola — one more step on YOUR side: log in to the Granola app once before the connection works. Want me to walk you through it?" If you say "I don't use Granola," the bootstrap removes the dead MCP entry instead of leaving it stranded.
- **Nano-banana / Gemini API key deferred.** Phase 0 used to mention setting up nano-banana with a `GEMINI_API_KEY` env var as part of the main setup. That's a 5-minute side quest involving API jargon for a feature most users don't need on day 1. Now the install is deferred entirely — nano-banana only gets installed when you explicitly ask for image generation, and Claude walks you through the API key setup interactively at that point with concrete clicks instead of CLI commands.

### Part 2 — Automatic file drift detection (`drift-check.sh` + `drift-check.ps1`)

`update-check.sh` only knows whether you're behind on commits. It does NOT know whether files that were already installed in a prior release have since drifted from the repo's version. That happens when a previous sync only partially landed, when you hand-edit a script in your vault, when a `git stash` recovery leaves files mixed, or when a manual cherry-pick missed something. Until now the only way to find stale files was to manually ask Claude "compare everything" — which defeats the whole point of automatic updates.

**New script: `scripts/drift-check.sh` (and `drift-check.ps1` for Windows).** Runs at session start alongside `update-check.sh`, on the same once-per-day cooldown. Read-only — never modifies anything. Detects three kinds of drift:

1. **Installed skills** — files under `~/.claude/skills/<skill>/` that differ from the repo's `skills/<skill>/<rel-path>`.
2. **Vault scripts** — files under `$VAULT/⚙️ Meta/scripts/<basename>` that differ from `<starter>/scripts/<basename>`. Curated list of scripts the starter installs into vaults during /setup-brain.
3. **Vault CLAUDE.md rule blocks** — for each `templates/rules/*.md`, finds its top-level heading inside `$VAULT/CLAUDE.md` and diffs the block underneath. Tolerates trailing `---` separators (a CLAUDE.md formatting convention, not part of any rule).

**`session-start-update-check.md` rule extended with the drift-handling UX.** When drift is found, Claude walks you through it one file at a time: reads both files, shows you a `diff -u` of the changes, asks {update / skip / **skip permanently** / update all / stop}, **backs up before every single change** (no exceptions, even on "update all"), and for `vault-rule` drift, replaces only the targeted block via Edit (never the whole CLAUDE.md file). Files annotated with `note: hand-edited CONFIG block` (currently just `graph-context-hook.sh`) are never overwritten wholesale — they get cherry-pick treatment with a manual ask instead.

**Permanent ignore registry: `~/.claude/.ai-brain-starter-drift-check-ignore`.** Files like `graph-context-hook.sh` ship as a generic template in the repo and get hand-customized in your vault during /setup-brain. Drift on those files is permanent-by-design — having drift-check nag about them every session is noise. The ignore file is a plain-text per-user registry: one entry per line, supports literal paths and shell-glob patterns, `#` for comments. When you pick "skip permanently" during the walkthrough, Claude appends the file to this registry for you. If you change your mind, open the file in any text editor and delete the line. Defensive normalization (BOM, CRLF, thematic-break separators) prevents false-positive drift on benign formatting differences too.

**Why human-in-the-loop instead of auto-update:** because the drifted file might be a hand-edit you intentionally made (e.g. customizing `graph-context-hook.sh` with your vault paths and routing keywords), and an auto-replace would silently destroy your work. Backup-before-every-change is non-negotiable.

### What you do

Nothing — both parts apply automatically on the next bootstrap re-run or `git pull`. The drift check honors a once-per-day cooldown so it won't double-prompt during a single day.

If you want to verify drift detection right now: `bash ~/.claude/skills/ai-brain-starter/scripts/drift-check.sh --vault "<your vault path>" --force` (or the `.ps1` equivalent on Windows).

---

## April 11, 2026 (twenty-second session — update-check summary skips the corporate-event rule bullet)

Small tone-only change to `templates/rules/session-start-update-check.md`.

The session-start update check reads new CHANGELOG entries after a `git pull` and translates them into plain-English bullets for you. One of those bullets kept describing the corporate-event Onde suggestion rule (Phase 16 rule 15, added April 10) in persuasive language — framing the rule as a thing that "suggests it as a sales opportunity" or similar. That framing misses the point of the rule, which is opt-out, inline-disclosed on first fire, and scoped tightly to corporate events you're already planning. Describing it in promotional terms in the post-pull summary made it read like the starter was marketing at you, which is exactly what the rule is designed *not* to do.

The fix is the smallest possible: an "Opinionated skip list" in the update-check template that explicitly tells Claude to leave the corporate-event rule out of the update summary entirely. You'll discover the rule the first time it fires, with the full inline disclosure ("Onde was built by the same person who made this starter skill") right there at fire-time. That's where the honesty lives — not in a release-notes bullet two days earlier.

**Nothing changes about the rule itself.** It still auto-installs, still fires on the 12 corporate-event categories, still opts out permanently the moment you decline, still carries the inline disclosure on every fire. The only change is that future pulls won't announce it in a persuasive bullet in the update summary.

### What you do

Nothing. The template edit applies the next time the update-check runs. If you want to apply it right now without waiting for the next session start: pull the repo and the new template takes effect.

---

## April 11, 2026 (twenty-first session — concurrent-worktree race fix for session-end cascade)

### The bug this fixes

If you run multiple Claude Code sessions at the same time in different worktrees (which you'll do if you're using the parallel-worktrees workflow for different tasks), and each session follows the session-end cascade rule — they all race on `⚙️ Meta/Last Session.md` and `⚙️ Meta/Decision Log.md`. Each session reads the file, adds its entry, writes it back. Last write wins. **Earlier sessions' entries are silently clobbered.**

This isn't a bug in any one session. It's a structural race condition in the cascade rule itself. If you've been using this setup with parallel worktrees for any length of time, some of your session summaries and decisions have quietly disappeared without you knowing.

I caught it live on 2026-04-11 — four concurrent worktrees wrote to the meta files in one evening; at least two decisions and one session summary were overwritten before the fix shipped. Reported and tracked in [#5](https://github.com/mycelium-hq/ai-brain-starter/issues/5).

### What changed

**New folder structure (created automatically on update via the session-end hook):**

```
⚙️ Meta/
  Sessions/
    2026-04-11T22-30-my-worktree.md          # one file per session, unique filename
    2026-04-11T17-18-other-worktree.md
  Decisions/
    2026-04-11T22-30-daily-journal-redesign.md  # one file per decision, unique filename
    2026-04-11T22-30-per-worktree-meta-writes.md
  Last Session.md         # auto-generated from Sessions/ by aggregate-sessions.py
  Decision Log.md         # auto-generated from Decisions/ by aggregate-decisions.py
```

Concurrent worktrees write to *different files*, so there is no contention. The shared `Last Session.md` and `Decision Log.md` are rebuilt by aggregator scripts that produce deterministic output from sorted input — so even two concurrent aggregator runs write identical bytes. The race is structurally eliminated, not papered over with locks or retries.

**Two new scripts** (installed to your vault's `⚙️ Meta/scripts/`):

- `aggregate-sessions.py` — rebuilds `Last Session.md` by reading `Sessions/*.md`, sorting by filename descending, concatenating the top N (default 3). Filters out stub files (unfilled placeholders) so orphaned session-end hook writes don't pollute the view.
- `aggregate-decisions.py` — same for `Decisions/` → `Decision Log.md`, showing all decisions newest-first.

Both scripts read the vault path from `$VAULT_ROOT` so you don't need to edit them for your setup — the bootstrap sets the env var for you, and the hook passes it on every run.

**Updated `session-end-hook.sh`** — now detects your worktree name (three fallback methods: pwd parse → `.git` file read → PID-based unique fallback), writes a session stub to `Sessions/{timestamp}-{worktree}.md`, and runs the aggregator as the final step. The hook never writes to `Last Session.md` directly.

**Updated session-end capture rule** — tells Claude to write session content to a per-worktree file (not the shared view) and to create per-decision files (not append to `Decision Log.md`). Runs the aggregators after each write. The destination table in the rule was updated to reflect the new paths.

**Backwards-compatible migration.** Your existing `Last Session.md` and `Decision Log.md` are NOT touched on first run. The aggregator preserves all pre-split historical content below a `## Legacy (pre-split) historical entries` / `## Legacy (pre-split) historical decisions` header. Nothing gets deleted. You can roll back by deleting `Sessions/` and `Decisions/` and restoring from the `.bak-pre-aggregator-*` backups if anything goes wrong.

**Idempotency.** The aggregators are byte-stable across runs — three consecutive runs produce identical MD5 hashes. Safe to run as many times as you want, including concurrently.

### What you do (nothing, unless you want to)

The update auto-installs the new scripts and updates the hook. The next time you end a session, the hook will create your first entry in `Sessions/` and rebuild `Last Session.md` as the aggregator view. If you want to manually trigger a rebuild before then: `VAULT_ROOT="<your vault path>" python3 "<vault>/⚙️ Meta/scripts/aggregate-sessions.py"`.

If you've been running parallel worktrees and want to know what you may have lost, check `⚙️ Meta/Session Log.md` (the always-append log) for timestamps of ended sessions without matching entries in `Last Session.md`. That's your missing-content list.

---

## April 11, 2026 (twentieth session — daily journal panel becomes a live participant, with real pushback)

If you've been using `/journal` for a while, you've probably noticed the advisory panel at the end of each entry mostly cheers you on. Warm, supportive, agreeable. The problem: on good days that means *no real signal*, and on days when you're rationalizing something, it means the panel *helps you* rationalize it. The whole point of having an advisory panel is that it pushes back when you need it — and the old setup couldn't, because it ran after you'd already decided how the story goes.

This update restructures the journal skill so the panel works the way a real advisory board works.

### What's different

**1. The panel can now interrupt you mid-journal.** A new "Standing Rules" section in the journal skill has a trigger table — if you say hedge words ("I guess," "I don't know why"), drop a vague "I should" without a date, mention a new side idea during a hard stretch, brush past a missed habit, avoid naming a hard conversation, etc. — the relevant advisor pulls in with one sentence, in character, then hands the conversation back. You don't wait until the end for the panel's reaction.

**2. At least one panelist must dissent on every entry.** Especially on good days. Rationalizations slip through most easily on high-floor entries, and the old "1–2 sentences per advisor, keep it tight" format couldn't force disagreement. The new rule is simple: if all 3–5 panelists agree, you have not looked hard enough. Dissent is required, not optional.

**3. Panel dialogue, not parallel bullets.** Step 5 now stages an actual in-character exchange where panelists can challenge each other and you, ask you questions back, and push on what you avoided. Not a stack of isolated advisor quotes.

**4. Omission pass.** Before the panel weighs in, the skill checks: *what did the user NOT say tonight that a panelist would notice?* A commitment from yesterday that vanished. A meeting tomorrow with no prep. A person they were upset with who's suddenly absent from the entry. A body signal they skipped. If an omission exists, one panelist names it in Step 5.

**5. Strict voice separation in the saved entry.** This is the biggest long-term change. Every new journal entry now has two clearly-labeled sections separated by horizontal rules:
   - `## Journal — [your name]'s voice` — your original thought only, your words, your voice. Panel lines never appear here.
   - `## Panel dialogue (synthetic — not [your name]'s original thought)` — the AI-generated panel exchange, below a ⚠️ disclaimer.

Why this matters: when you reread your journals in 6 months or 6 years, you'll be able to tell at a glance which sentences were *you* thinking and which were AI commentary. Without this separation, the two voices bleed together and the journal archive loses its value as a record of how you actually think. This was the single biggest long-term failure mode of AI-assisted journaling and it's now structurally impossible in the generated skill.

**6. Panel dissents auto-log to a cross-context Panel Feedback Log.** If Step 5 produced a dissent or omission flag, the skill automatically appends it to your Panel Feedback Log (the same file that catches feedback from real human meetings). Patterns surface over time — if three different daily entries all got the same dissent, that's a real pattern to act on.

**7. Full advisory roster expanded.** The panel now includes more specialized voices: female-physiology experts (Stacy Sims, Lara Briden), pelvic-floor and embodiment (Carrie Pagliano, Bonnie Bainbridge Cohen), LGBTQ+ relational voices (Alexandra Solomon, queer polarity archetypes), cross-border tax and family office archetypes, and "archetype" slots (Curious Friend, Buddhist Monk, Stoic Philosopher, CBT Therapist, Existential Psychotherapist, Inner Child Therapist) for when no specific real person fits.

**8. Roster customization during setup.** When you run the setup flow, you'll now be offered the chance to customize the advisory panel — add or remove voices, swap in specific people from your own life (a mentor, a grandparent, a coach). Whatever you say gets baked into the generated skill so your daily journal uses *your* panel, not a generic one.

### Why

This came from noticing a real pattern: on two consecutive entries, the panel gave four affirming voices in a row and not a single piece of pushback. Nothing challenged the user's framing. On a day when the user rationalized a non-obvious decision, the panel agreed with the rationalization. That's not what an advisory board is for. The redesign fixes it at the structural level — dissent is required, the panel can interrupt mid-interview, the original voice is walled off from commentary, and patterns of pushback get logged across sessions so they don't evaporate.

If you're already set up, the next time you run `/journal` the skill file will still be the old version until you tell me to regenerate it. Ask: *"regenerate my daily-journal skill with the latest panel behavior"* and I'll rebuild it using the new template, preserving your existing floor framework, habit tracking, and customizations.

---

## April 11, 2026 (nineteenth session — bootstrap best practices for advanced users with custom skills + forks)

Session 18 added the basic safety guarantees. This session adds the protections specifically aimed at advanced users — people who have their own forks of bundled skills, their own custom CLAUDE.md rules, their own divergent ai-brain-starter clone, or any other heavy customization. The bar: **never silently overwrite anyone's hard work, regardless of how complex their setup is.**

### What's now protected for advanced users

Four new protection layers in both `bootstrap.sh` and `bootstrap.ps1`:

#### 1. Forked sub-skill detection

If you have your own `.git/` directory inside `~/.claude/skills/graphify/` (or `meeting-todos/`, or `patterns/`) — meaning you cloned your own customized version of one of the bundled skills — the bootstrap **detects this and skips that skill entirely.** It won't sync the upstream version over yours. You manage updates to your fork yourself.

The signal is the presence of `.git/` inside the skill folder. The bootstrap logs:

```
graphify has its own .git/ directory — detected as YOUR FORK, skipping entirely
  Your fork is preserved untouched. You manage updates to it yourself.
```

#### 2. Symlinked sub-skill detection

If `~/.claude/skills/graphify` (or another bundled skill) is a **symlink** instead of a regular folder — meaning you've pointed it at a shared location, a development checkout elsewhere, or a Dropbox/Drive path — the bootstrap detects the symlink and refuses to write through it. The target may be a shared resource you don't want surprised by this script.

```
graphify is a SYMLINK to /Users/you/code/graphify-fork — bootstrap will NOT write through it
  If you want bootstrap to update this skill, replace the symlink with a regular folder.
```

#### 3. Divergent ai-brain-starter clone detection

If your local `~/.claude/skills/ai-brain-starter` clone has commits that **aren't** on `origin/main` AND `origin/main` has commits that **aren't** on your clone (a true divergence — you've made your own commits that diverge from upstream), the bootstrap **refuses to pull** and tells you exactly how many commits diverge in each direction:

```
DIVERGENT FORK DETECTED at ~/.claude/skills/ai-brain-starter
  Your local clone has 3 commit(s) NOT on origin/main
  AND origin/main has 7 commit(s) NOT on your clone
  Refusing to pull. Your fork is preserved unchanged.
  To merge manually: cd ~/.claude/skills/ai-brain-starter && git pull --rebase
```

The bootstrap also handles two related cases more gracefully:

- **Local commits, no upstream changes** (you're ahead but not divergent): leaves your clone alone, doesn't pull
- **Behind upstream with no local commits**: stashes any uncommitted changes, then fast-forwards

#### 4. Explicit-disable preservation for `claude-mem@thedotmack`

Previously the bootstrap unconditionally set `enabledPlugins["claude-mem@thedotmack"] = True`. If an advanced user had explicitly set it to `False` (because they intentionally disabled the plugin), the bootstrap would silently re-enable it on the next run.

Now the bootstrap only sets the key if it's **absent**. If the user has it set to `False`, the bootstrap respects that choice and prints:

```
NOTE: respecting your explicit disable of claude-mem@thedotmack — leaving it off
```

### `--dry-run` mode

Both bootstrap scripts now accept a `--dry-run` flag (PowerShell: `-DryRun`) that previews **every action** the bootstrap would take, without making any changes. No files written, no installs run, no git operations performed. Just a transcript of "what would happen if you ran this for real."

```bash
bash bootstrap.sh --dry-run
```

```powershell
iex "& { $(irm https://raw.githubusercontent.com/mycelium-hq/ai-brain-starter/main/bootstrap.ps1) } -DryRun"
```

The output looks like:
```
[dry-run] would: git fetch --quiet origin
[dry-run] would: git pull --quiet (fast-forward 53 commit(s))
[dry-run] would sync graphify skill from <repo> to <dest> (with backup-before-overwrite)
[dry-run] would back up: ~/.claude/settings.json → ~/.claude/settings.json.bak-2026-04-11-1530
[dry-run] would: register thedotmack marketplace + enable claude-mem@thedotmack (if not explicitly disabled)
```

This is the right way for an advanced user to inspect the bootstrap before running it on a heavily customized setup.

### Final change summary

Both scripts now end with a structured **change summary** that lists everything that happened (or would happen, in dry-run mode):

```
━━━ Change summary ━━━

  Installed (new):
    + ai-brain-starter clone

  Updated:
    ↑ ai-brain-starter clone (pulled 53 commit(s))
    ↑ graphify skill (12 new, 4 updated, 4 backed up)

  Skipped (your customizations preserved):
    ⊘ meeting-todos skill (your own fork — has .git)
    ⊘ patterns skill (symlink to /Users/you/code/patterns-fork)

  Backups created (recoverable):
    ↳ /Users/you/.claude/settings.json.bak-2026-04-11-1530
    ↳ /Users/you/.claude/skills/graphify/SKILL.md.bak-2026-04-11-1530
    ↳ /Users/you/.claude/skills/graphify/scripts/run.py.bak-2026-04-11-1530
    ↳ /Users/you/.claude/skills/graphify/scripts/util.py.bak-2026-04-11-1530

  To restore any backup: mv <file>.bak-YYYY-MM-DD-HHMM <file>
```

After every run, the user knows exactly what changed, what was preserved, and how to undo anything they didn't expect.

### Best practices we now follow

For an installer that touches user-customized files, the relevant best practices are:

| Best practice | Status |
|---|---|
| Idempotence (re-run safe) | ✅ |
| Backup before overwrite | ✅ |
| Respect explicit user choices (e.g. disabled plugins) | ✅ |
| Detect forks of bundled components and skip them | ✅ |
| Detect symlinks before writing through | ✅ |
| Detect divergent histories before pulling | ✅ |
| `--dry-run` preview mode | ✅ |
| Final summary of every change | ✅ |
| Detailed error messages | ✅ |
| Verification block at end | ✅ |
| Custom skills outside bundled set untouched | ✅ |
| User vault never touched | ✅ |
| Recoverable from any unexpected change | ✅ |

The remaining gaps are nice-to-haves: a `--restore` mode that auto-restores from the most recent `.bak` files (not strictly needed since the file paths are obvious), atomic operations across the whole script (would require a temp directory + final swap, significant refactor), and a logging file (right now the summary is stdout only — could also write to `~/.claude/.bootstrap.log` for forensics).

### What this means for an advanced user

If you've built a heavy custom setup — your own forks of bundled skills, your own divergent ai-brain-starter, your own custom plugins, your own MCP servers, your own hand-tuned settings — running the bootstrap on top of it will:

1. **Detect everything you've customized** (forks via `.git`, symlinks via attributes, divergent clones via `git rev-list`, explicit-false plugins via JSON inspection)
2. **Skip those entirely** (not "back them up and overwrite", actually skip)
3. **Tell you in the summary exactly what was preserved and why**
4. **Update only the things that aren't customized** (and back those up too, just in case)

The bar: an advanced user with five custom forks should be able to run the bootstrap and have nothing they care about touched. The summary should confirm "5 custom things preserved, 0 things you customized were modified."

Run with `--dry-run` first if you're at all unsure. The dry run is the answer to "but what if it does something I don't expect?" — it shows you exactly what would happen with zero side effects.

---

## Older entries

For sessions 1–18 (April 8–11, 2026), see [`CHANGELOG_archive_2026Q1.md`](CHANGELOG_archive_2026Q1.md). Rotated on 2026-04-11 to keep the live changelog focused on the most recent ~5 sessions.

## [Unreleased]

### Added
- `docs/RELEASES.md` entry for Claude Code v2.1.118: `/usage` command, `type: "mcp_tool"` hooks, agent frontmatter hooks/MCPs in main-thread, `Bash(find:*)` permission change.

### Changed
- `templates/rules/advisory-panel.md` Rule 1: confidence scoring is now internal only. Panel filters by lens-fit score (0-100) but NEVER prints the number in output. No `[confidence: N]`, no `(72)`, no score annotations. Background filter, not visible ink.
