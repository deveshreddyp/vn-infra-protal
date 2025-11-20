from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
import ast
import re  # <--- Added for Regex JSON extraction
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

# --- HELPER: Extract JSON from Text (Regex) ---
def extract_json_from_text(text):
    """Finds the first valid JSON object in a string, ignoring headers/footers."""
    text = text.strip()
    # 1. Try finding the first { and last }
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        json_str = match.group(0)
        try:
            return json.loads(json_str)
        except:
            pass
    
    # 2. Fallback: Try standard cleaning
    clean_text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean_text)
    except:
        return None

# --- HELPER: Case Insensitive Dictionary Get ---
def get_case_insensitive(data, key, default=None):
    """Finds a key in a dict ignoring case (e.g. matchingSkills vs MatchingSkills)"""
    if not isinstance(data, dict):
        return default
    
    # Direct match
    if key in data:
        return data[key]
    
    # Case-insensitive match
    key_lower = key.lower()
    for k, v in data.items():
        if k.lower() == key_lower:
            return v
            
    return default

# --- AI FUNCTIONS ---
def get_ai_scan(resume_text, jd_text):
    SYSTEM_PROMPT = """
    You are an HR AI. Compare the resume to the JD.
    Output ONLY valid JSON. Do not add markdown formatting.
    Format:
    {
      "candidateName": "Name",
      "candidateEmail": "Email or N/A",
      "matchScore": 85,
      "matchingSkills": ["Skill A", "Skill B"],
      "missingSkills": ["Skill C", "Skill D"],
      "summary": "Short summary here."
    }
    
    RESUME: {resume_text}
    JD: {jd_text}
    """
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(SYSTEM_PROMPT.format(resume_text=resume_text, jd_text=jd_text))
        
        data = extract_json_from_text(response.text)
        if not data:
            raise ValueError("AI did not return valid JSON")

        return data
    except Exception as e:
        print(f"AI Error: {e}")
        return {
            "matchScore": 0,
            "matchingSkills": [],
            "missingSkills": [],
            "summary": "Error analyzing resume. Please try again."
        }

def get_interview_questions(missing_skills):
    if not missing_skills:
        return []
    
    skills_str = ", ".join(missing_skills[:5]) # Limit to top 5
    PROMPT = f"Generate 3 interview questions for these missing skills: {skills_str}. Return valid JSON array of strings: [\"Question 1\", \"Question 2\"]"
    
    try:
        model = genai.GenerativeModel('gemini-flash-latest')
        response = model.generate_content(PROMPT)
        
        # Try parsing as list
        text = response.text.strip()
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        
        # Fallback clean
        clean = text.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
    except:
        return ["Could not generate questions."]

def extract_pdf_text(file_stream):
    try:
        reader = PyPDF2.PdfReader(file_stream)
        return "".join([p.extract_text() for p in reader.pages])
    except:
        return ""

# --- ROUTES ---

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
        
        # 1. Check Duplicate
        filename = f"{name.replace(' ', '_')}-{job_id}-{resume_file.filename}"
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE filename=%s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'Already applied'}), 400
            
        # 2. Get Job
        cur.execute("SELECT description FROM jobs WHERE id=%s", (job_id,))
        job = cur.fetchone()
        if not job: return jsonify({'error': 'Job not found'}), 400
        
        # 3. Process
        resume_bytes = resume_file.read()
        text = extract_pdf_text(io.BytesIO(resume_bytes))
        ai_data = get_ai_scan(text, job['description'])
        
        # 4. Extract safely with case-insensitivity
        score = get_case_insensitive(ai_data, 'matchScore', 0)
        summary = get_case_insensitive(ai_data, 'summary', '')
        matching = get_case_insensitive(ai_data, 'matchingSkills', [])
        missing = get_case_insensitive(ai_data, 'missingSkills', [])
        
        questions = get_interview_questions(matching) # Generate questions based on MATCHING or MISSING? Usually missing.
        # Let's fix logic: Questions usually for MISSING skills to test knowledge, or MATCHING to verify depth.
        # I will use MISSING as per previous logic.
        questions = get_interview_questions(missing)

        status = "Shortlisted" if int(score) >= 60 else "Pending"
        
        # 5. Insert
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
        
        return jsonify({'message': 'Applied successfully'})
        
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
        
        # Clean up lists for Frontend
        for app in apps:
            for field in ['matchingSkills', 'missingSkills', 'interviewQuestions']:
                val = app.get(field)
                
                if isinstance(val, list): continue # Already good
                
                if not val: 
                    app[field] = []
                    continue
                    
                if isinstance(val, str):
                    # Try parsing JSON
                    try:
                        app[field] = json.loads(val)
                    except:
                        # Try parsing Python List string
                        try:
                            app[field] = ast.literal_eval(val)
                        except:
                            # Fallback: Comma split
                            app[field] = [x.strip() for x in val.split(',') if x.strip()]
                            
        return jsonify({'applications': apps})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- OTHER ROUTES (Standard) ---
@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_app(filename):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM applications WHERE filename=%s", (filename,))
    conn.commit()
    return jsonify({'message': 'Deleted'})

@app.route('/update-status', methods=['POST'])
def update_status():
    d = request.json
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status=%s WHERE id=%s", (d['status'], d['id']))
    conn.commit()
    return jsonify({'message': 'Updated'})

@app.route('/update-notes', methods=['POST'])
def update_notes():
    d = request.json
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET notes=%s WHERE id=%s", (d['notes'], d['id']))
    conn.commit()
    return jsonify({'message': 'Updated'})

@app.route('/get-jobs', methods=['GET'])
def get_jobs():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs")
    jobs = cur.fetchall()
    return jsonify({'jobs': jobs})

@app.route('/add-job', methods=['POST'])
def add_job():
    d = request.json
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO jobs (title, description) VALUES (%s, %s)", (d['title'], d['description']))
    conn.commit()
    return jsonify({'message': 'Added'})

@app.route('/get-analytics', methods=['GET'])
def get_analytics():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total, AVG(score) as avg FROM applications")
    res = cur.fetchone()
    return jsonify({'total_apps': res['total'], 'avg_score': int(res['avg'] or 0), 'total_shortlisted': 0})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))