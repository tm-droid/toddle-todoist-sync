# Toddle → Todoist sync

Runs once a day (or on demand) and adds any new Toddle calendar events to
Todoist, following the same rules we worked out by hand: correct section,
subject + type labels, P1/P2/P3 priority, and a description linking back to
the original Toddle item.

## Files

- `toddle_todoist_sync.py` — the script
- `requirements.txt` — Python deps (`pip install -r requirements.txt`)
- `sync.yml` — goes in `.github/workflows/sync.yml` in your repo

## Setup

1. **Create a GitHub repo** (private is fine — e.g. `toddle-todoist-sync`).
   Add `toddle_todoist_sync.py` and `requirements.txt` at the top level, and
   `sync.yml` at the path `.github/workflows/sync.yml`.

2. **Get the Toddle calendar's ICS feed URL.**
   In Google Calendar (web): hover "Toddle - Class Stream Tasks" in the left
   sidebar → the three dots → *Settings and sharing* → scroll to
   *Integrate calendar* → copy **Secret address in iCal format**.
   (Keep this private — anyone with the URL can read your calendar.)

3. **Get a Todoist API token.**
   Todoist → Settings → Integrations → Developer → copy the **API token**.

4. **Your Todoist Inbox project ID** is `6Rr2cjPFQfF66HF9` — the same
   project everything's already in.

5. **(Optional) Get a free Gemini API key** for the title rewrite step, at
   [aistudio.google.com](https://aistudio.google.com). If you skip this, the
   script just uses "Complete `<original Toddle title>`" as the task name
   instead of a smarter rewrite.

6. **Add secrets to the repo.**
   In your GitHub repo: Settings → Secrets and variables → Actions → New
   repository secret. Add all of these:
   - `TODDLE_ICS_URL`
   - `TODOIST_API_TOKEN`
   - `TODOIST_PROJECT_ID` → `6Rr2cjPFQfF66HF9`
   - `GEMINI_API_KEY` (optional)

7. **Done.** The workflow runs automatically every morning. To run it
   immediately (e.g. right after this setup, or to test it), go to your
   repo's **Actions** tab → *Sync Toddle to Todoist* → **Run workflow**.

## Maintenance

If a brand-new class shows up in Toddle that isn't in `CLASS_MAP` yet
(like American Studies was, this time), it'll land in the catch-all
General section with no subject label until you add it to the dict in
`toddle_todoist_sync.py`.
