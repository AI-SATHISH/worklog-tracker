"""
Work Log Tracker - Data Layer
Live start/pause/resume/complete task timer with SQLite (local dev) or
Postgres (production, via DATABASE_URL) storage.
Categories: Attendance, Mobile, Report, Payroll, Statutory Compliance,
Automation, Meeting, Other.
Statuses: Not Started, In Progress, Paused, Completed, Followup, Pending.
"""
import sqlite3
import pandas as pd
from datetime import date, datetime, timedelta, timezone
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "worklog.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2

CATEGORIES = [
    "Attendance", "Mobile", "Report", "Payroll", "Statutory Compliance",
    "Automation", "Meeting", "Other"
]

STATUSES = ["Not Started", "In Progress", "Paused", "Completed", "Followup", "Pending", "Scheduled"]

TS_FMT = "%Y-%m-%d %H:%M:%S"
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist():
    """Current time in India Standard Time (UTC+5:30), regardless of server timezone."""
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)


def today_ist():
    """Today's date in IST — use this instead of date.today() (which is server-local/UTC)."""
    return now_ist().date()


def now_str():
    return now_ist().strftime(TS_FMT)


def parse_ts(s):
    if not s or (isinstance(s, float) and pd.isna(s)):
        return None
    try:
        return datetime.strptime(s, TS_FMT)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None


def _translate(sql):
    """Convert SQLite-style '?' placeholders to psycopg2's '%s' when on Postgres."""
    return sql.replace("?", "%s") if USE_PG else sql


class _ConnWrapper:
    """Thin shim so the rest of this module can keep using SQLite-style
    conn.execute(sql_with_question_marks, params) and pd.read_sql_query(...)
    unchanged, whether the backing store is SQLite (local dev) or Postgres
    (production, via DATABASE_URL)."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_translate(sql), params)
        return cur

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()

    def cursor(self):
        return self._raw.cursor()


def get_conn():
    if USE_PG:
        raw = psycopg2.connect(DATABASE_URL)
        conn = _ConnWrapper(raw)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worklog_tasks (
                id SERIAL PRIMARY KEY,
                entry_date TEXT NOT NULL,
                category TEXT,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'Not Started',
                start_time TEXT,
                end_time TEXT,
                pause_minutes REAL DEFAULT 0,
                pause_started_at TEXT,
                followup_date TEXT,
                scheduled_date TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        return conn
    else:
        raw = sqlite3.connect(DB_PATH)
        conn = _ConnWrapper(raw)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worklog_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                category TEXT,
                task TEXT NOT NULL,
                status TEXT DEFAULT 'Not Started',
                start_time TEXT,
                end_time TEXT,
                pause_minutes REAL DEFAULT 0,
                pause_started_at TEXT,
                followup_date TEXT,
                scheduled_date TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        existing_cols = [r[1] for r in raw.execute("PRAGMA table_info(worklog_tasks)").fetchall()]
        if "scheduled_date" not in existing_cols:
            conn.execute("ALTER TABLE worklog_tasks ADD COLUMN scheduled_date TEXT")
        conn.commit()
        return conn


def _read_sql(query, conn, params=None):
    return pd.read_sql_query(_translate(query), conn, params=params)


def format_minutes(mins):
    if mins is None or (isinstance(mins, float) and pd.isna(mins)):
        return "-"
    mins = max(0, int(round(mins)))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    elif h:
        return f"{h}h"
    else:
        return f"{m}m"


def get_worked_minutes(row):
    """
    Live worked time for a task row (dict-like: start_time, end_time,
    pause_minutes, pause_started_at, status). Computes up to `now` if
    still running, and accounts for an in-progress pause.
    """
    start = parse_ts(row.get("start_time"))
    if start is None:
        return None
    end = parse_ts(row.get("end_time"))
    reference = end if end else now_ist()
    total_seconds = (reference - start).total_seconds()
    total_minutes = total_seconds / 60.0

    pause_minutes = float(row.get("pause_minutes") or 0)
    # If currently paused, add the ongoing pause duration too
    if row.get("status") == "Paused" and row.get("pause_started_at"):
        p_start = parse_ts(row["pause_started_at"])
        if p_start:
            pause_minutes += (now_ist() - p_start).total_seconds() / 60.0

    worked = total_minutes - pause_minutes
    return max(worked, 0)


# ---------------------------------------------------------------------
# CRUD / lifecycle actions
# ---------------------------------------------------------------------

def add_task(entry_date, category, task, notes=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO worklog_tasks (entry_date, category, task, status, notes) VALUES (?, ?, ?, 'Not Started', ?)",
        (str(entry_date), category, task, notes)
    )
    conn.commit()
    conn.close()


def add_scheduled_task(scheduled_date, category, task, notes=""):
    """
    Calendar-based task: created for a future date. Sits in 'Scheduled'
    status until that date arrives, at which point promote_due_tasks()
    moves it into 'Pending' so it shows up for action that day.
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO worklog_tasks (entry_date, category, task, status, notes, scheduled_date) "
        "VALUES (?, ?, ?, 'Scheduled', ?, ?)",
        (str(scheduled_date), category, task, notes, str(scheduled_date))
    )
    conn.commit()
    conn.close()


