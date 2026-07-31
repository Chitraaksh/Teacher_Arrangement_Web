import streamlit as st
import pandas as pd
import json
import re
import pymongo
import certifi
from io import BytesIO
from datetime import date, timedelta

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
st.set_page_config(page_title="Teacher Arrangement System", layout="wide")

# Inject Custom CSS for Dropdown text wrapping, popover width, and app breadth
st.markdown("""
    <style>
    /* 1. Force text wrapping inside the SELECTED option of the selectbox */
    [data-baseweb="select"] > div {
        height: auto !important;
        min-height: 40px !important;
    }
    [data-baseweb="select"] span {
        white-space: normal !important;
        overflow-wrap: break-word !important;
        display: block !important;
        line-height: 1.2 !important;
    }
    
    /* 2. THE CSS BUG FIX: Force the Dropdown Popover to expand fully */
    /* BaseWeb injects an inline 'width' style via JS matching the selectbox. We MUST override the div wrapper, not just the UL */
    div[data-baseweb="popover"] > div {
        min-width: 470px !important; /* Force it to match your matrix column width */
        max-width: fit-content !important; 
    }
    div[data-baseweb="popover"] ul[data-baseweb="menu"] {
        max-width: none !important; 
        width: 100% !important;
    }
    div[data-baseweb="popover"] ul[data-baseweb="menu"] li[role="option"] {
        height: auto !important;
        min-height: 40px !important;
        padding-top: 8px !important;
        padding-bottom: 8px !important;
        display: flex !important;
        align-items: center !important;
        border-bottom: 1px solid #f0f2f6; 
    }
    div[data-baseweb="popover"] ul[data-baseweb="menu"] li[role="option"] span {
        white-space: normal !important;
        overflow-wrap: break-word !important;
        line-height: 1.3 !important;
        width: 100% !important;
    }
    
    /* 3. Increase Breadth (Width) of the entire app grid */
    .block-container {
        padding-left: 1rem !important;
        padding-right: 1rem !important;
        max-width: 100% !important;
    }
    
    /* Reduce gap between columns */
    [data-testid="column"] {
        padding-left: 0.3rem !important;
        padding-right: 0.3rem !important;
    }
    </style>
""", unsafe_allow_html=True)

DAYS_MAP = {
    '1': 'monday', '2': 'tuesday', '3': 'wednesday', 
    '4': 'thursday', '5': 'friday', '6': 'saturday'
}

MISC_KEYWORDS = [
    "TGT-ART", "TGT-MUSIC", "PET(M)", "PET(F)", "LIBRARIAN", 
    "TGT-CS", "PGT-CS", "COUNSELOR", "SKILL/IT"
]

# ==========================================
# DATABASE CONNECTION
# ==========================================
@st.cache_resource
def init_connection():
    try:
        return pymongo.MongoClient(
            st.secrets["mongo"]["uri"], 
            serverSelectionTimeoutMS=5000,
            tlsCAFile=certifi.where()
        )
    except Exception as e:
        st.error(f"Could not connect to Database. Please check your Streamlit Secrets. Error: {e}")
        st.stop()

db_client = init_connection()
db = db_client.teacher_arrangement

def save_rules_to_db(rules_dict):
    db.rules.replace_one({"_id": "main_rules"}, rules_dict, upsert=True)

def save_schedule_to_db(schedule_dict):
    db.schedule.replace_one({"_id": "main_schedule"}, schedule_dict, upsert=True)

def load_data_from_db():
    rules = db.rules.find_one({"_id": "main_rules"})
    schedule = db.schedule.find_one({"_id": "main_schedule"})
    
    if rules: rules.pop('_id', None)
    if schedule: schedule.pop('_id', None)
        
    return rules, schedule

def delete_schedule_from_db():
    db.schedule.delete_one({"_id": "main_schedule"})

