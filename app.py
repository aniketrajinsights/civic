from flask import Flask, render_template, request, redirect, session, url_for, flash,jsonify
from werkzeug.utils import secure_filename
import os
import config
from datetime import datetime
import google.generativeai as genai
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
from geopy.distance import geodesic

app = Flask(__name__)
app.secret_key = "secret123"
app.config['UPLOAD_FOLDER'] = 'static/uploads'

# 🔹 Load .env
load_dotenv()
# HOME / INDEX

@app.route('/')
def index():
    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Join feedback with user info for display
    cursor.execute("""
        SELECT feedback.rating, feedback.comment, users.name AS username
        FROM feedback
        JOIN users ON feedback.user_id = users.id
        ORDER BY feedback.created_at DESC
        LIMIT 20
    """)
    feedbacks = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('index.html', feedbacks=feedbacks)


# USER REGISTRATION


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form['name']
        email = request.form['email']
        password = request.form['password']
        state = request.form['state']

        try:
            conn = config.get_db_connection()
            cursor = conn.cursor()

            # Optional: Check if email already exists to avoid duplicate entry
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone():
                flash("Email already registered. Please log in.", "warning")
                return redirect(url_for('login'))

            # Insert user
            cursor.execute("""
                INSERT INTO users (name, email, password, state)
                VALUES (%s, %s, %s, %s)
            """, (name, email, password, state))
            conn.commit()

            flash("Registration successful. Please log in.", "success")
            return redirect(url_for('login'))

        except config.connector.Error as err:
            conn.rollback()
            print("MySQL Error:", err)
            flash("Registration failed due to a database error. Please try again.", "danger")

        finally:
            cursor.close()
            conn.close()

    return render_template('register.html')

# USER LOGIN

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = config.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE email=%s AND password=%s", (email, password))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user:
            session['user'] = user['id']
            session['user_state'] = user['state']  # store state
            flash("Login successful!", "success")
            return redirect(url_for('dashboard'))
        else:
            flash("Invalid credentials", "danger")

    return render_template('login.html')


# USER DASHBOARD

@app.route('/dashboard')
def dashboard():
    if 'user' not in session:
        return redirect(url_for('login'))

    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM reports WHERE user_id=%s", (session['user'],))
    reports = cursor.fetchall()
    cursor.close()
    conn.close()

    return render_template('dashboard.html', reports=reports, state=session.get('user_state'))


# SUBMIT REPORT



@app.route('/report', methods=['GET', 'POST'])
def report():
    if 'user' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        title = request.form.get('title')
        description = request.form.get('description')
        category = request.form.get('category')
        location = request.form.get('location')
        latitude = request.form.get('latitude')
        longitude = request.form.get('longitude')
        state = request.form.get('state')

        image = request.files.get('image')
        filename = None
        if image and image.filename != '':
            filename = secure_filename(image.filename)
            image.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

        conn = config.get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            INSERT INTO reports 
                (user_id, title, description, category, location, latitude, longitude, image, state) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (session['user'], title, description, category, location, latitude, longitude, filename, state))
        conn.commit()

        # ✅ Fetch the newly inserted report
        cursor.execute("SELECT * FROM reports WHERE id = LAST_INSERT_ID()")
        new_report = cursor.fetchone()

        # ✅ Call similarity updater
        update_similar_reports(new_report)

        cursor.close()
        conn.close()

        flash("Report submitted successfully!", "success")
        return redirect(url_for('dashboard'))

    return render_template('report_issue.html', state=session.get('user_state'))

# FEEDBACK

@app.route('/feedback', methods=['GET', 'POST'])
def feedback():
    if 'user' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        rating = request.form['rating']
        comment = request.form['comment']
        state = session.get('user_state')

        conn = config.get_db_connection()
        cursor = conn.cursor()
        # cursor.execute("INSERT INTO feedback (user_id, rating, comment) VALUES (%s, %s, %s)",
        #                (session['user'], rating, comment))
        cursor.execute("INSERT INTO feedback (user_id, rating, comment, state) VALUES (%s, %s, %s, %s)",
                       (session['user'], rating, comment, state))
        conn.commit()
        cursor.close()
        conn.close()

        flash("Thank you for your feedback!", "success")
        return redirect(url_for('dashboard'))

    return render_template('feedback.html',state=session.get('user_state'))


