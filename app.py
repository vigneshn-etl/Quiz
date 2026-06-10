import os
import re
import pypdf
import time
import random
import streamlit as st

st.set_page_config(page_title="S5 Stratos Training Portal", layout="centered")

# --- CUSTOM LOCAL PDF PARSING ENGINE (NO AI REQUIRED) ---
def local_parse_quiz_pdf(pdf_file, target_num_q):
    """
    Parses S5_Stratos_250_Questions.pdf deterministically using text mapping.
    1. Scans all pages to find questions (e.g., Q1., Q2., Q155.) and choices.
    2. Scans the back of the file to build a map of the absolute ANSWER KEY.
    3. Assembles clean question data structures.
    """
    try:
        reader = pypdf.PdfReader(pdf_file)
        full_text = ""
        for page in reader.pages:
            text_content = page.extract_text()
            if text_content:
                full_text += text_content + "\n"

        if not full_text.strip():
            st.error("Could not find readable text layers inside the uploaded PDF.")
            return []

        # Step A: Parse the Answer Key at the end of the document
        # Looks for patterns like "Q001: B", "Q015: C", "Q154: A"
        answer_key_map = {}
        ans_pattern = re.compile(r"Q\??(\d+):\s*([A-D])")
        for match in ans_pattern.finditer(full_text):
            q_num = int(match.group(1))
            correct_letter = match.group(2)
            answer_key_map[q_num] = correct_letter

        # Step B: Parse out questions and their choice blocks
        # Pattern captures 'Q1.', 'Q102.', etc. until the next question or section delimiter
        q_blocks = re.split(r"(?=Q\d+\.)", full_text)
        
        parsed_questions = []

        for block in q_blocks:
            if not block.strip().startswith("Q"):
                continue
                
            # Isolate Question Stem Number
            num_match = re.search(r"^Q(\d+)\.", block)
            if not num_match:
                continue
            q_id = int(num_match.group(1))

            # Split lines to separate the text description from the choices
            lines = [line.strip() for line in block.split("\n") if line.strip()]
            
            question_text = ""
            options_dict = {}
            
            for line in lines:
                if line.startswith(f"Q{q_id}."):
                    # Clean up topic tags from question line if present (e.g. [Retail Basics])
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
                    # Append multi-line question text stems if wrapped
                    question_text += " " + line

            # Validate that we have options and a mapped answer key target
            if len(options_dict) >= 2 and q_id in answer_key_map:
                correct_letter = answer_key_map[q_id]
                correct_text_option = options_dict.get(correct_letter, "")
                
                # Turn dictionary choices into a uniform list for Streamlit radios
                ordered_options = [options_dict.get(k, "") for k in sorted(options_dict.keys())]
                
                if correct_text_option:
                    parsed_questions.append({
                        "id": q_id,
                        "question": f"({q_id}) {question_text}",
                        "options": ordered_options,
                        "correct": correct_text_option
                    })

        if not parsed_questions:
            st.error("PDF format mismatch: Could not map raw question sequences or key items.")
            return []

        # Shuffle parsed array to distribute Easy/Hard variations randomly
        random.shuffle(parsed_questions)
        
        # Cap the output length exactly to the count selected by the Admin
        return parsed_questions[:target_num_q]

    except Exception as parse_err:
        st.error(f"Failed to process or tokenize document contents: {parse_err}")
        return []

# --- APPLICATION STATE REGISTER ---
if "quiz_questions" not in st.session_state:
    st.session_state.quiz_questions = []
if "num_questions" not in st.session_state:
    st.session_state.num_questions = 10
if "time_per_question" not in st.session_state:
    st.session_state.time_per_question = 20
if "score" not in st.session_state:
    st.session_state.score = 0
if "user_name" not in st.session_state:
    st.session_state.user_name = ""
if "leaderboard" not in st.session_state:
    st.session_state.leaderboard = {}
if "quiz_submitted" not in st.session_state:
    st.session_state.quiz_submitted = False
if "admin_logged_in" not in st.session_state:
    st.session_state.admin_logged_in = False

# --- FRONT-END WORKFLOW INTERFACE ---
st.title("🏆 Associate Quiz Portal")

tab_quiz, tab_admin = st.tabs(["✏️ Take Quiz", "🔐 Admin Dashboard"])