# ==========================================
# SESSION STATE INITIALIZATION
# ==========================================
if 'initialized' not in st.session_state:
    db_rules, db_schedule = load_data_from_db()
    
    st.session_state.rules = db_rules if db_rules else {"exceptions": [], "double_booking_exceptions": []}
    st.session_state.schedule = db_schedule
    
    if st.session_state.schedule:
        st.session_state.phase = 'setup'
    else:
        st.session_state.phase = 'upload'
        
    st.session_state.conflicts = []
    st.session_state.arrangements = {}
    st.session_state.raw_file = None # Added for Fix 1.b
    st.session_state.initialized = True

# ==========================================
# CORE ENGINE / PARSING LOGIC
# ==========================================
def is_clash_exception(teacher1: str, teacher2: str, class_name: str) -> bool:
    c_name = str(class_name).upper()
    for exc in st.session_state.rules.get("exceptions", []):
        if exc["class_name"].upper() == c_name:
            if {exc["teacher1"], exc["teacher2"]} == {teacher1, teacher2}:
                return True
    return False

def is_double_booking_exception(teacher: str, class1: str, class2: str) -> bool:
    t = teacher.upper()
    c1, c2 = str(class1).upper(), str(class2).upper()
    for exc in st.session_state.rules.get("double_booking_exceptions", []):
        if exc["teacher"].upper() == t:
            if {exc["class1"].upper(), exc["class2"].upper()} == {c1, c2}:
                return True
    return False

def parse_schedule(file_bytes):
    df = pd.read_excel(file_bytes, dtype=str, header=None).fillna('')
    schedule_data = {}
    inverted_index = {day: {} for day in DAYS_MAP.values()}
    conflicts = []

    for _, row in df.iterrows():
        designation = str(row.iloc[0]).strip()
        if not designation or designation.lower() in ['designation', 'post', 'teacher']:
            continue
            
        name = str(row.iloc[1]).strip()
        if name.lower() in ['name', 'teacher name']: name = ''
            
        teacher_key = f"{designation} {name}".strip() 
        schedule_data[teacher_key] = {'category': 'main'}
        
        if name: schedule_data[teacher_key]['name'] = name
        if any(kw in designation.upper() for kw in MISC_KEYWORDS):
            schedule_data[teacher_key]['category'] = "miscellaneous"
            
        for day in DAYS_MAP.values(): schedule_data[teacher_key][day] = {}

        for col_idx in range(2, len(df.columns)):
            period_num = str(col_idx - 1)
            cell_val = str(row.iloc[col_idx]).strip()
            if not cell_val or cell_val.lower().startswith('period'): continue

            # Updated regex to handle "10-A", "IX A", and days like "1, 2, 5"
            matches = re.finditer(r'([A-Za-z0-9\-\s]+?)\s*\(\s*([\d\-\s,]+)\s*\)', cell_val)
            for match in matches:
                class_name = match.group(1).strip()
                # Safely parse days whether they use commas or hyphens
                days_assigned = re.split(r'[\s,\-]+', match.group(2).strip())

                for day_num in days_assigned:
                    if day_num not in DAYS_MAP: continue
                    day_name = DAYS_MAP[day_num]
                    
                    if period_num in schedule_data[teacher_key][day_name]:
                        existing_class = schedule_data[teacher_key][day_name][period_num]
                        if class_name not in existing_class.split(','):
                            if not is_double_booking_exception(teacher_key, existing_class, class_name):
                                conflicts.append({
                                    "type": "double_booking", "teacher": teacher_key, 
                                    "class1": existing_class, "class2": class_name, 
                                    "day": day_name, "period": period_num
                                })
                                continue
                            else:
                                schedule_data[teacher_key][day_name][period_num] = f"{existing_class},{class_name}"
                            continue

                    if period_num not in inverted_index[day_name]: inverted_index[day_name][period_num] = {}

                    if class_name in inverted_index[day_name][period_num]:
                        existing_teacher = inverted_index[day_name][period_num][class_name]
                        if not is_clash_exception(existing_teacher, teacher_key, class_name):
                            conflicts.append({
                                "type": "clash", "teacher1": existing_teacher, 
                                "teacher2": teacher_key, "class_name": class_name, 
                                "day": day_name, "period": period_num
                            })
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
# CALLBACKS
# ==========================================
# FIX 1.c: State updater for selectbox to avoid manual st.rerun() logic
def update_arrangement(state_key, widget_key):
    st.session_state.arrangements[state_key] = st.session_state[widget_key]

