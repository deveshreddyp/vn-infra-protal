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
import psycopg2 
from psycopg2.extras import RealDictCursor

# --- 1. CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL') 
try:
    if not API_KEY:
        print("CRITICAL ERROR: GOOGLE_API_KEY environment variable not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")

# -----------------------------------

app = Flask(__name__)
CORS(app)

# --- FOLDERS (For storing resumes temporarily on ephemeral disk) ---
APPLICATION_FOLDER = os.path.join('/var/data', 'applications') 
os.makedirs(APPLICATION_FOLDER, exist_ok=True)

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
    if not missing_skills_list: return json.dumps([])
    skills_text = ", ".join(missing_skills_list)
    PROMPT = f"Generate a JSON array of 3 concise, technical interview questions for missing skills: {skills_text}. Return ONLY a valid JSON array of strings."
    model = genai.GenerativeModel('gemini-flash-latest')
    response = model.generate_content(PROMPT)
    return response.text.strip()

def extract_pdf_text(pdf_file_stream):
    try:
        pdf_reader = PyPDF2.PdfReader(pdf_file_stream)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        return None

# --- 3. DATABASE HELPER FUNCTIONS (PostgreSQL) ---
def get_db_conn():
    """Connects to the Render PostgreSQL database."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    print("Initializing PostgreSQL database...")
    conn = get_db_conn()
    cur = conn.cursor()
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL
        );
    ''')
    
    cur.execute('''
        CREATE TABLE IF NOT EXISTS applications (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            job_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            status TEXT NOT NULL,
            filename TEXT NOT NULL,
            summary TEXT,
            matchingSkills TEXT,
            missingSkills TEXT,
            interviewQuestions TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (job_id) REFERENCES jobs (id)
        );
    ''')
    conn.commit()
    cur.close()
    conn.close()
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
        return jsonify({'error': str(e)}), 500

@app.route('/scan-resume', methods=['POST'])
def scan_resume():
    try:
        resume_file = request.files['resume']
        jd_text = request.form['jobDescription']
        resume_text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not resume_text:
            return jsonify({'error': 'Could not read text from PDF.'}), 400
        ai_response = get_ai_scan(resume_text, jd_text)
        return jsonify(ai_response)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/apply', methods=['POST'])
def handle_application():
    try:
        resume_file = request.files['resume']
        name = request.form['name']
        email = request.form['email']
        job_id = request.form['jobId']
        filename = f"{name.replace(' ', '_')}-{job_id}-{resume_file.filename}"
        
        conn = get_db_conn()
        cur = conn.cursor()
        
        # Check for duplicate
        cur.execute("SELECT id FROM applications WHERE filename = %s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'You have already applied for this job with this resume.'}), 400
            
        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job: return jsonify({'error': 'Invalid job selected.'}), 400
        
        jd_text = job['description']
        
        # Read text from memory for scanning
        resume_text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not resume_text: return jsonify({'error': 'Could not read PDF.'}), 400
        
        ai_response = get_ai_scan(resume_text, jd_text)
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        questions = get_interview_questions(ai_response.get('missingSkills', []))
        
        # Save all data to PostgreSQL (using %s placeholders)
        cur.execute('''
            INSERT INTO applications (name, email, job_id, score, status, filename, 
                                      summary, matchingSkills, missingSkills, interviewQuestions, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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

@app.route('/get-applications', methods=['GET'])
def get_applications():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''
            SELECT a.*, j.title as "jobTitle" 
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.id
            ORDER BY a.id DESC
        ''')
        apps = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'applications': apps})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download-application/<filename>', methods=['GET'])
def download_application(filename):
    # This endpoint is disabled for free-tier deployment since files are not persistent.
    return jsonify({'error': 'Download is disabled in free-tier deployment.'}), 403

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_application(filename):
    # Only deletes the DB record now
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM applications WHERE filename = %s", (filename,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Deleted'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/update-status', methods=['POST'])
def update_status():
    try:
        data = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status = %s WHERE id = %s", (data['status'], data['id']))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Updated'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/update-notes', methods=['POST'])
def update_notes():
    try:
        data = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET notes = %s WHERE id = %s", (data['notes'], data['id']))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Updated'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json['message']
        CHATBOT_PROMPT = f"You are 'VN Infra Bot'... USER: {user_message}"
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(CHATBOT_PROMPT)
        return jsonify({'reply': response.text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/get-jobs', methods=['GET'])
def get_jobs():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, title, description FROM jobs ORDER BY title")
        jobs = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'jobs': jobs})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/add-job', methods=['POST'])
def add_job():
    try:
        data = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO jobs (title, description) VALUES (%s, %s)", (data['title'], data['description']))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Job added'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
        
@app.route('/get-analytics', methods=['GET'])
def get_analytics():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        total_apps = cur.execute('SELECT COUNT(*) FROM applications').fetchone()['count']
        total_shortlisted = cur.execute("SELECT COUNT(*) FROM applications WHERE status = 'Shortlisted'").fetchone()['count']
        avg_score_result = cur.execute('SELECT AVG(score) FROM applications').fetchone()['avg']
        avg_score = round(avg_score_result) if avg_score_result is not None else 0
        cur.close()
        conn.close()
        analytics_data = {
            "total_apps": total_apps,
            "total_shortlisted": total_shortlisted,
            "avg_score": avg_score
        }
        return jsonify(analytics_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Initialize DB (This will run once during local testing/setup)
    try:
        init_db()
    except Exception as e:
        print(f"DB Init failed (requires local Postgres): {e}")

    # Render Production Run Command
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)