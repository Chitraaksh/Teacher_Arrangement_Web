import streamlit as st
import pandas as pd
import json
import re
import pymongo
from io import BytesIO
from datetime import date, timedelta

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
st.set_page_config(page_title="Teacher Arrangement System", page_icon="🏫", layout="wide")

DAYS_MAP = {'1': 'monday', '2': 'tuesday', '3': 'wednesday', '4': 'thursday', '5': 'friday', '6': 'saturday'}
MISC_KEYWORDS = ["TGT-ART", "TGT-MUSIC", "PET(M)", "PET(F)", "LIBRARIAN", "TGT-CS", "PGT-CS", "COUNSELOR", "SKILL/IT"]

# ==========================================
# DATABASE CONNECTION
# ==========================================
@st.cache_resource
def init_connection():
    """Initializes the MongoDB connection using Streamlit Secrets."""
    return pymongo.MongoClient(st.secrets["MONGO_URI"])

def get_db_collection():
    client = init_connection()
    return client.school_db.app_data

def load_from_cloud():
    """Fetches data. Returns True if successful, False otherwise."""
    try:
        collection = get_db_collection()
        data = collection.find_one({"_id": "master_schedule"})
        if data and data.get("schedule"):
            st.session_state.schedule = data.get("schedule")
            st.session_state.rules = data.get("rules", {"exceptions": [], "double_booking_exceptions": []})
            return True
    except Exception as e:
        print(f"DB Load Error: {e}")
    return False

def save_to_cloud():
    """Saves current state to MongoDB."""
    collection = get_db_collection()
    collection.update_one(
        {"_id": "master_schedule"},
        {"$set": {
            "schedule": st.session_state.schedule,
            "rules": st.session_state.rules
        }},
        upsert=True
    )

# ==========================================
# SESSION STATE INITIALIZATION & AUTO-LOAD
# ==========================================
if 'initialized' not in st.session_state:
    st.session_state.initialized = True
    st.session_state.schedule = None
    st.session_state.rules = {"exceptions": [], "double_booking_exceptions": []}
    st.session_state.conflicts = []
    st.session_state.arrangements = {}

    # EXACT LOGIC: If found in DB, go to main app. Else ask for upload.
    if load_from_cloud():
        st.session_state.phase = 'setup'
    else:
        st.session_state.phase = 'upload'

# ==========================================
# CORE ENGINE / PARSING LOGIC (excel_parser.py)
# ==========================================
def is_clash_exception(teacher1: str, teacher2: str, class_name: str) -> bool:
    c_name = str(class_name).upper()
    for exc in st.session_state.rules.get("exceptions", []):
        if exc["class_name"].upper() == c_name and {exc["teacher1"], exc["teacher2"]} == {teacher1, teacher2}:
            return True
    return False

def is_double_booking_exception(teacher: str, class1: str, class2: str) -> bool:
    t, c1, c2 = teacher.upper(), str(class1).upper(), str(class2).upper()
    for exc in st.session_state.rules.get("double_booking_exceptions", []):
        if exc["teacher"].upper() == t and {exc["class1"].upper(), exc["class2"].upper()} == {c1, c2}:
            return True
    return False

