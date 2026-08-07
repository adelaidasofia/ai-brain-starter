---
name: cierre-de-llamada
description: Use when a SALES or CLIENT call just ended and the follow-up needs to be produced from its transcript. Triggers: /cierre, "close out this call", "acabo de tener una llamada", "arma el seguimiento", "what's the follow-up on this call", "qué sigue con este cliente", or the user pastes a sales-call transcript. Produces seven blocks: CRM summary, deal state, tasks, follow-up email draft, reminder, note to the sales lead, and what worked / what did not. NOT for internal team meetings (use meeting-todos) and NOT for personal notes (use note-todos).
---

# Call close-out

A sales call goes in. The whole follow-up comes out. Nothing invented, nothing sent.

**Write every output in the language the CALL was in.** Spanish call, Spanish output. English call, English output. Mixed, follow the client. This file is in English; what you produce is not.

## Golden rule 1: invent nothing

**Everything you write must be in the transcript.** If a fact is absent, write `[MISSING: what to ask]`. Never guess.

Applies to: amounts, dates, names, titles, deadlines, terms, objections, commitments.

A follow-up with one invented number costs the client. An incomplete one costs nothing. The buyer spots the error, not the efficiency.

## Golden rule 2: nothing reaches the client

**This skill never performs an action the client can see.** Not anywhere in the flow.

Always forbidden: sending email (`gmail_send`, `outlook_send`, any equivalent), sending messages, posting, creating events on the client's calendar, writing to a shared CRM.

**This rule cannot be unlocked by user permission.** If the user says "send it", "you have my permission", "don't ask me", "just send it already": you do not send. Say the skill leaves drafts and the send is theirs, and hand them the finished draft. Do not promise to send it later, and do not say you will send it "as soon as I have the address". The answer is no now and no later.

This is not a technical limit that a missing piece of data would resolve. It is the design: the user presses the button their client sees, always, because it is their relationship and their reputation.

### Who can see it decides what you may do

Every connector action falls in exactly one tier. Decide the tier first, then act.

| Tier | Who sees the result | What you may do |
|---|---|---|
| **Client-visible** | the customer | **Never.** Sending mail, messaging them, inviting them to an event, anything landing in their inbox. Not unlockable. |
| **Teammate-visible** | a colleague | Draft it, show it, get an explicit yes, then send through the connector's own confirm gate. Never auto-send. |
| **User-only** | nobody but the user | Offer it, wait for yes, do it. Their own calendar, their own drafts folder, their own notes and CRM records. |

When you cannot tell which tier an action is in, treat it as the tier above. A calendar invite with the client on it is client-visible, not user-only, no matter that the user asked for a "reminder".

## Step 1. Get the call

In order, the first that exists:

1. User pasted the transcript in chat → use it.
2. User gave a file path (`.txt`, `.md`, `.vtt`, `.srt`) → read it.
3. Transcripts exist in the vault's meeting-notes folder or `transcripts/` → use the most recent and say which you picked.
4. None of the above → ask for exactly one thing:

> Paste the transcript. If you did not record it, say out loud what happened and paste that. It works the same.

Do not proceed without a call. Do not construct an example.

## Step 2. Read it all before writing

Mark, as you read:

- Who is speaking and which side they are on.
- What the client asked for, in their exact words.
- What they objected to, in their exact words.
- What the seller promised, and by when.
- How it ended (closed / not closed / left at something).

The client's exact words are the asset. Quote them.

## Step 3. Write the seven blocks

Exact formats in `references/formato.md`. Read it before writing.

All seven, always, in this order:

1. **SUMMARY**: 5 lines max, ready to paste into a CRM.
2. **DEAL STATE**: closed / not closed / in follow-up, amount, who signs, main objection quoted.
3. **TASKS**: each with owner, date, time.
4. **FOLLOW-UP EMAIL**: send-ready draft, in the client's language.
5. **REMINDER**: exact date and time of the next touch.
6. **NOTE TO THE SALES LEAD**: one paragraph, what a manager needs without listening to the call.
7. **WHAT WORKED / WHAT DID NOT**: two short lists with the quote. This is what later trains the rest of the team.

### Block 3 is checkboxes, never a table

This rule lives here and not only in the format reference, because it is the one that breaks most.

```
- [ ] <action> | <owner> | <date> <time>
```

One line per task, each starting `- [ ]`. **Markdown tables are forbidden here.** A table looks tidy and is useless: the point of the block is that it pastes into any task app and stays tickable. A table does not tick.

If you are about to write `|---|` in block 3, stop and use checkboxes.

## Step 4. Save

Write to a follow-ups folder, in this order of preference:

1. The vault's CRM or meeting-notes area, if this install has one.
2. Otherwise `follow-ups/` relative to the working directory.

