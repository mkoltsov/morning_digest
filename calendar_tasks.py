"""calendar_tasks.py — Google Calendar and Google Tasks fetching."""
from datetime import datetime, timezone, timedelta


def fetch_week_events(cal):
    now      = datetime.now(timezone.utc)
    week_end = now + timedelta(days=7)
    result   = cal.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=week_end.isoformat(),
        maxResults=30,
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def fetch_tasks(tasks_svc):
    lists     = tasks_svc.tasklists().list(maxResults=20).execute().get("items", [])
    all_tasks = []
    for tl in lists:
        items = tasks_svc.tasks().list(
            tasklist=tl["id"], showCompleted=False, maxResults=100
        ).execute().get("items", [])
        for t in items:
            if t.get("status") != "completed":
                t["_list"] = tl["title"]
                all_tasks.append(t)
    return all_tasks
