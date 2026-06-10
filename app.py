import os
import json
import pypdf
import time
import streamlit as st
from google import genai
from google.genai import types

st.set_page_config(page_title="Enterprise Team Quiz Portal", layout="centered")

# --- SECURE CREDENTIAL INITIALIZATION ---
API_KEY = os.environ.get("GEMINI_API_KEY")

if not API_KEY:
    st.error("❌ Critical Error: GEMINI_API_KEY environment variable is missing. Please add it to your hosting platform's Secrets settings.")
    st.stop()

try:
    ai_client = genai.Client(api_key=API_KEY)
except Exception as conn_err:
    st.error(f"Failed to initialize Google Gemini Engine: {conn_err}")
    st.stop()

# --- STATE TRACKING INITIALIZATION ---
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

# --- CLOUD-SAFE PDF PARSING ENGINE ---
def extract_quiz_from_pdf(pdf_file, num_q):
    """Extracts raw text from an uploaded file and forces Gemini to output a strict JSON array."""
    try:
        reader = pypdf.PdfReader(pdf_file)
        raw_text = ""
        for page in reader.pages[:10]: # Read up to 10 pages for larger question sets
            text_content = page.extract_text()
            if text_content:
                raw_text += text_content + "\n"
        
        if not raw_text.strip():
            st.error("Could not find readable text layers inside the uploaded PDF.")
            return []
            
        system_instructions = (
            "You are an expert technical evaluator. Your job is to extract data facts from the provided text "
            f"and output EXACTLY {num_q} unique, high-quality multiple choice assessment questions based on it."
        )
        
        user_prompt = f"Generate exactly {num_q} multiple-choice questions from this source material:\n\n{raw_text}"
        
        response_schema = {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "question": {"type": "STRING"},
                    "options": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"}
                    },
                    "correct": {"type": "STRING"}
                },
                "required": ["question", "options", "correct"]
            }
        }
        
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instructions,
                temperature=0.3,
                response_mime_type="application/json",
                response_schema=response_schema
            )
        )
        
        return json.loads(response.text.strip())
        
    except Exception as api_err:
        st.error(f"Gemini Engine failed to parse the document asset: {api_err}")
        return []

# --- APPLICATION WORKFLOW USER INTERFACE ---
st.title("🏆 Associate Quiz Portal")

# Create main app tabs
tab_quiz, tab_admin = st.tabs(["✏️ Take Quiz", "🔐 Admin Dashboard"])

# --- TAB 2: ADMIN PANEL (PASSWORD PROTECTED) ---
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
                    st.success("Logged in successfully!")
                    st.rerun()
                else:
                    st.error("Invalid Username or Password.")
    else:
        st.write("🟢 Authenticated as Administrator")
        if st.button("Logout"):
            st.session_state.admin_logged_in = False
            st.rerun()
            
        st.write("---")
        st.subheader("Quiz Configuration Settings")
        
        # Admin controls for rules
        st.session_state.num_questions = st.number_input(
            "Number of questions to generate:", min_value=1, max_value=25, value=st.session_state.num_questions
        )
        st.session_state.time_per_question = st.number_input(
            "Time limit per question (seconds):", min_value=5, max_value=120, value=st.session_state.time_per_question
        )
        
        st.write("---")
        st.subheader("Upload PDF & Generate Quiz")
        uploaded_file = st.file_uploader("Upload Quiz Source (PDF)", type="pdf")
        
        if st.button("Build & Publish Quiz") and uploaded_file:
            with st.spinner("Gemini is reading text and building your custom quiz configurations..."):
                compiled_quiz = extract_quiz_from_pdf(uploaded_file, st.session_state.num_questions)
                if compiled_quiz:
                    st.session_state.quiz_questions = compiled_quiz
                    st.session_state.score = 0 
                    st.session_state.quiz_submitted = False
                    st.success(f"Successfully generated and published a {len(compiled_quiz)}-question quiz!")

# --- TAB 1: ASSOCIATE QUIZ INTERFACE ---
with tab_quiz:
    if not st.session_state.user_name:
        st.subheader("Welcome! Please register your session identity to begin:")
        name_input = st.text_input("Enter Your Full Name:")
        if st.button("Access Testing Portal") and name_input:
            st.session_state.user_name = name_input.strip()
            st.rerun()
    else:
        st.write(f"👤 Associate: **{st.session_state.user_name}**")
        
        if not st.session_state.quiz_questions:
            st.info("📢 Waiting for the Admin to upload a PDF and publish a quiz. Please check back shortly!")
        elif st.session_state.quiz_submitted:
            st.success(f"🎉 Your submission is locked in! You scored: {st.session_state.score} / {len(st.session_state.quiz_questions)}")
        else:
            total_allowed_time = len(st.session_state.quiz_questions) * st.session_state.time_per_question
            st.warning(f"⏳ Instructions: You have **{st.session_state.time_per_question} seconds per question**. Total time allowed: **{total_allowed_time} seconds**.")
            
            # --- PROGRESSIVE TIMING ENGINE ---
            if "start_time" not in st.session_state:
                st.session_state.start_time = time.time()
                
            elapsed_time = int(time.time() - st.session_state.start_time)
            remaining_time = total_allowed_time - elapsed_time
            
            if remaining_time <= 0:
                st.error("💥 Time's up! Your answers are being auto-submitted.")
                st.session_state.quiz_submitted = True
                # Evaluate whatever they had checked
                st.rerun()
            else:
                st.metric(label="⌛ Time Remaining for Entire Quiz", value=f"{remaining_time} seconds")
                # Trigger an app refresh every 2 seconds to update the clock countdown seamlessly
                st.empty() 
                
            # Dynamic Test Rendering Form
            quiz_form = st.form(key="active_quiz_form")
            selections = {}
            
            for index, item in enumerate(st.session_state.quiz_questions):
                quiz_form.markdown(f"**Question {index+1}:** {item['question']}")
                selections[index] = quiz_form.radio(
                    "Choose your answer option:", 
                    item['options'], 
                    key=f"item_choice_{index}"
                )
                quiz_form.markdown("---")
                
            if quiz_form.form_submit_button("Submit Score to Board") or remaining_time <= 0:
                calculated_points = 0
                for index, item in enumerate(st.session_state.quiz_questions):
                    if selections[index] == item['correct']:
                        calculated_points += 1
                
                st.session_state.score = calculated_points
                st.session_state.quiz_submitted = True
                st.session_state.leaderboard[st.session_state.user_name] = calculated_points
                
                # Clean up timer token state for future runs
                del st.session_state.start_time
                st.balloons()
                st.rerun()

    # Dashboard Performance Standings Component
    st.write("---")
    st.header("📊 Global Standings")
    
    if st.session_state.leaderboard:
        sorted_leaderboard = sorted(st.session_state.leaderboard.items(), key=lambda kv: kv[1], reverse=True)
        for position, (associate, points) in enumerate(sorted_leaderboard):
            medal = "🥇" if position == 0 else "🥈" if position == 1 else "🥉" if position == 2 else "🔹"
            st.markdown(f"{medal} **Rank #{position+1}** — {associate} : `{points} Points`")
    else:
        st.info("The ranking register is currently blank.")