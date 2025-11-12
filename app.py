from flask import Flask, request, jsonify
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

# --- CONFIG ---
API_KEY = os.environ.get('GOOGLE_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
genai.configure(api_key=API_KEY)

app = Flask(__name__)
CORS(app)

# --- FOLDERS ---
APPLICATION_FOLDER = os.environ.get('APPLICATION_FOLDER', '/var/data/applications')
os.makedirs(APPLICATION_FOLDER, exist_ok=True)


# --- DB HELPER ---
def get_db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not configured")
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


# --- PDF + AI HELPERS ---
def extract_pdf_text(pdf_file_like):
    """Extract text safely from PDF resumes."""
    text = ""
    try:
        reader = PyPDF2.PdfReader(pdf_file_like)
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text += page_text + "\n"
    except Exception as e:
        print("extract_pdf_text error:", e)
    return text.strip()


def get_ai_scan(resume_text, job_description):
    """Analyze resume vs job description using Gemini AI."""
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
Compare this resume with the job description and return JSON with:
summary, matchingSkills (array), missingSkills (array), matchScore (integer 0–100).

Resume:
{resume_text[:6000]}

Job Description:
{job_description[:3000]}
"""
        response = model.generate_content(prompt, generation_config=GenerationConfig(temperature=0.2))
        text = response.text or ""

        # Parse JSON output if possible
        start = text.find("{")
        if start != -1:
            try:
                return json.loads(text[start:])
            except Exception:
                pass

        # fallback
        return {
            "summary": text[:1000],
            "matchingSkills": [],
            "missingSkills": [],
            "matchScore": 0
        }
    except Exception as e:
        print("AI scan error:", e)
        return {"summary": "Error scanning resume.", "matchingSkills": [], "missingSkills": [], "matchScore": 0}


def get_interview_questions(missing_skills):
    """Generate interview questions from missing skills."""
    if not missing_skills:
        return "No missing skills identified."
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        skills = ", ".join(missing_skills)
        prompt = f"Generate 5 short interview questions to test skills: {skills}"
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("Question generation error:", e)
        return "Error generating questions."


# --- ENDPOINTS ---
@app.route('/login', methods=['POST'])
def login():
    try:
        data = request.json or {}
        if data.get('password') == 'deva':
            return jsonify({'success': True})
        return jsonify({'success': False, 'error': 'Incorrect Password'}), 401
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

        cur.execute("SELECT id FROM applications WHERE filename = %s", (filename,))
        if cur.fetchone():
            return jsonify({'error': 'You have already applied for this job with this resume.'}), 400

        cur.execute("SELECT description FROM jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            return jsonify({'error': 'Invalid job selected.'}), 400

        resume_text = extract_pdf_text(io.BytesIO(resume_file.read()))
        if not resume_text:
            return jsonify({'error': 'Could not read text from PDF.'}), 400

        ai_response = get_ai_scan(resume_text, job['description'])
        score = ai_response.get('matchScore', 0)
        status = "Shortlisted" if score >= 60 else "Pending"
        questions = get_interview_questions(ai_response.get('missingSkills', []))

        cur.execute("""
            INSERT INTO applications (name, email, job_id, score, status, filename,
                                      summary, matchingSkills, missingSkills, interviewQuestions, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (name, email, job_id, score, status, filename,
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


@app.route('/get-applications', methods=['GET'])
def get_applications():
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT a.*, j.title as "jobTitle"
            FROM applications a
            LEFT JOIN jobs j ON a.job_id = j.id
            ORDER BY a.id DESC
        """)
        apps = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({'applications': apps})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/chat', methods=['POST'])
def chat():
    try:
        user_message = request.json.get('message', '')
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(f"USER: {user_message}")
        return jsonify({'reply': response.text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("\\n--- Running in LOCAL TESTING Mode ---")
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
