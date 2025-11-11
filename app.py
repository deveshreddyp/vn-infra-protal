from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
from google.generativeai.types import GenerationConfig
from os import remove as os_remove

# --- 1. CONFIGURE YOUR API KEY ---
API_KEY = os.environ.get('GOOGLE_API_KEY') # Make sure your secret key is here
try:
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")
    print("Please make sure you have set your API_KEY correctly.")

# -----------------------------------

app = Flask(__name__)
CORS(app)

# --- FOLDERS & DATABASE FILE ---
UPLOAD_FOLDER = 'uploads' 
APPLICATION_FOLDER = 'applications' 
DB_FILE = 'applications.json' 
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(APPLICATION_FOLDER, exist_ok=True)

# --- 2. PRE-DEFINED JOB DESCRIPTIONS ---
JOB_DESCRIPTIONS = {
    "web_developer": """
        Full-Stack Web Developer. Responsibilities: Build and maintain websites.
        Required Skills: HTML, CSS, JavaScript, React, Python, Flask, Django, and SQL databases like PostgreSQL or MySQL.
        Must be proficient with Git/GitHub. Experience with cloud platforms (AWS, Heroku) is a major plus.
    """,
    "site_engineer": """
        Civil Site Engineer. Responsibilities: Supervise construction projects,
        manage on-site operations, and ensure safety standards.
        Required Skills: AutoCAD, Project Management, MS Project, building science,
        and in-depth understanding of construction procedures. BSc/BA in engineering.
    """
}

# --- 3. REFACTORED AI SCANNER FUNCTION ---
def get_ai_scan(resume_text, jd_text):
    SYSTEM_PROMPT = """
    You are an expert HR recruiter...
    {{
      "candidateName": "The candidate's full name",
      "candidateEmail": "The candidate's email, or 'N/A'",
      "matchScore": <A percentage score from 0 to 100>,
      "matchingSkills": ["List of skills..."],
      "missingSkills": ["List of skills..."],
      "summary": "A 2-3 sentence summary..."
    }}
    ---RESUME TEXT---
    {resume_text}
    ---END RESUME---
    ---JOB DESCRIPTION---
    {jd_text}
    ---END JD---
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    prompt = SYSTEM_PROMPT.format(resume_text=resume_text, jd_text=jd_text)
    generation_config = GenerationConfig(temperature=0)
    response = model.generate_content(prompt, generation_config=generation_config)
    clean_response_text = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(clean_response_text)

def extract_pdf_text(pdf_file_stream):
    try:
        pdf_reader = PyPDF2.PdfReader(pdf_file_stream)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return None

# --- 4. HELPER FUNCTIONS FOR OUR "DATABASE" ---
def load_db():
    if not os.path.exists(DB_FILE):
        return []
    try:
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return []

def save_db(data):
    with open(DB_FILE, 'w') as f:
        json.dump(data, f, indent=4)

# --- 5. API ENDPOINTS ---

@app.route('/scan-resume', methods=['POST'])
def scan_resume():
    # (This function is unchanged)
    try:
        resume_file = request.files['resume']
        jd_text = request.form['jobDescription']
        resume_bytes = io.BytesIO(resume_file.read())
        resume_text = extract_pdf_text(resume_bytes)
        if not resume_text:
            return jsonify({'error': 'Could not read text from PDF.'}), 400
        ai_response = get_ai_scan(resume_text, jd_text)
        return jsonify(ai_response)
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (scan-resume) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/apply', methods=['POST'])
def handle_application():
    # (This function is unchanged)
    try:
        resume_file = request.files['resume']
        candidate_name = request.form['name']
        candidate_email = request.form['email']
        job_key = request.form['jobTitle']
        filename = f"{candidate_name.replace(' ', '_')}-{job_key}-{resume_file.filename}"
        file_path = os.path.join(APPLICATION_FOLDER, filename)
        resume_file.save(file_path)
        jd_text = JOB_DESCRIPTIONS.get(job_key)
        if not jd_text:
            return jsonify({'error': 'Invalid job title selected.'}), 400
        with open(file_path, 'rb') as f:
            resume_bytes = io.BytesIO(f.read())
            resume_text = extract_pdf_text(resume_bytes)
        if not resume_text:
            return jsonify({'error': 'Could not read text from PDF.'}), 400
        ai_response = get_ai_scan(resume_text, jd_text)
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        new_application = {
            "name": candidate_name,
            "email": candidate_email,
            "jobTitle": job_key.replace('_', ' ').title(),
            "score": score,
            "status": status,
            "filename": filename
        }
        applications = load_db()
        applications.append(new_application)
        save_db(applications)
        print(f"🎉 NEW AUTO-SCANNED APPLICATION: {candidate_name}, Score: {score}, Status: {status}")
        return jsonify({'message': f'Application for {candidate_name} received! You will be contacted shortly.'})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (apply) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/get-applications', methods=['GET'])
def get_applications():
    # (This function is unchanged)
    try:
        applications = load_db()
        applications.reverse() 
        return jsonify({'applications': applications})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (get-applications) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/download-application/<filename>', methods=['GET'])
def download_application(filename):
    # (This function is unchanged)
    try:
        return send_from_directory(
            APPLICATION_FOLDER,
            filename,
            as_attachment=True
        )
    except Exception as e:
        # --- THIS IS THE FIX ---
        # The extra "D" is removed from the print statement
        print("\n" + "="*50 + "\n>>> CRASH REPORT (download-application) <<<\n")
        # ------------------------
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_application(filename):
    # (This function is unchanged)
    try:
        applications = load_db()
        app_to_remove = None
        for app in applications:
            if app['filename'] == filename:
                app_to_remove = app
                break
        if app_to_remove:
            applications.remove(app_to_remove)
            save_db(applications) 
        file_path = os.path.join(APPLICATION_FOLDER, filename)
        if os.path.exists(file_path):
            os_remove(file_path)
        print(f"🗑️ DELETED APPLICATION: {filename}")
        return jsonify({'message': 'Application deleted successfully'})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (delete-application) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

# --- 6. NEW: ENDPOINT FOR THE CHATBOT ---
@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json['message']
        
        # This is the new "personality" for the AI
        CHATBOT_PROMPT = f"""
        You are 'VN Infra Bot', a helpful AI assistant for the VN Infra Reyality HR Portal.
        Your main website is https://vr-infra-website.web.app.
        
        The portal has 4 main parts:
        1. Recruiter Portal: For company staff (password 'deva') to scan resumes and see a dashboard of applicants.
        2. Candidate Resume Checker: A tool for candidates to check their resume score against a job.
        3. Apply for a Job: A form to apply for open positions.
        4. About this App: A pop-up explaining these features.
        
        Your job is to answer questions clearly and concisely about these features.
        Be friendly and professional.
        
        USER'S QUESTION: "{user_message}"
        YOUR ANSWER:
        """
        
        model = genai.GenerativeModel('gemini-flash-latest')
        generation_config = GenerationConfig(temperature=0.7) # A bit creative
        response = model.generate_content(CHATBOT_PROMPT, generation_config=generation_config)
        
        return jsonify({'reply': response.text})

    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (chat) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

# Run the app
if __name__ == '__main__':
    app.run(debug=True, port=5000)