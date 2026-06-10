import os
import re
import pypdf
import time
import random
import sqlite3
import json
import pandas as pd
import streamlit as st

st.set_page_config(page_title="S5 Stratos Enterprise Portal", layout="centered")

# --- DATABASE SETUP & MIGRATION LAYER ---
DB_FILE = "quiz_data.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Creates tables for persistent quizzes and leaderboards if they don't exist."""
    with get_db_connection() as conn:
        # Table to store unique quiz metadata
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quizzes (
                quiz_id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_name TEXT UNIQUE,
                time_per_q INTEGER,
                num_questions INTEGER,
                questions_json TEXT
            )
        """)
        # Table to store associate attempts permanently
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leaderboard (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                quiz_name TEXT,
                associate_name TEXT,
                score INTEGER,
                total_possible INTEGER,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

# --- DETERMINISTIC S5 STRATOS PDF PARSER ---
def parse_stratos_pdf(pdf_file, target_num_q):
    """Parses S5_Stratos_250_Questions.pdf layout natively without cloud wrappers."""
    try:
        reader = pypdf.PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            text_content = page.extract_text()
            if text_content:
                full_text += text_content + "\n"

        if not full_text.strip():
            return []

        # Parse Answer Key
        answer_key_map = {}
        ans_pattern = re.compile(r"Q\??(\d+):\s*([A-D])")
        for match in ans_pattern.finditer(full_text):
            answer_key_map[int(match.group(1))] = match.group(2)

        # Tokenize question blocks
        q_blocks = re.split(r"(?=Q\d+\.)", full_text)
        parsed_questions = []

        for block in q_blocks:
            if not block.strip().startswith("Q"):
                continue
                
            num_match = re.search(r"^Q(\d+)\.", block)
            if not num_match:
                continue
            q_id = int(num_match.group(1))

            lines = [line.strip() for line in block.split("\n") if line.strip()]
            question_text = ""
            options_dict = {}
            
            for line in lines:
                if line.startswith(f"Q{q_id}."):
                    # Strip bracketed tags and question numbers cleanly
                    question_text = re.sub(r"\[.*?\]", "", line).replace(f"Q{q_id}.", "").strip()
                elif line.startswith("A."):
                    options_dict["A"] = line.replace("A.", "").strip()
                elif line.startswith("B."):
                    options_dict["B"] = line.replace("B.", "").strip()
                elif line.startswith("C."):
                    options_dict["C"] = line.replace("C.", "").strip()
                elif line.startswith("D."):
                    options_dict["D"] = line.replace("D.", "").strip()
                elif not question_text and not line.startswith(("A.", "B.", "C.", "D.")) and "S5 Stratos" not in line:
                    question_text += " " + line

            # Form structure validation
            if len(options_dict) >= 2 and q_id in answer_key_map:
                correct_letter = answer_key_map[q_id]
                correct_text_option = options_dict.get(correct_letter, "")
                ordered_options = [options_dict.get(k, "") for k in sorted(options_dict.keys())]
                
                if correct_text_option:
                    # FIX: Keep question_text totally isolated without attaching IDs/prefixes
                    parsed_questions.append({
                        "question": question_text.strip(),
                        "options": ordered_options,
                        "correct": correct_text_option
                    })

        random.shuffle(parsed_questions)
        return parsed_questions[:target_num_q]
    except Exception as e:
        st.error(f"Parsing engine exception: {e}")
        return []

# --- CORE STATE COMPONENT VARIABLES ---
if "user_name" not in st.session_state:
    st.session_state.user_name = ""
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False
if "current_quiz" not in st.session_state:
    st.session_state.current_quiz = None
if "quiz_started" not in st.session_state:
    st.session_state.quiz_started = False

# --- APP LAYOUT NAVIGATION ---
st.title("🏆 S5 Stratos Enterprise Quiz Portal")
tab_quiz, tab_admin = st.tabs(["✏️ Take Quiz", "🔐 Admin Dashboard"])

# --- TAB 2: PERSISTENT ADMIN CONTROL CENTER ---
with tab_admin:
    st.header("Admin Settings")
    if not st.session_state.admin_logged_in:
        with st.form("admin_login"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            if st.form_submit_button("Login") and u == "admin" and p == "admin":
                st.session_state.admin_logged_in = True
                st.rerun()
    else:
        st.success("🟢 System Administrator Session Active")
        if st.button("Log Out"):
            st.session_state.admin_logged_in = False
            st.rerun()
            
        st.write("---")
        st.subheader("📁 Deploy a New Quiz Module")
        
        new_quiz_name = st.text_input("Quiz Module Title (e.g., Inventory Optimization Module 1)")
        adm_num_q = st.number_input("Number of questions to pull:", min_value=1, max_value=100, value=10)
        adm_time_q = st.number_input("Time limit per question (seconds):", min_value=5, max_value=120, value=20)
        adm_file = st.file_uploader("Upload Stratos Question Bank (PDF)", type="pdf", key="adm_file_loader")
        
        if st.button("Compile & Save Module") and new_quiz_name and adm_file:
            with st.spinner("Processing PDF data streams..."):
                extracted_list = parse_stratos_pdf(adm_file, adm_num_q)
                if extracted_list:
                    try:
                        with get_db_connection() as conn:
                            conn.execute(
                                "INSERT INTO quizzes (quiz_name, time_per_q, num_questions, questions_json) VALUES (?, ?, ?, ?)",
                                (new_quiz_name.strip(), adm_time_q, len(extracted_list), json.dumps(extracted_list))
                            )
                            conn.commit()
                        st.success(f"Successfully deployed module '{new_quiz_name}' directly to persistent system database!")
                    except sqlite3.IntegrityError:
                        st.error("A quiz module with that exact title already exists.")

        st.write("---")
        st.subheader("📊 Download Historical Leaderboards")
        
        # Pull global scores database table straight into a Pandas DataFrame
        with get_db_connection() as conn:
            df_leaderboard = pd.read_sql_query("SELECT quiz_name, associate_name, score, total_possible, timestamp FROM leaderboard ORDER BY timestamp DESC", conn)
            
        if not df_leaderboard.empty:
            st.dataframe(df_leaderboard)
            # Convert to a clean downloadable CSV layout string
            csv_data = df_leaderboard.to_csv(index=False).encode('utf-8')
            st.download_button(
                label="📥 Download Full Report (.CSV)",
                data=csv_data,
                file_name="S5_Stratos_Global_Leaderboard.csv",
                mime="text/csv"
            )
        else:
            st.info("No records compiled in historical dashboard ledger yet.")

# --- TAB 1: INTERACTIVE RUNTIME ASSOCIATE INTERFACE ---
with tab_quiz:
    if not st.session_state.user_name:
        st.subheader("Associate Onboarding")
        name_input = st.text_input("Enter Your Full Name to Begin:")
        if st.button("Launch Profile") and name_input.strip():
            st.session_state.user_name = name_input.strip()
            st.rerun()
    else:
        st.write(f"👤 Associate: **{st.session_state.user_name}**")
        
        # Load available saved modules directly from DB
        with get_db_connection() as conn:
            quiz_rows = conn.execute("SELECT quiz_id, quiz_name, time_per_q, num_questions FROM quizzes").fetchall()
            
        if not quiz_rows:
            st.info("📢 No evaluation modules have been published by the system administrator yet.")
        elif not st.session_state.quiz_started:
            st.subheader("Select Available Assessment Module")
            quiz_options = {r["quiz_name"]: r for r in quiz_rows}
            selected_module = st.selectbox("Choose a quiz to start:", list(quiz_options.keys()))
            
            # Query historical attempts specifically for this active player to block double takes
            with get_db_connection() as conn:
                existing_attempt = conn.execute(
                    "SELECT score FROM leaderboard WHERE quiz_name = ? AND associate_name = ?", 
                    (selected_module, st.session_state.user_name)
                ).fetchone()
                
            if existing_attempt:
                st.warning(f"⚠️ You have already submitted this assessment. Your registered score: **{existing_attempt['score']}**")
            else:
                target_meta = quiz_options[selected_module]
                total_time_calc = target_meta["num_questions"] * target_meta["time_per_q"]
                st.markdown(f"""
                **Module Rules:**
                * Total Questions: `{target_meta['num_questions']}`
                * Allowed Pacing Pace: `{target_meta['time_per_q']} seconds per question`
                * Maximum Time Window: `{total_time_calc} seconds total`
                """)
                
                if st.button("🚀 Start Examination"):
                    with get_db_connection() as conn:
                        chosen_quiz = conn.execute("SELECT * FROM quizzes WHERE quiz_id = ?", (target_meta["quiz_id"],)).fetchone()
                    
                    st.session_state.current_quiz = {
                        "name": chosen_quiz["quiz_name"],
                        "time_limit": total_time_calc,
                        "questions": json.loads(chosen_quiz["questions_json"])
                    }
                    st.session_state.quiz_started = True
                    st.session_state.start_time = time.time()
                    st.rerun()
        else:
            # --- ACTIVE RUNNING ASSESSMENT PANEL ---
            active_data = st.session_state.current_quiz
            total_limit = active_data["time_limit"]
            
            elapsed = int(time.time() - st.session_state.start_time)
            remaining = total_limit - elapsed
            
            if remaining <= 0:
                st.error("💥 Time-limit window exceeded! Auto-submitting current answers...")
                # Fallthrough evaluates form choices instantly
                remaining = 0
                
            st.metric(label="⌛ Overall Time Remaining", value=f"{remaining} Seconds")
            
            # Inject empty refresh block to ensure clock cycles display accurately
            st.empty()
            if remaining > 0:
                time.sleep(1) # Ticks view engine
                
            # RENDER QUESTIONS FORM
            quiz_form = st.form(key="active_test_form")
            user_selections = {}
            
            for idx, q_meta in enumerate(active_data["questions"]):
                # Clean prompt mapping layout
                quiz_form.markdown(f"**Question {idx+1}:** {q_meta['question']}")
                
                # FIX: Prepend a completely blank option so no choices are auto-selected by default
                form_choices = ["Select an option..."] + q_meta["options"]
                user_selections[idx] = quiz_form.selectbox(
                    "Choose the correct option:",
                    form_choices,
                    key=f"user_sel_{idx}"
                )
                quiz_form.markdown("---")
                
            submit_trigger = quiz_form.form_submit_button("Lock In & Submit Answers")
            
            if submit_trigger or remaining <= 0:
                final_score = 0
                for idx, q_meta in enumerate(active_data["questions"]):
                    # If they skipped it or ran out of time, user_selections[idx] will be "Select an option..."
                    if user_selections[idx] == q_meta["correct"]:
                        final_score += 1
                        
                # WRITE DIRECTLY TO SYSTEM STORAGE FILE
                with get_db_connection() as conn:
                    conn.execute(
                        "INSERT INTO leaderboard (quiz_name, associate_name, score, total_possible) VALUES (?, ?, ?, ?)",
                        (active_data["name"], st.session_state.user_name, final_score, len(active_data["questions"]))
                    )
                    conn.commit()
                
                st.balloons()
                st.success(f"Assessment complete! Score written to central storage: {final_score} / {len(active_data['questions'])}")
                
                # Reset Session States safely
                st.session_state.quiz_started = False
                st.session_state.current_quiz = None
                if "start_time" in st.session_state:
                    del st.session_state.start_time
                st.button("Return to Module Directory")

    # --- REAL-TIME PORTAL LEADERBOARD REGISTER ---
    st.write("---")
    st.subheader("📊 Current Module Standings")
    
    with get_db_connection() as conn:
        all_scores = conn.execute(
            "SELECT quiz_name, associate_name, score, total_possible FROM leaderboard ORDER BY score DESC, timestamp ASC"
        ).fetchall()
        
    if all_scores:
        # Group by active quiz module titles for clean segmented viewing
        modules_tracked = set([row["quiz_name"] for row in all_scores])
        for mod in modules_tracked:
            with st.expander(f"🏆 Rankings: {mod}", expanded=True):
                rank = 1
                for row in all_scores:
                    if row["quiz_name"] == mod:
                        medal = "🥇" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🔹"
                        st.markdown(f"{medal} **Rank #{rank}** — {row['associate_name']} : `{row['score']} / {row['total_possible']} Points`")
                        rank += 1
    else:
        st.info("No associate results recorded yet.")