def promote_due_tasks():
    """
    Call once per page load. Any task whose scheduled_date or followup_date
    has arrived (<= today, IST) and is still sitting in 'Scheduled' or
    'Followup' status gets moved into 'Pending' so it surfaces for action.
    Returns the number of tasks promoted.
    """
    today = str(today_ist())
    conn = get_conn()
    cur = conn.execute(
        "UPDATE worklog_tasks SET status='Pending', entry_date=? "
        "WHERE status='Scheduled' AND scheduled_date IS NOT NULL AND scheduled_date <= ?",
        (today, today)
    )
    promoted_scheduled = cur.rowcount
    cur2 = conn.execute(
        "UPDATE worklog_tasks SET status='Pending' "
        "WHERE status='Followup' AND followup_date IS NOT NULL AND followup_date <= ?",
        (today,)
    )
    promoted_followup = cur2.rowcount
    conn.commit()
    conn.close()
    return promoted_scheduled + promoted_followup


def start_task(task_id):
    """Begin work: records the real start timestamp now."""
    task_id = int(task_id)
    conn = get_conn()
    conn.execute(
        "UPDATE worklog_tasks SET status='In Progress', start_time=? WHERE id=?",
        (now_str(), task_id)
    )
    conn.commit()
    conn.close()


def pause_task(task_id):
    task_id = int(task_id)
    conn = get_conn()
    conn.execute(
        "UPDATE worklog_tasks SET status='Paused', pause_started_at=? WHERE id=?",
        (now_str(), task_id)
    )
    conn.commit()
    conn.close()