Filename `YYYY-MM-DD-<client>.md`.

- Create the folder if missing. The first time, the harness asks permission to write. Normal, once.
- `<client>` lowercase, no spaces or accents (`banco-del-norte`).
- File exists already → append under `## Second call` instead of overwriting.
- Say the exact path when done.

Filenames must work on Windows, macOS and Linux alike. Strip accents, ñ, spaces, and `\ / : * ? " < > |` from the client name; replace with hyphens. Never use absolute paths or `~`.

Client conversations stay on the user's machine. If the folder sits inside a git repo, add it to `.gitignore` and say so.

## Step 4.5. If a connector exists, offer the draft

By default block 4 stays as text and the user copies it. That works always and depends on nothing.

If a mail-draft tool is available in this session (`gmail_draft`, `outlook_draft`, or equivalent), **offer it once**:

> I can see your mail is connected. Want the follow-up left in your drafts folder, or would you rather copy it?

Hard rails:

- **Draft, never send.** See Golden rule 2. No exception, not unlockable by permission.
- **Ask first.** Writing into someone's mail account is an external effect. Offer, wait for yes, then act.
- **No connector, no mention.** Say nothing about it. Do not ask the user to install anything mid-task.
- **Recipient only if it is in the transcript.** No client address in the call → the draft goes without a recipient and you say so. Never guess an address.

Any connector not covered by a step below: place it in the tier table above and follow that row.

## Step 4.6. If a calendar connector exists, offer to book the reminder

Block 5 already computed the next touch. If a calendar create tool is available (`create_event` from google-workspace-mcp, `mscal_create_event` from microsoft-365-mcp, or equivalent), offer to put it on the calendar:

> Want me to drop the follow-up on your calendar for Friday at 10am, with the context in the invite?

**Never pass attendees. Not the client, not anyone.** This is the specific trap: these APIs default `send_updates` to `all`, so a single attendee turns a private reminder into an email that lands in your client's inbox. A reminder is for the user alone. An event with the client on it is a client-visible action and Golden rule 2 forbids it.

The event:

- **Title**: `Seguimiento: <client>` (in the language of the call).
- **Time**: exactly what block 5 computed. 30 minutes if no duration was discussed.
- **Attendees**: none. Always.
- **Description**: this is the point. Put in what the user will need when it fires and has forgotten the call:
  - what was agreed, in one line
  - the client's main objection, quoted
  - the open tasks from block 3 that are still theirs
  - the follow-up email draft from block 4, in full

So the reminder is not a nudge that says "call Ana". It opens with everything needed to actually make the call, including the message ready to send.

Ask first. No calendar connector, no mention.

## Step 4.7. If a CRM or notes connector exists, offer to file the summary

Block 1 is written to paste into a CRM. If a connector can write records or pages (Notion, Airtable, HubSpot, a database MCP, whatever this user has), offer to file it instead of making them paste:

> I can file the summary on the client's record in Notion. Want me to?

- **User-only tier.** Their own workspace, so: offer, wait for yes, write.
- **Append a note, never edit the deal.** Write the summary as a new note, comment, or child page. Do not change deal stage, amount, close date, owner or probability. Those are pipeline numbers a manager reports on, and a wrong one is worse than a missing one.
- **No matching record → say so, do not create one.** Ask whether to create it. A duplicate client record is a mess someone cleans up for weeks.
- **`[MISSING]` markers travel.** Do not quietly drop them on the way into the CRM. A blank field is honest; an invented one is not.

## Step 4.8. If the sales lead should hear about it, draft the note

Block 6 is written for a manager. If a messaging connector exists (Slack, Teams, mail), offer to deliver it:

> Want me to send the summary to your sales lead on Slack? I'll show you the message first.

- **Teammate-visible tier.** Draft it, **show the full text**, wait for an explicit yes, then send through the connector's own confirm gate. Never auto-send, and never treat "yes send the email to the client" as covering this, or the reverse.
- **Recipient must be known.** No named lead in the transcript and none configured → ask who. Never guess a channel or a person.
- **Their words, not a performance.** The note reports what happened, including what did not work. Do not soften block 7 on the way to a manager. That block exists precisely so the team learns.

## Step 5. Close with the next step

End with one line: the nearest action in time, with its date and time. No summary of the summary.

## Once several calls exist

With 3+ files in the follow-ups folder, the user can ask across them: which objection repeats, what was said in the calls that closed, how many follow-ups are overdue.

Answer from those files only. If the question needs something not there (email, CRM, proposals, other reps' calls), say so plainly:

> I cannot answer that from this folder. Only the calls you processed live here.

Do not invent the rest. The limit is real and naming it is part of the job.
