"""
Work Log Tracker - Streamlit App
Live start/pause/resume/complete task timer + manager-ready summaries.
"""
import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta, datetime
import hashlib
from data_layer import (
    add_task, add_scheduled_task, promote_due_tasks, get_scheduled_tasks,
    start_task, pause_task, resume_task, complete_task,
    mark_followup, mark_pending, reopen_task, delete_task, update_notes,
    update_task_details,
    get_tasks, get_tasks_for_date, get_followups, get_pending,
    get_week_range, compute_daily_summary, generate_manager_summary,
    compute_category_breakdown, compute_status_breakdown, compute_daily_task_counts,
    get_worked_minutes, format_minutes, today_ist, get_all_tasks_for_export,
    parse_ts,
    CATEGORIES, STATUSES
)
import io

st.set_page_config(page_title="Work Log Tracker", layout="wide", page_icon="📝")

# ---------- Simple shared login ----------
DASH_USER = "hradmin"
DASH_PASS_HASH = "bcb70742aad2b11dddb9cc1708e1b918199f8484f3d4f30aeece88d140cfd04a"

def check_login():
    if st.session_state.get("wl_authenticated"):
        return True
    st.title("🔒 Work Log Tracker — Login")
    with st.form("login_form"):
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")
        if submitted:
            if u == DASH_USER and hashlib.sha256(p.encode()).hexdigest() == DASH_PASS_HASH:
                st.session_state["wl_authenticated"] = True
                st.rerun()
            else:
                st.error("Invalid username or password.")
    return False

if not check_login():
    st.stop()

# ---------- Promote due calendar/followup tasks into Pending ----------
promote_due_tasks()

# ---------- Styling ----------
st.markdown("""
<style>
    div[data-testid="stMetric"] {
        background-color: #1e293b;
        border-radius: 10px;
        padding: 14px 16px;
        border: 1px solid #334155;
    }
    [data-testid="stMetricLabel"] { color: #cbd5e1 !important; }
    [data-testid="stMetricLabel"] p { color: #cbd5e1 !important; }
    [data-testid="stMetricValue"] { color: #f8fafc !important; font-weight: 700 !important; }
    [data-testid="stMetricValue"] div { color: #f8fafc !important; }
    .task-card {
        background: #1e293b; border: 1px solid #334155; border-radius: 10px;
        padding: 12px 16px; margin-bottom: 10px;
    }
</style>
""", unsafe_allow_html=True)

STATUS_EMOJI = {
    "Not Started": "⬜", "In Progress": "🔄", "Paused": "⏸️",
    "Completed": "✅", "Followup": "🔁", "Pending": "⏳", "Scheduled": "📅"
}

# ---------- Sidebar navigation ----------
st.sidebar.title("📝 Work Log Tracker")
page = st.sidebar.radio(
    "Navigate",
    ["🏠 Today's Tasks", "📅 Calendar", "🔁 Followup & Pending", "📅 Daily View", "📊 Weekly/Monthly Summary", "📤 Manager Summary"],
    label_visibility="collapsed"
)

st.sidebar.divider()
_export_df = get_all_tasks_for_export()
_buf = io.BytesIO()
_export_df.to_excel(_buf, index=False, sheet_name="Work Log")
_buf.seek(0)
st.sidebar.download_button(
    "⬇️ Download Excel (full history)",
    data=_buf,
    file_name=f"work_log_{today_ist()}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    use_container_width=True
)


def _val(row, key):
    """Get a value from a pandas row, treating NaN/None/empty as absent."""
    v = row.get(key)
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if v == "":
        return None
    return v


