import streamlit as st
import xml.etree.ElementTree as ET
import google.generativeai as genai
import re
import os
import json
from google.cloud import firestore
from google.oauth2 import service_account
import streamlit_authenticator as stauth
import time
from streamlit_echarts import st_echarts

def local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

local_css("style.css")


# --- 1. TACTICAL SECRET & DB LOADER (MAINTAINED) ---
try:
    credentials_info = st.secrets["gcp_service_account_firestore"]
except (st.errors.StreamlitSecretNotFoundError, KeyError):
    creds_json = os.environ.get("GCP_SERVICE_ACCOUNT_FIRESTORE")
    credentials_info = json.loads(creds_json) if creds_json else None

if not credentials_info:
    st.error("CRITICAL: GCP Credentials missing.")
    st.stop()

gcp_service_creds = service_account.Credentials.from_service_account_info(credentials_info)
db = firestore.Client(credentials=gcp_service_creds, project=credentials_info["project_id"], database="uledb")

# --- 2. ENGINE UTILITIES ---
def load_universal_schema(file_path):
    tree = ET.parse(file_path)
    return tree.getroot()

# --- 3. SESSION STATE INITIALIZATION ---
if "active_lesson" not in st.session_state:
    st.session_state.active_lesson = "CAT-GEAR-01"
    st.session_state.needs_handshake = True  # NEW: Trigger first handshake on load
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "archived_status" not in st.session_state:
    # Tracks which element items are met: { "Lesson_ID": { "Element_Text": True/False } }
    st.session_state.archived_status = {}
if "all_histories" not in st.session_state:
    # Structure: { "Lesson-ID": [ {role, content}, ... ] }
    st.session_state.all_histories = {}