def resume_task(task_id):
    """Resume from pause: add the elapsed pause duration into pause_minutes."""
    task_id = int(task_id)
    conn = get_conn()
    row = conn.execute("SELECT pause_started_at, pause_minutes FROM worklog_tasks WHERE id=?", (task_id,)).fetchone()
    if row and row[0]:
        p_start = parse_ts(row[0])
        added = (now_ist() - p_start).total_seconds() / 60.0 if p_start else 0
        new_pause_total = (row[1] or 0) + added
        conn.execute(
            "UPDATE worklog_tasks SET status='In Progress', pause_started_at=NULL, pause_minutes=? WHERE id=?",
            (new_pause_total, task_id)
        )
    else:
        conn.execute("UPDATE worklog_tasks SET status='In Progress', pause_started_at=NULL WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def complete_task(task_id):
    """Mark task completed: if currently paused, fold in that pause first."""
    task_id = int(task_id)
    conn = get_conn()
    row = conn.execute("SELECT status, pause_started_at, pause_minutes FROM worklog_tasks WHERE id=?", (task_id,)).fetchone()
    pause_minutes = (row[2] or 0) if row else 0
    if row and row[0] == "Paused" and row[1]:
        p_start = parse_ts(row[1])
        if p_start:
            pause_minutes += (now_ist() - p_start).total_seconds() / 60.0
    conn.execute(
        "UPDATE worklog_tasks SET status='Completed', end_time=?, pause_started_at=NULL, pause_minutes=? WHERE id=?",
        (now_str(), pause_minutes, task_id)
    )
    conn.commit()
    conn.close()


def mark_followup(task_id, followup_date=None, notes=None):
    task_id = int(task_id)
    conn = get_conn()
    if notes is not None:
        conn.execute(
            "UPDATE worklog_tasks SET status='Followup', followup_date=?, notes=? WHERE id=?",
            (str(followup_date) if followup_date else None, notes, task_id)
        )
    else:
        conn.execute(
            "UPDATE worklog_tasks SET status='Followup', followup_date=? WHERE id=?",
            (str(followup_date) if followup_date else None, task_id)
        )
    conn.commit()
    conn.close()


def mark_pending(task_id, notes=None):
    task_id = int(task_id)
    conn = get_conn()
    if notes is not None:
        conn.execute("UPDATE worklog_tasks SET status='Pending', notes=? WHERE id=?", (notes, task_id))
    else:
        conn.execute("UPDATE worklog_tasks SET status='Pending' WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


def reopen_task(task_id):
    """Move a Followup/Pending task back to Not Started so it can be started again."""
    task_id = int(task_id)
    conn = get_conn()
    conn.execute(
        "UPDATE worklog_tasks SET status='Not Started', start_time=NULL, end_time=NULL, "
        "pause_minutes=0, pause_started_at=NULL WHERE id=?",
        (task_id,)
    )
    conn.commit()
    conn.close()


def delete_task(task_id):
    task_id = int(task_id)
    conn = get_conn()
    conn.execute("DELETE FROM worklog_tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------

def _attach_worked(df):
    if not df.empty:
        df["worked_minutes"] = df.apply(lambda r: get_worked_minutes(r), axis=1)
    return df


def get_tasks(start_date=None, end_date=None, statuses=None):
    conn = get_conn()
    query = "SELECT * FROM worklog_tasks"
    conditions = []
    params = []
    if start_date and end_date:
        conditions.append("entry_date >= ? AND entry_date <= ?")
        params += [str(start_date), str(end_date)]
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        conditions.append(f"status IN ({placeholders})")
        params += list(statuses)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY entry_date DESC, id DESC"
    df = _read_sql(query, conn, params=params)
    conn.close()
    return _attach_worked(df)


def get_tasks_for_date(entry_date):
    return get_tasks(entry_date, entry_date)


def update_notes(task_id, notes):
    task_id = int(task_id)
    conn = get_conn()
    conn.execute("UPDATE worklog_tasks SET notes=? WHERE id=?", (notes, task_id))
    conn.commit()
    conn.close()


_UNSET = object()


def update_task_details(task_id, task=None, category=None, notes=None,
                         start_time=_UNSET, end_time=_UNSET, pause_minutes=_UNSET,
                         followup_date=_UNSET):
    """
    Edit a task's description, category, remarks, and/or timing fields in place.
    start_time/end_time: pass a datetime (or None to clear) — stored as TS_FMT string.
    pause_minutes: pass a number (or None to clear).
    followup_date: pass a date (or None to clear).
    Fields left as the default (_UNSET) are not touched.
    """
    task_id = int(task_id)
    conn = get_conn()
    fields, params = [], []
    if task is not None:
        fields.append("task=?"); params.append(task)
    if category is not None:
        fields.append("category=?"); params.append(category)
    if notes is not None:
        fields.append("notes=?"); params.append(notes)
    if start_time is not _UNSET:
        fields.append("start_time=?")
        params.append(start_time.strftime(TS_FMT) if start_time else None)
    if end_time is not _UNSET:
        fields.append("end_time=?")
        params.append(end_time.strftime(TS_FMT) if end_time else None)
    if pause_minutes is not _UNSET:
        fields.append("pause_minutes=?")
        params.append(float(pause_minutes) if pause_minutes is not None else 0)
    if followup_date is not _UNSET:
        fields.append("followup_date=?")
        params.append(str(followup_date) if followup_date else None)
    if fields:
        params.append(task_id)
        conn.execute(f"UPDATE worklog_tasks SET {', '.join(fields)} WHERE id=?", params)
        conn.commit()
    conn.close()


def get_scheduled_tasks():
    conn = get_conn()
    df = _read_sql(
        "SELECT * FROM worklog_tasks WHERE status='Scheduled' ORDER BY scheduled_date ASC, id ASC", conn
    )
    conn.close()
    return _attach_worked(df)


def get_tasks_by_scheduled_date(target_date):
    conn = get_conn()
    df = _read_sql(
        "SELECT * FROM worklog_tasks WHERE scheduled_date=? ORDER BY id ASC", conn, params=[str(target_date)]
    )
    conn.close()
    return _attach_worked(df)


def get_followups():
    conn = get_conn()
    df = _read_sql("SELECT * FROM worklog_tasks WHERE status='Followup' ORDER BY followup_date ASC, id ASC", conn)
    conn.close()
    return _attach_worked(df)


def get_pending():
    conn = get_conn()
    df = _read_sql("SELECT * FROM worklog_tasks WHERE status='Pending' ORDER BY id ASC", conn)
    conn.close()
    return _attach_worked(df)


def get_week_range(any_date):
    monday = any_date - timedelta(days=any_date.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def compute_daily_summary(entry_date):
    df = get_tasks_for_date(entry_date)
    if df.empty:
        return {"total_tasks": 0, "completed": 0, "in_progress": 0, "paused": 0, "followup": 0, "pending": 0, "scheduled": 0}
    return {
        "total_tasks": len(df),
        "completed": int((df["status"] == "Completed").sum()),
        "in_progress": int((df["status"] == "In Progress").sum()),
        "paused": int((df["status"] == "Paused").sum()),
        "followup": int((df["status"] == "Followup").sum()),
        "pending": int((df["status"] == "Pending").sum()),
        "scheduled": int((df["status"] == "Scheduled").sum()),
    }


def generate_manager_summary(entry_date):
    df = get_tasks_for_date(entry_date)
    followups = get_followups()
    pending = get_pending()

    if df.empty and followups.empty and pending.empty:
        return f"No entries logged for {entry_date}."

    label = entry_date.strftime('%d %b %Y') if hasattr(entry_date, 'strftime') else entry_date
    lines = [f"📋 Work Summary — {label}", ""]

    def task_line(row):
        parts = [f"  • {row['task']}"]
        tag = []
        if row.get("category"):
            tag.append(str(row["category"]))
        st = parse_ts(row.get("start_time"))
        en = parse_ts(row.get("end_time"))
        if st:
            tag.append(f"{st.strftime('%H:%M')}–{en.strftime('%H:%M') if en else '...'}")
        wm = row.get("worked_minutes")
        if wm is not None:
            tag.append(f"worked {format_minutes(wm)}")
        pm = row.get("pause_minutes")
        if pm:
            tag.append(f"pause {format_minutes(pm)}")
        if tag:
            parts.append(f" ({', '.join(tag)})")
        return "".join(parts)

    for status, emoji in [("Completed", "✅"), ("In Progress", "🔄"), ("Paused", "⏸️")]:
        subset = df[df["status"] == status] if not df.empty else df
        if not subset.empty:
            lines.append(f"{emoji} {status}:")
            for _, row in subset.iterrows():
                lines.append(task_line(row))
            lines.append("")

    if not df.empty:
        total_worked = df["worked_minutes"].dropna().sum()
        total_pause = df["pause_minutes"].fillna(0).sum()
        if total_worked or total_pause:
            lines.append(f"⏱️ Total worked: {format_minutes(total_worked)}" +
                          (f"  |  Total pause/break: {format_minutes(total_pause)}" if total_pause else ""))
            lines.append("")

    if not followups.empty:
        lines.append("🔁 Followup:")
        for _, row in followups.iterrows():
            due = f" (follow up {row['followup_date']})" if row.get("followup_date") else ""
            lines.append(f"  • {row['task']}{due}")
        lines.append("")

    if not pending.empty:
        lines.append("⏳ Pending:")
        for _, row in pending.iterrows():
            lines.append(f"  • {row['task']}" + (f" — {row['notes']}" if row.get('notes') else ""))
        lines.append("")

    return "\n".join(lines)


def compute_category_breakdown(start_date, end_date):
    df = get_tasks(start_date, end_date)
    if df.empty:
        return pd.DataFrame(columns=["Category", "Count"])
    g = df.groupby("category").size().reset_index(name="Count")
    g = g.rename(columns={"category": "Category"})
    return g.sort_values("Count", ascending=False)


def compute_status_breakdown(start_date, end_date):
    df = get_tasks(start_date, end_date)
    if df.empty:
        return pd.DataFrame(columns=["Status", "Count"])
    g = df.groupby("status").size().reset_index(name="Count")
    g = g.rename(columns={"status": "Status"})
    return g


def compute_daily_task_counts(start_date, end_date):
    df = get_tasks(start_date, end_date)
    if df.empty:
        return pd.DataFrame(columns=["Date", "Count"])
    g = df.groupby("entry_date").size().reset_index(name="Count")
    g = g.rename(columns={"entry_date": "Date"})
    return g.sort_values("Date")


def get_all_tasks_for_export():
    """
    Full history of every task ever logged, full detail for Excel export:
    Date, Category, Task, Status, Start Time, Completion Date & Time,
    Pause Time, Followup Date, Remarks.
    """
    conn = get_conn()
    df = _read_sql("SELECT * FROM worklog_tasks ORDER BY entry_date DESC, id DESC", conn)
    conn.close()
    if df.empty:
        return pd.DataFrame(columns=[
            "Date", "Category", "Task", "Status", "Start Time",
            "Completion Date & Time", "Pause Time", "Followup Date", "Remarks"
        ])

    def completion_val(row):
        if row["status"] == "Completed" and row.get("end_time"):
            return row["end_time"]
        if row["status"] == "Scheduled" and row.get("scheduled_date"):
            return f"Scheduled for {row['scheduled_date']}"
        return ""

    def pause_val(row):
        pm = row.get("pause_minutes")
        if pm:
            return format_minutes(pm)
        return ""

    out = pd.DataFrame({
        "Date": df["entry_date"],
        "Category": df["category"],
        "Task": df["task"],
        "Status": df["status"],
        "Start Time": df["start_time"].fillna(""),
        "Completion Date & Time": df.apply(completion_val, axis=1),
        "Pause Time": df.apply(pause_val, axis=1),
        "Followup Date": df["followup_date"].fillna(""),
        "Remarks": df["notes"].fillna(""),
    })
    return out


def export_all_tasks_excel(path):
    """Writes the full task history to an .xlsx file at `path`. Returns the row count."""
    df = get_all_tasks_for_export()
    df.to_excel(path, index=False, sheet_name="Work Log")
    return len(df)
