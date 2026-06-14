import os
import re
import pypdf
import time
import json  
import sqlite3
import datetime
import pandas as pd
import streamlit as st

st.set_page_config(page_title="S5 Stratos Retail Quiz", layout="centered")

# --- DATABASE LAYER & ROBUST CONFIGURATION ---
DB_FILE = "quiz_data.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes schema to handle quiz blocks, leaderboards, and associate user accounts with recovery keys."""
    with get_db_connection() as conn:
        # Table to store generated quiz segments
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_name TEXT UNIQUE,
                questions_json TEXT,
                is_enabled INTEGER DEFAULT 0,
                time_per_q INTEGER DEFAULT 20
            )
        """)
        
        # Table to store unique user credentials along with recovery data
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                password TEXT,
                security_question TEXT,
                security_answer TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Database Migrator: Ensure existing databases seamlessly get recovery columns without crashing
        try:
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "security_question" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN security_question TEXT DEFAULT 'What is your favorite color?'")
            if "security_answer" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN security_answer TEXT DEFAULT 'blue'")
        except sqlite3.OperationalError:
            pass

        # Safe table execution reset for leaderboard
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leaderboard (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quiz_name TEXT,
                    associate_email TEXT,
                    score INTEGER,
                    total_possible INTEGER,
                    time_taken_seconds INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cursor = conn.execute("PRAGMA table_info(leaderboard)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "time_taken_seconds" not in columns:
                conn.execute("ALTER TABLE leaderboard ADD COLUMN time_taken_seconds INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            conn.execute("DROP TABLE IF EXISTS leaderboard")
            conn.execute("""
                CREATE TABLE leaderboard (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quiz_name TEXT,
                    associate_email TEXT,
                    score INTEGER,
                    total_possible INTEGER,
                    time_taken_seconds INTEGER,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()

init_db()

# --- HIGHLY FLEXIBLE & RESILIENT S5 STRATOS PDF PARSER ---
def parse_and_split_pdf(pdf_file, q_per_quiz, time_limit_q):
    """Parses full PDF sequentially using loose text boundaries to account for spacing differences."""
    try:
        reader = pypdf.PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            text_content = page.extract_text()
            if text_content:
                full_text += text_content + "\n"

        if not full_text.strip():
            st.error("No readable text layers detected inside the target PDF.")
            return False

        # 1. Parse Answer Key from the back using loose spacing tolerances
        answer_key_map = {}
        ans_pattern = re.compile(r"Q\??0*(\d+):\s*([A-D])", re.IGNORECASE)
        for match in ans_pattern.finditer(full_text):
            q_num = int(match.group(1))
            correct_letter = match.group(2).upper()
            answer_key_map[q_num] = correct_letter

        # 2. Tokenize questions based on Q followed by numbers and a delimiter
        q_blocks = re.split(r"(?=Q\s*\d+\s*[\.\s\]\)])", full_text)
        all_ordered_questions = []
        
        for block in q_blocks:
            cleaned_block = block.strip()
            if not cleaned_block.startswith("Q"):
                continue
                
            num_match = re.match(r"^Q\s*(\d+)", cleaned_block, re.IGNORECASE)
            if not num_match:
                continue
            q_id = int(num_match.group(1))

            lines = [line.strip() for line in cleaned_block.split("\n") if line.strip()]
            question_text = ""
            options_dict = {}
            
            for line in lines:
                if "S5 Stratos" in line and ("Operations" in line or "Training" in line or "Page" in line or "Practice Questions" in line):
                    continue
                
                if re.match(r"^[A]\s*[\.\s\)]", line, re.IGNORECASE):
                    options_dict["A"] = re.sub(r"^[A]\s*[\.\s\)]", "", line).strip()
                elif re.match(r"^[B]\s*[\.\s\)]", line, re.IGNORECASE):
                    options_dict["B"] = re.sub(r"^[B]\s*[\.\s\)]", "", line).strip()
                elif re.match(r"^[C]\s*[\.\s\)]", line, re.IGNORECASE):
                    options_dict["C"] = re.sub(r"^[C]\s*[\.\s\)]", "", line).strip()
                elif re.match(r"^[D]\s*[\.\s\)]", line, re.IGNORECASE):
                    options_dict["D"] = re.sub(r"^[D]\s*[\.\s\)]", "", line).strip()
                else:
                    cleaned_line = re.sub(r"^Q\s*\d+\s*[\.\s\]\)]", "", line, flags=re.IGNORECASE)
                    cleaned_line = re.sub(r"\[.*?\]", "", cleaned_line).strip()
                    if cleaned_line:
                        if not question_text:
                            question_text = cleaned_line
                        else:
                            question_text += " " + cleaned_line

            if len(options_dict) >= 2 and q_id in answer_key_map:
                correct_letter = answer_key_map[q_id]
                correct_text_option = options_dict.get(correct_letter, "")
                ordered_options = [options_dict.get(k, "") for k in sorted(options_dict.keys())]
                
                if correct_text_option:
                    all_ordered_questions.append({
                        "pdf_num": q_id,
                        "question": question_text.strip(),
                        "options": ordered_options,
                        "correct": correct_text_option,
                        "correct_letter": correct_letter
                    })

        if not all_ordered_questions:
            st.error("Could not construct structured segments from target document mapping tokens.")
            return False

        all_ordered_questions.sort(key=lambda x: x["pdf_num"])
        total_extracted = len(all_ordered_questions)
        quiz_counter = 1
        
        with get_db_connection() as conn:
            conn.execute("DELETE FROM quizzes")
            
            for i in range(0, total_extracted, q_per_quiz):
                chunk = all_ordered_questions[i:i + q_per_quiz]
                name_string = f"Quiz {quiz_counter:03d}"
                
                conn.execute(
                    "INSERT INTO quizzes (quiz_name, questions_json, is_enabled, time_per_q) VALUES (?, ?, ?, ?)",
                    (name_string, json.dumps(chunk), 0, time_limit_q)
                )
                quiz_counter += 1
            conn.commit()
            
        st.success(f"Success! Segmented {total_extracted} questions cleanly into {quiz_counter - 1} separate evaluation modules.")
        return True
    except Exception as e:
        st.error(f"Partition setup execution crash: {e}")
        return False

# --- SECURITY QUESTIONS REGISTER ---
SECURITY_QUESTIONS = [
    "What was the name of your first pet?",
    "What is your mother's maiden name?",
    "In what city or town did your parents meet?",
    "What was the name of your elementary school?",
    "What was your first car model?"
]

# --- SYSTEM STATE RETENTION ENGINE ---
if "user_email" not in st.session_state:
    st.session_state.user_email = ""
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False
if "active_quiz_run" not in st.session_state:
    st.session_state.active_quiz_run = None
if "quiz_started" not in st.session_state:
    st.session_state.quiz_started = False

# --- FRONTEND INTERFACE VIEW TABS ---
tab_quiz, tab_admin = st.tabs(["✏️ Take Quiz", "🔐 Admin Dashboard"])

# --- TAB 2: SYSTEM ADMINISTRATION MODULES ---
with tab_admin:
    st.header("Admin Control Center")
    if not st.session_state.admin_logged_in:
        with st.form("admin_login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Authenticate Portal") and u == "admin" and p == "555tratos":
                st.session_state.admin_logged_in = True
                st.rerun()
    else:
        st.success("🟢 Administrator Authentication Active")
        if st.button("Exit Session Control"):
            st.session_state.admin_logged_in = False
            st.rerun()
            
        st.write("---")
        st.subheader("⚙️ Step 1: Initialize & Partition Question Bank PDF")
        
        adm_q_per_quiz = st.number_input("Questions per quiz segment:", min_value=5, max_value=50, value=25)
        adm_time_per_q = st.number_input("Time allocation per question (seconds):", min_value=5, max_value=120, value=20)
        source_file = st.file_uploader("Upload Question Bank PDF File", type="pdf")
        
        if st.button("Parse and Construct Sequential Quizzes") and source_file:
            with st.spinner("Executing dynamic structural mapping..."):
                if parse_and_split_pdf(source_file, adm_q_per_quiz, adm_time_per_q):
                    time.sleep(1)
                    st.rerun()

        st.write("---")
        st.subheader("🔓 Step 2: Manage Quiz Availability & Fetch Slack Output")
        
        with get_db_connection() as conn:
            all_quizzes_rows = conn.execute("SELECT * FROM quizzes ORDER BY quiz_name ASC").fetchall()
            
        if all_quizzes_rows:
            for q_row in all_quizzes_rows:
                col_name, col_status, col_slack = st.columns([2, 2, 3])
                
                with col_name:
                    st.markdown(f"**{q_row['quiz_name']}** ({len(json.loads(q_row['questions_json']))} Questions)")
                
                with col_status:
                    toggle_label = "Disable" if q_row["is_enabled"] == 1 else "Enable"
                    
                    if st.button(toggle_label, key=f"tgl_{q_row['quiz_name']}"):
                        new_state = 0 if q_row["is_enabled"] == 1 else 1
                        with get_db_connection() as conn:
                            conn.execute("UPDATE quizzes SET is_enabled = ? WHERE quiz_id = ?", (new_state, q_row["quiz_id"]))
                            conn.commit()
                        st.rerun()
                        
                with col_slack:
                    if st.checkbox("Generate Slack Keys", key=f"key_chk_{q_row['quiz_name']}"):
                        questions_list = json.loads(q_row["questions_json"])
                        slack_text = f"*Answer Key Report for {q_row['quiz_name']}*\n"
                        for idx, item in enumerate(questions_list):
                            slack_text += f"\n*Q{idx+1}. {item['question']}*\n👉 Answer: `{item['correct_letter']}) {item['correct']}`\n"
                        
                        st.text_area("Copy Text for Slack (Read-Only):", value=slack_text, height=150, key=f"txt_{q_row['quiz_name']}", disabled=True)
        else:
            st.info("No compiled quiz segments found in local storage.")

        st.write("---")
        st.subheader("📋 Leaderboard Exports for Slack Sharing")
        with get_db_connection() as conn:
            raw_scores = conn.execute("SELECT quiz_name, associate_email, score, total_possible, time_taken_seconds FROM leaderboard ORDER BY score DESC, time_taken_seconds ASC").fetchall()
            
        if raw_scores:
            quiz_modules_present = sorted(list(set([r["quiz_name"] for r in raw_scores])))
            selected_export_mod = st.selectbox("Select quiz score to export for Slack:", quiz_modules_present)
            
            slack_board_output = f"*📊 Current Standings Register — {selected_export_mod}*\n"
            rank_idx = 1
            for row in raw_scores:
                if row["quiz_name"] == selected_export_mod:
                    medal = "🥇" if rank_idx == 1 else "🥈" if rank_idx == 2 else "🥉" if rank_idx == 3 else "▪️"
                    slack_board_output += f"{medal} *Rank #{rank_idx}* — {row['associate_email']} | Score: `{row['score']}/{row['total_possible']}` | Time: `{row['time_taken_seconds']}s`\n"
                    rank_idx += 1
                    
            st.text_area("Click below to copy and paste directly into Slack (Read-Only):", value=slack_board_output, height=150, disabled=True)
        else:
            st.info("No recorded historical scores available.")

        st.write("---")
        st.subheader("🗑️ System Infrastructure Reset")
        if st.checkbox("Confirm permanent deletion of database registers"):
            if st.button("🔴 Wipe System Performance & User Registers"):
                with get_db_connection() as conn:
                    conn.execute("DELETE FROM leaderboard")
                    conn.execute("DELETE FROM users")
                    conn.commit()
                st.success("Leaderboard history and user access registers successfully wiped!")
                st.rerun()

# --- TAB 1: RUNTIME ASSOCIATE INTERFACE SYSTEM ---
with tab_quiz:
    st.markdown("### S5 Stratos Retail Quiz")
    
    if not st.session_state.user_email:
        st.subheader("Associate Authentication Portal")
        
        email_input = st.text_input("Enter your S5 Stratos Email ID:").strip().lower()
        
        if email_input:
            if "@" not in email_input or "." not in email_input:
                st.warning("Please enter a valid structure email account profile.")
            else:
                with get_db_connection() as conn:
                    existing_user = conn.execute("SELECT * FROM users WHERE email = ?", (email_input,)).fetchone()
                
                # --- SCENARIO A: LOGGING IN EXISTING REGISTERED USER ---
                if existing_user:
                    st.info("👋 Returning User Profile Found. Enter your password to log in.")
                    password_input = st.text_input("Enter your Password:", type="password", key="login_pwd_input")
                    
                    if st.button("🔐 Authenticate and Sign In"):
                        if existing_user["password"] == password_input:
                            st.session_state.user_email = email_input
                            st.success("Authentication confirmed!")
                            st.rerun()
                        else:
                            st.error("Incorrect password credentials provided. Please try again.")
                    
                    # --- PASSWORD RESET WORKFLOW LAYER ---
                    st.write("---")
                    with st.expander("🔑 Forgot Password? Reset via Security Question"):
                        st.markdown(f"**Security Question Challenge:** *{existing_user['security_question']}*")
                        recovery_answer = st.text_input("Your Security Answer:", key="reset_ans_input").strip().lower()
                        new_password_input = st.text_input("Choose New Password:", type="password", key="reset_new_pwd")
                        
                        if st.button("🔄 Update Password Credentials"):
                            if existing_user["security_answer"].strip().lower() != recovery_answer:
                                st.error("The verification answer provided does not match our records.")
                            elif len(new_password_input) < 4:
                                st.error("New password must be at least 4 characters long.")
                            else:
                                with get_db_connection() as conn:
                                    conn.execute("UPDATE users SET password = ? WHERE email = ?", (new_password_input, email_input))
                                    conn.commit()
                                st.success("Password successfully reset! You can now log in above using your new credential.")
                                time.sleep(1)
                                st.rerun()

                # --- SCENARIO B: REGISTERING NEW ACCOUNT PROFILE ---
                else:
                    st.warning("✨ New Profile detected. Configure your initial login and account safety settings below.")
                    reg_password = st.text_input("Create Account Password:", type="password", key="reg_pwd")
                    reg_question = st.selectbox("Select a Password Recovery Security Question:", SECURITY_QUESTIONS)
                    reg_answer = st.text_input("Provide the Answer (Case-Insensitive):", key="reg_ans").strip().lower()
                    
                    if st.button("📝 Register Account Profile"):
                        if len(reg_password) < 4:
                            st.error("For basic security protections, password inputs must be at least 4 characters long.")
                        elif not reg_answer:
                            st.error("You must provide an answer to your selected security question for account recovery functionality.")
                        else:
                            with get_db_connection() as conn:
                                conn.execute(
                                    "INSERT INTO users (email, password, security_question, security_answer) VALUES (?, ?, ?, ?)", 
                                    (email_input, reg_password, reg_question, reg_answer)
                                )
                                conn.commit()
                            st.session_state.user_email = email_input
                            st.success("Account profile successfully secured and logged in!")
                            st.rerun()
    else:
        st.sidebar.markdown(f"👤 Session User: `{st.session_state.user_email}`")
        if st.sidebar.button("Log Out of Session"):
            st.session_state.user_email = ""
            st.session_state.quiz_started = False
            st.session_state.active_quiz_run = None
            st.rerun()

        with get_db_connection() as conn:
            enabled_rows = conn.execute("SELECT * FROM quizzes WHERE is_enabled = 1 ORDER BY quiz_name ASC").fetchall()

        if not enabled_rows:
            st.info("📢 There are currently no assessment windows open for entry. Please coordinate with the training supervisor.")
        elif not st.session_state.quiz_started:
            st.subheader("Available Assessment Modules")
            selection_map = {r["quiz_name"]: r for r in enabled_rows}
            user_choice = st.selectbox("Choose an open module window to execute:", list(selection_map.keys()))
            
            # ST_RULE CHECK: Enforce strictly one attempt per unique email account profile
            with get_db_connection() as conn:
                duplicate_check = conn.execute(
                    "SELECT score, time_taken_seconds FROM leaderboard WHERE quiz_name = ? AND associate_email = ?",
                    (user_choice, st.session_state.user_email)
                ).fetchone()
                
            if duplicate_check:
                st.error(f"❌ Access Denied: You have already submitted an entry for this quiz module. Registered score: {duplicate_check['score']} | Time taken: {duplicate_check['time_taken_seconds']}s. Only 1 attempt is allowed.")
            else:
                meta_block = selection_map[user_choice]
                q_array = json.loads(meta_block["questions_json"])
                calculated_max_time = len(q_array) * meta_block["time_per_q"]
                
                st.markdown(f"""
                **Module Operational Parameters:**
                * Active Assessment Identifier: `{meta_block['quiz_name']}`
                * Segment Size: `{len(q_array)} Multiple Choice Questions`
                * Maximum Permitted Runway: `{calculated_max_time} Seconds Total`
                """)
                
                if st.button("🚀 Begin Assessment Session"):
                    st.session_state.active_quiz_run = {
                        "name": meta_block["quiz_name"],
                        "time_limit": calculated_max_time,
                        "questions": q_array
                    }
                    st.session_state.quiz_started = True
                    st.session_state.start_time = time.time()
                    st.rerun()
        else:
            # --- LIVE TESTING EXECUTION MODULE ENGINE ---
            run_data = st.session_state.active_quiz_run
            max_seconds = run_data["time_limit"]
            
            seconds_spent = int(time.time() - st.session_state.start_time)
            remaining_seconds = max_seconds - seconds_spent
            
            if remaining_seconds <= 0:
                st.error("💥 Time limit expired! Locking choices and submitting answers...")
                remaining_seconds = 0
                
            st.metric(label="⌛ Time Remaining in Active Module Window", value=f"{remaining_seconds} Seconds")
            st.empty()
            if remaining_seconds > 0:
                time.sleep(1)
                
            test_form = st.form(key="live_evaluation_form_block")
            choices_map = {}
            
            for index, q_obj in enumerate(run_data["questions"]):
                test_form.markdown(f"**Question {index+1}:** {q_obj['question']}")
                options_selections = ["Select an option..."] + q_obj["options"]
                choices_map[index] = test_form.selectbox(
                    "Choose the correct option:",
                    options_selections,
                    key=f"associate_choice_{index}"
                )
                test_form.markdown("---")
                
            if test_form.form_submit_button("Finalize Assessment & Log Score") or remaining_seconds <= 0:
                total_duration = int(time.time() - st.session_state.start_time)
                if total_duration > max_seconds:
                    total_duration = max_seconds
                    
                points = 0
                for index, q_obj in enumerate(run_data["questions"]):
                    if choices_map[index] == q_obj["correct"]:
                        points += 1
                        
                with get_db_connection() as conn:
                    conn.execute(
                        "INSERT INTO leaderboard (quiz_name, associate_email, score, total_possible, time_taken_seconds) VALUES (?, ?, ?, ?, ?)",
                        (run_data["name"], st.session_state.user_email, points, len(run_data["questions"]), total_duration)
                    )
                    conn.commit()
                    
                st.balloons()
                st.success(f"Assessment Complete! Registered Score: {points}/{len(run_data['questions'])} within {total_duration}s.")
                
                st.session_state.quiz_started = False
                st.session_state.active_quiz_run = None
                if "start_time" in st.session_state:
                    del st.session_state.start_time
                st.button("Return to Module Directory")

    # --- TIME-SEGMENTED METRIC LEADERBOARDS ---
    st.write("---")
    st.subheader("📊 Dynamic Global Standings")
    
    with get_db_connection() as conn:
        db_scores = conn.execute("""
            SELECT quiz_name, associate_email, score, total_possible, time_taken_seconds, timestamp 
            FROM leaderboard 
            ORDER BY score DESC, time_taken_seconds ASC, timestamp ASC
        """).fetchall()
        
    if db_scores:
        view_mode = st.radio("Group Standings Filter Tier:", ["Per Active Quiz Module", "Weekly Cumulative Performance", "Monthly Cumulative Performance"], horizontal=True)
        now_dt = datetime.datetime.now()
        
        if view_mode == "Per Active Quiz Module":
            distinct_quizzes = sorted(list(set([r["quiz_name"] for r in db_scores])))
            for q_name in distinct_quizzes:
                with st.expander(f"🏆 Rankings: {q_name}", expanded=True):
                    r_idx = 1
                    for r in db_scores:
                        if r["quiz_name"] == q_name:
                            mdl = "🥇" if r_idx == 1 else "🥈" if r_idx == 2 else "🥉" if r_idx == 3 else "🔹"
                            st.markdown(f"{mdl} **Rank #{r_idx}** — {r['associate_email']} : `{r['score']}/{r['total_possible']} Pts` (Time: `{r['time_taken_seconds']}s`)")
                            r_idx += 1
                            
        elif view_mode == "Weekly Cumulative Performance":
            st.caption("Aggregated points matched against the current week.")
            weekly_totals = {}
            current_week_num = now_dt.isocalendar()[1]
            current_year_num = now_dt.year
            
            for r in db_scores:
                row_dt = datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                if row_dt.isocalendar()[1] == current_week_num and row_dt.year == current_year_num:
                    email = r["associate_email"]
                    if email not in weekly_totals:
                        weekly_totals[email] = {"score": 0, "time": 0}
                    weekly_totals[email]["score"] += r["score"]
                    weekly_totals[email]["time"] += r["time_taken_seconds"]
            
            sorted_weekly = sorted(weekly_totals.items(), key=lambda x: (-x[1]["score"], x[1]["time"]))
            for w_idx, (email, metrics) in enumerate(sorted_weekly):
                mdl = "🥇" if w_idx == 0 else "🥈" if w_idx == 1 else "🥉" if w_idx == 2 else "🔹"
                st.markdown(f"{mdl} **Rank #{w_idx+1}** — {email} : `{metrics['score']} Total Correct` (Time: `{metrics['time']}s`)")
                
        elif view_mode == "Monthly Cumulative Performance":
            st.caption("Aggregated points matched against the current calendar month.")
            monthly_totals = {}
            current_month = now_dt.month
            current_year = now_dt.year
            
            for r in db_scores:
                row_dt = datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
                if row_dt.month == current_month and row_dt.year == current_year:
                    email = r["associate_email"]
                    if email not in monthly_totals:
                        monthly_totals[email] = {"score": 0, "time": 0}
                    monthly_totals[email]["score"] += r["score"]
                    monthly_totals[email]["time"] += r["time_taken_seconds"]
                    
            sorted_monthly = sorted(monthly_totals.items(), key=lambda x: (-x[1]["score"], x[1]["time"]))
            for m_idx, (email, metrics) in enumerate(sorted_monthly):
                mdl = "🥇" if m_idx == 0 else "🥈" if m_idx == 1 else "🥉" if m_idx == 2 else "🔹"
                st.markdown(f"{mdl} **Rank #{m_idx+1}** — {email} : `{metrics['score']} Total Correct` (Time: `{metrics['time']}s`)")
    else:
        st.info("No recorded associate scores available.")