from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import google.generativeai as genai
import PyPDF2
import io
import json
from google.generativeai.types import GenerationConfig
import psycopg2
from psycopg2.extras import RealDictCursor

# --- 1. CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL') # Render provides this

try:
    if not API_KEY:
        print("CRITICAL: GOOGLE_API_KEY not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")

app = Flask(__name__)
CORS(app)

# --- 2. DATABASE HELPER ---
def get_db_conn():
    """Connects to the Render PostgreSQL database."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """Creates tables in PostgreSQL."""
    print("Initializing PostgreSQL database...")
    conn = get_db_conn()
    cur = conn.cursor()
    
    # Create Jobs Table
    cur.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL
        );
    ''')
    
    # Create Applications Table
    # Note: We store the filename string for reference, but not the file itself
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

# --- 3. AI & PDF HELPERS ---
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
    clean_text = response.text.strip().replace("```json", "").replace("```", "")
    return json.loads(clean_text)

def get_interview_questions(missing_skills_list):
    if not missing_skills_list: return json.dumps([])
    skills_text = ", ".join(missing_skills_list)
    prompt = f"Generate 3 technical interview questions for missing skills: {skills_text}. Return JSON array of strings."
    model = genai.GenerativeModel('gemini-flash-latest')
    response = model.generate_content(prompt)
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

# --- 4. API ENDPOINTS ---

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    if data.get('password') == 'deva':
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Incorrect Password'}), 401

@app.route('/scan-resume', methods=['POST'])
def scan_resume():
    try:
        resume_file = request.files['resume']
        jd_text = request.form['jobDescription']
        resume_text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not resume_text: return jsonify({'error': 'Could not read PDF'}), 400
        
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
        
        # 1. Read PDF in memory (do not save to disk)
        file_stream = io.BytesIO(resume_file.read())
        resume_text = extract_pdf_text(file_stream)
        if not resume_text: return jsonify({'error': 'Could not read PDF'}), 400

        # 2. Get Job Description
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        
        if not job:
            return jsonify({'error': 'Invalid job selected'}), 400
        
        # 3. Run AI Scan
        ai_response = get_ai_scan(resume_text, job['description'])
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        
        # 4. Generate Questions
        questions = get_interview_questions(ai_response.get('missingSkills', []))
        
        # 5. Save to PostgreSQL
        # Note: We use %s for placeholders in Postgres
        cur.execute('''
            INSERT INTO applications (name, email, job_id, score, status, filename, 
                                      summary, matchingSkills, missingSkills, interviewQuestions, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (name, email, job_id, score, status, resume_file.filename,
              ai_response.get('summary'), 
              json.dumps(ai_response.get('matchingSkills')),
              json.dumps(ai_response.get('missingSkills')),
              questions, ""))
        
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

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_application(filename):
    # Note: We only delete the DB record now, as there is no file on disk
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

if __name__ == '__main__':
    # We only init DB locally if we set the var, 
    # but on Render we usually do it differently. 
    # For this setup, we can try running it on startup.
    try:
        init_db()
    except Exception as e:
        print(f"DB Init failed (might already exist): {e}")
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)