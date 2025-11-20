from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
import ast  # <--- ADDED: Essential for parsing Python-style lists from DB
from google.generativeai.types import GenerationConfig
import psycopg2
from psycopg2.extras import RealDictCursor
import sys

# --- 1. CRITICAL CONFIGURATION ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
try:
    if not API_KEY:
        print("CRITICAL ERROR: GOOGLE_API_KEY environment variable not set.")
    genai.configure(api_key=API_KEY)
except Exception as e:
    print(f"Error configuring API key: {e}")

# --- FLASK APP INITIALIZATION ---
app = Flask(__name__)
CORS(app)

# --- 2. DATABASE HELPER FUNCTIONS (PostgreSQL) ---
def get_db_conn():
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

# --- 3. RUN DB INITIALIZATION ON IMPORT ---
try:
    init_db()
except Exception as e:
    print(f"CRITICAL DB INIT FAILED: {e}")

# --- 4. AI & PDF HELPERS ---
def get_ai_scan(resume_text, jd_text):
    SYSTEM_PROMPT = """
    You are an expert HR recruiter. Compare the resume to the job description.
    Output a strictly valid JSON object with these keys:
    {{
      "candidateName": "The candidate's full name",
      "candidateEmail": "The candidate's email, or 'N/A'",
      "matchScore": <A percentage score from 0 to 100 as an integer>,
      "matchingSkills": ["List of skills present in both..."],
      "missingSkills": ["List of skills in JD but missing in Resume..."],
      "summary": "A 2-3 sentence summary of the fit."
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
    
    # Clean potential markdown
    clean_response_text = response.text.strip().replace("```json", "").replace("```", "")
    try:
        return json.loads(clean_response_text)
    except:
        # Fallback if JSON is broken
        return {
            "candidateName": "Unknown", "candidateEmail": "N/A", "matchScore": 0,
            "matchingSkills": [], "missingSkills": [], "summary": "Error parsing AI response."
        }

def get_interview_questions(missing_skills_list):
    if not missing_skills_list:
        return []

    skills_text = ", ".join(missing_skills_list)
    PROMPT = (
        f"Generate 3 technical interview questions for missing skills: {skills_text}. "
        f"Return ONLY a valid JSON array of strings."
    )
    model = genai.GenerativeModel('gemini-flash-latest')
    response = model.generate_content(PROMPT)

    text = response.text.strip().replace("```json", "").replace("```", "").strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(q).strip() for q in parsed if str(q).strip()]
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    return [str(q).strip() for q in v if str(q).strip()]
    except Exception:
        pass

    # Fallback: split by lines if JSON parsing fails
    questions = []
    for line in text.splitlines():
        cleaned = line.strip().lstrip("-•*").lstrip("0123456789. ").strip()
        if cleaned:
            questions.append(cleaned)

    return questions

def extract_pdf_text(pdf_file_stream):
    try:
        pdf_reader = PyPDF2.PdfReader(pdf_file_stream)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        return None

# --- 5. API ENDPOINTS ---

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

        # Check duplicate
        cur.execute("SELECT id FROM applications WHERE filename = %s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'You have already applied for this job with this resume.'}), 400

        # Fetch JD
        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            return jsonify({'error': 'Invalid job selected.'}), 400
        jd_text = job['description']

        # Process Resume
        resume_bytes = resume_file.read()
        resume_text = extract_pdf_text(io.BytesIO(resume_bytes))
        if not resume_text:
            return jsonify({'error': 'Could not read PDF.'}), 400

        # AI Processing
        ai_response = get_ai_scan(resume_text, jd_text)
        score = ai_response.get('matchScore', 0) or 0
        status = "Shortlisted" if score >= 60 else "Pending"

        matching_skills = ai_response.get('matchingSkills') or []
        missing_skills = ai_response.get('missingSkills') or []
        questions_list = get_interview_questions(missing_skills)

        # Insert into DB
        cur.execute('''
            INSERT INTO applications (
                name, email, job_id, score, status, filename,
                summary, matchingSkills, missingSkills, interviewQuestions, notes
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            name,
            email,
            job_id,
            score,
            status,
            filename,
            ai_response.get('summary'),
            json.dumps(matching_skills),
            json.dumps(missing_skills),
            json.dumps(questions_list),
            ""
        ))

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': f'Application received for {name}!'})
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/get-applications', methods=['GET'])
def get_applications():
    # --- ROBUST PARSING LOGIC ADDED HERE ---
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

        for app in apps:
            # Clean up matchingSkills, missingSkills, and interviewQuestions
            for field in ['matchingSkills', 'missingSkills', 'interviewQuestions']:
                val = app.get(field)

                # 1. If it's already a list, great.
                if isinstance(val, list):
                    continue
                
                # 2. If empty/None, make it empty list.
                if not val:
                    app[field] = []
                    continue

                # 3. If it's a string, try to parse it safely.
                if isinstance(val, str):
                    cleaned = val.strip().replace("```json", "").replace("```", "").strip()
                    parsed_list = None

                    # Try JSON first (Double quotes)
                    try:
                        parsed_list = json.loads(cleaned)
                    except:
                        # Try Python Literal (Single quotes) using 'ast'
                        try:
                            parsed_list = ast.literal_eval(cleaned)
                        except:
                            pass # Failed both standard parsers

                    # If parsing worked and gave us a list
                    if isinstance(parsed_list, list):
                        app[field] = [str(x) for x in parsed_list]
                    
                    # Fallback 1: If it's interview questions, maybe it's a bulleted list string?
                    elif field == 'interviewQuestions':
                        lines = []
                        for line in cleaned.splitlines():
                            # Remove bullets/numbers
                            clean_line = line.strip().lstrip("-•*").lstrip("0123456789.) ").strip()
                            if clean_line:
                                lines.append(clean_line)
                        app[field] = lines
                    
                    # Fallback 2: Comma separated string?
                    elif ',' in cleaned:
                         app[field] = [s.strip() for s in cleaned.split(',') if s.strip()]
                    else:
                        app[field] = []
                else:
                    app[field] = []

        return jsonify({'applications': apps})
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

@app.route('/download-application/<filename>', methods=['GET'])
def download_application(filename):
    return jsonify({'error': 'Download is disabled in free-tier deployment.'}), 403

@app.route('/delete-application/<filename>', methods=['DELETE'])
def delete_application(filename):
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
        cur.execute('''
            SELECT 
                COUNT(*) AS total_apps,
                COUNT(CASE WHEN status = 'Shortlisted' THEN 1 END) AS total_shortlisted,
                AVG(score) AS avg_score
            FROM applications
        ''')
        stats = cur.fetchone()
        cur.close()
        conn.close()

        avg_score = round(stats['avg_score']) if stats['avg_score'] is not None else 0
        analytics_data = {
            "total_apps": stats['total_apps'],
            "total_shortlisted": stats['total_shortlisted'],
            "avg_score": avg_score
        }
        return jsonify(analytics_data)
    except Exception as e:
        print(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # Local testing only
    print("\n--- Running in LOCAL TESTING Mode ---")
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)