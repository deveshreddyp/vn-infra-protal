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
import sqlite3

# --- 1. CONFIGURE YOUR API KEY ---
# This reads the key from Render's "Environment Variables"
API_KEY = os.environ.get('GOOGLE_API_KEY') 
try:
    if not API_KEY:
        print("CRITICAL ERROR: GOOGLE_API_KEY environment variable not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")

# -----------------------------------

app = Flask(__name__)
CORS(app)

# --- FOLDERS & DATABASE FILE (FOR RENDER DEPLOYMENT) ---
# Render's free disk is mounted at '/var/data'
DATA_DIR = '/var/data'
DB_NAME = os.path.join(DATA_DIR, 'vn_infra.db') 
APPLICATION_FOLDER = os.path.join(DATA_DIR, 'applications') 
os.makedirs(APPLICATION_FOLDER, exist_ok=True) # Make sure the folder exists

# --- 2. AI SCANNER FUNCTIONS ---
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

def get_interview_questions(missing_skills_list):
    if not missing_skills_list:
        return json.dumps([])
    skills_text = ", ".join(missing_skills_list)
    QUESTION_PROMPT = f"""
    A job candidate is missing the following skills: {skills_text}.
    Generate a JSON array of 3 concise, technical interview questions...
    Example: ["Question 1", "Question 2", "Question 3"]
    """
    model = genai.GenerativeModel('gemini-flash-latest')
    generation_config = GenerationConfig(temperature=0.5)
    response = model.generate_content(QUESTION_PROMPT, generation_config=generation_config)
    return response.text.strip()

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

# --- 3. DATABASE HELPER FUNCTIONS ---
def get_db_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    print(f"Initializing database at: {DB_NAME}")
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL
            );
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                job_id INTEGER NOT NULL,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                summary TEXT,
                matchingSkills TEXT,
                missingSkills TEXT,
                interviewQuestions TEXT,
                notes TEXT,
                FOREIGN KEY (job_id) REFERENCES jobs (id)
            );
        ''')
        try:
            conn.execute('ALTER TABLE applications ADD COLUMN summary TEXT')
            conn.execute('ALTER TABLE applications ADD COLUMN matchingSkills TEXT')
            conn.execute('ALTER TABLE applications ADD COLUMN missingSkills TEXT')
        except sqlite3.OperationalError: pass
        try:
            conn.execute('ALTER TABLE applications ADD COLUMN interviewQuestions TEXT')
        except sqlite3.OperationalError: pass
        try:
            conn.execute('ALTER TABLE applications ADD COLUMN notes TEXT')
        except sqlite3.OperationalError: pass
        try:
            conn.execute('ALTER TABLE applications RENAME COLUMN jobTitle TO job_id_temp_old')
            conn.execute('ALTER TABLE applications ADD COLUMN job_id INTEGER')
        except sqlite3.OperationalError:
            pass 
        conn.commit()
        print("Database initialized.")

# --- 4. API ENDPOINTS ---

@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json
        password_attempt = data.get('password')
        if password_attempt == 'deva':
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Incorrect Password'}), 401
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (login) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/scan-resume', methods=['POST'])
def scan_resume():
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
    try:
        resume_file = request.files['resume']
        candidate_name = request.form['name']
        candidate_email = request.form['email']
        job_id = request.form['jobId']
        filename = f"{candidate_name.replace(' ', '_')}-{job_id}-{resume_file.filename}"
        file_path = os.path.join(APPLICATION_FOLDER, filename)
        
        with get_db_conn() as conn:
            cursor = conn.execute("SELECT id FROM applications WHERE filename = ?", (filename,))
            if cursor.fetchone():
                return jsonify({'error': 'You have already applied for this job with this resume.'}), 400
        with get_db_conn() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return jsonify({'error': 'Invalid job selected.'}), 400
        jd_text = job['description']
        resume_file.save(file_path)
        with open(file_path, 'rb') as f:
            resume_bytes = io.BytesIO(f.read())
            resume_text = extract_pdf_text(resume_bytes)
        if not resume_text:
            return jsonify({'error': 'Could not read text from PDF.'}), 400
        ai_response = get_ai_scan(resume_text, jd_text)
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        summary = ai_response.get('summary', 'N/A')
        matching_skills = json.dumps(ai_response.get('matchingSkills', []))
        missing_skills = json.dumps(ai_response.get('missingSkills', []))
        interview_questions_json = get_interview_questions(ai_response.get('missingSkills', []))
        with get_db_conn() as conn:
            conn.execute('''
                INSERT INTO applications (name, email, job_id, score, status, filename, 
                                          summary, matchingSkills, missingSkills, interviewQuestions, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (candidate_name, candidate_email, job_id, 
                  score, status, filename, summary, matching_skills, 
                  missing_skills, interview_questions_json, ""))
            conn.commit()
        print(f"🎉 NEW AUTO-SCANNED APPLICATION: {candidate_name}, Score: {score}, Status: {status}")
        return jsonify({'message': f'Application for {candidate_name} received! You will be contacted shortly.'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'You have also applied for this job with this resume.'}), 400
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (apply) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/get-applications', methods=['GET'])
def get_applications():
    try:
        with get_db_conn() as conn:
            cursor = conn.execute('''
                SELECT 
                    a.id, a.name, a.email, a.score, a.status, a.filename,
                    a.summary, a.matchingSkills, a.missingSkills,
                    a.interviewQuestions, a.notes,
                    j.title AS jobTitle 
                FROM applications a
                LEFT JOIN jobs j ON a.job_id = j.id
                ORDER BY a.id DESC
            ''')
            applications = [dict(row) for row in cursor.fetchall()]
        return jsonify({'applications': applications})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (get-applications) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/download-application/<filename>', methods=['GET'])