# ==========================================
# UI PHASES
# ==========================================
def ui_upload():
    st.title("Teacher Arrangement System")
    st.write("Upload your master schedule to begin.")
    st.info("Since this is connected to a cloud database, you only need to do this once per school term! Your data will be saved securely.")

    sched_file = st.file_uploader("Upload Schedule Excel (.xlsx)", type=["xlsx"])
    if sched_file and st.button("Process Schedule", type="primary"):
        # FIX 1.b: Cache the file into session state immediately so we don't lose it
        st.session_state.raw_file = sched_file.getvalue()
        
        sched, conf = parse_schedule(BytesIO(st.session_state.raw_file))
        if conf:
            st.session_state.conflicts = conf
            st.session_state.temp_schedule = sched
            st.session_state.phase = 'resolve'
            st.rerun()
        else:
            st.session_state.schedule = sched
            save_schedule_to_db(sched)
            st.session_state.phase = 'setup'
            st.rerun()

def ui_resolve():
    st.title("Resolve Schedule Conflicts")
    st.warning(f"Found {len(st.session_state.conflicts)} conflicts. Please resolve them to continue.")
    
    for i, c in enumerate(st.session_state.conflicts):
        st.error(f"Conflict {i+1}: {c['type'].replace('_', ' ').title()} - Day: {c['day'].capitalize()} | Period: {c['period']}")
        
        if c['type'] == 'clash':
            st.write(f"Teachers: **{c['teacher1']}** & **{c['teacher2']}** mapped to Class **{c['class_name']}**")
            if st.button(f"Allow & Add Rule (Clash {i+1})", key=f"btn_c_{i}"):
                st.session_state.rules['exceptions'].append({
                    "teacher1": c['teacher1'], "teacher2": c['teacher2'], "class_name": c['class_name']
                })
                save_rules_to_db(st.session_state.rules)
                st.rerun()
                
        elif c['type'] == 'double_booking':
            st.write(f"Teacher **{c['teacher']}** booked for **{c['class1']}** & **{c['class2']}**")
            if st.button(f"Allow & Add Rule (DB {i+1})", key=f"btn_db_{i}"):
                st.session_state.rules['double_booking_exceptions'].append({
                    "teacher": c['teacher'], "class1": c['class1'], "class2": c['class2']
                })
                save_rules_to_db(st.session_state.rules)
                st.rerun()

    st.divider()
    if st.button("Re-evaluate Schedule with new rules", type="primary"):
        if st.session_state.raw_file:
            # FIX 1.b: Re-parse using the cached file in memory, no re-upload required
            sched, conf = parse_schedule(BytesIO(st.session_state.raw_file))
            if conf:
                st.session_state.conflicts = conf
                st.session_state.temp_schedule = sched
            else:
                st.session_state.schedule = sched
                save_schedule_to_db(sched)
                st.session_state.phase = 'setup'
            st.rerun()
        else:
            st.error("File cache lost. Please upload again.")
            st.session_state.phase = 'upload'
            st.rerun()

def sidebar_menu():
    with st.sidebar:
        st.header("Database Controls")
        if st.button("Upload New Master Schedule (Reset)"):
            delete_schedule_from_db()
            st.session_state.schedule = None
            st.session_state.raw_file = None
            st.session_state.phase = 'upload'
            st.rerun()
            
        st.divider()
        st.caption("Connected securely to MongoDB Atlas")

def ui_setup():
    sidebar_menu()
    st.title("Select Day & Absentees")
    st.success("Master schedule loaded from Cloud Database!")
    
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