# ADMIN LOGIN

@app.route('/admin', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        state = request.form['state']

        conn = config.get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT * FROM admin 
            WHERE username = %s AND password = %s AND state = %s
        """, (username, password, state))
        admin = cursor.fetchone()
        cursor.close()
        conn.close()

        if admin:
            session['admin'] = True
            session['admin_state'] = state  # optional: store state for dashboard filtering
            return redirect('/admin/dashboard')
        else:
            flash("Invalid credentials or state mismatch", "danger")
    return render_template('admin_login.html')


# ADMIN DASHBOARD

def update_similar_reports(new_report):
    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("SELECT * FROM reports WHERE category = %s AND id != %s", (new_report['category'], new_report['id']))
    existing_reports = cursor.fetchall()

    new_coords = (float(new_report['latitude']), float(new_report['longitude']))
    similar_ids = []

    for report in existing_reports:
        existing_coords = (float(report['latitude']), float(report['longitude']))
        distance = geodesic(new_coords, existing_coords).meters

        if distance <= 500:
            similar_ids.append(report['id'])

    # Update similar_count for all matching reports
    for report_id in similar_ids:
        cursor.execute("UPDATE reports SET similar_count = similar_count + 1 WHERE id = %s", (report_id,))
    cursor.execute("UPDATE reports SET similar_count = %s WHERE id = %s", (len(similar_ids), new_report['id']))
    print(f"New report ID: {new_report['id']}, matched with: {similar_ids}")

    conn.commit()
    cursor.close()
    conn.close()

@app.route('/update_status/<int:report_id>', methods=['POST'])
def update_status(report_id):
    new_status = request.form['status']
    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Get the report being updated
    cursor.execute("SELECT * FROM reports WHERE id = %s", (report_id,))
    report = cursor.fetchone()

    # Update status of the selected report
    cursor.execute("UPDATE reports SET status = %s WHERE id = %s", (new_status, report_id))

    # Find similar reports within 100m and same category
    cursor.execute("SELECT * FROM reports WHERE category = %s AND id != %s", (report['category'], report_id))
    others = cursor.fetchall()
    updated_ids = []

    for r in others:
        dist = geodesic((float(report['latitude']), float(report['longitude'])),
                        (float(r['latitude']), float(r['longitude']))).meters
        if dist <= 100:
            updated_ids.append(r['id'])

    # Update their statuses too
    for uid in updated_ids:
        cursor.execute("UPDATE reports SET status = %s WHERE id = %s", (new_status, uid))

    conn.commit()
    cursor.close()
    conn.close()

    flash("Status updated and synced with nearby reports.", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if 'admin' not in session:
        return redirect(url_for('admin_login'))

    admin_state = session.get('admin_state')

    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    if admin_state == 'India':
        # Show everything for national-level admin
        cursor.execute("""
            SELECT reports.*, users.name
            FROM reports
            JOIN users ON reports.user_id = users.id
        """)
        reports = cursor.fetchall()

        cursor.execute("""
            SELECT feedback.*, users.name 
            FROM feedback
            JOIN users ON feedback.user_id = users.id
            ORDER BY feedback.created_at DESC
        """)
        feedbacks = cursor.fetchall()
    else:
        # Show only state-specific data
        cursor.execute("""
            SELECT reports.*, users.name
            FROM reports
            JOIN users ON reports.user_id = users.id
            WHERE reports.state = %s
        """, (admin_state,))
        reports = cursor.fetchall()

        cursor.execute("""
            SELECT feedback.*, users.name 
            FROM feedback
            JOIN users ON feedback.user_id = users.id
            WHERE feedback.state = %s
            ORDER BY feedback.created_at DESC
        """, (admin_state,))
        feedbacks = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('admin_dashboard.html', reports=reports, feedbacks=feedbacks, state=admin_state)


# UPDATE STATUS



# LOGOUT

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# MAP VIEW

@app.route('/map')
def map_view():
    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id, title, description, status, latitude, longitude
        FROM reports
    """)
    reports = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template('map.html', reports=reports)

