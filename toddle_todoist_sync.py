#!/usr/bin/env python3
"""
Sync Toddle (school LMS) calendar events into Todoist tasks.

Reads events from the Toddle calendar's public ICS feed, classifies each one
into the right Todoist section / subject label / type label / priority
(matching the conventions already used in Tanush's Todoist), optionally
rewrites vague Toddle titles into short action-item task names via the
Gemini API, and creates the task -- skipping anything already synced by
checking whether the Toddle share link already appears in an existing
task's description.

Environment variables (set as GitHub Actions secrets, or export locally
before running):
    TODDLE_ICS_URL     - "Secret address in iCal format" for the Toddle
                          calendar. In Google Calendar: hover the calendar
                          in the sidebar -> Settings and sharing -> scroll
                          to "Integrate calendar" -> copy that URL.
    TODOIST_API_TOKEN  - Todoist Settings -> Integrations -> Developer.
    TODOIST_PROJECT_ID - The Todoist project tasks get created in (Inbox).
    GEMINI_API_KEY      - Optional. If unset, titles fall back to a plain
                           "Complete <original title>" instead of an AI
                           rewrite.
    GEMINI_MODEL        - Optional, defaults to "gemini-flash-latest".
                           Check aistudio.google.com if that alias ever stops
                           working and swap in whatever the current
                           recommended flash model is.
"""

import json
import os
import re
import sys
from datetime import date, datetime

import requests
from icalendar import Calendar

TODOIST_API = "https://api.todoist.com/api/v1/tasks"

TODDLE_ICS_URL = os.environ["TODDLE_ICS_URL"]
TODOIST_TOKEN = os.environ["TODOIST_API_TOKEN"]
PROJECT_ID = os.environ["TODOIST_PROJECT_ID"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")

HEADERS = {
    "Authorization": f"Bearer {TODOIST_TOKEN}",
    "Content-Type": "application/json",
}

# Local record of every Toddle link ever synced, independent of whether the
# resulting Todoist task is still active. This is the real source of truth --
# Todoist's task-list endpoint only returns ACTIVE tasks, so a completed and
# checked-off task disappears from it entirely, which made the API-only dedup
# check below blind to anything already finished.
STATE_FILE = os.environ.get("SYNC_STATE_FILE", "synced_links.json")


def load_synced_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_synced_state(links: set):
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(links), f, indent=2)

# ---- Class -> Todoist section / subject label ------------------------------
# Update this if a new class starts showing up in Toddle that isn't here yet.
CLASS_MAP = {
    "AT Computer Science 1": ("6fjg9XGX4FC9P9fh", "coding"),
    "Integrated Math III with Precalculus 7": ("6fJqCgVpFXW39gp9", "math"),
    "Accelerated Chemistry** 2": ("6fJqF5pHjv9cp7h9", "science"),
    "AP Spanish Language/Culture 2": ("6fJqFCjpXfWwwj39", "spanish"),
    "Life Skills 7": ("6fJqF8CJpx8q4rw9", "life skills"),
    "English 10/American History 1": ("6fJqFCF8cWWJxH3h", "american studies"),
}
GENERAL_SECTION = "6hGchhxg6WVqWfF9"  # catch-all for classes not in the map

# Classes where regular (non-assessment) work runs P2 instead of P3.
WORRY_CLASSES = {"Accelerated Chemistry** 2", "AP Spanish Language/Culture 2"}

# Todoist's REST API priority field is inverted from what you see in the app:
# API 4 = shows as "Priority 1" / red in the app ... API 1 = "Priority 4" / normal.
PRIORITY_API = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}


def parse_toddle_fields(description: str):
    """Pull Task Type / Classes / View Task link out of a Toddle event body."""
    task_type = re.search(r"Task Type:\s*(.+)", description)
    classes = re.search(r"Classes:\s*(.+)", description)
    link = re.search(r"View Task:\s*(\S+)", description)
    return (
        task_type.group(1).strip() if task_type else "",
        classes.group(1).strip() if classes else "",
        link.group(1).strip() if link else "",
    )


