from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
import ast
import re  # For robust JSON extraction
from google.generativeai.types import GenerationConfig
import psycopg2
from psycopg2.extras import RealDictCursor

# --- CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

try:
    if not API_KEY:
        print("CRITICAL ERROR: GOOGLE_API_KEY not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API: {e}")

app = Flask(__name__)
CORS(app)

# --- DATABASE ---
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        
        # Jobs Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL
            );
        ''')
        
        # Applications Table
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
        print("Database Initialized.")
    except Exception as e:
        print(f"DB Init Error: {e}")

init_db()

# --- HELPER FUNCTIONS ---

def extract_json_from_text(text):
    """Extracts valid JSON from a string, ignoring Markdown or extra text."""
    text = text.strip()
    # 1. Try Regex to find { ... } or [ ... ]
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    
    # 2. Fallback: clean code blocks
    clean_text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean_text)
    except:
        return None

def get_case_insensitive(data, key, default=None):
    """Helper to get value from dict ignoring key case."""
    if not isinstance(data, dict): return default
    if key in data: return data[key]
    for k, v in data.items():
        if k.lower() == key.lower(): return v
    return default

def extract_pdf_text(file_stream):
    try:
        reader = PyPDF2.PdfReader(file_stream)
        return "".join([p.extract_text() for p in reader.pages])
    except:
        return ""

# --- AI FUNCTIONS ---

def get_ai_scan(resume_text, jd_text):
    SYSTEM_PROMPT = """
    You are an HR AI. Compare the resume to the JD.
    Output ONLY valid JSON. No markdown.
    Format:
    {
      "candidateName": "Name",
      "candidateEmail": "Email",
      "matchScore": 85,
      "matchingSkills": ["Skill A", "Skill B"],
      "missingSkills": ["Skill C"],
      "summary": "Brief summary."
    }
    RESUME: {resume_text}
    JD: {jd_text}
    """
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(SYSTEM_PROMPT.format(resume_text=resume_text, jd_text=jd_text))
        data = extract_json_from_text(response.text)
        if not data: raise ValueError("Invalid JSON from AI")
        return data
    except Exception as e:
        print(f"AI Scan Error: {e}")
        return {}

def get_interview_questions(missing_skills):
    if not missing_skills: return []
    skills_str = ", ".join(missing_skills[:5])
    PROMPT = f"Generate 3 interview questions for: {skills_str}. Return JSON array: [\"Q1\", \"Q2\"]"
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(PROMPT)
        data = extract_json_from_text(response.text)
        if isinstance(data, list): return data
        if isinstance(data, dict): return list(data.values())[0] # extraction fallback
        return []
    except:
        return []

# --- ROUTES ---

@app.route('/login', methods=['POST'])
def login():
    try:
        # Handle both JSON requests and Form data
        data = request.json if request.is_json else request.form
        
        # Get password safely
        raw_password = data.get('password', '')
        
        # Clean it: trim spaces, convert to lowercase
        password_attempt = str(raw_password).strip().lower()
        
        print(f"LOGIN ATTEMPT: Received '{raw_password}' -> Checked '{password_attempt}'")

        if password_attempt == 'deva':
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Incorrect Password'}), 401
            
    except Exception as e:
        print(f"Login Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/scan-resume', methods=['POST'])
def scan_resume():
    try:
        resume_file = request.files['resume']
        jd_text = request.form['jobDescription']
        text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not text: return jsonify({'error': 'Empty PDF'}), 400
        result = get_ai_scan(text, jd_text)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/apply', methods=['POST'])
def apply():
    try:
        resume_file = request.files['resume']
        name = request.form['name']
        email = request.form['email']
        job_id = request.form['jobId']
        
        filename = f"{name.replace(' ', '_')}-{job_id}-{resume_file.filename}"
        
        conn = get_db_conn()
        cur = conn.cursor()
        
        # Check duplicate
        cur.execute("SELECT id FROM applications WHERE filename=%s", (filename,))
        if cur.fetchone(): return jsonify({'error': 'Already applied'}), 400
        
        # Get JD
        cur.execute("SELECT description FROM jobs WHERE id=%s", (job_id,))
        job = cur.fetchone()
        if not job: return jsonify({'error': 'Job not found'}), 400
        
        # Process
        resume_bytes = resume_file.read()
        text = extract_pdf_text(io.BytesIO(resume_bytes))
        ai_data = get_ai_scan(text, job['description'])
        
        score = int(get_case_insensitive(ai_data, 'matchScore', 0))
        summary = get_case_insensitive(ai_data, 'summary', '')
        matching = get_case_insensitive(ai_data, 'matchingSkills', [])
        missing = get_case_insensitive(ai_data, 'missingSkills', [])
        
        questions = get_interview_questions(missing)
        status = "Shortlisted" if score >= 60 else "Pending"
        
        cur.execute('''
            INSERT INTO applications 
            (name, email, job_id, score, status, filename, summary, matchingSkills, missingSkills, interviewQuestions, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            name, email, job_id, score, status, filename, summary,
            json.dumps(matching), json.dumps(missing), json.dumps(questions), ""
        ))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Applied'})
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
        
        # CLEAN UP DATA FOR FRONTEND
        for app in apps:
            for field in ['matchingSkills', 'missingSkills', 'interviewQuestions']:
                val = app.get(field)
                
                # If it's already a list, perfect
                if isinstance(val, list): continue
                
                # If None/Empty, empty list
                if not val: 
                    app[field] = []
                    continue
                
                # If string, parse it
                if isinstance(val, str):
                    try:
                        # Try JSON
                        app[field] = json.loads(val)
                    except:
                        try:
                            # Try Python List string
                            app[field] = ast.literal_eval(val)
                        except:
                            # Fallback: Split by newlines or commas
                            if '\n' in val:
                                app[field] = [x.strip("-• ").strip() for x in val.splitlines() if x.strip()]
                            else:
                                app[field] = [x.strip() for x in val.split(',') if x.strip()]
                            
        return jsonify({'applications': apps})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_app(filename):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM applications WHERE filename=%s", (filename,))
        conn.commit()
        return jsonify({'message': 'Deleted'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/update-status', methods=['POST'])
def update_status():
    try:
        d = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status=%s WHERE id=%s", (d['status'], d['id']))
        conn.commit()
        return jsonify({'message': 'Updated'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/update-notes', methods=['POST'])
def update_notes():
    try:
        d = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET notes=%s WHERE id=%s", (d['notes'], d['id']))
        conn.commit()
        return jsonify({'message': 'Updated'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/get-jobs', methods=['GET'])
def get_jobs():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM jobs")
        jobs = cur.fetchall()
        return jsonify({'jobs': jobs})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/add-job', methods=['POST'])
def add_job():
    try:
        d = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO jobs (title, description) VALUES (%s, %s)", (d['title'], d['description']))
        conn.commit()
        return jsonify({'message': 'Added'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/get-analytics', methods=['GET'])
def get_analytics():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as total, AVG(score) as avg FROM applications")
        res = cur.fetchone()
        
        cur.execute("SELECT COUNT(*) as shortlisted FROM applications WHERE status='Shortlisted'")
        short_res = cur.fetchone()
        
        return jsonify({
            'total_apps': res['total'], 
            'avg_score': int(res['avg'] or 0), 
            'total_shortlisted': short_res['shortlisted']
        })
    except Exception as e: return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("--- Running in LOCAL Mode ---")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))