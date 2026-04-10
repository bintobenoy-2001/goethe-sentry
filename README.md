# Goethe Sentry

Local async daemon for monitoring Goethe Institute booking pages and sending Telegram alerts when a slot opens.

The current target defaults reflect two different live systems:

- Standard Goethe India B2 page: shared page for Bangalore, Mumbai, Chennai, Delhi, Pune, Kolkata. Current live state is an error surface and Akamai blocks direct API access, so these targets are present but disabled by default.
- Partner portal: an India center dropdown is discovered live from `https://trivandrum.german.in/#examSection`. At the time of implementation, the live source exposes `Trivandrum` and `Kochi`. `Kolhapur` is pinned in config and reported as `missing_on_site` because it is not currently present in the source.

## Files

- `src/monitor.py`: main daemon loop and startup check
- `check_once.py`: one-shot runner for GitHub Actions or cron
- `src/browser.py`: Playwright fetch and extraction logic
- `src/detector.py`: layered booking state detection
- `src/notifier.py`: Telegram Bot API client via `httpx`
- `src/state.py`: persistent target state in `logs/state.json`
- `config/targets.json`: editable targets, selectors, wait strategy, and system notes
- `run.sh`: launcher

Targets can also define `interaction_steps` for shared portals where a dropdown or click is required before the correct centre-specific exam list appears.
Partner-portal targets can define `center_discovery` so the bot expands discovered centers dynamically without code changes.

## Setup

1. Create a virtual environment if you want one:

   ```bash
   python3.12 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Fill `.env`:

   ```bash
   TELEGRAM_BOT_TOKEN=123456789:replace_me
   TELEGRAM_CHAT_ID=123456789
   ```

4. Update `config/targets.json` with the targets you actually want enabled.
5. If Chromium is not already installed for Playwright, run:

   ```bash
   .venv/bin/playwright install chromium
   ```

Note: the pinned `playwright==1.44.0` stack is safest on Python 3.12 in this project. On systems with both Python 3.12 and 3.14 installed, the launcher will prefer 3.12 automatically.

## Dry Check

Run the startup check without entering the daemon loop:

```bash
python3 src/monitor.py --check
```

Or through the launcher:

```bash
./run.sh --check
```

## Run

```bash
./run.sh
```

The launcher will:

- create `.venv` with Python 3.12 when available
- install pinned dependencies if they are missing
- load `.env`
- refuse to start if Telegram secrets are empty
- print the active target banner before the daemon loop starts

## GitHub Actions Mode

The repository also includes `.github/workflows/sentry.yml` for a zero-infra one-shot mode:

- runs every 5 minutes on GitHub-hosted runners
- executes `python check_once.py`
- exits after one pass
- restores and saves `logs/state.json` through the Actions cache
- keeps local daemon mode unchanged

Required GitHub repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

To use it:

1. Push this repository to GitHub.
2. Add the two repository secrets.
3. Enable GitHub Actions for the repo.
4. Optionally trigger the workflow manually with `workflow_dispatch`.

Notes:

- The one-shot workflow does not send startup or shutdown messages every run.
- Alert suppression and recovery detection still work because the cached `logs/state.json` is reused across runs.
- If the state cache is ever missed, the next run behaves like a cold start and may resend currently-available targets once.
- `workflow_dispatch` supports optional `debug_logging` and `dry_run` inputs for test runs.
- `workflow_dispatch` also supports `center_scope` and `selected_centers` so you can run all discovered India centers, only pinned centers, or only a specific list like `Kolhapur`.

## Current Live Caveats

- Standard Goethe currently exposes a shared India B2 page at `https://www.goethe.de/ins/in/en/spr/prf/gzb2.cfm`.
- The exam widget currently shows `Sorry, our dates cannot be displayed temporarily. Please try again later.` and the underlying `examfinderv3` API returned `403 Forbidden` during investigation.
- Those standard Goethe targets are intentionally disabled in `config/targets.json` until you validate selectors or solve the Akamai gate from your own environment.
- The partner portal now discovers India centers from the live `#cmbExamCentre` dropdown at runtime.
- Current discovered India centers from the live source: `Trivandrum`, `Kochi`.
- `Kolhapur` is pinned in config but currently absent from the live source, so it is tracked as `missing_on_site`.
- If new centers appear in that dropdown later, they can be monitored automatically when `include_centers` is set to `all`.