@app.route('/leaderboard')
def leaderboard():
    conn = config.get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Leaderboard logic: users ranked by number of reports submitted
    query = """
    SELECT u.id, u.name, COUNT(r.id) AS total_reports
    FROM users u
    LEFT JOIN reports r ON u.id = r.user_id
    GROUP BY u.id, u.name
    ORDER BY total_reports DESC
    LIMIT 10;
    """
    cursor.execute(query)
    leaderboard_data = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template("leaderboard.html", leaderboard=leaderboard_data)


@app.route('/submit_rating/<int:report_id>', methods=['POST'])
def submit_rating(report_id):
    rating = request.form.get('rating')
    if rating and rating.isdigit() and 1 <= int(rating) <= 5:
        conn = config.get_db_connection()  # Get connection
        cursor = conn.cursor()
        cursor.execute("UPDATE reports SET rating = %s WHERE id = %s", (rating, report_id))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))




# 🔹 Configure Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = "gemini-2.5-flash"
model = genai.GenerativeModel(MODEL_NAME)



# 🔹 Step 1: Scrape Website

def scrape_website(url):
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        texts = soup.stripped_strings
        return " ".join(texts)
    except Exception as e:
        return f"Error scraping {url}: {e}"

# Add your website pages here
urls = [
    
]


knowledge_base = ""
for url in urls:
    knowledge_base += scrape_website(url) + "\n\n"


# 🔹 Step 2: Ask Gemini


INSTRUCTIONS = """
You are the official chatbot for the Crowdsourced Civic Issue Reporting and Resolution System. Your role is to guide users through structured processes for reporting and managing civic issues.

Your primary function is to gather all necessary information in a sequential, question-and-answer format. Do not engage in free-form conversation until a process is complete.

**Core User Flows:**

1.  **Issue Reporting:**
    * **Trigger:** The user's message indicates a desire to report a problem (e.g., "hi," "hello," "I want to report," "there is a pothole").
    * **Process:** Immediately initiate the reporting sequence by asking the following questions one by one. Do not proceed to the next question until the current one is answered.
        * **Question 1:** "What type of civic issue are you facing? (e.g., Pothole, garbage overflow, broken streetlight)"
        * **Question 2:** "Can you describe the location of the issue?"
        * **Question 3:** "Do you have any additional details or photos? (Type 'no' if you don't)"
        * **Question 4:** "Would you like to submit this issue for resolution?"
    * **Completion:** Once the user answers "yes" to the final question, provide a summary and a reference ID.
        * **Example Completion Message:** "Your report has been submitted! Reference ID: [ID]. We will notify the relevant department. Thank you for helping improve your community! 🙏"

2.  **General Inquiry:**
    * **Trigger:** The user asks a question about the app's features or purpose.
    * **Process:** Refer to your knowledge base to provide a direct and concise answer. Do not initiate the reporting sequence.
        * **Example Response:** "The leaderboard ranks users by the number of issues they report. The top users are featured on the homepage. 🏆"

3.  **Final Response:**
    * **Completion of any task:** After completing a task (e.g., submitting a report, answering a question), end the conversation with a simple thank you. Do not ask "Is there anything else?".
    * **Example Final Response:** "Thank you! Have a great day."

**IMPORTANT:** If the user's input is a simple greeting like "hi," immediately start the Issue Reporting sequence to guide them into the intended workflow. If they say "hi" and then "thank you," stop the sequence and say "You're welcome."

Be direct, clear, and helpful. Use emojis to make the interaction engaging without being overly conversational.
🚀 How to Use the Website
Here is a step-by-step guide on how to interact with your Civic Issue Reporting System:

1. Registration and Login
For New Users: Go to the home page and click the "Register" button. Fill out the form with your name, email, password, and state, then click "Register." You'll be redirected to the login page.

For Existing Users: From the home page, click "User Login." Enter your registered email and password to access your personal dashboard.

2. Reporting a New Issue
Access the Form: Once logged in, go to your Dashboard and click the "Report New Issue" button.

Fill out the Details: On the report_issue.html page, fill in the title, description, and category of the issue.

Select the Location: This is a key feature. Use the interactive map to pinpoint the exact location of the issue. You can either:

Click on the Map: Click on the map to drop a marker at the issue's location. The latitude and longitude will be automatically captured.

Use Geolocation: Click the "Use My Current Location" button to have the map automatically center on your location.

Upload a Photo: If you have a photo of the issue, use the "Upload Image" field to attach it.

Submit: Click the "Submit Report" button to send your report to the system.

3. Tracking Your Reports
After submitting a report, you will be taken back to your Dashboard.

Here, you can see a table of all the reports you have submitted.

You can track the Status column to see if your report is Pending, In Progress, or Resolved.

You can also filter the table by status or category using the dropdown menus.

4. Giving Feedback
From your Dashboard, click the "Give Feedback About App" button.

You can provide a rating (1-5 stars) and a comment on your experience with the application. This feedback is visible to administrators.

5. Viewing the Public Map and Leaderboard
Public Map: From the home page, click "View Issues on Map." This map displays all reported issues, color-coded by their status (Pending, In Progress, Resolved). You can also toggle a heatmap to see clusters of reports.

Leaderboard: The "View Leaderboard" button on the home page shows the top users who have submitted the most reports, promoting friendly competition.

🤖 How to Use the Gemini Chatbot
The chatbot, integrated on your home page, offers a structured way to report an issue or get information without navigating through menus.

Start the Chat: On the home page, click the chatbot image to open the chat window.

Initiate a Report: Type "hi" or "hello" to begin the conversation. The chatbot will immediately start the step-by-step reporting process.

Follow the Prompts: Answer the chatbot's questions one by one:

What type of civic issue?

Can you describe the location?

Any additional details or photos?

Would you like to submit?

Confirm Submission: Once you confirm, the chatbot will summarize the report and provide a reference ID.

Get Information: The chatbot is also a knowledge base. You can ask it questions like "What is the leaderboard?" or "How do I give feedback?" to get a direct answer.

🔒 Admin Access
Admin Login: From the home page, click "Admin Login." You will need a special username and password to log in.

Admin Dashboard: Once logged in, you can view a comprehensive table of all user reports, filter them, and manually update a report's status to In Progress or Resolved. You can also review all user feedback.
    """