def render_edit_form(row, key_prefix, category_key=None):
    """Inline editable form for task description + category + remarks + timing."""
    tid = row["id"]
    if st.session_state.get(f"show_edit_{key_prefix}_{tid}"):
        with st.form(f"edit_form_{key_prefix}_{tid}"):
            e_task = st.text_input("Task Description", value=row["task"], key=f"edit_task_{key_prefix}_{tid}")
            cur_cat = row.get("category") if row.get("category") in CATEGORIES else CATEGORIES[0]
            e_cat = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(cur_cat), key=f"edit_cat_{key_prefix}_{tid}")
            e_notes = st.text_input("Remarks", value=_val(row, "notes") or "", key=f"edit_notes_{key_prefix}_{tid}")

            st.caption("⏱️ Time (edit if the automatic tracking needs correction)")
            cur_start = parse_ts(_val(row, "start_time"))
            cur_end = parse_ts(_val(row, "end_time"))
            cur_pause = _val(row, "pause_minutes") or 0

            tc1, tc2 = st.columns(2)
            with tc1:
                has_start = st.checkbox("Has start time", value=cur_start is not None, key=f"edit_hasstart_{key_prefix}_{tid}")
                e_start_date = st.date_input("Start date", value=(cur_start.date() if cur_start else today_ist()), key=f"edit_startdate_{key_prefix}_{tid}", disabled=not has_start)
                e_start_time = st.time_input("Start time", value=(cur_start.time() if cur_start else datetime.now().time().replace(second=0, microsecond=0)), key=f"edit_starttime_{key_prefix}_{tid}", disabled=not has_start)
            with tc2:
                has_end = st.checkbox("Has end time", value=cur_end is not None, key=f"edit_hasend_{key_prefix}_{tid}")
                e_end_date = st.date_input("End date", value=(cur_end.date() if cur_end else today_ist()), key=f"edit_enddate_{key_prefix}_{tid}", disabled=not has_end)
                e_end_time = st.time_input("End time", value=(cur_end.time() if cur_end else datetime.now().time().replace(second=0, microsecond=0)), key=f"edit_endtime_{key_prefix}_{tid}", disabled=not has_end)

            e_pause = st.number_input("Pause time (minutes)", min_value=0, value=int(round(cur_pause)), step=1, key=f"edit_pause_{key_prefix}_{tid}")

            ec1, ec2 = st.columns(2)
            save = ec1.form_submit_button("💾 Save Changes", use_container_width=True, type="primary")
            cancel = ec2.form_submit_button("✖ Cancel", use_container_width=True)
            if save:
                if not e_task.strip():
                    st.error("Task description cannot be empty.")
                else:
                    new_start = datetime.combine(e_start_date, e_start_time) if has_start else None
                    new_end = datetime.combine(e_end_date, e_end_time) if has_end else None
                    if new_start and new_end and new_end < new_start:
                        st.error("End time cannot be before start time.")
                    else:
                        update_task_details(
                            tid, task=e_task.strip(), category=e_cat, notes=e_notes.strip(),
                            start_time=new_start, end_time=new_end, pause_minutes=e_pause
                        )
                        st.session_state[f"show_edit_{key_prefix}_{tid}"] = False
                        st.rerun()
            if cancel:
                st.session_state[f"show_edit_{key_prefix}_{tid}"] = False
                st.rerun()


