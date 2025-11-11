import os
import psycopg2
from psycopg2.extras import RealDictCursor
import sys

# CRITICAL: This file reads the DB URL from the environment, provided by Render.
DATABASE_URL = os.environ.get('DATABASE_URL') 

if not DATABASE_URL:
    print("FATAL: DATABASE_URL environment variable is missing.")
    # Exits with status 1, causing the deploy to fail if the URL isn't set.
    sys.exit(1)

def get_db_conn():
    """Establishes a connection to the database."""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    print("--- Running Database Initialization ---")
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
    # Execute the initialization
    try:
        init_db()
    except Exception as e:
        print(f"DATABASE ERROR: Failed to create tables: {e}")
        # Exit with a non-zero status so Render fails the deploy
        sys.exit(1)