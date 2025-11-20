from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import os
import traceback
import google.generativeai as genai
import PyPDF2
import io
import json
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

# --- 4. AI & PDF HELPERS (Unchanged structure, safer parsing) ---
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
    """
    Always return a Python list of question strings.
    """
    if not missing_skills_list:
        return []

    skills_text = ", ".join(missing_skills_list)
    PROMPT = (
        f"Generate 3 technical interview questions for missing skills: {skills_text}. "
        f"Return ONLY a valid JSON array of strings."
    )
    model = genai.GenerativeModel('gemini-flash-latest')
    response = model.generate_content(PROMPT)

    text = response.text.strip()
    # Remove common code fences if present
    text = text.replace("```json", "").replace("```", "").strip()

    # Try to parse as JSON first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(q).strip() for q in parsed if str(q).strip()]
        if isinstance(parsed, dict):
            # If the model wraps it in an object, try to pull any list value
            for v in parsed.values():
                if isinstance(v, list):
                    return [str(q).strip() for q in v if str(q).strip()]
    except Exception:
        pass

    # Fallback: split by lines / bullets
    questions = []
    for line in text.splitlines():
        if not line.strip():
            continue
        # Strip bullets and numbering like "1. ", "- ", "• "
        cleaned = line.strip()
        cleaned = cleaned.lstrip("-•").lstrip("0123456789. ").strip()
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
    # --- THIS FUNCTION IS NOW FIXED ---
    try:
        resume_file = request.files['resume']
        name = request.form['name']
        email = request.form['email']
        job_id = request.form['jobId']
        filename = f"{name.replace(' ', '_')}-{job_id}-{resume_file.filename}"

        conn = get_db_conn()
        cur = conn.cursor()

        # Avoid duplicate applications by filename
        cur.execute("SELECT id FROM applications WHERE filename = %s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'You have already applied for this job with this resume.'}), 400

        # Fetch JD text
        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            return jsonify({'error': 'Invalid job selected.'}), 400

        jd_text = job['description']

        # Read resume bytes once
        resume_bytes = resume_file.read()
        resume_text = extract_pdf_text(io.BytesIO(resume_bytes))
        if not resume_text:
            return jsonify({'error': 'Could not read PDF.'}), 400

        # Get AI scan result
        ai_response = get_ai_scan(resume_text, jd_text)

        score = ai_response.get('matchScore', 0) or 0
        status = "Shortlisted" if score >= 60 else "Pending"

        # Ensure skills lists are always lists
        matching_skills = ai_response.get('matchingSkills') or []
        missing_skills = ai_response.get('missingSkills') or []

        # Generate interview questions as a Python list
        questions_list = get_interview_questions(missing_skills)

        # Save all data to PostgreSQL (as JSON strings in TEXT columns)
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

        # Normalize skills & questions so frontend always gets arrays
        for app in apps:
            for field in ['matchingSkills', 'missingSkills', 'interviewQuestions']:
                val = app.get(field)

                # Already a list (new rows after this fix)
                if isinstance(val, list):
                    continue

                if not val:
                    app[field] = []
                    continue

                # Try to parse JSON strings like '["Python","ML"]'
                if isinstance(val, str):
                    stripped = val.strip()
                    stripped = stripped.replace("```json", "").replace("```", "").strip()
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, list):
                            app[field] = parsed
                            continue
                    except Exception:
                        # For interviewQuestions we might have old bullet text
                        if field == 'interviewQuestions':
                            lines = []
                            for line in stripped.splitlines():
                                if not line.strip():
                                    continue
                                cleaned = line.strip()
                                cleaned = cleaned.lstrip("-•").lstrip("0123456789. ").strip()
                                if cleaned:
                                    lines.append(cleaned)
                            app[field] = lines
                        else:
                            app[field] = []
                else:
                    app[field] = []

        return jsonify({'applications': apps})
    except Exception as e:
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
    # --- THIS FUNCTION IS NOW FIXED ---
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        # Run one query for all stats
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
        print(traceback.format_exc())  # Print full error
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # This block is only for local testing.
    # Gunicorn does NOT run this block.
    # The 'init_db()' call must be in 'setup.py'
    print("\n--- Running in LOCAL TESTING Mode (Requires local Postgres) ---")
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