def render_task_card(row):
    status = row["status"]
    wm = _val(row, "worked_minutes")
    worked_str = format_minutes(wm) if wm is not None else "-"
    tid = row["id"]

    with st.container():
        st.markdown('<div class="task-card">', unsafe_allow_html=True)
        c1, c2, c3 = st.columns([5, 2, 3])
        with c1:
            st.markdown(f"**{STATUS_EMOJI.get(status,'')} {row['task']}**")
            st.caption(f"{row['category']}  •  {status}" + (f"  •  worked {worked_str}" if wm else ""))
        with c2:
            st_time = _val(row, "start_time")
            if st_time:
                st.caption(f"Start: {st_time[11:16]}")
            en_time = _val(row, "end_time")
            if en_time:
                st.caption(f"End: {en_time[11:16]}")
            pm = _val(row, "pause_minutes")
            if pm:
                st.caption(f"Pause: {format_minutes(pm)}")
        with c3:
            b1, b2, b3, b4, b5 = st.columns(5)
            if status == "Not Started":
                if b1.button("▶️", key=f"start_{tid}", help="Start"):
                    start_task(tid); st.rerun()
            if status == "In Progress":
                if b1.button("⏸️", key=f"pause_{tid}", help="Pause"):
                    pause_task(tid); st.rerun()
                if b2.button("✅", key=f"complete_{tid}", help="Complete"):
                    complete_task(tid); st.rerun()
            if status == "Paused":
                if b1.button("▶️", key=f"resume_{tid}", help="Resume"):
                    resume_task(tid); st.rerun()
                if b2.button("✅", key=f"complete2_{tid}", help="Complete"):
                    complete_task(tid); st.rerun()
            if status in ("In Progress", "Paused", "Not Started"):
                if b3.button("🔁", key=f"fu_{tid}", help="Mark Followup"):
                    st.session_state[f"show_fu_date_{tid}"] = True
                if b4.button("⏳", key=f"pend_{tid}", help="Mark Pending"):
                    mark_pending(tid); st.rerun()
            if b5.button("✏️", key=f"edit_{tid}", help="Edit task"):
                st.session_state[f"show_edit_main_{tid}"] = not st.session_state.get(f"show_edit_main_{tid}", False)
                st.rerun()
            if status in ("Completed", "Followup", "Pending"):
                if b1.button("🗑️", key=f"del_{tid}", help="Delete"):
                    delete_task(tid); st.rerun()

        render_edit_form(row, "main")

        # Followup date picker (shown only after clicking 🔁)
        if st.session_state.get(f"show_fu_date_{tid}"):
            fc1, fc2 = st.columns([3, 1])
            with fc1:
                fu_date = st.date_input("Follow up on date", value=today_ist() + timedelta(days=1), key=f"fu_date_input_{tid}")
            with fc2:
                st.write("")
                if st.button("✔ Confirm", key=f"fu_confirm_{tid}"):
                    mark_followup(tid, followup_date=fu_date)
                    st.session_state[f"show_fu_date_{tid}"] = False
                    st.rerun()

        # Remarks / notes — always editable per task
        notes_val = _val(row, "notes") or ""
        remark_val = st.text_input("Remarks", value=notes_val, key=f"remark_{tid}", placeholder="Enter remarks for this task")
        if remark_val != notes_val:
            if st.button("💾 Save Remarks", key=f"save_remark_{tid}"):
                update_notes(tid, remark_val)
                st.rerun()

        st.markdown("</div>", unsafe_allow_html=True)


