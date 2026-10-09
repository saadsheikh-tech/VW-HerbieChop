"""Read-only Google Calendar access via the calendar's private iCal address.

Google Calendar → Settings → (your calendar) → Integrate calendar → "Secret address in
iCal format". Put it in .env as GOOGLE_CALENDAR_ICS_URL. No password, no OAuth app, read
only, and revocable by resetting the secret address. Recurring events are expanded.

The simulation runs on its own clock, so today's real events are copied onto the
simulated day (same times), replacing that day's demo events.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("herbie.calendar")
BERLIN = ZoneInfo("Europe/Berlin")


def events_on(ics_text: str, day: date) -> list[dict]:
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics_text)
    out = []
    for ev in recurring_ical_events.of(cal).at(day):
        start, end = ev.get("DTSTART").dt, (ev.get("DTEND") or ev.get("DTSTART")).dt
        if not isinstance(start, datetime):  # all-day event
            continue
        start = start.astimezone(BERLIN).replace(tzinfo=None) if start.tzinfo else start
        end = end.astimezone(BERLIN).replace(tzinfo=None) if getattr(end, "tzinfo", None) else end
        text = " ".join(str(ev.get(k, "")) for k in ("LOCATION", "DESCRIPTION"))
        link = next((w for w in text.split() if w.startswith("https://meet.google.com") or "teams.microsoft.com" in w
                     or w.startswith("https://zoom.us") or ".zoom.us/" in w), None)
        out.append({"title": str(ev.get("SUMMARY", "Untitled")), "start": start, "end": end, "link": link})
    return sorted(out, key=lambda e: e["start"])


def fetch_today(url: str | None = None, today: date | None = None) -> list[dict] | None:
    """Real events for today, or None if no calendar is configured / reachable."""
    url = url or os.environ.get("GOOGLE_CALENDAR_ICS_URL")
    if not url:
        return None
    import httpx

    try:
        r = httpx.get(url, timeout=10, follow_redirects=True)
        r.raise_for_status()
        return events_on(r.text, today or datetime.now(BERLIN).date())
    except Exception as e:
        log.warning("calendar fetch failed: %s", type(e).__name__)
        return None


def map_onto(memory, user_id: str, events: list[dict], sim_day: datetime) -> int:
    """Replace the simulated day's calendar with real events at the same times of day."""
    day0 = sim_day.replace(hour=0, minute=0, second=0, microsecond=0)
    memory.delete_events_between(user_id, day0, day0 + timedelta(days=1))
    for e in events:
        start = day0.replace(hour=e["start"].hour, minute=e["start"].minute)
        end = start + (e["end"] - e["start"])
        memory.add_event(user_id, e["title"], start, end, e["link"], "google")
    return len(events)
