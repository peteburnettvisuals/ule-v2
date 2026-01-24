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
db = firestore.Client(credentials=gcp_service_creds, project=credentials_info["project_id"], database="spldb")

# --- 2. ENGINE UTILITIES ---
def load_universal_schema(file_path):
    tree = ET.parse(file_path)
    return tree.getroot()

# --- 3. SESSION STATE INITIALIZATION ---
if "active_csf" not in st.session_state:
    st.session_state.active_csf = "CSF-GOV-01"
    st.session_state.needs_handshake = True  # NEW: Trigger first handshake on load
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "archived_status" not in st.session_state:
    # Tracks which criteria items are met: { "CSF_ID": { "Criteria_Text": True/False } }
    st.session_state.archived_status = {}
if "all_histories" not in st.session_state:
    # Structure: { "CSF-ID": [ {role, content}, ... ] }
    st.session_state.all_histories = {}


# --- 4. THE AI AUDITOR ENGINE (STABILIZED) ---
def get_auditor_response(user_input, csf_data):
    api_key = st.secrets.get("GEMINI_API_KEY")
    genai.configure(api_key=api_key)
    
    # 1. CLEAN CONTEXT STRINGS
    c_name = str(csf_data.get('name', 'Audit Item'))
    c_brief = str(csf_data.get('context_brief', 'No context.'))
    c_type = str(csf_data.get('type', 'Proportional'))
    c_list = ", ".join(csf_data.get('criteria', []))

    # 2. THE RE-INJECTED MISSION RULES
    # This ensures the AI knows the 'Secret Handshake' for the UI
    base_prompt = (
        f"You are the Bid Readiness Assistant for {c_name}. "
        f"MISSION BRIEF: {c_brief}. EVALUATION MODE: {c_type}. "
        f"CRITERIA: {c_list}. "
        f"RULES: If MODE is 'Binary', append [VALIDATE: ALL] strictly when the user states that they beleive that all shown criteria are met."
        f"If MODE is 'Proportional', append [SCORE: X] where X is 0-100 based on what the user says they have available."
        f"TONE OF VOICE: Friendly, helpful, professional."
    )

    model = genai.GenerativeModel(model_name='gemini-2.0-flash')
    
    if user_input == "INITIATE_HANDSHAKE":
        # One-shot stable start
        full_call = f"{base_prompt} USER: Please introduce this CSF, explain why it's important, and ask if I believe that my organisation can meet the requirements shown."
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
        response = chat.send_message(f"Audit Rules: {base_prompt}\n\nUser Evidence: {user_input}")
        
    return response.text

def calculate_live_score(root, archived_status, csf_scores):
    total_weighted_points = 0
    
    # Iterate through all CSFs in the XML (Plain-text find)
    for csf in root.findall('.//CSF'):
        csf_id = csf.get('id')
        attribs = csf.find('CanonicalAttributes')
        
        # Pull the weight (Multiplier) from the XML
        multiplier = int(attribs.find('Multiplier').text)
        
        # Calculate the raw 0-100 score for this factor
        if archived_status.get(csf_id) == True:
            # Binary 'Must' items are cleared, awarding 100% of weight
            raw_score = 100  
        else:
            # Proportional 'Best Guess' from the AI (Default to 0 if not yet audited)
            raw_score = csf_scores.get(csf_id, 0)
            
        # Add the weighted contribution to the total
        total_weighted_points += raw_score * multiplier
                
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
        "spl_bid_cookie",
        "spl_secret_key",
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
            "csf_scores": st.session_state.get("csf_scores", {}),
            "archived_status": st.session_state.archived_status,
            "active_csf": st.session_state.active_csf,
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
            st.session_state.csf_scores = data.get("csf_scores", {})
            st.session_state.archived_status = data.get("archived_status", {})
            st.session_state.active_csf = data.get("active_csf", "CSF-GOV-01")
            # Set chat_history to the last active session
            st.session_state.chat_history = st.session_state.all_histories.get(st.session_state.active_csf, [])
            return True
    return False


# --- 5. UI LAYOUT (3-COLUMN SKETCH) ---
st.set_page_config(layout="wide", page_title="SPL Bid Readiness")

# Load XML data
root = load_universal_schema('bidcheck-config.xml')