# ==========================================================================
# TODAY'S TASKS
# ==========================================================================
if page == "🏠 Today's Tasks":
    st.title("Today's Tasks")

    with st.form("add_task_form", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        with c1:
            task = st.text_input("Task Description *", placeholder="e.g. Process monthly payroll for Chennai branch")
        with c2:
            category = st.selectbox("Category", CATEGORIES)
        notes = st.text_input("Remarks (optional)")
        submitted = st.form_submit_button("➕ Add Task", type="primary", use_container_width=True)
        if submitted:
            if not task.strip():
                st.error("Task description is required.")
            else:
                add_task(today_ist(), category, task.strip(), notes.strip())
                st.success(f"Added: {task}")
                st.rerun()

    st.divider()
    st.caption("⏱️ Time is tracked automatically: click ▶️ Start when you begin, ⏸️ Pause for breaks/interruptions, ▶️ again to resume, ✅ when done. 🔁 = Followup, ⏳ = Pending.")

    df = get_tasks_for_date(today_ist())
    if df.empty:
        st.info("No tasks yet today. Add your first one above.")
    else:
        active = df[df["status"].isin(["Not Started", "In Progress", "Paused"])]
        done = df[df["status"] == "Completed"]

        if not active.empty:
            st.subheader("Active")
            for _, row in active.iterrows():
                render_task_card(row)

        if not done.empty:
            st.subheader("Completed Today")
            for _, row in done.iterrows():
                render_task_card(row)

# ==========================================================================
# CALENDAR
# ==========================================================================
elif page == "📅 Calendar":
    st.title("Calendar — Schedule Tasks for Future Days")
    st.caption("Schedule a task for any date. When that day begins, it automatically moves into your Pending list.")

    with st.form("add_scheduled_form", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        with c1:
            sched_task = st.text_input("Task Description *", placeholder="e.g. Submit PF statutory filing")
        with c2:
            sched_category = st.selectbox("Category", CATEGORIES, key="sched_cat")
        sched_date = st.date_input("Schedule for date", value=today_ist() + timedelta(days=1))
        sched_notes = st.text_input("Remarks (optional)", key="sched_notes")
        submitted = st.form_submit_button("📅 Add to Calendar", type="primary", use_container_width=True)
        if submitted:
            if not sched_task.strip():
                st.error("Task description is required.")
            else:
                add_scheduled_task(sched_date, sched_category, sched_task.strip(), sched_notes.strip())
                st.success(f"Scheduled for {sched_date.strftime('%d %b %Y')}: {sched_task}")
                st.rerun()

    st.divider()
    sched_df = get_scheduled_tasks()
    if sched_df.empty:
        st.info("No tasks scheduled for future dates.")
    else:
        st.subheader("Upcoming Scheduled Tasks")
        for _, row in sched_df.iterrows():
            with st.container():
                st.markdown(f"**📅 {row['task']}**  —  {row['category']}")
                st.caption(f"Scheduled for: {row['scheduled_date']}")
                notes_v = _val(row, "notes")
                if notes_v:
                    st.caption(f"📝 {notes_v}")
                cs1, cs2 = st.columns(2)
                if cs1.button("✏️ Edit", key=f"edit_sched_{row['id']}"):
                    st.session_state[f"show_edit_sched_{row['id']}"] = not st.session_state.get(f"show_edit_sched_{row['id']}", False)
                    st.rerun()
                if cs2.button("🗑️ Remove", key=f"del_sched_{row['id']}"):
                    delete_task(row['id']); st.rerun()
                render_edit_form(row, "sched")
                st.divider()

# ==========================================================================
# FOLLOWUP & PENDING
# ==========================================================================
elif page == "🔁 Followup & Pending":
    st.title("Followup & Pending Tasks")

    tab1, tab2 = st.tabs(["🔁 Followup", "⏳ Pending"])

    with tab1:
        fu = get_followups()
        if fu.empty:
            st.info("No followup tasks.")
        else:
            for _, row in fu.iterrows():
                with st.container():
                    st.markdown(f"**🔁 {row['task']}**  —  {row['category']}")
                    due = _val(row, "followup_date")
                    st.caption(f"Follow up on: {due}" if due else "No follow-up date set")
                    notes_val = _val(row, "notes") or ""
                    remark_val = st.text_input("Remarks", value=notes_val, key=f"remark_fu_{row['id']}")
                    if remark_val != notes_val:
                        if st.button("💾 Save Remarks", key=f"save_remark_fu_{row['id']}"):
                            update_notes(row['id'], remark_val); st.rerun()
                    c1, c2, c3 = st.columns(3)
                    if c1.button("↩️ Reopen (start again)", key=f"reopen_fu_{row['id']}"):
                        reopen_task(row['id']); st.rerun()
                    if c2.button("✏️ Edit", key=f"edit_fu_{row['id']}"):
                        st.session_state[f"show_edit_fu_{row['id']}"] = not st.session_state.get(f"show_edit_fu_{row['id']}", False)
                        st.rerun()
                    if c3.button("🗑️ Remove", key=f"del_fu_{row['id']}"):
                        delete_task(row['id']); st.rerun()
                    render_edit_form(row, "fu")
                    st.divider()

    with tab2:
        pend = get_pending()
        if pend.empty:
            st.info("No pending tasks.")
        else:
            for _, row in pend.iterrows():
                with st.container():
                    st.markdown(f"**⏳ {row['task']}**  —  {row['category']}")
                    notes_val = _val(row, "notes") or ""
                    remark_val = st.text_input("Remarks", value=notes_val, key=f"remark_pd_{row['id']}")
                    if remark_val != notes_val:
                        if st.button("💾 Save Remarks", key=f"save_remark_pd_{row['id']}"):
                            update_notes(row['id'], remark_val); st.rerun()
                    c1, c2, c3 = st.columns(3)
                    if c1.button("↩️ Reopen (start again)", key=f"reopen_pd_{row['id']}"):
                        reopen_task(row['id']); st.rerun()
                    if c2.button("✏️ Edit", key=f"edit_pd_{row['id']}"):
                        st.session_state[f"show_edit_pd_{row['id']}"] = not st.session_state.get(f"show_edit_pd_{row['id']}", False)
                        st.rerun()
                    if c3.button("🗑️ Remove", key=f"del_pd_{row['id']}"):
                        delete_task(row['id']); st.rerun()
                    render_edit_form(row, "pd")
                    st.divider()

# ==========================================================================
# DAILY VIEW
# ==========================================================================
elif page == "📅 Daily View":
    st.title("Daily View")
    view_date = st.date_input("Select Date", value=today_ist())

    summary = compute_daily_summary(view_date)
    df = get_tasks_for_date(view_date)
    total_worked = int(df["worked_minutes"].dropna().sum()) if not df.empty else 0
    total_pause = int(df["pause_minutes"].fillna(0).sum()) if not df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Tasks", summary["total_tasks"])
    c2.metric("Completed", summary["completed"])
    c3.metric("In Progress", summary["in_progress"] + summary["paused"])
    c4.metric("Worked Time", format_minutes(total_worked))
    c5.metric("Pause/Break", format_minutes(total_pause))

    st.divider()
    if df.empty:
        st.info("No entries for this date.")
    else:
        st.caption("✏️ Click Edit on any task below to correct its description, category, remarks, or timing — for today or any past date.")
        for _, row in df.sort_values("id").iterrows():
            render_task_card(row)

        st.divider()
        fig = px.pie(df, names="status", title="Status Breakdown", hole=0.4)
        st.plotly_chart(fig, use_container_width=True)

# ==========================================================================
# WEEKLY/MONTHLY SUMMARY
# ==========================================================================
elif page == "📊 Weekly/Monthly Summary":
    st.title("Weekly / Monthly Summary")

    range_type = st.radio("Range", ["This Week", "This Month", "Custom"], horizontal=True)

    if range_type == "This Week":
        start, end = get_week_range(today_ist())
    elif range_type == "This Month":
        start = today_ist().replace(day=1)
        end = today_ist()
    else:
        c1, c2 = st.columns(2)
        start = c1.date_input("From", value=today_ist() - timedelta(days=7))
        end = c2.date_input("To", value=today_ist())

    st.caption(f"Showing: {start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}")

    df = get_tasks(start, end)
    total_worked = int(df["worked_minutes"].dropna().sum()) if not df.empty else 0
    total_pause = int(df["pause_minutes"].fillna(0).sum()) if not df.empty else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Tasks", len(df))
    c2.metric("Completed", int((df["status"] == "Completed").sum()) if not df.empty else 0)
    c3.metric("Followup/Pending", int(df["status"].isin(["Followup", "Pending"]).sum()) if not df.empty else 0)
    c4.metric("Total Worked", format_minutes(total_worked))
    c5.metric("Total Pause/Break", format_minutes(total_pause))

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Tasks by Category")
        cat = compute_category_breakdown(start, end)
        if not cat.empty:
            fig = px.bar(cat, x="Count", y="Category", orientation="h", text="Count")
            fig.update_layout(yaxis=dict(autorange="reversed"))
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No data.")
    with col2:
        st.subheader("Status Breakdown")
        stat = compute_status_breakdown(start, end)
        if not stat.empty:
            fig2 = px.pie(stat, names="Status", values="Count", hole=0.4)
            st.plotly_chart(fig2, use_container_width=True)
        else:
            st.info("No data.")

    st.subheader("Tasks per Day")
    daily = compute_daily_task_counts(start, end)
    if not daily.empty:
        fig3 = px.bar(daily, x="Date", y="Count", text="Count")
        st.plotly_chart(fig3, use_container_width=True)

    with st.expander("All Entries in Range"):
        st.dataframe(df, use_container_width=True)
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download as CSV", csv, "worklog_export.csv", "text/csv")

# ==========================================================================
# MANAGER SUMMARY
# ==========================================================================
elif page == "📤 Manager Summary":
    st.title("Manager Summary")
    st.caption("Generate a clean, shareable end-of-day summary to send your manager.")

    summary_date = st.date_input("Date", value=today_ist())
    summary_text = generate_manager_summary(summary_date)

    st.text_area("Summary (copy this to send)", value=summary_text, height=350)

    st.download_button("⬇️ Download as Text", summary_text, f"summary_{summary_date}.txt", "text/plain")
    st.info("💡 Tip: Copy the text above and paste it into Telegram/WhatsApp/Email to send to your manager.")
