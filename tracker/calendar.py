from datetime import datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo
import exchange_calendars as xcals
import pandas as pd

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=1)
def calendar():
    year = datetime.now(NY).year
    return xcals.get_calendar("XNYS", start="2000-01-01", end=f"{year + 5}-12-31")


def session_on_or_after(day):
    return calendar().date_to_session(pd.Timestamp(day), direction="next")


def chart_date(observed):
    return session_on_or_after(observed.astimezone(NY).date()).date().isoformat()


def entry_session(observed):
    cal = calendar()
    session = session_on_or_after(observed.astimezone(NY).date())
    if cal.session_open(session).to_pydatetime() <= observed:
        session = cal.next_session(session)
    return session.date().isoformat()


def horizon_date(entry, horizon):
    return calendar().session_offset(pd.Timestamp(entry), horizon - 1).date().isoformat()


def completed(day, now):
    cal = calendar()
    session = pd.Timestamp(day)
    return cal.is_session(session) and cal.session_close(session).to_pydatetime() <= now


def schedule(now):
    local = now.astimezone(NY)
    today = datetime.combine(local.date(), time(22, 15), NY)
    due = today if local >= today else datetime.combine(local.date() - timedelta(days=1), time(22, 15), NY)
    upcoming = datetime.combine(due.date() + timedelta(days=1), time(22, 15), NY)
    return due, upcoming