def compute_candidates(p_str, c_name, absent, day_schedule):
    busy_for_others = set()
    assigned_in_period = {}
    extra_assignments = {t: 0 for t in st.session_state.schedule.keys()}
    
    for (a, p), sel in st.session_state.arrangements.items():
        if p == p_str and sel and sel != "Self Study":
            if p not in assigned_in_period: assigned_in_period[p] = {}
            t_name = sel.split(" (")[0] if "(Co-teacher)" in sel or "(Combine w/" in sel else sel.rsplit(" (", 1)[0]
            if a != absent: busy_for_others.add(t_name)
            if "(Combine w/" not in sel and "(Co-teacher)" not in sel:
                extra_assignments[t_name] = extra_assignments.get(t_name, 0) + 1

    candidates = ["Self Study"]
    
    co_teachers = day_schedule.get(p_str, {}).get(c_name, [])
    for ct in co_teachers:
        if ct != absent and ct not in st.session_state.absent_teachers and ct not in busy_for_others:
            candidates.append(f"{ct} (Co-teacher)")

    if ',' not in c_name:
        match = re.match(r'^(\d+)([A-Za-z]?)$', c_name)
        if match:
            class_num, section = int(match.group(1)), match.group(2).upper()
            if section:
                parallel_class = f"{class_num}{'B' if section == 'A' else 'A'}"
                parallel_teachers = day_schedule.get(p_str, {}).get(parallel_class, [])
                for pt in parallel_teachers:
                    if pt not in st.session_state.absent_teachers and pt not in busy_for_others:
                        candidates.append(f"{pt} (Combine w/ {parallel_class})")

    free_list = []
    for t in st.session_state.schedule.keys():
        if t in st.session_state.absent_teachers or str(p_str) in st.session_state.schedule[t].get(st.session_state.selected_day, {}) or t in busy_for_others:
            continue
        base_load = len([x for x in st.session_state.schedule[t].get(st.session_state.selected_day, {}).keys() if str(x).isdigit()])
        free_list.append((t, base_load + extra_assignments.get(t, 0)))

    # FIX 2.b: Deterministic sorting - primary by load, secondary by teacher name alphabetically
    free_list.sort(key=lambda x: (x[1], x[0]))
    candidates.extend([f"{t} ({load} periods)" for t, load in free_list])
    
    return candidates