# --- THE MAIN UI WRAPPER ---
if not st.session_state.get("authentication_status"):
    # Render the Login UI
    col_l, col_r = st.columns([1, 1], gap="large")
    with col_l:
        st.image("https://peteburnettvisuals.com/wp-content/uploads/2026/01/bidready1.png")
        
    
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
                with st.spinner("Restoring Audit Intelligence..."):
                    load_audit_progress()
                
                st.toast(f"Welcome back! Loading your data ...")
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
                        st.session_state.csf_scores = {}
                        st.session_state.archived_status = {}
                        st.session_state.active_csf = "CSF-GOV-01" # Start at the beginning
                        
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
        "https://peteburnettvisuals.com/wp-content/uploads/2026/01/bidready-inlinelogo.png", 
        use_container_width=True
        )
                        
        try:
            # 1. Calculation Safety
            total_factors = root.findall('.//CSF')
            max_score = sum(int(csf.find('CanonicalAttributes/Multiplier').text) * 100 for csf in total_factors)
            
            archived = st.session_state.get("archived_status", {})
            scores = st.session_state.get("csf_scores", {})
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
        
        st.subheader("Readiness Categories:")
        for cat in root.findall('Category'):
            cat_id = cat.get('id')
            cat_name = cat.get('name')
            
            # 1. Check for Category Completion (Your existing logic)
            cat_csf_ids = [csf.get('id') for csf in cat.findall('CSF')]
            is_cat_complete = all(
                st.session_state.archived_status.get(cid) == True or 
                st.session_state.get("csf_scores", {}).get(cid, 0) >= 85
                for cid in cat_csf_ids
            )
            
            cat_label = f"{cat_name} ✅" if is_cat_complete else cat_name
            
            if st.button(cat_label, key=f"cat_btn_{cat_id}", use_container_width=True):
                # 2. Update the active Category
                st.session_state.active_cat = cat_id
                
                # 3. SWITCH FOCUS: Find the first CSF in this new category
                first_csf = cat.find('CSF')
                if first_csf is not None:
                    new_csf_id = first_csf.get('id')
                    st.session_state.active_csf = new_csf_id
                    
                    # 4. LOAD HISTORY: Ensure Col 2 switches to the right chat
                    st.session_state.chat_history = st.session_state.all_histories.get(new_csf_id, [])
                    
                    # 5. HANDSHAKE CHECK: Trigger if it's a fresh area
                    st.session_state.needs_handshake = not bool(st.session_state.chat_history)
                
                st.rerun()

    # MAIN INTERFACE: 3 Columns
    col1, col2, col3 = st.columns([0.2, 0.5, 0.3], gap="medium")

    # --- COLUMN 1: Unique Key Fix ---
    with col1:
        active_cat_id = st.session_state.get("active_cat", "CAT-GOV")
        category_node = root.find(f".//Category[@id='{active_cat_id}']")
        cat_display_name = category_node.get('name') if category_node is not None else "Selection"

        st.subheader(f"Critical Success Factors for {cat_display_name}")
        
        if category_node is not None:
            # Use a set to track rendered IDs in this specific loop run
            rendered_ids = set()
            
            for csf in category_node.findall('CSF'):
                csf_id = csf.get('id')
                
                # Avoid duplicate rendering in the same loop
                if csf_id in rendered_ids:
                    continue
                rendered_ids.add(csf_id)
                
                csf_name = csf.get('name')
                
                # Keep your Tick Logic
                is_validated = st.session_state.archived_status.get(csf_id, False)
                current_val = st.session_state.get("csf_scores", {}).get(csf_id, 0)
                display_label = f"{csf_name} ✅" if (is_validated or current_val >= 85) else csf_name
                
                is_active = st.session_state.active_csf == csf_id
                
                # UNIQUE KEY: Combine Category + CSF ID to ensure uniqueness
                button_key = f"btn_{active_cat_id}_{csf_id}"
                
                if st.button(
                    display_label, 
                    key=button_key, 
                    type="primary" if is_active else "secondary", 
                    use_container_width=True
                ):
                    st.session_state.active_csf = csf_id
                    # Load history for this specific CSF from our dictionary
                    st.session_state.chat_history = st.session_state.all_histories.get(csf_id, [])
                    
                    # If there is no history, trigger the handshake flag
                    if not st.session_state.chat_history:
                        st.session_state.needs_handshake = True 
                    else:
                        st.session_state.needs_handshake = False
                    st.rerun()

    # --- COLUMN 2: THE AUDIT CHAT (Surgical Fix) ---
    with col2:
        active_csf_node = root.find(f".//CSF[@id='{st.session_state.active_csf}']")
        attribs = active_csf_node.find('CanonicalAttributes')
        
        # Standard Handshake Trigger (Same as before, but with 'model' role fix)
        if st.session_state.get("needs_handshake", False):
            with st.spinner("Lead Auditor entering..."):
                handshake_data = {
                    'id': str(st.session_state.active_csf),
                    'name': str(active_csf_node.get('name')),
                    'type': str(attribs.find('Type').text),
                    'context_brief': str(attribs.find('Context').text),
                    # Ensure this is a list of STRINGS, not XML Elements
                    'criteria': [str(i.text) for i in attribs.find('Criteria').findall('Item')]
}
                
                response = get_auditor_response("INITIATE_HANDSHAKE", handshake_data)
                clean_resp = re.sub(r"\[.*?\]", "", response).strip()
                
                # THE FIX: Role must be 'model', not 'assistant'
                st.session_state.chat_history = [{"role": "model", "content": clean_resp}]
                st.session_state.needs_handshake = False
                st.rerun()

        # 2. STANDARD DISPLAY: Only reached if handshake is False
        st.subheader(f"💬 Validating: {active_csf_node.get('name')}")

        chat_container = st.container(height=500)
        for msg in st.session_state.chat_history:
            # MAP THE ROLE: Gemini uses 'model', but Streamlit UI likes 'assistant'
            ui_role = "assistant" if msg["role"] == "model" else "user"
            
            with chat_container.chat_message(ui_role):
                st.write(msg["content"])
                
        if user_input := st.chat_input("Your response ..."):
            # We re-package the context for the evaluation turn
            csf_context = {
                'id': st.session_state.active_csf,
                'name': active_csf_node.get('name'),
                'type': attribs.find('Type').text,
                'context_brief': attribs.find('Context').text,
                'criteria': [i.text for i in attribs.find('Criteria').findall('Item')]
            }
            
            st.session_state.chat_history.append({"role": "user", "content": user_input})
            response = get_auditor_response(user_input, csf_context)

            # 1. PROCESS AI LOGIC (No saves here)
            score_match = re.search(r"\[SCORE: (\d+)\]", response)
            if score_match:
                val = int(score_match.group(1))
                if "csf_scores" not in st.session_state: st.session_state.csf_scores = {}
                st.session_state.csf_scores[st.session_state.active_csf] = val

            if "[VALIDATE: ALL]" in response:
                st.session_state.archived_status[st.session_state.active_csf] = True

            # 2. UPDATE LOCAL STATE
            clean_resp = re.sub(r"\[.*?\]", "", response).strip()
            st.session_state.chat_history.append({"role": "model", "content": clean_resp})
            st.session_state.all_histories[st.session_state.active_csf] = st.session_state.chat_history

            # 3. CONSOLIDATED AUTOSAVE WITH SPINNER
            with st.status("🔒 Securing Evidence to C2...") as status:
                save_audit_progress()
                time.sleep(2) # The perceptual buffer for database commitment
                status.update(label="✅ Progress Secure", state="complete", expanded=False)
            
            st.rerun()

    # --- COLUMN 3: STABILIZED CHECKLIST ---
    with col3:
        st.subheader("Requirement Checklist")
        criteria_nodes = active_csf_node.findall(".//Item")
        
        # Check if the ENTIRE CSF is already validated (True)
        csf_validated_globally = st.session_state.archived_status.get(st.session_state.active_csf) == True
        
        for item_node in criteria_nodes:
            text = item_node.text
            priority = item_node.get("priority")
            
            # FIX: Ensure we don't call .get() on a boolean 'True'
            csf_entry = st.session_state.archived_status.get(st.session_state.active_csf, {})
            
            if csf_validated_globally:
                is_met = True
            elif isinstance(csf_entry, dict):
                is_met = csf_entry.get(text, False)
            else:
                is_met = False
                
            # Color coding logic remains the same
            bg_color = "#28a745" if is_met else ("#dc3545" if priority == "Must" else "#ffc107")
            st.markdown(f"""
                <div style="background-color:{bg_color}; padding:15px; border-radius:5px; margin-bottom:10px; color:white; font-weight:bold;">
                    [{priority.upper()}] {text}
                </div>
            """, unsafe_allow_html=True)