def download_application(filename):
    try:
        return send_from_directory(APPLICATION_FOLDER, filename, as_attachment=True)
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (download-application) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_application(filename):
    try:
        with get_db_conn() as conn:
            conn.execute('DELETE FROM applications WHERE filename = ?', (filename,))
            conn.commit()
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

@app.route('/update-status', methods=['POST'])
def update_status():
    try:
        data = request.json
        app_id = data.get('id')
        new_status = data.get('status')
        if not app_id or not new_status:
            return jsonify({'error': 'Missing ID or status'}), 400
        with get_db_conn() as conn:
            conn.execute('UPDATE applications SET status = ? WHERE id = ?', (new_status, app_id))
            conn.commit()
        print(f"✅ STATUS UPDATED: ID {app_id} set to {new_status}")
        return jsonify({'message': 'Status updated successfully'})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (update-status) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/update-notes', methods=['POST'])
def update_notes():
    try:
        data = request.json
        app_id = data.get('id')
        new_notes = data.get('notes')
        if app_id is None or new_notes is None:
            return jsonify({'error': 'Missing ID or notes'}), 400
        with get_db_conn() as conn:
            conn.execute('UPDATE applications SET notes = ? WHERE id = ?', (new_notes, app_id))
            conn.commit()
        print(f"✅ NOTES UPDATED: ID {app_id}")
        return jsonify({'message': 'Notes updated successfully'})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (update-notes) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json['message']
        CHATBOT_PROMPT = f"""
        You are 'VN Infra Bot', a helpful AI assistant...
        USER'S QUESTION: "{user_message}"
        YOUR ANSWER:
        """
        model = genai.GenerativeModel('gemini-flash-latest')
        generation_config = GenerationConfig(temperature=0.7) 
        response = model.generate_content(CHATBOT_PROMPT, generation_config=generation_config)
        return jsonify({'reply': response.text})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (chat) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/get-jobs', methods=['GET'])
def get_jobs():
    try:
        with get_db_conn() as conn:
            cursor = conn.execute('SELECT id, title, description FROM jobs ORDER BY title')
            jobs = [dict(row) for row in cursor.fetchall()]
        return jsonify({'jobs': jobs})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (get-jobs) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

@app.route('/add-job', methods=['POST'])
def add_job():
    try:
        data = request.json
        title = data.get('title')
        description = data.get('description')
        if not title or not description:
            return jsonify({'error': 'Title and description are required.'}), 400
        with get_db_conn() as conn:
            conn.execute('INSERT INTO jobs (title, description) VALUES (?, ?)', (title, description))
            conn.commit()
        print(f"✅ NEW JOB ADDED: {title}")
        return jsonify({'message': 'Job added successfully'})
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (add-job) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500
        
@app.route('/get-analytics', methods=['GET'])
def get_analytics():
    try:
        with get_db_conn() as conn:
            total_apps = conn.execute('SELECT COUNT(*) FROM applications').fetchone()[0]
            total_shortlisted = conn.execute("SELECT COUNT(*) FROM applications WHERE status = 'Shortlisted'").fetchone()[0]
            avg_score_result = conn.execute('SELECT AVG(score) FROM applications').fetchone()[0]
            avg_score = round(avg_score_result) if avg_score_result is not None else 0
            analytics_data = {
                "total_apps": total_apps,
                "total_shortlisted": total_shortlisted,
                "avg_score": avg_score
            }
        return jsonify(analytics_data)
    except Exception as e:
        print("\n" + "="*50 + "\n>>> CRASH REPORT (get-analytics) <<<\n")
        traceback.print_exc() 
        print("="*50 + "\n")
        return jsonify({'error': str(e)}), 500

# Run the app
if __name__ == '__main__':
    init_db() 
    # Use Gunicorn's port, or 5000 as a fallback
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)