# --- TAB: ADMIN INTERFACE (PASSWORD PROTECTED) ---
with tab_admin:
    st.header("Admin Control Center")
    
    if not st.session_state.admin_logged_in:
        with st.form("admin_login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submit_login = st.form_submit_button("Login")
            
            if submit_login:
                if username == "admin" and password == "admin":
                    st.session_state.admin_logged_in = True
                    st.success("Authenticated successfully!")
                    st.rerun()
                else:
                    st.error("Invalid Username or Password.")
    else:
        st.write("🟢 Authenticated as Administrator")
        if st.button("Logout Dashboard"):
            st.session_state.admin_logged_in = False
            st.rerun()
            
        st.write("---")
        st.subheader("Quiz Execution Parameters")
        
        st.session_state.num_questions = st.number_input(
            "Target question pool count:", min_value=1, max_value=250, value=st.session_state.num_questions
        )
        st.session_state.time_per_question = st.number_input(
            "Allocation timer per question (seconds):", min_value=5, max_value=120, value=st.session_state.time_per_question
        )
        
        st.write("---")
        st.subheader("Upload Document Source")
        uploaded_file = st.file_uploader("Upload S5 Stratos Question Bank (PDF Only)", type="pdf")
        
        if st.button("Extract and Deploy Quiz") and uploaded_file:
            with st.spinner("Native scanner parsing questions and compiling answers..."):
                compiled_quiz = local_parse_quiz_pdf(uploaded_file, st.session_state.num_questions)
                if compiled_quiz:
                    st.session_state.quiz_questions = compiled_quiz
                    st.session_state.score = 0 
                    st.session_state.quiz_submitted = False
                    st.success(f"Deployed! Successfully loaded {len(compiled_quiz)} questions without external cloud requirements.")

# --- TAB: ASSOCIATE RUNTIME INTERFACE ---
with tab_quiz:
    if not st.session_state.user_name:
        st.subheader("Welcome! Please register your session identity:")
        name_input = st.text_input("Enter Your Full Name:")
        if st.button("Access Portal") and name_input:
            st.session_state.user_name = name_input.strip()
            st.rerun()
    else:
        st.write(f"👤 Current Associate: **{st.session_state.user_name}**")
        
        if not st.session_state.quiz_questions:
            st.info("📢 Waiting for the Admin to upload the Question Bank PDF and deploy a quiz.")
        elif st.session_state.quiz_submitted:
            st.success(f"🎉 Your quiz session is complete! Score: {st.session_state.score} / {len(st.session_state.quiz_questions)}")
        else:
            total_allowed_time = len(st.session_state.quiz_questions) * st.session_state.time_per_question
            st.warning(f"⏳ Constraints: You have {st.session_state.time_per_question} seconds per item. Total test window: **{total_allowed_time} seconds**.")
            
            # Live Countdown Synchronization 
            if "start_time" not in st.session_state:
                st.session_state.start_time = time.time()
                
            elapsed_time = int(time.time() - st.session_state.start_time)
            remaining_time = total_allowed_time - elapsed_time
            
            if remaining_time <= 0:
                st.error("💥 Time has expired! Evaluating completed items...")
                st.session_state.quiz_submitted = True
                st.rerun()
            else:
                st.metric(label="⌛ Time Remaining", value=f"{remaining_time} seconds")
                st.empty()
                
            # Dynamic Form Assembly
            quiz_form = st.form(key="active_quiz_form")
            selections = {}
            
            for index, item in enumerate(st.session_state.quiz_questions):
                quiz_form.markdown(f"**Question {index+1}:** {item['question']}")
                selections[index] = quiz_form.radio(
                    "Select choice option:", 
                    item['options'], 
                    key=f"item_choice_{index}"
                )
                quiz_form.markdown("---")
                
            if quiz_form.form_submit_button("Submit Quiz Assessment") or remaining_time <= 0:
                calculated_points = 0
                for index, item in enumerate(st.session_state.quiz_questions):
                    if selections[index] == item['correct']:
                        calculated_points += 1
                
                st.session_state.score = calculated_points
                st.session_state.quiz_submitted = True
                st.session_state.leaderboard[st.session_state.user_name] = calculated_points
                
                if "start_time" in st.session_state:
                    del st.session_state.start_time
                st.balloons()
                st.rerun()

    # --- LEADERBOARD INTERFACE COMPONENT ---
    st.write("---")
    st.header("📊 Global Leaderboard")
    
    if st.session_state.leaderboard:
        sorted_leaderboard = sorted(st.session_state.leaderboard.items(), key=lambda kv: kv[1], reverse=True)
        for position, (associate, points) in enumerate(sorted_leaderboard):
            medal = "🥇" if position == 0 else "🥈" if position == 1 else "🥉" if position == 2 else "🔹"
            st.markdown(f"{medal} **Rank #{position+1}** — {associate} : `{points} Points`")
    else:
        st.info("The global leaderboard register is currently empty.")