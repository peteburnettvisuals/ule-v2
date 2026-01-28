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
import datetime
from streamlit_echarts import st_echarts
import vertexai
from vertexai.generative_models import GenerativeModel, ChatSession
from vertexai.preview import caching

# ----- CSS Loader -------------

def local_css(file_name):
    with open(file_name) as f:
        st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

local_css("style.css")

# ----- Vertex Caching -----------

PROJECT_ID = "ulev2-485705"  # Replace with your actual ID
LOCATION = "europe-west3"       # Frankfurt
CACHE_NAME = "skyhigh-syllabus-cache"


# --- DB LOADER (MAINTAINED) ---
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

# ---- VERTEX LOADER ---------------

vertex_creds_info = st.secrets["gcp_service_account_vertex"]
vertex_credentials = service_account.Credentials.from_service_account_info(vertex_creds_info)

vertexai.init(
    project=vertex_creds_info["project_id"],
    location="europe-west3", # Verify your region
    credentials=vertex_credentials
)

# --- 2. ENGINE UTILITIES ---

def initialize_engine():
    """Initializes Vertex AI and manages the Frankfurt Context Cache."""
    vertexai.init(project=PROJECT_ID, location=LOCATION)
    
    # 1. Try to find an existing active cache
    try:
        # List all caches in your Frankfurt project
        all_caches = caching.CachedContent.list()
        
        # Look for the most recent cache created for the 2.5 Flash model
        # Note: In production, we'd filter by a specific metadata tag or name
        target_cache = next(c for c in all_caches if "gemini-2.5-flash" in c.model_name)
        
        st.sidebar.success(f"✅ Using Active Cache: {target_cache.name[-4:]}")
        return GenerativeModel.from_cached_content(cached_content=target_cache)

    except (StopIteration, Exception):
        # 2. If no cache exists, create one from the XML
        st.sidebar.info("⏳ Initialising SkyHigh LMS ...")
        
        with open("skyhigh_textbook.xml", "r", encoding="utf-8") as f:
            xml_content = f.read()

        # The 'Handshake' System Instruction
        system_instruction = (
            "You are the SkyHigh AI Instructor. Use the provided XML as your Primary Authority. "
            "Prioritize <Module> content over the <ReferenceLibrary>. Use [AssetID] format for images."
        )

        # Create the cache with a 1-hour TTL (Time To Live)
        # Your SIM padding ensures we cross the 32,768 token floor!
        new_cache = caching.CachedContent.create(
            model_name="gemini-2.5-flash",
            system_instruction=system_instruction,
            contents=[xml_content],
            ttl=datetime.timedelta(hours=1),
        )
        
        st.sidebar.success("🚀 LMS Warmed Up & Cached!")
        return GenerativeModel.from_cached_content(cached_content=new_cache)

# ------- JSON Manifest Loader -----------------

def load_manifest():
    with open("manifest.json", "r") as f:
        return json.load(f)

manifest = load_manifest()


# --- 3. SESSION STATE INITIALIZATION ---
if "model" not in st.session_state:
    st.session_state.model = initialize_engine()
if "active_lesson" not in st.session_state:
    st.session_state.active_lesson = "GEAR-01"
    st.session_state.needs_handshake = True  # NEW: Trigger first handshake on load
if "active_mod" not in st.session_state:
    st.session_state.active_mod = manifest['modules'][0]['id']
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "archived_status" not in st.session_state:
    # Tracks which element items are met: { "Lesson_ID": { "Element_Text": True/False } }
    st.session_state.archived_status = {}
if "all_histories" not in st.session_state:
    # Structure: { "Lesson-ID": [ {role, content}, ... ] }
    st.session_state.all_histories = {}

# --- 4. THE AI INSTRUCTOR ENGINE (VERTEX CACHE VERSION) ---