knowledge_base = """
### 🎯 Application Purpose
The Crowdsourced Civic Issue Reporting and Resolution System is a platform designed to connect citizens with their local municipality. Its core purpose is to streamline the process of reporting public issues, such as infrastructure problems and public service failures, to ensure faster resolution.

### 🧩 Key Features
-   **User Dashboard:** A personal portal for users to view, track, and manage all of the issues they have reported.
-   **Public Map:** An interactive map that displays the precise location of all reported issues, visually categorized by their current status.
-   **Leaderboard:** A social feature that encourages reporting by recognizing the most active users. Rankings are based on the total number of submitted reports.
-   **Feedback System:** Allows users to provide ratings and comments on their experience with the application itself, as well as on specific resolved issues.
-   **Admin Panel:** A dashboard for authorized administrators to view and update the status of reports from their specific state or jurisdiction.

### 📝 Issue Statuses & Definitions
-   **Pending:** The initial status of a report. It means the issue has been submitted and is awaiting review by a municipal administrator.
-   **In Progress:** The report has been reviewed, and action is being taken to resolve the issue. This signifies that a municipal team is actively working on the problem.
-   **Resolved:** The issue has been fixed. The report is now closed, and the original user has the option to rate the resolution.

### 🗺️ Location & Mapping
The system uses a combination of manual address entry, a map-based pin-drop system, and a "Use My Location" feature to ensure precise geographic data for every report. This data is critical for assigning reports to the correct municipal department.

### 🔒 User Data & Privacy
All user data is stored securely in our database. Passwords are encrypted. User information is only shared with the relevant administrators for the purpose of issue resolution.
    """

def ask_gemini(user_message):
    prompt = f"{INSTRUCTIONS}\n\nKnowledge Base:\n{knowledge_base}\n\nUser: {user_message}"
    response = model.generate_content(prompt)
    return response.text if response and response.text else "⚠️ No response"


# 🔹 Step 3: Flask Routes


@app.route("/chatbox")
def chatbox():
    return render_template("chat.html")

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json()
    user_message = data.get("message", "").strip()

    if not user_message:
        return jsonify({"reply": "⚠️ Please enter a message."})

    try:
        reply = ask_gemini(user_message)
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"❌ Error: {str(e)}"})


if __name__ == '__main__':
    app.run(debug=True)
