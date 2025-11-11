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
import sqlite3 # <-- SWITCHED BACK TO SQLITE

# --- 1. CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
# DATABASE_URL is now ignored for the connection
genai.configure(api_key=API_KEY)

app = Flask(__name__)
CORS(app)

# --- FOLDERS & DATABASE FILE (Ephemeral Local Storage) ---
# DB will be created as a file in the app directory, which is temporary.
DB_NAME = 'vn_infra.db' 
APPLICATION_FOLDER = 'applications' 
os.makedirs(APPLICATION_FOLDER, exist_ok=True) # This still needs to run

# --- 2. DATABASE HELPER FUNCTIONS (SQLite) ---
def get_db_conn():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row 
    return conn

def init_db():
    print(f"Initializing SQLite database at: {DB_NAME}")
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
        # ... (Migration logic is removed for brevity but is necessary) ...
        conn.commit()
        print("Database initialized.")

# --- 3. AI & PDF HELPERS (Unchanged) ---
# (get_ai_scan, get_interview_questions, extract_pdf_text functions are unchanged)

# --- 4. API ENDPOINTS (Using SQLite placeholders) ---

@app.route('/apply', methods=['POST'])
def handle_application():
    try:
        resume_file = request.files['resume']
        name = request.form['name']
        email = request.form['email']
        job_id = request.form['jobId']
        filename = f"{name.replace(' ', '_')}-{job_id}-{resume_file.filename}"
        
        # --- Check for duplicate & Get Job ---
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE filename = ?", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'You have already applied for this job with this resume.'}), 400
        cur.execute("SELECT description FROM jobs WHERE id = ?", (job_id,))
        job = cur.fetchone()
        if not job: return jsonify({'error': 'Invalid job selected.'}), 400
        jd_text = job['description']
        
        # --- Save file (will be deleted on restart) ---
        file_path = os.path.join(APPLICATION_FOLDER, filename)
        resume_file.save(file_path)

        # --- Run AI Scan ---
        resume_text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not resume_text: return jsonify({'error': 'Could not read PDF.'}), 400
        ai_response = get_ai_scan(resume_text, job['description'])
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        questions = get_interview_questions(ai_response.get('missingSkills', []))
        
        # --- Save to SQLite (using ? placeholders) ---
        cur.execute('''
            INSERT INTO applications (name, email, job_id, score, status, filename, 
                                      summary, matchingSkills, missingSkills, interviewQuestions, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, email, job_id, score, status, filename, 
              ai_response.get('summary'), json.dumps(ai_response.get('matchingSkills')),
              json.dumps(ai_response.get('missingSkills')), questions, ""))
        
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': f'Application received for {name}!'})
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# (All other endpoints follow the SQLite pattern and are unchanged in logic)

# --- Final Run Block ---
if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        print(f"DB Init failed: {e}")
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)