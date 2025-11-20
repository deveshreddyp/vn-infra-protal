from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
import ast
import re
from google.generativeai.types import GenerationConfig
import psycopg2
from psycopg2.extras import RealDictCursor

# --- CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

try:
    if not API_KEY:
        print("CRITICAL ERROR: GOOGLE_API_KEY environment variable not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")

app = Flask(__name__)
CORS(app)

# --- DATABASE HELPERS ---
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        print("Initializing Database...")
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
        print("Database Ready.")
    except Exception as e:
        print(f"DB Init Failed: {e}")

init_db()

# --- AI & TEXT HELPERS ---

def extract_json_from_text(text):
    """Extracts JSON object from text using Regex to handle markdown or chatter."""
    text = text.strip()
    # Regex to find content between the first { and the last }
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except:
            pass
    
    # Fallback: Remove markdown code blocks
    clean_text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean_text)
    except:
        return None

def extract_pdf_text(file_stream):
    try:
        reader = PyPDF2.PdfReader(file_stream)
        text = "".join([page.extract_text() for page in reader.pages])
        return text
    except Exception as e:
        print(f"PDF Error: {e}")
        return None

def get_ai_scan(resume_text, jd_text):
    # UPDATED MODEL NAME TO STABLE VERSION
    model_name = 'gemini-1.5-flash' 
    
    SYSTEM_PROMPT = """
    You are an expert Tech Recruiter. Compare the Resume to the Job Description.
    Return ONLY a valid JSON object. Do not write any intro text.
    Format:
    {
      "candidateName": "Full Name",
      "candidateEmail": "Email or 'N/A'",
      "matchScore": 85,
      "matchingSkills": ["Skill1", "Skill2"],
      "missingSkills": ["Skill3", "Skill4"],
      "summary": "2 sentence analysis of the candidate."
    }
    RESUME: {resume_text}
    JOB DESCRIPTION: {jd_text}
    """
    try:
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(SYSTEM_PROMPT.format(resume_text=resume_text, jd_text=jd_text))
        
        data = extract_json_from_text(response.text)
        
        if not data:
            print("AI Error: Returned invalid JSON.")
            return {}
            
        return data
    except Exception as e:
        print(f"AI Scan Exception: {e}")
        return {}

def get_interview_questions(missing_skills):
    if not missing_skills: return []
    
    skills_str = ", ".join(missing_skills[:5])
    PROMPT = f"Generate 3 interview questions for: {skills_str}. Return JSON array of strings: [\"Q1\", \"Q2\"]"
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(PROMPT)
        data = extract_json_from_text(response.text)
        
        if isinstance(data, list): return data
        if isinstance(data, dict): return list(data.values())[0]
        return []
    except:
        return []

# --- ROUTES ---

@app.route('/login', methods=['POST'])
def login():
    try:
        # Handle both JSON and Form data
        data = request.json if request.is_json else request.form
        
        # Safe Get & Clean
        raw_password = data.get('password', '')
        password_attempt = str(raw_password).strip().lower()
        
        print(f"LOGIN DEBUG: received='{raw_password}' cleaned='{password_attempt}'")

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
        text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not text: return jsonify({'error': 'PDF is empty/unreadable'}), 400
        
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

        # Check Duplicate
        cur.execute("SELECT id FROM applications WHERE filename = %s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'Application already exists.'}), 400

        # Get JD
        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job: return jsonify({'error': 'Job not found'}), 400

        # Process
        resume_bytes = resume_file.read()
        text = extract_pdf_text(io.BytesIO(resume_bytes))
        ai_data = get_ai_scan(text, job['description'])

        # Safely get fields (Case insensitive lookup helper)
        def get_field(d, key, default):
            for k in d.keys():
                if k.lower() == key.lower(): return d[k]
            return default

        score = int(get_field(ai_data, 'matchScore', 0))
        summary = get_field(ai_data, 'summary', '')
        matching = get_field(ai_data, 'matchingSkills', [])
        missing = get_field(ai_data, 'missingSkills', [])
        
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
        return jsonify({'message': 'Application Submitted!'})
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

        # Parse JSON strings back to lists
        for app in apps:
            for field in ['matchingSkills', 'missingSkills', 'interviewQuestions']:
                val = app.get(field)
                if isinstance(val, list): continue
                if not val: 
                    app[field] = []
                    continue
                
                # Try JSON, then Eval, then Split
                if isinstance(val, str):
                    try:
                        app[field] = json.loads(val)
                    except:
                        try:
                            app[field] = ast.literal_eval(val)
                        except:
                            app[field] = [x.strip() for x in val.split(',') if x.strip()]

        return jsonify({'applications': apps})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_app(filename):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM applications WHERE filename = %s", (filename,))
        conn.commit()
        return jsonify({'message': 'Deleted'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/update-status', methods=['POST'])
def update_status():
    try:
        d = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET status = %s WHERE id = %s", (d['status'], d['id']))
        conn.commit()
        return jsonify({'message': 'Updated'})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/update-notes', methods=['POST'])
def update_notes():
    try:
        d = request.json
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("UPDATE applications SET notes = %s WHERE id = %s", (d['notes'], d['id']))
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
        short = cur.fetchone()
        
        return jsonify({
            "total_apps": res['total'],
            "avg_score": int(res['avg'] or 0),
            "total_shortlisted": short['shortlisted']
        })
    except Exception as e: return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    print("--- Local Mode ---")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))