def get_instructor_response(user_input):
    # Retrieve the model from session state (which is already linked to the Frankfurt Cache)
    model = st.session_state.model 
    
    # Check if a chat session already exists for this lesson
    if "chat_session" not in st.session_state:
        st.session_state.chat_session = model.start_chat()

    # We simply tell the AI which lesson we are on. 
    # Because it's grounded in the XML, it knows exactly what 'GEAR-01' means.
    context_prefix = f"[CURRENT LESSON: {st.session_state.active_lesson}] "
    
    response = st.session_state.chat_session.send_message(context_prefix + user_input)
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

# New Asset Resolver helper
def resolve_asset_path(asset_id):
    asset_info = manifest['resource_library'].get(asset_id)
    if asset_info:
        return asset_info['path']
    return None


# --- 5. UI LAYOUT (3-COLUMN SKETCH) ---
st.set_page_config(layout="wide", page_title="ULE2 Demo System")

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

    # --- SIDEBAR: PROGRESS & TELEMETRY (JSON VERSION) ---
    with st.sidebar:
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/ULEv2-inline4.png", use_container_width=True)
        
        # 1. Calculation: Extract all lesson IDs from the JSON manifest
        all_lessons = [lesson for mod in manifest['modules'] for lesson in mod['lessons']]
        total_count = len(all_lessons)
        
        # Count how many of these IDs are marked True in archived_status
        completed_count = sum(1 for l in all_lessons if st.session_state.archived_status.get(l['id']) == True)
        
        # Calculate Percentage
        readiness_pct = round((completed_count / total_count) * 100, 1) if total_count > 0 else 0.0

        # 2. ECharts Gauge (Stays the same, just consumes the new readiness_pct)
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
                    "color": "#1e293b", # Adjust based on your style.css
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
        for mod in manifest['modules']:
            # Use the icon and title from the JSON
            if st.button(f"{mod['icon']} {mod['title']}", key=f"side_{mod['id']}", width="stretch"):
                st.session_state.active_mod = mod['id']
                st.session_state.active_lesson = mod['lessons'][0]['id'] # Default to first lesson
                # Reset chat for the new lesson
                if "chat_session" in st.session_state:
                    del st.session_state.chat_session 
                st.rerun()

    # MAIN INTERFACE: 3 Columns
    col1, col2, col3 = st.columns([0.2, 0.5, 0.3], gap="medium")

    # --- COLUMN 1: THE SEQUENTIAL LESSON ROADMAP ---
    with col1:
        # 1. Resolve the Active Module from the JSON manifest
        active_mod_id = st.session_state.get("active_mod", "MOD-01")  # Updated to new ID format
        module_data = next((m for m in manifest['modules'] if m['id'] == active_mod_id), manifest['modules'][0])
        
        mod_display_name = module_data['title']
        st.subheader(f"Lessons for {mod_display_name}")

        # 2. Iterate through lessons in the current module
        lessons = module_data.get('lessons', [])
        
        for idx, lesson in enumerate(lessons):
            lesson_id = lesson['id']
            lesson_name = lesson['title']
            # We can pull estimated time or type from JSON for a richer label
            est_time = lesson.get('estimated_time', '5m')

            # --- 1. MASTERY & ACTIVE STATUS ---
            is_complete = st.session_state.archived_status.get(lesson_id) == True
            is_active = st.session_state.active_lesson == lesson_id

            # --- 2. SEQUENTIAL UNLOCK LOGIC ---
            # Rule: First lesson of the first module is always unlocked. 
            # Others require the previous lesson in the list to be complete.
            if idx == 0:
                is_unlocked = True
            else:
                prev_lesson_id = lessons[idx-1]['id']
                is_unlocked = st.session_state.archived_status.get(prev_lesson_id) == True

            # --- 3. ICON LOGIC ---
            if is_complete:
                icon = "✅"
            elif not is_unlocked:
                icon = "🔒"
            elif is_active:
                icon = "🎯"
            else:
                icon = "📖"

            # --- 4. RENDER BUTTON ---
            display_label = f"{icon} {lesson_name} ({est_time})"
            
            if st.button(
                display_label, 
                key=f"btn_roadmap_{lesson_id}", 
                type="primary" if is_active else "secondary", 
                use_container_width=True, # Streamlit's new way to do stretch
                disabled=not is_unlocked
            ):
                st.session_state.active_lesson = lesson_id
                # Cleanly swap chat history
                st.session_state.chat_history = st.session_state.all_histories.get(lesson_id, [])
                
                # Reset Chat Session for Vertex AI to prevent context bleed
                if "chat_session" in st.session_state:
                    del st.session_state.chat_session
                
                # Determine if we need to start fresh or resume
                st.session_state.needs_handshake = not bool(st.session_state.chat_history)
                st.rerun()

                

    
    # --- COLUMN 2: THE SEMANTIC MENTOR (REFACTORED FOR CACHE) ---
    with col2:
        # We now find lesson metadata from the JSON Manifest instead of raw XML parsing
        current_module = next((m for m in manifest['modules'] if m['id'] == st.session_state.get("active_mod")), manifest['modules'][0])
        current_lesson = next((l for l in current_module['lessons'] if l['id'] == st.session_state.active_lesson), current_module['lessons'][0])
        
        lesson_name = current_lesson['title']

        # 1. THE HANDSHAKE (Now using the Cache)
        if st.session_state.get("needs_handshake", False):
            # We don't send the XML text anymore; we just set the context ID
            handshake_prompt = f"INITIATE_LESSON: {st.session_state.active_lesson}. Greet the student and begin."
            response_text = get_instructor_response(handshake_prompt)
            
            # Resolve initial visuals from the [AssetID] tags
            asset_match = re.search(r"\[(IMG-.*?)\]", response_text)
            if asset_match:
                st.session_state.active_visual = asset_match.group(1)
            
            # Clean response for UI (removes brackets)
            ui_text = re.sub(r"\[.*?\]", "", response_text).strip()
            st.session_state.chat_history = [{"role": "model", "content": ui_text}]
            st.session_state.needs_handshake = False
            st.rerun()

        # 2. CHAT DISPLAY
        st.subheader(f"🎯 LESSON: {lesson_name}")
        chat_container = st.container(height=500)
        for msg in st.session_state.chat_history:
            with chat_container.chat_message("assistant" if msg["role"] == "model" else "user"):
                st.write(msg["content"])

        # 3. USER INPUT PROCESSING
        if user_input := st.chat_input("Ask a question...", key=f"chat_{st.session_state.active_lesson}"):
            st.session_state.chat_history.append({"role": "user", "content": user_input})
            
            # Call the engine (which uses the Frankfurt Cache)
            raw_response = get_instructor_response(user_input)

            # Update Visuals: Look for [IMG-XXXX] in the AI's reply
            asset_match = re.search(r"\[(IMG-.*?)\]", raw_response)
            if asset_match:
                st.session_state.active_visual = asset_match.group(1)

            # Logic Gates: Check for completion or scoring tags
            if "[VALIDATE: ALL]" in raw_response:
                st.session_state.archived_status[st.session_state.active_lesson] = True
                st.balloons()
            
            # Clean and Save
            ui_response = re.sub(r"\[.*?\]", "", raw_response).strip()
            st.session_state.chat_history.append({"role": "model", "content": ui_response})
            st.session_state.all_histories[st.session_state.active_lesson] = st.session_state.chat_history
            
            save_audit_progress()
            st.rerun()

    # --- COLUMN 3: HUD (ASSET RESOLVER) ---
    with col3:
        st.markdown("<div style='margin-top: 3.85rem;'></div>", unsafe_allow_html=True)
        st.subheader("Reference Deck")
        
        asset_id = st.session_state.get("active_visual")
        if asset_id:
            path = resolve_asset_path(asset_id)
            if path:
                # If path starts with gs://, you'd use a GCS signed URL helper here.
                # For the demo, we assume local assets/ folder mapping
                local_file = path.split("/")[-1] 
                st.image(f"assets/{local_file}", caption=f"Resource: {asset_id}")
        else:
            st.info("Awaiting visual guidance from the instructor...")