# --- 4. THE AI AUDITOR ENGINE (STABILIZED) ---
def get_auditor_response(user_input, lesson_data):
    api_key = st.secrets.get("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    
    # 1. CLEAN CONTEXT STRINGS
    c_name = str(lesson_data.get('name', 'Lesson'))
    c_brief = str(lesson_data.get('context_brief', 'No context.'))
    c_list = ", ".join(lesson_data.get('element', []))

    # 2. THE RE-INJECTED MISSION RULES (Mastery Focused)
    base_prompt = (
        f"You are the trainer for the lesson: {c_name}. "
        f"LESSON OBJECTIVE: {c_brief}. "
        f"REQUIRED LEARNING ELEMENTS: {c_list}. "
        f"YOUR MISSION: Guide the student through these elements one by one. "
        f"RULES: Strictly append [VALIDATE: ALL] only after the student has successfully passed the Application Scenario. "
        f"TONE: Encouraging, expert, safety-conscious."
    )

    model = genai.GenerativeModel(model_name='gemini-2.0-flash')
    
    if user_input == "INITIATE_HANDSHAKE":
        # One-shot stable start
        full_call = f"{base_prompt} USER: Please introduce this Lesson, explain why it's important, and start explaining the first element."
        response = model.generate_content(full_call)
    else:
        # --- THE TRANSLATOR: Schema fix for [role, parts, text] ---
        gemini_history = []
        for msg in st.session_state.chat_history:
            gemini_history.append({
                "role": msg["role"],
                "parts": [{"text": msg["content"]}]
            })
        
        chat = model.start_chat(history=gemini_history)
        # Re-verify the rules on every turn to prevent AI drift
        response = chat.send_message(f"Training Content: {base_prompt}\n\nUser Progress: {user_input}")
        
    return response.text

# Updated calculate_live_score (Line 101)
def calculate_live_score(root, archived_status, lesson_scores):
    total_weighted_points = 0
    for lesson in root.findall('.//Lesson'):
        lesson_id = lesson.get('id')
        # Look for Multiplier directly as a child of Lesson
        m_node = lesson.find('Multiplier')
        multiplier = int(m_node.text) if m_node is not None else 1
        
        if archived_status.get(lesson_id) == True:
            total_weighted_points += 100 * multiplier
    return total_weighted_points

def get_user_credentials():
    creds = {"usernames": {}}
    try:
        users_ref = db.collection("users").stream()
        for doc in users_ref:
            data = doc.to_dict()
            
            # THE FIX: Assign the email field to the 'u_name' variable
            # This tells the login widget to treat the email as the username
            u_name = data.get("email") 
            
            if u_name:
                creds["usernames"][u_name] = {
                    "name": data.get("full_name"),
                    "password": data.get("password"), 
                    "company": data.get("company")
                }
    except Exception as e:
        st.error(f"Intel Sync Error: {e}")
    return creds

# --- AUTHENTICATION GATEKEEPER ---
credentials_data = get_user_credentials()

if "authenticator" not in st.session_state:
    st.session_state.authenticator = stauth.Authenticate(
        credentials_data,
        "ule_session_cookie",
        "ule_secret_key",
        cookie_expiry_days=30
    )

authenticator = st.session_state.authenticator

# --- DATABASE SYNC ENGINE ---
def save_audit_progress():
    """Pushes all histories, scores, and archived states to Firestore."""
    if st.session_state.get("authentication_status") and st.session_state.get("username"):
        user_email = st.session_state["username"]
        doc_ref = db.collection("audits").document(user_email)
        
        doc_ref.set({
            "all_histories": st.session_state.all_histories,
            "lesson_scores": st.session_state.get("lesson_scores", {}),
            "archived_status": st.session_state.archived_status,
            "active_lesson": st.session_state.active_lesson,
            "last_updated": firestore.SERVER_TIMESTAMP
        }, merge=True)

def load_audit_progress():
    """Pull previous audit state from Firestore into session_state."""
    if st.session_state.get("authentication_status") and st.session_state.get("username"):
        user_email = st.session_state["username"]
        doc = db.collection("audits").document(user_email).get()
        
        if doc.exists:
            data = doc.to_dict()
            st.session_state.all_histories = data.get("all_histories", {})
            st.session_state.lesson_scores = data.get("lesson_scores", {})
            st.session_state.archived_status = data.get("archived_status", {})
            st.session_state.active_lesson = data.get("active_lesson", "Lesson-GOV-01")
            # Set chat_history to the last active session
            st.session_state.chat_history = st.session_state.all_histories.get(st.session_state.active_lesson, [])
            return True
    return False


# --- 5. UI LAYOUT (3-COLUMN SKETCH) ---
st.set_page_config(layout="wide", page_title="ULE2 Demo System")

# Load XML data
root = load_universal_schema('ule2-demo.xml')

# --- THE MAIN UI WRAPPER ---
if not st.session_state.get("authentication_status"):
    # Render the Login UI
    col_l, col_r = st.columns([1, 1], gap="large")
    with col_l:
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/ULEv2-welcome.png")
        
    
    with col_r: # This is the right-hand column from your login screen
        st.header("System Access")
        tab_register, tab_login = st.tabs(["Register New Account", "Resume Assessment"])
        
        with tab_login:
            # 1. Standard login widget
            # UPDATED: Use the 'fields' parameter to relabel the Username box to Email
            auth_result = authenticator.login(
                location="main", 
                key="spl_login_form",
                fields={'Form name': 'Login', 'Username': 'Corporate Email', 'Password': 'Password'}
    )
            
            # 2. THE SILENT GATE: Catch the login the moment it happens
            if st.session_state.get("authentication_status"):
                # 1. Capture the email (username) from the authenticator
                user_email = st.session_state["username"] 
                
                # 2. HYDRATE: Pull Company and Full Name from the credentials we loaded from Firestore
                # This fixes the "Company Name Not Listed" error in your sidebar
                user_info = credentials_data['usernames'].get(user_email, {})
                st.session_state["company"] = user_info.get("company", "Organization Not Listed")
                st.session_state["name"] = user_info.get("name", "Auditor")
                

                # NEW: Restore previous session from DB
                with st.spinner("Loading your data ..."):
                    load_audit_progress()
                
                st.toast(f"Welcome back! Loading your account data ...")
                time.sleep(1) 
                st.rerun() # This triggers the 'else' block immediately
                
            elif st.session_state.get("authentication_status") is False:
                st.error("Invalid Credentials. Check your corporate email and password.")

        with tab_register:
            st.subheader("New User Registration")
            with st.form("registration_form"):
                new_email = st.text_input("Corporate/Work Email")
                new_company = st.text_input("Company Name")
                new_name = st.text_input("Full Name")
                new_password = st.text_input("Password", type="password")
                
                submit_reg = st.form_submit_button("Register")
                
                if submit_reg:
                    if new_email and new_password and new_company:
                        # UPDATED SYNTAX: No more .generate()[0]
                        hashed_password = stauth.Hasher.hash(new_password)
                        
                        # Save to Firestore using email as the unique Document ID
                        db.collection("users").document(new_email).set({
                            "email": new_email,
                            "company": new_company,
                            "full_name": new_name,
                            "password": hashed_password,
                            "created_at": firestore.SERVER_TIMESTAMP,
                        })
                        
                        # 2. THE JUMP: Manually authenticate and hydrate the session
                        st.session_state["authentication_status"] = True
                        st.session_state["username"] = new_email
                        st.session_state["name"] = new_name
                        st.session_state["company"] = new_company
                        
                        # 3. INITIALIZE: Ensure they start with a clean slate for the first audit
                        st.session_state.all_histories = {}
                        st.session_state.lesson_scores = {}
                        st.session_state.archived_status = {}
                        st.session_state.active_lesson = "CAT-GEAR-01" # Start at the beginning
                        
                        st.success(f"Welcome {new_name}! System initialized for {new_company}.")
                        time.sleep(1.5)
                        st.rerun() # This skips the login screen and goes straight to Assess UI
                    else:
                        st.warning("Please fill in all mandatory fields.")

else:

    # Render the Assess UI    

    # SIDEBAR: The Speedometer & Nav
    with st.sidebar:
        st.image(
        "https://peteburnettvisuals.com/wp-content/uploads/2026/01/ULEv2-inline4.png", 
        use_container_width=True
        )
                        
        try:
            # 1. Calculation Safety
            total_lessons = root.findall('.//Lesson')
            max_score = 0
            for lesson in total_lessons:
                m_node = lesson.find('Multiplier')
                max_score += (int(m_node.text) if m_node is not None else 1) * 100
            
            archived = st.session_state.get("archived_status", {})
            scores = st.session_state.get("lesson_scores", {})
            live_score = calculate_live_score(root, archived, scores)
            
            # Ensure readiness_pct is a clean number for ECharts
            readiness_pct = round((live_score / max_score) * 100, 1) if max_score > 0 else 0.0

            unix_key = int(time.time())

            # 2. Hardened HUD Config
            gauge_option = {
                "series": [{
                    "type": "gauge",
                    "startAngle": 190,
                    "endAngle": -10,
                    "radius": "100%", 
                    "center": ["50%", "85%"], # Pivot point pushed up to avoid clipping
                    "pointer": {"show": False},
                    "itemStyle": {
                        "color": "#a855f7", 
                        "shadowBlur": 15, 
                        "shadowColor": "rgba(168, 85, 247, 0.6)"
                    },
                    "progress": {"show": True, "roundCap": True, "width": 15},
                    "axisLine": {
                        "lineStyle": {
                            "width": 15, 
                            "color": [[1, "rgba(255, 255, 255, 0.05)"]]
                        }
                    },
                    "axisTick": {"show": False},
                    "splitLine": {"show": False},
                    "axisLabel": {"show": False},
                    "detail": {
                        "offsetCenter": [0, "-15%"], 
                        "formatter": "{value}%",
                        "color": "#1e293b",
                        "fontSize": "1.5rem",
                        "fontWeight": "bold",
                        "fontFamily": "Open Sans, sans-serif"
                    },
                    "data": [{"value": readiness_pct}]
                }]
            }

            # 3. Force Render with unique key
            st_echarts(
                options=gauge_option, 
                height="150px", 
                key=f"gauge_{unix_key}" 
            )
            st.markdown(f"<p style='text-align: center; margin-top:-30px;'>{live_score} / {max_score} PTS</p>", unsafe_allow_html=True)

        except Exception as e:
            # This will tell you exactly if a variable name is missing
            st.error(f"HUD Telemetry Error: {e}")
        
              
        st.caption(f"USER: {st.session_state.get('name', 'Unknown User')}")
        st.caption(f"COMPANY: {st.session_state.get('company', 'Company Name Not Listed')}")       
        
        st.subheader("Modules:")
        modules = root.findall('Module')
        for idx, mod_node in enumerate(modules):
            mod_id = mod_node.get('id')
            mod_name = mod_node.get('name')
            
            # Check if this module is unlocked
            if idx == 0:
                mod_unlocked = True
            else:
                # Check if ALL lessons in the PREVIOUS module are complete
                prev_mod_lessons = modules[idx-1].findall('Lesson')
                mod_unlocked = all(st.session_state.archived_status.get(l.get('id')) == True for l in prev_mod_lessons)
            
            # Check if the WHOLE current module is complete
            current_mod_lessons = mod_node.findall('Lesson')
            mod_complete = all(st.session_state.archived_status.get(l.get('id')) == True for l in current_mod_lessons)
            
            # Icon Logic
            m_icon = "✅" if mod_complete else ("🔒" if not mod_unlocked else "📂")
            
            if st.button(f"{m_icon} {mod_name}", key=f"side_{mod_id}", disabled=not mod_unlocked, use_container_width=True):
                st.session_state.active_mod = mod_id
                # Set first lesson of module as active
                st.session_state.active_lesson = current_mod_lessons[0].get('id')
                st.rerun()

    # MAIN INTERFACE: 3 Columns
    col1, col2, col3 = st.columns([0.2, 0.5, 0.3], gap="medium")

    # --- COLUMN 1: THE SEQUENTIAL LESSON ROADMAP ---
    with col1:
        active_mod_id = st.session_state.get("active_mod", "CAT-GEAR")
        module_node = root.find(f".//Module[@id='{active_mod_id}']")
        mod_display_name = module_node.get('name') if module_node is not None else "Selection"

        st.subheader(f"Lessons for {mod_display_name}")
        
        if module_node is not None:
            # Capture all lessons in a list to allow index-based 'previous lesson' checks
            lessons = module_node.findall('Lesson')
            rendered_ids = set()
            
            for idx, lesson in enumerate(lessons):
                lesson_id = lesson.get('id')
                
                if lesson_id in rendered_ids:
                    continue
                rendered_ids.add(lesson_id)
                
                lesson_name = lesson.get('name')
                
                # 1. MASTERY & ACTIVE STATUS
                is_complete = st.session_state.archived_status.get(lesson_id) == True
                is_active = st.session_state.active_lesson == lesson_id
                
                # 2. SEQUENTIAL UNLOCK LOGIC
                # Rule: Lesson 0 is always unlocked. Others require the previous lesson to be complete.
                if idx == 0:
                    is_unlocked = True
                else:
                    prev_lesson_id = lessons[idx-1].get('id')
                    is_unlocked = st.session_state.archived_status.get(prev_lesson_id) == True

                # 3. ICON LOGIC
                if is_complete:
                    icon = "✅"
                elif not is_unlocked:
                    icon = "🔒"
                elif is_active:
                    icon = "🎯"
                else:
                    icon = "📖"

                display_label = f"{icon} {lesson_name}"
                button_key = f"btn_{active_mod_id}_{lesson_id}"
                
                # 4. RENDER BUTTON
                if st.button(
                    display_label, 
                    key=button_key, 
                    type="primary" if is_active else "secondary", 
                    use_container_width=True,
                    disabled=not is_unlocked  # This is the 'Mastery Gate'
                ):
                    st.session_state.active_lesson = lesson_id
                    st.session_state.chat_history = st.session_state.all_histories.get(lesson_id, [])
                    
                    if not st.session_state.chat_history:
                        st.session_state.needs_handshake = True 
                    else:
                        st.session_state.needs_handshake = False
                    st.rerun()

                # 5. UX FEEDBACK: Give them a hint if they try to click a locked lesson
                # (Streamlit handles the 'disabled' look, but a toast adds that 'explainer' feel)

    # --- COLUMN 2: THE AUDIT CHAT (Surgical Fix) ---
    with col2:
        # 1. Attempt to find the node
        active_lesson_node = root.find(f".//Lesson[@id='{st.session_state.active_lesson}']")
        
        # 2. Safety Gate: Only proceed if the lesson was found
        if active_lesson_node is not None:
            # Standard Handshake & Chat logic goes here
            if st.session_state.get("needs_handshake", False):
                handshake_data = {
                    'id': str(st.session_state.active_lesson),
                    'name': str(active_lesson_node.get('name')),
                    'context_brief': str(active_lesson_node.find('Context').text),
                    'element': [str(i.text) for i in active_lesson_node.find('LessonElements').findall('Element')]
                }

                
                response = get_auditor_response("INITIATE_HANDSHAKE", handshake_data)
                clean_resp = re.sub(r"\[.*?\]", "", response).strip()
                
                # THE FIX: Role must be 'model', not 'assistant'
                st.session_state.chat_history = [{"role": "model", "content": clean_resp}]
                st.session_state.needs_handshake = False
                st.rerun()

            # 2. STANDARD DISPLAY: Only reached if handshake is False
            st.subheader(f"🎯 LESSON: {active_lesson_node.get('name')}")

            chat_container = st.container(height=500)
            for msg in st.session_state.chat_history:
                # MAP THE ROLE: Gemini uses 'model', but Streamlit UI likes 'assistant'
                ui_role = "assistant" if msg["role"] == "model" else "user"
                
                with chat_container.chat_message(ui_role):
                    st.write(msg["content"])
                    
            if user_input := st.chat_input("Your response ..."):
                # We re-package the context for the evaluation turn
                lesson_context = {
                    'id': st.session_state.active_lesson,
                    'name': active_lesson_node.get('name'),
                    'context_brief': active_lesson_node.find('Context').text,
                    'element': [i.text for i in active_lesson_node.find('LessonElements').findall('Element')]
                }
                
                st.session_state.chat_history.append({"role": "user", "content": user_input})
                response = get_auditor_response(user_input, lesson_context)

                # 1. PROCESS AI LOGIC (No saves here)
                score_match = re.search(r"\[SCORE: (\d+)\]", response)
                if score_match:
                    val = int(score_match.group(1))
                    if "lesson_scores" not in st.session_state: st.session_state.lesson_scores = {}
                    st.session_state.lesson_scores[st.session_state.active_lesson] = val

                if "[VALIDATE: ALL]" in response:
                    st.session_state.archived_status[st.session_state.active_lesson] = True

                # 2. UPDATE LOCAL STATE
                clean_resp = re.sub(r"\[.*?\]", "", response).strip()
                st.session_state.chat_history.append({"role": "model", "content": clean_resp})
                st.session_state.all_histories[st.session_state.active_lesson] = st.session_state.chat_history

                # 3. CONSOLIDATED AUTOSAVE WITH SPINNER
                with st.status("🔒 Uploading progress to system ...") as status:
                    save_audit_progress()
                    time.sleep(2) # The perceptual buffer for database commitment
                    status.update(label="✅ Progress Secure", state="complete", expanded=False)
                
                st.rerun()

        else:
            # Fallback UI if the lesson ID is missing or mismatched
            st.warning("Please select a valid lesson from the sidebar to begin.")        

    # --- COLUMN 3: STABILIZED CHECKLIST ---
    # --- COLUMN 3: ENRICHED ELEMENT CHECKLIST ---
    with col3:
        st.subheader("Content for this Lesson")
        element_list_node = active_lesson_node.find('LessonElements')
        
        if element_list_node is not None:
            for item_node in element_list_node.findall("Element"):
                # Pull the new 'title' attribute
                title = item_node.get('title', "Requirement")
                detail = item_node.text
                
                # Mastery logic check
                is_met = st.session_state.archived_status.get(st.session_state.active_lesson) == True
                bg_color = "#28a745" if is_met else "#334155" # Emerald for pass, Slate for pending

                st.markdown(f"""
                    <div style="background-color:{bg_color}; padding:12px; border-radius:8px; margin-bottom:10px; color:white; border-left: 5px solid #a855f7;">
                        <div style="font-size:0.8rem; opacity:0.8;">💡 LEARNING OBJECTIVE</div>
                        <div style="font-weight:bold; font-size:1rem;">{title}</div>
                    </div>
                """, unsafe_allow_html=True)