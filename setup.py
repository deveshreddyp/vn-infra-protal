import os
import psycopg2
from psycopg2.extras import RealDictCursor
import sys

DATABASE_URL = os.environ.get('DATABASE_URL') 

if not DATABASE_URL:
    print("FATAL: DATABASE_URL environment variable is missing.")
    sys.exit(1)

def get_db_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    print("--- Running Database Initialization ---")
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
    print("--- Database Tables Created Successfully ---")

if __name__ == '__main__':
    try:
        init_db()
    except Exception as e:
        print(f"DATABASE ERROR: Failed to create tables: {e}")
        sys.exit(1)