def parse_schedule(uploaded_file):
    df = pd.read_excel(uploaded_file, dtype=str, header=None).fillna('')
    schedule_data = {}
    inverted_index = {day: {} for day in DAYS_MAP.values()}
    conflicts = []

    for _, row in df.iterrows():
        designation = str(row.iloc[0]).strip()
        if not designation or designation.lower() in ['designation', 'post', 'teacher']: continue
        name = str(row.iloc[1]).strip()
        if name.lower() in ['name', 'teacher name']: name = ''

        teacher_key = f"{designation} {name}".strip()
        schedule_data[teacher_key] = {'category': "miscellaneous" if any(kw in designation.upper() for kw in MISC_KEYWORDS) else "main"}
        if name: schedule_data[teacher_key]['name'] = name
        for day in DAYS_MAP.values(): schedule_data[teacher_key][day] = {}

        for col_idx in range(2, len(df.columns)):
            period_num = str(col_idx - 1)
            cell_val = str(row.iloc[col_idx]).strip()
            if not cell_val or cell_val.lower().startswith('period'): continue

            for match in re.finditer(r'([A-Za-z0-9]+)\s*\(\s*([\d\-\s]+)\s*\)', cell_val):
                class_name = match.group(1).strip()
                for day_num in re.split(r'\s*-\s*', match.group(2).strip()):
                    if day_num not in DAYS_MAP: continue
                    day_name = DAYS_MAP[day_num]

                    if period_num in schedule_data[teacher_key][day_name]:
                        existing_class = schedule_data[teacher_key][day_name][period_num]
                        if class_name not in existing_class.split(','):
                            if not is_double_booking_exception(teacher_key, existing_class, class_name):
                                conflicts.append({"type": "double_booking", "teacher": teacher_key, "class1": existing_class, "class2": class_name, "day": day_name, "period": period_num})
                                continue
                            else:
                                schedule_data[teacher_key][day_name][period_num] = f"{existing_class},{class_name}"
                            continue

                    if period_num not in inverted_index[day_name]: inverted_index[day_name][period_num] = {}

                    if class_name in inverted_index[day_name][period_num]:
                        existing_teacher = inverted_index[day_name][period_num][class_name]
                        if not is_clash_exception(existing_teacher, teacher_key, class_name):
                            conflicts.append({"type": "clash", "teacher1": existing_teacher, "teacher2": teacher_key, "class_name": class_name, "day": day_name, "period": period_num})
                            continue

                    inverted_index[day_name][period_num][class_name] = teacher_key
                    schedule_data[teacher_key][day_name][period_num] = class_name
    return schedule_data, conflicts

def get_target_date(day_name: str) -> str:
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    days_ahead = days.index(day_name.lower()) - date.today().weekday()
    if days_ahead < 0: days_ahead += 7
    return (date.today() + timedelta(days=days_ahead)).strftime("%d-%b-%Y")

# ==========================================
# UI PHASES
# ==========================================
def ui_upload():
    st.title("Teacher Arrangement System")
    st.warning("No Master Schedule found in the cloud Database. Please upload an Excel schedule to begin.")

    sched_file = st.file_uploader("Upload Master Schedule (.xlsx)", type=["xlsx"])
    if sched_file and st.button("Process Schedule", type="primary"):
        sched, conf = parse_schedule(sched_file)
        if conf:
            st.session_state.conflicts = conf
            st.session_state.temp_schedule = sched
            st.session_state.phase = 'resolve'
            st.rerun()
        else:
            st.session_state.schedule = sched
            save_to_cloud() # Instantly back it up
            st.session_state.phase = 'setup'
            st.rerun()

def ui_resolve():
    st.title("Resolve Schedule Conflicts")
    st.warning(f"Found {len(st.session_state.conflicts)} conflicts.")

    for i, c in enumerate(st.session_state.conflicts):
        st.error(f"Conflict {i+1}: {c['type'].replace('_', ' ').title()} - Day: {c['day'].capitalize()} | Period: {c['period']}")
        if c['type'] == 'clash':
            st.write(f"Teachers: **{c['teacher1']}** & **{c['teacher2']}** mapped to Class **{c['class_name']}**")
            if st.button(f"Allow & Add Rule (Clash {i+1})", key=f"btn_c_{i}"):
                st.session_state.rules['exceptions'].append({"teacher1": c['teacher1'], "teacher2": c['teacher2'], "class_name": c['class_name']})
                st.rerun()
        elif c['type'] == 'double_booking':
            st.write(f"Teacher **{c['teacher']}** booked for **{c['class1']}** & **{c['class2']}**")
            if st.button(f"Allow & Add Rule (DB {i+1})", key=f"btn_db_{i}"):
                st.session_state.rules['double_booking_exceptions'].append({"teacher": c['teacher'], "class1": c['class1'], "class2": c['class2']})
                st.rerun()

    st.divider()
    if st.button("Re-evaluate Schedule with new rules"):
        st.session_state.phase = 'upload'
        st.rerun()