def signature_keys(content: str, description: str) -> set:
    """Every dedup key a Todoist task's stored content/description could be
    matched against. Deliberately does NOT include a due date -- a
    rescheduled assignment is still the same assignment, not a new one, so
    date changes need to be checked separately rather than baked into
    identity.

    Searches for a markdown link ANYWHERE in the description, not just
    descriptions that are nothing else -- confirmed live that some earlier
    tasks have explanatory prose before the link, which a full-string-only
    match was silently failing to see (this is also why title-only
    fallback matching can never recover a title Todoist has already
    overwritten via its own link-preview auto-linkification -- there's
    nothing here that can fix a description that's already lost the
    original wording, only avoid missing one that hasn't).
    """
    keys = set(re.findall(r"https://share\.toddleapp\.com/tiny/[^\s)\]]+", description or ""))
    for m in re.finditer(r"\[([^\]]+)\]\([^)]+\)", description or ""):
        keys.add(f"title:{m.group(1).strip().lower()}")
    if not keys and (description or content):
        keys.add(f"title:{(description or content).strip().lower()}")
    return keys


def event_keys(title: str, link: str) -> set:
    """The same style of key, computed from a fresh Toddle calendar event."""
    keys = {f"title:{title.strip().lower()}"}
    if link:
        keys.add(link)
    return keys


def classify(task_type: str, class_name: str, title: str, description: str):
    """Return (section_id, subject_label, type_label, priority)."""
    section_id, subject_label = CLASS_MAP.get(class_name, (GENERAL_SECTION, None))
    text = f"{title} {description}".lower()

    is_assessment = task_type.lower() == "assessment" or "quiz" in text

    if is_assessment:
        if "formative" in text or "quiz" in text:
            type_label = "formative"
        elif "prueba" in text or re.search(r"\btest\b", text) or "lab assessment" in text:
            type_label = "test"
        else:
            type_label = "summative"
        priority = "p1"
    else:
        type_label = "assignment"
        priority = "p2" if class_name in WORRY_CLASSES else "p3"

    return section_id, subject_label, type_label, priority


def fallback_title(raw_title: str, type_label: str) -> str:
    """A verb-appropriate title when Gemini is unavailable or unconfigured,
    used instead of a hardcoded "Complete X" for every single task."""
    text = raw_title.lower()
    if "due" in text or "project" in text:
        verb = "Submit"
    elif type_label in ("formative", "test"):
        verb = "Take"
    elif type_label == "summative":
        verb = "Prepare for"
    else:
        verb = "Finish"
    return f"{verb} {raw_title}"


def rewrite_title(raw_title: str, description: str, type_label: str) -> str:
    """Turn a Toddle item title into a short action-item task name."""
    if not GEMINI_KEY:
        return fallback_title(raw_title, type_label)

    prompt = (
        "Rewrite this school assignment title as a short action-item task "
        "name (3-10 words, starting with a verb like Finish/Complete/Take/"
        "Prepare for/Submit). Return ONLY the rewritten title, nothing else."
        f"\n\nTitle: {raw_title}\nContext: {description[:400]}"
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    )
    try:
        resp = requests.post(
            url, json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20
        )
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        return text.strip().strip('"')
    except Exception as exc:  # fall back rather than crash the whole sync
        print(f"  ! Gemini rewrite failed ({exc}), using fallback title", file=sys.stderr)
        return fallback_title(raw_title, type_label)


def task_due_date(task: dict) -> str:
    """Defensively pull a task's due date across possible API response
    shapes -- Todoist's schema for this endpoint has already moved once
    during this project, so this checks a couple of plausible layouts
    rather than assuming one."""
    due = task.get("due") or {}
    if isinstance(due, dict) and due.get("date"):
        return due["date"]
    return task.get("due_date") or task.get("dueDate") or ""


