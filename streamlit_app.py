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

# --- 4. THE AI INSTRUCTOR ENGINE (CARTRIDGE OPTIMIZED) ---
def get_instructor_response(user_input, lesson_cartridge):
    # 1. Personalization Hydration
    user_name = st.session_state.get("name", "Student")
    profile = st.session_state.get("u_profile", "A student eager to learn.")
    
    # 2. The Semantic Prompt
    # We pass the raw XML text block directly as the 'Cartridge'
    base_prompt = f"""
    You are the SkyHigh Master Instructor, an expert skydiving coach. 
    STUDENT: {user_name} (Goal: {profile})

    LESSON CARTRIDGE (Your Source of Truth):
    {lesson_cartridge}

    PEDAGOGICAL RULES:
    1. Greet the student using first name, and introduce the overall lesson topic found within the cartridge.
    2. Explain the 'Teaching Content' points conversationally, one at a time. Check the student's understanding on each point before moving on. Close weith a question rather than using instructions like "(Waiting for student response)".
    3. You have visual assets listed which will enrich the students understanding. You MUST use the exact [[filename.jpg]] tags found in the 'Visual Resources' section to illustrate your points. Show the first relevant asset right away.
    4. Once the content is explained, transition to the 'Test Scenario' provided in the cartridge.
    5. If the student successfully navigates the scenario, append [VALIDATE: ALL].

    STRICT: Use ONLY the information in the cartridge. Do not hallucinate external skydiving facts. 
    If you want the UI to show an image, you MUST output the [[filename.jpg]] tag.
    """

    model = genai.GenerativeModel(model_name='gemini-2.0-flash')
    
    # 3. History Sync
    gemini_history = []
    for msg in st.session_state.chat_history:
        role = "model" if msg["role"] == "model" else "user"
        gemini_history.append({"role": role, "parts": [{"text": msg["content"]}]})

    # 4. Execution Logic
    if user_input == "INITIATE_HANDSHAKE":
        full_call = f"{base_prompt}\n\nUSER: {user_name} is ready for training. Begin the lesson."
        response = model.generate_content(full_call)
    else:
        chat = model.start_chat(history=gemini_history)
        # We inject the base_prompt as a 'System Instruction' on every turn to prevent drift
        context_injection = f"SYSTEM INSTRUCTION: Stick to the Lesson Cartridge and the CDAA method. \nUSER: {user_input}"
        response = chat.send_message(context_injection)
        
    return response.text

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
    """Pull previous user profile and audit state from Firestore."""
    if st.session_state.get("authentication_status") and st.session_state.get("username"):
        user_email = st.session_state["username"]
        
        # 1. HYDRATE PROFILE: Pull Aspirations/Experience from 'users' collection
        user_doc = db.collection("users").document(user_email).get()
        if user_doc.exists:
            u_data = user_doc.to_dict()
            exp = u_data.get('experience', 'New recruit')
            asp = u_data.get('aspiration', 'Professional certification')
            # Create the string for the AI prompt
            st.session_state["u_profile"] = f"Experience: {exp}. Goals: {asp}"
        
        # 2. HYDRATE PROGRESS: Pull Audit data from 'audits' collection
        audit_doc = db.collection("audits").document(user_email).get()
        if audit_doc.exists:
            data = audit_doc.to_dict()
            st.session_state.all_histories = data.get("all_histories", {})
            st.session_state.lesson_scores = data.get("lesson_scores", {})
            st.session_state.archived_status = data.get("archived_status", {})
            # Use your new XML ID 'CAT-GEAR-01' as the default fallback
            st.session_state.active_lesson = data.get("active_lesson", "CAT-GEAR-01")
            
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
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/ULEv2welcome2.png")
        
    
    with col_r: # This is the right-hand column from your login screen
        st.header("System Access")
        tab_register, tab_login = st.tabs(["Register New Account", "Resume Training"])
        
        with tab_login:
            # 1. Standard login widget
            # UPDATED: Use the 'fields' parameter to relabel the Username box to Email
            auth_result = authenticator.login(
                location="main", 
                key="spl_login_form",
                fields={'Form name': 'Login', 'Username': 'Email', 'Password': 'Password'}
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
                new_email = st.text_input("Email")
                new_company = st.text_input("Company Name")
                new_name = st.text_input("Full Name")
                new_password = st.text_input("Password", type="password")
                u_experience = st.text_area("What is your current experience level in this field?", placeholder="e.g., Total beginner...")
                u_aspiration = st.text_area("What do you hope to achieve with this training?", placeholder="e.g., I want to become a solo jumper...")
                        
                submit_reg = st.form_submit_button("Register")
                
                if submit_reg:
                    if new_email and new_password and new_company:
                        # 1. THE SECURITY SEAL: Hash the password
                        hashed_password = stauth.Hasher.hash(new_password)
                        
                        # 2. DATA COMMIT: Save with the new profile fields
                        db.collection("users").document(new_email).set({
                            "email": new_email,
                            "company": new_company,
                            "full_name": new_name,
                            "password": hashed_password,
                            "experience": u_experience,
                            "aspiration": u_aspiration,
                            "created_at": firestore.SERVER_TIMESTAMP,
                        })
                        
                        # 3. HYDRATION: Manually set session state to bypass the login screen
                        st.session_state["u_profile"] = f"Experience: {u_experience}. Goals: {u_aspiration}"
                        st.session_state["authentication_status"] = True
                        st.session_state["username"] = new_email
                        st.session_state["name"] = new_name
                        st.session_state["company"] = new_company
                        
                        # 4. INITIALIZE progress containers
                        st.session_state.all_histories = {}
                        st.session_state.lesson_scores = {}
                        st.session_state.archived_status = {}
                        st.session_state.active_lesson = "CAT-GEAR-01" 
                        
                        st.success(f"Welcome {new_name}! System initialized for {new_company}.")
                        time.sleep(1.5)
                        st.rerun() 
                    else:
                        st.warning("Please fill in all mandatory fields.")

else:

    # Render the Assess UI    

    # --- SIDEBAR: PROGRESS & TELEMETRY ---
    with st.sidebar:
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/ULEv2-inline4.png", use_container_width=True)
        
        # 1. Calculation: Percentage of Cartridges Completed
        total_lessons = root.findall('.//Lesson')
        total_count = len(total_lessons)
        
        # Count how many lesson IDs are marked True in archived_status
        completed_count = sum(1 for l in total_lessons if st.session_state.archived_status.get(l.get('id')) == True)
        
        # Simple Progress Percentage
        readiness_pct = round((completed_count / total_count) * 100, 1) if total_count > 0 else 0.0

        # 2. Hardened HUD Config (ECharts Gauge)
        gauge_option = {
            "series": [{
                "type": "gauge",
                "startAngle": 190,
                "endAngle": -10,
                "radius": "100%", 
                "center": ["50%", "85%"],
                "pointer": {"show": False},
                "itemStyle": {
                    "color": "#a855f7", 
                    "shadowBlur": 15, 
                    "shadowColor": "rgba(168, 85, 247, 0.6)"
                },
                "progress": {"show": True, "roundCap": True, "width": 15},
                "axisLine": {"lineStyle": {"width": 15, "color": [[1, "rgba(255, 255, 255, 0.05)"]]}},
                "axisTick": {"show": False},
                "splitLine": {"show": False},
                "axisLabel": {"show": False},
                "detail": {
                    "offsetCenter": [0, "-15%"], 
                    "formatter": "{value}%",
                    "color": "#1e293b",
                    "fontSize": "1.5rem",
                    "fontWeight": "bold"
                },
                "data": [{"value": readiness_pct}]
            }]
        }

        st_echarts(options=gauge_option, height="150px", key=f"gauge_{int(time.time())}")
        st.markdown(f"<p style='text-align: center; margin-top:-30px;'>{completed_count} / {total_count} LESSONS COMPLETE</p>", unsafe_allow_html=True)
        
        st.divider() #
        
        # 3. Module Selection
        st.subheader("Training Modules")
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
            
            if st.button(f"{m_icon} {mod_name}", key=f"side_{mod_id}", disabled=not mod_unlocked, width="stretch"):
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
                lesson_desc = lesson.get('desc', "No description available.")

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

                display_label = f"{icon} {lesson_name} : {lesson_desc}"
                button_key = f"btn_{active_mod_id}_{lesson_id}"
                
                # 4. RENDER BUTTON
                if st.button(
                    display_label, 
                    key=button_key, 
                    type="primary" if is_active else "secondary", 
                    width="stretch",
                    disabled=not is_unlocked  # This is the 'Mastery Gate'
                ):
                    st.session_state.active_lesson = lesson_id
                    st.session_state.chat_history = st.session_state.all_histories.get(lesson_id, [])
                    
                    if not st.session_state.chat_history:
                        st.session_state.needs_handshake = True 
                    else:
                        st.session_state.needs_handshake = False
                    st.rerun()

                

    
    # --- COLUMN 2: THE SEMANTIC MENTOR (REFACTORED) ---
    with col2:
        active_lesson_node = root.find(f".//Lesson[@id='{st.session_state.active_lesson}']")
        
        if active_lesson_node is not None:
            # 1. THE CARTRIDGE EXTRACTION
            # We pull the raw text (Teaching, Visuals, Scenario) in one go
            lesson_cartridge = active_lesson_node.text.strip()
            lesson_name = active_lesson_node.get('name')

            # 2. THE HANDSHAKE (Cartridge-Based)
            if st.session_state.get("needs_handshake", False):
                # Pass the raw text lump directly to the smart engine
                response = get_instructor_response("INITIATE_HANDSHAKE", lesson_cartridge)
                
                # Catch any initial visual tag
                img_match = re.search(r"\[\[(.*?\.jpg|.*?\.png)\]\]", response)
                if img_match:
                    st.session_state.active_visual = img_match.group(1)
                
                # Clean and initialize history
                clean_resp = re.sub(r"\[.*?\]", "", response).replace("]", "").strip()
                st.session_state.chat_history = [{"role": "model", "content": clean_resp}]
                st.session_state.needs_handshake = False
                st.rerun()

            # 3. CHAT DISPLAY (Persistent & Clean)
            st.subheader(f"🎯 LESSON: {lesson_name}")
            chat_container = st.container(height=500)
            
            for msg in st.session_state.chat_history:
                ui_role = "assistant" if msg["role"] == "model" else "user"
                with chat_container.chat_message(ui_role):
                    # UI is kept clean; tags are stripped because they render in Col 3
                    st.write(msg["content"])

            # 4. USER INPUT & RESPONSE PROCESSING
            if user_input := st.chat_input("Your response ...", key=f"chat_input_{st.session_state.active_lesson}"):
                st.session_state.chat_history.append({"role": "user", "content": user_input})
                
                # Get response using the single cartridge source
                response = get_instructor_response(user_input, lesson_cartridge)

                # --- THE VISUAL BRIDGE ---
                # Catch visual tags to update Column 3
                img_match = re.search(r"\[\[(.*?\.jpg|.*?\.png)\]\]", response)
                if img_match:
                    st.session_state.active_visual = img_match.group(1)

                # --- THE LOGIC GATE ---
                # Handle mastery validation and scoring
                if "[VALIDATE: ALL]" in response:
                    st.session_state.archived_status[st.session_state.active_lesson] = True
                
                score_match = re.search(r"\[SCORE: (\d+)\]", response)
                if score_match:
                    st.session_state.lesson_scores[st.session_state.active_lesson] = int(score_match.group(1))

                # --- UI CLEANUP & SAVE ---
                clean_resp = re.sub(r"\[.*?\]", "", response).replace("]", "").strip()
                st.session_state.chat_history.append({"role": "model", "content": clean_resp})
                st.session_state.all_histories[st.session_state.active_lesson] = st.session_state.chat_history
                
                with st.status("🔒 Securing Training Data...") as status:
                    save_audit_progress()
                    status.update(label="✅ Progress Secure", state="complete")
                
                st.rerun()

        else:
            # Fallback UI if the lesson ID is missing or mismatched
            st.warning("Please select a valid lesson from the sidebar to begin.")        

    # --- COLUMN 3: STABILIZED CHECKLIST ---
    with col3:
        st.subheader("Visual Reference")
        
        # Check if the Instructor has "passed" an image to the deck
        active_img = st.session_state.get("active_visual")
        
        if active_img:
            try:
                # We use use_container_width to ensure it fits the 2026 UI spec
                st.image(f"assets/{active_img}", use_container_width=True)
                
                # Optional: Add a caption to help the student focus
                st.markdown(f"""
                    <p style='color: #cbd5e1; font-size: 0.8rem; font-style: italic; margin-top: -10px;'>
                        Currently viewing: {active_img}
                    </p>
                """, unsafe_allow_html=True)
            except:
                st.warning(f"Static Asset Sync: {active_img} not found in /assets/")
        else:
            # Initial State: A placeholder or helpful hint
            st.info("Visual aids will appear here automatically as the Instructor introduces them.")