def ui_setup():
    st.title("Select Day & Absentees")

    with st.sidebar:
        st.header("Workspace Data")
        st.success("Currently connected to Cloud Workspace.")
        if st.button("💾 Force Save to Cloud"):
            save_to_cloud()
            st.toast("Data synchronized to MongoDB!", icon="✅")

        st.divider()
        st.write("Need to upload a new master schedule?")
        if st.button("Upload New Excel"):
            st.session_state.phase = 'upload'
            st.rerun()

    st.session_state.selected_day = st.selectbox("Select Day:", ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"])
    all_teachers = sorted(list(st.session_state.schedule.keys()))
    st.session_state.absent_teachers = st.multiselect("Select Absent Teachers:", all_teachers)

    total_periods = 0
    if st.session_state.absent_teachers:
        for t in st.session_state.absent_teachers:
            periods = st.session_state.schedule.get(t, {}).get(st.session_state.selected_day, {})
            total_periods += len([p for p in periods.keys() if str(p).isdigit()])

    st.metric("Total Periods to Arrange", total_periods)

    if st.button("Continue to Arrangement", type="primary") and st.session_state.absent_teachers:
        st.session_state.phase = 'arrange'
        st.rerun()

# EXACT PORT of gui.py -> refresh_all_comboboxes()
def compute_candidates(p_str, c_name, absent, day_schedule):
    busy_for_others = set()
    extra_assignments = {t: 0 for t in st.session_state.schedule.keys()}

    for key, sel in st.session_state.arrangements.items():
        if key[1] == p_str and sel and sel != "Self Study":
            t_name = sel.split(" (")[0] if "(Co-teacher)" in sel or "(Combine w/" in sel else sel.rsplit(" (", 1)[0]
            if key[0] != absent: busy_for_others.add(t_name)
            if "(Combine w/" not in sel and "(Co-teacher)" not in sel:
                extra_assignments[t_name] = extra_assignments.get(t_name, 0) + 1

    candidates = ["Self Study"]
    for ct in day_schedule.get(p_str, {}).get(c_name, []):
        if ct != absent and ct not in st.session_state.absent_teachers and ct not in busy_for_others:
            candidates.append(f"{ct} (Co-teacher)")

    if ',' not in c_name:
        match = re.match(r'^(\d+)([A-Za-z]?)$', c_name)
        if match:
            class_num, section = int(match.group(1)), match.group(2).upper()
            if section:
                parallel_class = f"{class_num}{'B' if section == 'A' else 'A'}"
                for pt in day_schedule.get(p_str, {}).get(parallel_class, []):
                    if pt not in st.session_state.absent_teachers and pt not in busy_for_others:
                        candidates.append(f"{pt} (Combine w/ {parallel_class})")

    free_list = []
    for t in st.session_state.schedule.keys():
        if t in st.session_state.absent_teachers or str(p_str) in st.session_state.schedule[t].get(st.session_state.selected_day, {}) or t in busy_for_others:
            continue
        base_load = len([x for x in st.session_state.schedule[t].get(st.session_state.selected_day, {}).keys() if str(x).isdigit()])
        free_list.append((t, base_load + extra_assignments.get(t, 0)))

    free_list.sort(key=lambda x: x[1])
    candidates.extend([f"{t} ({load} periods)" for t, load in free_list])
    return candidates

def ui_arrange():
    day = st.session_state.selected_day
    st.title(f"Make Arrangements ({day.capitalize()})")

    day_schedule = {}
    max_period = 0
    for t, t_data in st.session_state.schedule.items():
        for p, c in t_data.get(day, {}).items():
            if str(p).isdigit():
                max_period = max(max_period, int(p))
                if p not in day_schedule: day_schedule[p] = {}
                if c not in day_schedule[p]: day_schedule[p][c] = []
                day_schedule[p][c].append(t)
    if max_period == 0: max_period = 8

    cols = st.columns([2] + [1] * max_period)
    cols[0].markdown("**Absent Teacher**")
    for p in range(1, max_period + 1): cols[p].markdown(f"**Period {p}**")

    for absent in st.session_state.absent_teachers:
        cols = st.columns([2] + [1] * max_period)
        cols[0].write(absent)
        periods = st.session_state.schedule.get(absent, {}).get(day, {})

        for p in range(1, max_period + 1):
            p_str = str(p)
            with cols[p]:
                if p_str in periods:
                    c_name = periods[p_str]
                    st.caption(c_name)
                    state_key = (absent, p_str)
                    candidates = compute_candidates(p_str, c_name, absent, day_schedule)
                    current_val = st.session_state.arrangements.get(state_key, "Self Study")

                    idx = next((i for i, cand in enumerate(candidates) if cand.split(' (')[0] == current_val.split(' (')[0]), 0)
                    sel = st.selectbox("Substitute", candidates, index=idx, key=f"sel_{absent}_{p}", label_visibility="collapsed")
                    st.session_state.arrangements[state_key] = sel
                else:
                    st.write("-")

    st.divider()
    if st.button("Finalize & Export", type="primary"):
        st.session_state.max_period = max_period
        st.session_state.phase = 'export'
        st.rerun()

def generate_excel():
    day = st.session_state.selected_day
    max_period = st.session_state.max_period
    period_columns = [str(i) for i in range(1, max_period + 1)]

    arrangements_mapped = {a: {} for a in st.session_state.absent_teachers}
    detailed_assignments = {t: {} for t in st.session_state.schedule.keys() if t not in st.session_state.absent_teachers}

    for (absent, p), selection in st.session_state.arrangements.items():
        c_name = st.session_state.schedule[absent][day][p]
        if selection == "Self Study": arrangements_mapped[absent][p] = f"{c_name} - Self Study"
        elif "(Co-teacher)" in selection:
            sub_name = selection.split(" (")[0]
            arrangements_mapped[absent][p] = f"{c_name} - {sub_name} (Co-teacher)"
            detailed_assignments[sub_name][p] = f"{c_name} (Covering Co-teacher)"
        elif "(Combine w/" in selection:
            sub_name = selection.split(" (")[0]
            parallel = selection.split("w/ ")[1].replace(")", "")
            arrangements_mapped[absent][p] = f"{c_name} - {sub_name} (Combined w/ {parallel})"
            detailed_assignments[sub_name][p] = f"{c_name} (Combined w/ {parallel})"
        else:
            sub_name = selection.rsplit(" (", 1)[0]
            arrangements_mapped[absent][p] = f"{c_name} - {sub_name}"
            detailed_assignments[sub_name][p] = f"{c_name} (Arrangement)"

    data1 = [{"Absent Teacher": a, **{f"Period {p}": arrangements_mapped[a].get(p, "") if p in st.session_state.schedule[a].get(day, {}) else "" for p in period_columns}} for a in sorted(arrangements_mapped.keys())]
    df1 = pd.DataFrame(data1)

    active_subs = [t for t, assigns in detailed_assignments.items() if assigns]
    data2 = []
    for sub in sorted(active_subs):
        row = {"Substitute Teacher": sub}
        engaged_count = 0
        for p in period_columns:
            if p in detailed_assignments[sub]:
                row[f"Period {p}"] = detailed_assignments[sub][p]
                engaged_count += 1
            elif p in st.session_state.schedule[sub].get(day, {}):
                row[f"Period {p}"] = st.session_state.schedule[sub][day][p]
                engaged_count += 1
            else: row[f"Period {p}"] = "Free"
        row["Total Engaged"], row["Total Free"] = engaged_count, max_period - engaged_count
        data2.append(row)
    df2 = pd.DataFrame(data2)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df1.to_excel(writer, sheet_name='Daily Arrangement', index=False)
        if not df2.empty:
            writer.sheets['Daily Arrangement'].cell(row=len(df1) + 3, column=1, value="SUBSTITUTE TEACHER DAILY SCHEDULES (Printable)")
            df2.to_excel(writer, sheet_name='Daily Arrangement', index=False, startrow=len(df1) + 3)
    return output.getvalue()

def ui_export():
    st.title("Export Complete!")
    st.success("Your schedule has been calculated successfully.")

    excel_data = generate_excel()
    file_name = f"Arrangement_{st.session_state.selected_day.capitalize()}_{get_target_date(st.session_state.selected_day)}.xlsx"

    st.download_button("Download Excel File", data=excel_data, file_name=file_name, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
    st.divider()
    if st.button("Start New Arrangement"):
        st.session_state.phase = 'setup'
        st.session_state.arrangements = {}
        st.rerun()

# ==========================================
# MAIN ROUTING
# ==========================================
if st.session_state.phase == 'upload': ui_upload()
elif st.session_state.phase == 'resolve': ui_resolve()
elif st.session_state.phase == 'setup': ui_setup()
elif st.session_state.phase == 'arrange': ui_arrange()
elif st.session_state.phase == 'export': ui_export()