def active_tasks_by_key() -> dict:
    """dedup key -> {"id", "due_date", "labels", "priority", "section_id"}
    for every ACTIVE Todoist task.

    Note: Todoist's task list only returns active tasks -- a completed and
    checked-off task disappears from it entirely. See completed_task_keys()
    below for the other half of the picture.
    """
    tasks = {}
    cursor = None
    while True:
        params = {"project_id": PROJECT_ID}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(TODOIST_API, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        for task in data.get("results", []):
            info = {
                "id": task.get("id"),
                "due_date": task_due_date(task),
                "labels": set(task.get("labels") or []),
                "priority": task.get("priority"),
                "section_id": task.get("section_id"),
            }
            for key in signature_keys(task.get("content", ""), task.get("description", "")):
                tasks[key] = info
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return tasks


def completed_task_keys(since: str = "2026-08-01") -> set:
    """dedup keys seen among COMPLETED tasks. These are never touched or
    recreated regardless of a reschedule -- if it's done, it's done.

    Uses a different endpoint (/tasks/completed/by_completion_date) than
    active_tasks_by_key(), and -- confirmed against Todoist's own docs --
    that endpoint responds with "items"/"nextCursor" (camelCase), not
    "results"/"next_cursor" like the regular task list does.
    """
    keys = set()
    cursor = None
    url = f"{TODOIST_API}/completed/by_completion_date"
    while True:
        params = {
            "projectId": PROJECT_ID,
            "since": since,
            "until": date.today().isoformat(),
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        for task in data.get("items", []):
            keys.update(signature_keys(task.get("content", ""), task.get("description", "")))
        cursor = data.get("nextCursor")
        if not cursor:
            break
    return keys


def create_task(content, description, due_date, section_id, labels, priority):
    payload = {
        "content": content,
        "description": description,
        "due_date": due_date,
        "project_id": PROJECT_ID,
        "priority": PRIORITY_API[priority],
        "labels": labels,
    }
    if section_id:
        payload["section_id"] = section_id
    resp = requests.post(TODOIST_API, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


def update_task(task_id: str, **fields):
    # The due_date-only version of this was confirmed working against live
    # data; labels/priority/section_id in the same payload shape haven't
    # been separately confirmed yet, but follow the identical pattern.
    resp = requests.post(f"{TODOIST_API}/{task_id}", headers=HEADERS, json=fields)
    resp.raise_for_status()


def main():
    ics_resp = requests.get(TODDLE_ICS_URL, timeout=30)
    ics_resp.raise_for_status()
    cal = Calendar.from_ical(ics_resp.content)

    active = active_tasks_by_key()
    done_keys = completed_task_keys()
    state_keys = load_synced_state()
    today_str = date.today().isoformat()
    added, skipped, updated, past_due = 0, 0, 0, 0

    for component in cal.walk("VEVENT"):
        title = str(component.get("summary", "")).strip()
        description = str(component.get("description", "") or "")
        dtend = component.get("dtend")
        if dtend:
            dtend_val = dtend.dt
            # Timed events parse to a datetime; all-day events parse to a
            # plain date (which has no .date() method of its own).
            due_date = (dtend_val.date() if isinstance(dtend_val, datetime) else dtend_val).isoformat()
        else:
            due_date = date.today().isoformat()

        if due_date < today_str:
            past_due += 1
            continue  # don't recreate tasks for things already past due

        if "do not submit" in description.lower():
            continue  # placeholder / reference-only Toddle items

        task_type, class_name, link = parse_toddle_fields(description)

        if not task_type and not class_name:
            continue  # not an actual Toddle assignment (e.g. a school break /
                       # holiday entry on the same calendar) -- has neither field

        keys = event_keys(title, link)

        if keys & done_keys:
            skipped += 1
            continue  # already completed -- never touch, reschedule or not

        section_id, subject_label, type_label, priority = classify(
            task_type, class_name, title, description
        )
        labels = [l for l in (subject_label, type_label) if l]

        match = next((active[k] for k in keys if k in active), None)
        if match:
            skipped += 1
            changes = {}
            if match["due_date"] and match["due_date"] != due_date:
                changes["due_date"] = due_date
            if set(labels) != match["labels"]:
                changes["labels"] = labels
            if PRIORITY_API[priority] != match["priority"]:
                changes["priority"] = PRIORITY_API[priority]
            if section_id and section_id != match["section_id"]:
                changes["section_id"] = section_id
            if changes:
                update_task(match["id"], **changes)
                summary = ", ".join(f"{k}={v}" for k, v in changes.items())
                print(f"  ~ Updated: {title}  ({summary})")
                updated += 1
            continue

        if keys & state_keys:
            skipped += 1
            continue  # seen before via the local state file (e.g. later deleted)

        new_title = rewrite_title(title, description, type_label)
        desc = f"[{title}]({link})" if link else title

        create_task(new_title, desc, due_date, section_id, labels, priority)
        print(f"  + {new_title}  (due {due_date}, {priority}, {labels})")
        added += 1
        state_keys.update(keys)

    save_synced_state(state_keys)
    print(
        f"\nDone. Added {added}, updated {updated} task(s), skipped {skipped} "
        f"already-synced event(s), ignored {past_due} past-due event(s)."
    )


if __name__ == "__main__":
    main()
