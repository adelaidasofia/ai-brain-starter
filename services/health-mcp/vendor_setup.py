"""Vendor + OS aware setup guides for the /health-setup skill.

Each guide is the minimum steps the user needs to do off-machine to get
their wearable data flowing into this MCP. Returned as a structured dict
the skill renders to the user.
"""
from __future__ import annotations

from typing import Any

SUPPORTED_VENDORS = {"apple_health", "oura", "fitbit", "google_health", "garmin", "whoop"}
SUPPORTED_OS = {"macos", "linux", "windows"}


_GUIDES: dict[str, dict[str, Any]] = {
    "apple_health": {
        "display_name": "Apple Health",
        "requires_iphone": True,
        "free_paths": ["xml_export", "simple_health_export_csv"],
        "paid_paths": ["health_auto_export_tcp"],
        "summary": (
            "Apple Health is the native iOS health store. Three import modes: "
            "free XML export, free Simple Health Export CSV, paid Health Auto "
            "Export TCP live."
        ),
        "common_steps": [
            "On your iPhone, open the Health app",
            "Tap your profile picture (top right)",
            "Scroll to the bottom and tap **Export All Health Data**",
            "Wait 1-3 minutes for the export bundle (export.zip)",
        ],
        "transfer_steps_by_os": {
            "macos": ["AirDrop the export.zip from iPhone to your Mac", "Save it to your Downloads folder", "Run `health_import_xml(\"~/Downloads/export.zip\")`"],
            "windows": ["Email or iCloud-share the export.zip from iPhone to your PC", "Save it to your Downloads folder", "Run `health_import_xml(\"%USERPROFILE%\\\\Downloads\\\\export.zip\")`"],
            "linux": ["Email or share the export.zip to your Linux machine", "Save it locally", "Run `health_import_xml(\"/path/to/export.zip\")`"],
        },
        "env_vars": {},
        "tool_to_run": "health_import_xml",
        "ongoing_cadence": "Re-export from iOS Health every 1-4 weeks for a fresh snapshot. The streaming parser handles 1M+ records.",
        "notes": [
            "All ingestion modes are local-only. Your data never leaves your machine.",
            "Idempotent: re-importing the same file returns skipped=True unless force=True.",
        ],
    },
    "oura": {
        "display_name": "Oura Ring",
        "requires_iphone": False,
        "free_paths": ["oura_v2_api"],
        "paid_paths": [],
        "summary": (
            "Oura Ring gen 2/3/4. Uses the Oura v2 Cloud API with a Personal "
            "Access Token (free). Works without iPhone — token is generated "
            "from any browser."
        ),
        "common_steps": [
            "Open https://cloud.ouraring.com/personal-access-tokens in a browser",
            "Sign in with your Oura account",
            "Click **Create New Personal Access Token**",
            "Name it (e.g., `ai-brain-starter`) and copy the token immediately — it is shown only once",
        ],
        "transfer_steps_by_os": {
            "macos": [
                "Open Terminal",
                "Run: `echo 'export OURA_PERSONAL_ACCESS_TOKEN=<paste-token>' >> ~/.zshrc`",
                "Reload your shell: `source ~/.zshrc`",
                "Verify: `echo $OURA_PERSONAL_ACCESS_TOKEN`",
                "Restart Claude Code",
                "Run `health_import_oura(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
            "windows": [
                "Open PowerShell as your user (not admin)",
                "Run: `[System.Environment]::SetEnvironmentVariable('OURA_PERSONAL_ACCESS_TOKEN', '<paste-token>', 'User')`",
                "Close + reopen PowerShell, restart Claude Code",
                "Run `health_import_oura(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
            "linux": [
                "Append to ~/.bashrc or ~/.zshrc: `export OURA_PERSONAL_ACCESS_TOKEN=<paste-token>`",
                "Reload your shell, restart Claude Code",
                "Run `health_import_oura(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
        },
        "env_vars": {"OURA_PERSONAL_ACCESS_TOKEN": "PAT from cloud.ouraring.com/personal-access-tokens"},
        "tool_to_run": "health_import_oura",
        "ongoing_cadence": "Re-run with a recent start_date weekly, OR set up a scheduled task to call health_import_oura every morning for yesterday's data.",
        "notes": [
            "Personal Access Tokens never expire unless you revoke them. Treat them like a password.",
            "Free tier: no rate limits in practice for personal use (Oura's docs cite 5000/day).",
            "If your token leaks, revoke it from the same page and generate a new one.",
        ],
    },
    "fitbit": {
        "display_name": "Fitbit",
        "requires_iphone": False,
        "free_paths": ["fitbit_web_api_personal_app"],
        "paid_paths": [],
        "summary": (
            "Fitbit Web API via a personal OAuth2 app (free). Slightly more "
            "setup than Oura because Fitbit requires app registration before "
            "issuing tokens."
        ),
        "common_steps": [
            "Open https://dev.fitbit.com/apps in a browser",
            "Sign in with your Fitbit account",
            "Click **Register a New App**",
            "Use these values: Application Name=`ai-brain-starter`, Application Type=`Personal`, OAuth 2.0 Application Type=`Server`, Redirect URL=`https://localhost:8080/callback`, Default Access Type=`Read & Write`",
            "Save the **OAuth 2.0 Client ID** and **Client Secret**",
            "Use Fitbit's Tutorial token-generator at https://dev.fitbit.com/apps/oauthinteractivetutorial to run the OAuth2 flow and obtain an access_token + refresh_token (one-click flow once your app is registered)",
        ],
        "transfer_steps_by_os": {
            "macos": [
                "Open Terminal",
                "Run: `echo 'export FITBIT_ACCESS_TOKEN=<paste-token>' >> ~/.zshrc`",
                "Run: `echo 'export FITBIT_REFRESH_TOKEN=<paste-refresh-token>' >> ~/.zshrc`",
                "Run: `echo 'export FITBIT_CLIENT_ID=<paste-client-id>' >> ~/.zshrc`",
                "Run: `echo 'export FITBIT_CLIENT_SECRET=<paste-client-secret>' >> ~/.zshrc`",
                "Reload: `source ~/.zshrc`",
                "Restart Claude Code",
                "Run `health_import_fitbit(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
            "windows": [
                "Open PowerShell as your user",
                "Run for each variable: `[System.Environment]::SetEnvironmentVariable('FITBIT_ACCESS_TOKEN', '<paste-token>', 'User')`",
                "Same for FITBIT_REFRESH_TOKEN, FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET",
                "Restart PowerShell + Claude Code",
                "Run `health_import_fitbit(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
            "linux": [
                "Append all four env vars to ~/.bashrc or ~/.zshrc",
                "Reload your shell, restart Claude Code",
                "Run `health_import_fitbit(start=\"2026-01-01\", end=\"2026-05-10\")`",
            ],
        },
        "env_vars": {
            "FITBIT_ACCESS_TOKEN": "OAuth2 access token (8-hour expiry, auto-refreshed if refresh token + client id + secret are set)",
            "FITBIT_REFRESH_TOKEN": "Long-lived refresh token (used to mint new access tokens)",
            "FITBIT_CLIENT_ID": "OAuth2 Client ID from your registered Fitbit Personal app",
            "FITBIT_CLIENT_SECRET": "OAuth2 Client Secret from your registered Fitbit Personal app",
        },
        "tool_to_run": "health_import_fitbit",
        "ongoing_cadence": "Set up a scheduled task to call health_import_fitbit every morning for yesterday. Access token auto-refreshes if all four env vars are set.",
        "notes": [
            "Fitbit HRV requires a Premium subscription. Without Premium, you get steps + sleep + RHR, no HRV.",
            "Fitbit rate-limit is 150 requests/hour for personal apps. The importer runs one request per metric per day, ~5/day, so a year of backfill stays well under the limit.",
            "If you hit 429 errors, wait 1 hour or contact Fitbit dev support for an exception.",
        ],
    },
    "google_health": {
        "display_name": "Google Health API",
        "requires_iphone": False,
        "free_paths": ["google_health_api_oauth"],
        "paid_paths": [],
        "summary": (
            "The Google Health API is the successor to the Fitbit Web API and "
            "Google Fit REST API (both retired from September 2026). Cloud REST "
            "+ OAuth2; unifies Fitbit, Pixel Watch, and third-party device data. "
            "Free, but setup is the fiddliest of the vendors: a Google Cloud "
            "project + OAuth client + one-time consent flow."
        ),
        "common_steps": [
            "Open https://console.cloud.google.com and create a new project (or pick one)",
            "In APIs & Services > Library, search 'Google Health API' and click Enable",
            "In APIs & Services > OAuth consent screen: set up an External app, add yourself under Audience > Test users",
            "In the Data Access page, add the Google Health API scopes for the metrics you want (activity, heart rate, sleep, body — add all for full sync), then Update",
            "In APIs & Services > Credentials > Create credentials > OAuth client ID: choose Web application; set Authorized redirect URI to https://www.google.com; save the Client ID + Client Secret",
            "Run the OAuth flow once (OAuth Playground at https://developers.google.com/oauthplayground works: gear icon > use your own client ID/secret, authorize the Health scopes, exchange for tokens) to get an access_token + refresh_token",
            "IMPORTANT: publish the OAuth app to Production (OAuth consent screen > Publish app). In Testing status Google expires refresh tokens after 7 DAYS, which breaks daily auto-sync every week.",
        ],
        "transfer_steps_by_os": {
            "macos": [
                "Open Terminal",
                "Run: `echo 'export GOOGLE_HEALTH_ACCESS_TOKEN=<paste-access-token>' >> ~/.zshrc`",
                "Run: `echo 'export GOOGLE_HEALTH_REFRESH_TOKEN=<paste-refresh-token>' >> ~/.zshrc`",
                "Run: `echo 'export GOOGLE_HEALTH_CLIENT_ID=<paste-client-id>' >> ~/.zshrc`",
                "Run: `echo 'export GOOGLE_HEALTH_CLIENT_SECRET=<paste-client-secret>' >> ~/.zshrc`",
                "Reload: `source ~/.zshrc`",
                "Restart Claude Code",
                "Verify: `health_vendor_healthcheck('google_health')`",
                "Run `health_import_google_health(start=\"2026-01-01\", end=\"2026-06-30\")`",
            ],
            "windows": [
                "Open PowerShell as your user",
                "Run for each variable: `[System.Environment]::SetEnvironmentVariable('GOOGLE_HEALTH_ACCESS_TOKEN', '<paste-token>', 'User')`",
                "Same for GOOGLE_HEALTH_REFRESH_TOKEN, GOOGLE_HEALTH_CLIENT_ID, GOOGLE_HEALTH_CLIENT_SECRET",
                "Restart PowerShell + Claude Code",
                "Verify: `health_vendor_healthcheck('google_health')`",
                "Run `health_import_google_health(start=\"2026-01-01\", end=\"2026-06-30\")`",
            ],
            "linux": [
                "Append all four GOOGLE_HEALTH_* env vars to ~/.bashrc or ~/.zshrc",
                "Reload your shell, restart Claude Code",
                "Verify: `health_vendor_healthcheck('google_health')`",
                "Run `health_import_google_health(start=\"2026-01-01\", end=\"2026-06-30\")`",
            ],
        },
        "env_vars": {
            "GOOGLE_HEALTH_ACCESS_TOKEN": "OAuth2 access token (short-lived; auto-refreshed when the other three vars are set)",
            "GOOGLE_HEALTH_REFRESH_TOKEN": "OAuth2 refresh token (7-day lifetime in Testing status; long-lived once the app is published to Production)",
            "GOOGLE_HEALTH_CLIENT_ID": "OAuth Web-application client ID from Google Cloud Credentials",
            "GOOGLE_HEALTH_CLIENT_SECRET": "OAuth Web-application client secret",
        },
        "tool_to_run": "health_import_google_health",
        "ongoing_cadence": "Set up a scheduled task to call health_import_google_health every morning for yesterday. Access token auto-refreshes ONLY if the app is published to Production (Testing refresh tokens die after 7 days).",
        "notes": [
            "Successor to Fitbit Web API + Google Fit REST API, both retired Sept 2026.",
            "Health Connect (Android on-device) is NOT usable here — it has no cloud API. The Google Health API is the cloud path.",
            "Publish the OAuth app to Production or daily sync breaks weekly (7-day refresh-token expiry in Testing).",
            "Phase 1 imports all daily numeric metrics + sleep stages. Workouts, ECG, irregular-rhythm, and structured nutrition are a planned Phase 2.",
            "If a metric is missing, confirm its scope was added on the Data Access page — one un-granted scope is skipped silently, the rest still import.",
        ],
    },
    "garmin": {
        "display_name": "Garmin",
        "requires_iphone": False,
        "free_paths": [],
        "paid_paths": [],
        "summary": (
            "Garmin Connect data is not yet directly supported. For now, "
            "Garmin users have two options: (1) sync Garmin Connect to Apple "
            "Health on iPhone via the Garmin Connect app, then export via "
            "Apple Health; (2) wait for v0.4 which will add direct Garmin "
            "Connect IQ + Garmin Health API support."
        ),
        "common_steps": [
            "Install Garmin Connect app on your iPhone",
            "Garmin Connect > More > Health Stats > Apple Health > enable sync for the metrics you want",
            "Wait 24h for back-sync to complete",
            "Then follow the Apple Health setup guide",
        ],
        "transfer_steps_by_os": {},
        "env_vars": {},
        "tool_to_run": "(via apple_health)",
        "ongoing_cadence": "Same as Apple Health.",
        "notes": [
            "Garmin Connect IQ is a planned v0.4 addition. Track at github.com/mycelium-hq/ai-brain-starter/issues",
        ],
    },
    "whoop": {
        "display_name": "Whoop",
        "requires_iphone": False,
        "free_paths": [],
        "paid_paths": [],
        "summary": (
            "Whoop API access requires Whoop Pro subscription + OAuth2 app "
            "registration. Not yet directly supported in v0.3. For now, the "
            "open-wearables platform at github.com/the-momentum/open-wearables "
            "supports Whoop natively if you want multi-vendor right now."
        ),
        "common_steps": [
            "Confirm you have Whoop Pro",
            "Visit developer.whoop.com to register an OAuth2 app",
            "Track v0.4 progress at github.com/mycelium-hq/ai-brain-starter/issues for native support",
        ],
        "transfer_steps_by_os": {},
        "env_vars": {},
        "tool_to_run": "(deferred to v0.4)",
        "ongoing_cadence": "n/a until v0.4",
        "notes": ["open-wearables is the heavier-but-multi-vendor alternative today."],
    },
}


def vendor_setup_guide(vendor: str, os_kind: str = "macos") -> dict[str, Any]:
    """Return setup steps for a vendor + OS combination."""
    v = vendor.lower().strip().replace("-", "_").replace(" ", "_")
    if v == "apple" or v == "apple_watch" or v == "iphone" or v == "ios":
        v = "apple_health"
    if v in {"google", "googlehealth", "google_fit", "googlefit", "pixel", "health_connect"}:
        v = "google_health"
    if v not in SUPPORTED_VENDORS:
        return {
            "vendor": v,
            "error": f"Unsupported vendor. Supported: {sorted(SUPPORTED_VENDORS)}",
        }
    os_k = os_kind.lower().strip()
    if os_k in {"mac", "darwin", "osx"}:
        os_k = "macos"
    if os_k in {"win", "windows10", "windows11"}:
        os_k = "windows"

    guide = dict(_GUIDES[v])
    guide["vendor"] = v
    guide["os"] = os_k
    if "transfer_steps_by_os" in guide and os_k in guide["transfer_steps_by_os"]:
        guide["transfer_steps"] = guide["transfer_steps_by_os"][os_k]
    return guide
