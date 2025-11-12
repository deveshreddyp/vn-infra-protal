# vn-infra-protal
AI RESUME SCREENER
# VN Infra Reyality AI Recruitment Portal

## 🚀 Project Overview
This is a comprehensive, full-stack HR portal designed for VN Infra Reyality. The application automates the candidate screening process, provides real-time analytics to recruiters, and includes a secure dashboard for managing applications. The backend is powered by Python and a permanent PostgreSQL database.

## ✨ Key Features
The application is structured into three main modes:

1.  **Recruiter Portal (Dashboard):**
    * **Secure Login:** Password protection (`deva` during development) verified by the backend server.
    * **Auto-Analysis:** Manual form to scan any resume against any job description.
    * **Application Management:** View, filter, and sort all submitted applications.
    * **Shortlisting:** Automatically labels candidates with a score of **60% or higher** as "Shortlisted."
    * **Recruiter Workflow:** Detailed pop-up modal showing **AI Summaries, Missing Skills, AI-Generated Interview Questions,** and allowing for **Manual Status Updates** and **Recruiter Notes**.
    * **Analytics:** Displays total applications, average score, and total shortlisted count at-a-glance.

2.  **Candidate Portal:**
    * **Resume Checker:** Allows candidates to check their resume match score against a mock JD.

3.  **Application Portal:**
    * **Dynamic Job Application:** Candidates select a job from a list dynamically loaded from the database and submit their resume for automated scoring.

4.  **AI Chatbot:** A floating chatbot on the homepage provides instant information and clarification about the portal's features.

## 🛠️ Installation and Setup

### 1. Local Development Setup (Using SQLite)

To run the project locally for testing:

1.  **Clone the repository.**
2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
3.  **Configure API Key:** Open `app.py` and set your Gemini API key (on line 14):
    ```python
    API_KEY = 'YOUR_SECRET_KEY'
    ```
4.  **Run Server:**
    ```bash
    python app.py
    ```
    * (This will automatically create the `vn_infra.db` file and the necessary tables).
5.  **Access:** Open `index.html` in your browser.

### 2. Deployment Setup (Render PostgreSQL)

This application is designed to be deployed to Render's free services.

| Service | Type | Start Command |
| :--- | :--- | :--- |
| **vn-infra-protal-1** | Web Service | `gunicorn app:app` |
| **vn-infra-protal-2** | Static Site | (None) |

**Configuration Steps:**

1.  **PostgreSQL DB:** Create a **free PostgreSQL** database instance on Render.
2.  **Environment Variables (CRITICAL):** In the **vn-infra-protal-1** Web Service settings, set the following two variables:
    * `GOOGLE_API_KEY`: [Your secret Gemini Key]
    * `DATABASE_URL`: [The Internal Database URL provided by your new PostgreSQL service]
3.  **Build Command:** Your build command should be set to:
    ```bash
    pip install -r requirements.txt && python setup.py
    ```
    *(Note: `setup.py` creates the necessary tables on the live database.)*

---

## 🔑 Access Credentials (Development)

| Role | Access Point | Password | Notes |
| :--- | :--- | :--- | :--- |
| **Recruiter** | `/login.html` | `deva` | **Insecure on local environment. Do not use for production.** |
| **Applicant** | `/apply.html` | N/A | Fully public. |

---

### Author and Contact

| Detail | Value |
| :--- | :--- |
| **Author Name** | Pusalapati Devesh Reddy |
| **Author Email** | deveshreddypusalapati@gmail.com |
| **Project Status** | Deployment Ready (Fully Featured) |