def ui_arrange():
    sidebar_menu()
    day = st.session_state.selected_day
    st.title(f"Make Arrangements ({day.capitalize()})")
    
    # CSS injected exclusively for the Arrangement Matrix layout to fix the scrolling logic
    st.markdown("""
        <style>
        /* Force rows to not wrap */
        [data-testid="stHorizontalBlock"] {
            flex-wrap: nowrap !important;
            gap: 0 !important; 
        }
        
        /* The CSS Bug Fix - Make the PARENT vertical block handle horizontal overflow, NOT individual rows */
        div[data-testid="stVerticalBlockBorderWrapper"] > div {
            overflow-x: auto !important;
        }

        /* Uniform dimensions for every column cell in the matrix */
        [data-testid="column"] {
            min-width: 470px !important;
            max-width: 470px !important;
            flex: 0 0 470px !important;
            padding: 15px 15px !important;
            border-right: 1px solid #d3d3d3 !important;
            border-bottom: 1px solid #d3d3d3 !important;
        }
        
        [data-testid="column"]:first-child {
            border-left: 1px solid #d3d3d3 !important;
            background-color: #f7f9fc;
        }
        
        .empty-cell {
            text-align: center;
            color: #a0a0a0;
            margin-top: 15px;
        }
        </style>
    """, unsafe_allow_html=True)
    
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

    st.info("**Scroll horizontally and vertically** to view and assign all periods. All cells are uniformly sized.")

    with st.container(height=650):
        # Header Row
        cols = st.columns(max_period + 1)
        cols[0].markdown("### Absent Teacher")
        for p in range(1, max_period + 1): 
            cols[p].markdown(f"### Period {p}")

        # Data Rows
        for absent in st.session_state.absent_teachers:
            cols = st.columns(max_period + 1)
            
            # Column 0: Absent Teacher Name
            cols[0].markdown(f"**{absent}**")
            
            periods = st.session_state.schedule.get(absent, {}).get(day, {})
            
            # Column 1 to N: Periods
            for p in range(1, max_period + 1):
                p_str = str(p)
                with cols[p]:
                    if p_str in periods:
                        c_name = periods[p_str]
                        st.caption(f"**Class:** {c_name}")
                        
                        state_key = (absent, p_str)
                        candidates = compute_candidates(p_str, c_name, absent, day_schedule)
                        
                        current_val = st.session_state.arrangements.get(state_key, "Self Study")
                        
                        idx = 0
                        for i, cand in enumerate(candidates):
                            if cand.split(' (')[0] == current_val.split(' (')[0]:
                                idx = i; break
                                
                        widget_key = f"sel_{absent}_{p}"
                        # FIX 1.c: Use on_change callback instead of manual state management and st.rerun()
                        st.selectbox(
                            "Substitute", 
                            candidates, 
                            index=idx, 
                            key=widget_key, 
                            on_change=update_arrangement,
                            args=(state_key, widget_key),
                            label_visibility="collapsed"
                        )
                    else:
                        st.markdown('<div class="empty-cell">— Free —</div>', unsafe_allow_html=True)

    st.divider()
    if st.button("Finalize & Export", type="primary", use_container_width=True):
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
        if absent not in st.session_state.schedule or day not in st.session_state.schedule[absent] or p not in st.session_state.schedule[absent][day]:
            continue
        c_name = st.session_state.schedule[absent][day][p]
        if selection == "Self Study":
            arrangements_mapped[absent][p] = f"{c_name} - Self Study"
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

    data1 = []
    for absent in sorted(arrangements_mapped.keys()):
        row = {"Absent Teacher": absent}
        for p in period_columns:
            row[f"Period {p}"] = arrangements_mapped[absent].get(p, "") if p in st.session_state.schedule[absent].get(day, {}) else ""
        data1.append(row)
    df1 = pd.DataFrame(data1)

    data2 = []
    active_subs = [t for t, assigns in detailed_assignments.items() if assigns]
    for sub in sorted(active_subs):
        row = {"Substitute Teacher": sub}
        engaged_count = 0
        for p in period_columns:
            if p in detailed_assignments[sub]:
                row[f"Period {p}"] = detailed_assignments[sub][p]
                engaged_count += 1
            elif p in st.session_state.schedule.get(sub, {}).get(day, {}):
                row[f"Period {p}"] = st.session_state.schedule[sub][day][p]
                engaged_count += 1
            else:
                row[f"Period {p}"] = "Free"
        row["Total Engaged"] = engaged_count
        row["Total Free"] = max_period - engaged_count
        data2.append(row)
    df2 = pd.DataFrame(data2)

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df1.to_excel(writer, sheet_name='Daily Arrangement', index=False, startrow=0)
        table2_start = len(df1) + 3 
        if not df2.empty:
            worksheet = writer.sheets['Daily Arrangement']
            worksheet.cell(row=table2_start, column=1, value="SUBSTITUTE TEACHER DAILY SCHEDULES (Printable)")
            df2.to_excel(writer, sheet_name='Daily Arrangement', index=False, startrow=table2_start)
    
    return output.getvalue()

def ui_export():
    sidebar_menu()
    st.title("Export Complete!")
    st.success("Your schedule has been calculated successfully.")
    
    excel_data = generate_excel()
    target_date = get_target_date(st.session_state.selected_day)
    file_name = f"Arrangement_{st.session_state.selected_day.capitalize()}_{target_date}.xlsx"
    
    st.download_button(
        label="Download Excel File",
        data=excel_data,
        file_name=file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary"
    )
    
    st.divider()
    if st.button("Start New Arrangement (Same Schedule)"):
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
