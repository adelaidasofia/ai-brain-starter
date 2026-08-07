---
type: skill
name: security-snapshot
description: 'Use when the user says /security-snapshot, /snapshot <domain>, "run a security check on X", "generate a security report for [company]", or wants a security hygiene snapshot or free lead-magnet report on a prospect''s public domain: SSL/TLS grade, HTTP security headers, SPF/DMARC email authentication, server fingerprint leaks. Passive, unauthenticated scans only. NOT for penetration testing, internal infrastructure audits, or application-layer vulnerability assessment.'
argument-hint: "<domain> [--company 'Display Name']"
tool_access:
  - Bash
  - Read
  - Write
policy_constraints:
  - rule: Only run passive, unauthenticated scans against the public-facing domain
    exception_handling: Refuse to run if the request implies authenticated probing or penetration testing
  - rule: Never store credentials or PII in the report or output directory
    exception_handling: Strip any unexpected credential-shaped strings before writing the markdown file
  - rule: Cap external API usage to the documented endpoints (SSL Labs, public DNS lookups, headers fetch)
    exception_handling: Abort and report partial results if a required endpoint is unavailable rather than fall back to an undocumented service
required_inputs:
  - name: domain
    type: string
    required: true
    description: The public domain to scan (e.g. example.com). Must resolve via public DNS.
  - name: company
    type: string
    required: false
    description: Display name for the company, used in the report header. Defaults to the domain.
output_shape:
  format: markdown-file
  fields:
    report_path: absolute path to the saved markdown report
    sections:
      - ssl_tls_grade
      - http_security_headers
      - email_authentication
      - server_fingerprint
    summary_grade: overall hygiene grade (A through F) printed to stdout
---

> **`{SKILL_DIR}`** = this skill's own folder (locally: the directory this SKILL.md lives in; a served brain substitutes the real absolute path before you read this). Shared starter files live at the repo root two levels up: `{SKILL_DIR}/../..`. If a path does not resolve, name the missing file and stop — never guess another location.

When the user types /security-snapshot [domain] or asks for a security check on a prospect, run the security snapshot generator and deliver a client-ready report.

## Why this skill exists

Prospects rarely have budget for a full security audit upfront, but they will read a free one-page report that exposes real issues with their public-facing setup. This skill generates that report in under 3 minutes and opens the door for a paid follow-up on security work, AI implementation, or adjacent consulting.

## Command

```bash
python3 "{SKILL_DIR}/../../scripts/security-snapshot.py" <domain> --company "<Display Name>"
```

The script ships with the starter repo. Output goes to `$SNAPSHOTS_DIR` if set, otherwise `$VAULT_ROOT/security-snapshots/` if `VAULT_ROOT` is set, otherwise a `security-snapshots/` folder next to wherever you run the command from. It takes 60-180 seconds because SSL Labs is slow. The script prints the saved report path to stdout and progress to stderr.

## Workflow

1. **Get the domain.** If the user only gave a company name, ask for the domain (e.g., "Is it acme.com or acmecorp.com?"). Do not guess.
2. **Run the script.** Use Bash with a long timeout (180000ms) because SSL Labs polling is slow.
3. **Read the output.** The script saves to `$SNAPSHOTS_DIR/<domain>/<YYYY-MM-DD>-snapshot.md` (defaults to `$VAULT_ROOT/security-snapshots/` when `SNAPSHOTS_DIR` is unset). Read the file before summarizing.
4. **Summarize for the user.** Do NOT dump the full report into chat. Give:
   - Top 3 findings by severity with one-line reasons
   - SSL grade
   - Whether SPF + DMARC are both present
   - Path to the saved report
5. **Offer the follow-up.** If there are high or critical findings, offer to draft the outreach email that pairs with the report. Match the user's own outreach voice, do not invent a new one.

## Voice rules for the delivered report

The script produces the base report. If the user asks you to customize or rewrite any section before sending, follow the generic voice rules in `templates/rules/voice-firewall.md`:

- No em dashes. Use commas, colons, periods, or parentheses.
- No exclamation marks. Anywhere.
- No hype language ("leverage synergies", "game-changing"). Facts plus plain severity.
- No claims the scan did not verify. The report already lists what was NOT checked. Keep that section intact.

## When NOT to use this skill

- For the user's own domains. Those are internal audits, not lead magnets. Use the script directly without the prospect-facing wrapper (edit the signature block or use `--out` to write to an internal folder).
- For a full penetration test. The script is passive recon only. If a prospect needs an actual pentest, route them to a licensed pentester.
- For ongoing monitoring. One-shot snapshots only. If a prospect needs continuous monitoring, that is a paid engagement to scope separately.
- For domains in active DDoS or incident response. The scan is visible in their logs and will add noise at the worst possible time.

## Optional enhancements (document, do not auto-run)

The script does not currently cover these because they need paid API keys or explicit authorization. Offer to add manually when relevant:

- **Have I Been Pwned domain search** (paid, ~$4/mo). Shows how many of their employee emails appear in past breaches. Highest-impact finding a prospect will see.
- **AbuseIPDB lookup** (free tier with key). Checks if their server IPs appear on abuse blocklists.
- **VirusTotal URL scan** (free tier with key). Checks if their domain is flagged by any of 70+ threat intel engines.
- **DNS zone transfer attempt** (no auth needed). If their nameservers allow AXFR, that is a critical finding.

## Output location

```
$SNAPSHOTS_DIR/
├── acme.com/
│   ├── 2026-04-16-snapshot.md
│   └── 2026-07-22-snapshot.md       (if re-run later)
└── another-prospect.co/
    └── 2026-04-18-snapshot.md
```

One folder per domain. Re-running on the same day overwrites. Running weeks later creates a new dated file so you can track improvement (or lack of it) across conversations.

## Rules

- **Never fabricate findings.** If the script could not reach a service, the report says so. Do not invent severity ratings.
- **Always verify the saved file exists before telling the user.** If save fails, tell the user immediately, never silently.
- **The report is client-ready.** Do not add personal notes, internal thoughts, or CRM commentary inside the report file. Those go in a separate note if needed.
- **Respect the domain.** Only run this on domains of companies you are actually prospecting, companies that have asked for it, or your own domains. Do not bulk-scan the internet.
