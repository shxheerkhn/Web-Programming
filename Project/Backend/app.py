import os
import sqlite3
import hashlib
import secrets
import json
import re
import random
from datetime import datetime
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session, g)
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, 
            template_folder=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Frontend', 'templates')),
            static_folder=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'Frontend', 'static')))
app.secret_key = secrets.token_hex(32)
DATABASE = 'freelance.db'

# ──────────────────────────────────────────────
# DATABASE HELPERS
# ──────────────────────────────────────────────
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
    return db

@app.teardown_appcontext
def close_db(exc):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv

def mutate_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    return cur.lastrowid

def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('client','freelancer')),
            skills TEXT DEFAULT '',
            bio TEXT DEFAULT '',
            rating REAL DEFAULT 0.0,
            total_reviews INTEGER DEFAULT 0,
            balance REAL DEFAULT 1000.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            skills_required TEXT NOT NULL,
            budget REAL NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            fraud_score INTEGER DEFAULT 0,
            fraud_level TEXT DEFAULT 'Low',
            fraud_reasons TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(client_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            freelancer_id INTEGER NOT NULL,
            cover_letter TEXT NOT NULL,
            bid_amount REAL NOT NULL,
            timeline TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(job_id, freelancer_id),
            FOREIGN KEY(job_id) REFERENCES jobs(id),
            FOREIGN KEY(freelancer_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS escrow (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            freelancer_id INTEGER,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'held',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            released_at TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            type TEXT DEFAULT 'info',
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
    """)
    db.commit()
    db.close()

# ──────────────────────────────────────────────
# AI ENGINE  (Rule-Based, no external API)
# ──────────────────────────────────────────────
FRAUD_RED_FLAGS = [
    (r'\b(urgent|asap|immediately)\b', 2, "Urgency language detected"),
    (r'\$\s*\d{4,}', 3, "Unusually high payment amount"),
    (r'\b(bitcoin|crypto|western union|wire transfer)\b', 4, "Suspicious payment method"),
    (r'\b(guaranteed|100%|no experience)\b', 2, "Unrealistic guarantees"),
    (r'(.)\1{4,}', 1, "Spam-like character repetition"),
    (r'\b(click here|visit link|external site)\b', 3, "Suspicious external links"),
    (r'\b(get rich|easy money|passive income)\b', 3, "Get-rich-quick language"),
    (r'\b(personal info|social security|bank account)\b', 4, "Sensitive info request"),
]

def analyze_fraud(title, description):
    text = (title + ' ' + description).lower()
    score = 0
    reasons = []
    for pattern, weight, reason in FRAUD_RED_FLAGS:
        if re.search(pattern, text, re.IGNORECASE):
            score += weight
            reasons.append(reason)
    # Budget check vs description length (low effort high pay)
    word_count = len(description.split())
    if word_count < 20:
        score += 2
        reasons.append("Very short job description")
    if score <= 3:
        level = "Low"
    elif score <= 7:
        level = "Medium"
    else:
        level = "High"
    return min(score, 10), level, reasons

def match_freelancers(job_skills_str, all_freelancers):
    job_skills = set(s.strip().lower() for s in job_skills_str.split(',') if s.strip())
    results = []
    for fl in all_freelancers:
        fl_skills = set(s.strip().lower() for s in (fl['skills'] or '').split(',') if s.strip())
        if not fl_skills:
            continue
        common = job_skills & fl_skills
        if not common:
            continue
        match_pct = round(len(common) / max(len(job_skills), 1) * 100)
        results.append({
            'id': fl['id'],
            'username': fl['username'],
            'skills': fl['skills'],
            'rating': fl['rating'],
            'total_reviews': fl['total_reviews'],
            'bio': fl['bio'],
            'matched_skills': list(common),
            'match_pct': match_pct
        })
    results.sort(key=lambda x: (-x['match_pct'], -x['rating']))
    return results[:5]

def generate_proposal(job_title, job_description, job_skills, freelancer_name, freelancer_skills):
    templates = [
        """Dear Client,

I am excited to apply for your "{title}" project. With hands-on experience in {skills}, I am confident I can deliver outstanding results that exceed your expectations.

After carefully reviewing your project requirements, I understand you need {summary}. My approach would be to begin with a thorough analysis of your needs, followed by a structured implementation plan that prioritizes quality and timely delivery.

My relevant expertise in {fl_skills} positions me well to tackle this challenge. I have successfully completed similar projects and understand the nuances involved.

I propose to complete this project within the agreed timeline with regular updates and milestone check-ins. I am available for immediate discussion and can begin as soon as the project is confirmed.

Looking forward to the opportunity to work with you.

Best regards,
{name}""",
        """Hello,

Your "{title}" project immediately caught my attention — it aligns perfectly with my core expertise in {skills}.

Here's how I would approach this: {summary}. I believe in transparent communication and delivering clean, well-documented work every time.

With my background in {fl_skills}, I have the technical depth needed to handle this project efficiently. I work systematically, maintain clear communication throughout, and always deliver on time.

I am prepared to start immediately and would love to discuss the finer details of your project. Please feel free to ask any questions.

Sincerely,
{name}"""
    ]
    template = random.choice(templates)
    skill_list = ', '.join(job_skills.split(',')[:3]).strip() or 'the required technologies'
    fl_skill_list = ', '.join(freelancer_skills.split(',')[:3]).strip() or 'relevant technologies'
    summary_words = job_description.split()
    summary = ' '.join(summary_words[:15]) + ('...' if len(summary_words) > 15 else '')
    return template.format(
        title=job_title,
        skills=skill_list,
        fl_skills=fl_skill_list,
        summary=summary,
        name=freelancer_name
    )

# ──────────────────────────────────────────────
# AUTH HELPERS
# ──────────────────────────────────────────────
def current_user():
    uid = session.get('user_id')
    if uid:
        return query_db("SELECT * FROM users WHERE id=?", [uid], one=True)
    return None

def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return wrapper

def notify(user_id, message, ntype='info'):
    mutate_db("INSERT INTO notifications (user_id, message, type) VALUES (?,?,?)",
              [user_id, message, ntype])

# ──────────────────────────────────────────────
# PAGE ROUTES
# ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', user=current_user())

@app.route('/register')
def register_page():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('register.html')

@app.route('/login')
def login_page():
    if session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    user = current_user()
    if user['role'] == 'client':
        jobs = query_db("SELECT * FROM jobs WHERE client_id=? ORDER BY created_at DESC", [user['id']])
        return render_template('dashboard-client.html', user=user, jobs=jobs)
    else:
        jobs = query_db("SELECT j.*, u.username as client_name FROM jobs j JOIN users u ON j.client_id=u.id WHERE j.status='open' ORDER BY j.created_at DESC")
        my_proposals = query_db("""
            SELECT p.*, j.title as job_title, j.budget as job_budget, u.username as client_name
            FROM proposals p
            JOIN jobs j ON p.job_id=j.id
            JOIN users u ON j.client_id=u.id
            WHERE p.freelancer_id=? ORDER BY p.created_at DESC
        """, [user['id']])
        return render_template('dashboard_freelancer.html', user=user, jobs=jobs, my_proposals=my_proposals)

@app.route('/jobs')
@login_required
def jobs_page():
    user = current_user()
    jobs = query_db("""
        SELECT j.*, u.username as client_name
        FROM jobs j JOIN users u ON j.client_id=u.id
        WHERE j.status='open' ORDER BY j.created_at DESC
    """)
    return render_template('jobs.html', user=user, jobs=jobs)

@app.route('/jobs/<int:job_id>')
@login_required
def job_detail(job_id):
    user = current_user()
    job = query_db("""
        SELECT j.*, u.username as client_name, u.rating as client_rating
        FROM jobs j JOIN users u ON j.client_id=u.id WHERE j.id=?
    """, [job_id], one=True)
    if not job:
        return redirect(url_for('jobs_page'))
    proposals = []
    escrow_info = None
    user_proposal = None
    matched = []
    if user['role'] == 'client' and job['client_id'] == user['id']:
        proposals = query_db("""
            SELECT p.*, u.username as freelancer_name, u.skills as freelancer_skills,
                   u.rating as freelancer_rating, u.total_reviews
            FROM proposals p JOIN users u ON p.freelancer_id=u.id
            WHERE p.job_id=? ORDER BY p.created_at DESC
        """, [job_id])
        escrow_info = query_db("SELECT * FROM escrow WHERE job_id=?", [job_id], one=True)
        freelancers = query_db("SELECT * FROM users WHERE role='freelancer'")
        matched = match_freelancers(job['skills_required'], [dict(f) for f in freelancers])
    if user['role'] == 'freelancer':
        user_proposal = query_db("SELECT * FROM proposals WHERE job_id=? AND freelancer_id=?",
                                  [job_id, user['id']], one=True)
    fraud_reasons = json.loads(job['fraud_reasons'] or '[]')
    return render_template('job_detail.html', user=user, job=job,
                           proposals=proposals, escrow_info=escrow_info,
                           user_proposal=user_proposal, matched=matched,
                           fraud_reasons=fraud_reasons)

@app.route('/profile')
@login_required
def profile_page():
    user = current_user()
    return render_template('profile.html', user=user)

@app.route('/escrow')
@login_required
def escrow_page():
    user = current_user()
    if user['role'] == 'client':
        escrows = query_db("""
            SELECT e.*, j.title as job_title, u.username as freelancer_name
            FROM escrow e JOIN jobs j ON e.job_id=j.id
            LEFT JOIN users u ON e.freelancer_id=u.id
            WHERE e.client_id=? ORDER BY e.created_at DESC
        """, [user['id']])
    else:
        escrows = query_db("""
            SELECT e.*, j.title as job_title, u.username as client_name
            FROM escrow e JOIN jobs j ON e.job_id=j.id
            JOIN users u ON e.client_id=u.id
            WHERE e.freelancer_id=? ORDER BY e.created_at DESC
        """, [user['id']])
    return render_template('escrow.html', user=user, escrows=escrows)

# ──────────────────────────────────────────────
# API ROUTES
# ──────────────────────────────────────────────
@app.route('/api/register', methods=['POST'])
def api_register():
    d = request.get_json()
    username = d.get('username','').strip()
    email = d.get('email','').strip().lower()
    password = d.get('password','')
    role = d.get('role','')
    skills = d.get('skills','').strip()
    bio = d.get('bio','').strip()
    if not all([username, email, password, role]):
        return jsonify({'error': 'All fields are required'}), 400
    if role not in ('client', 'freelancer'):
        return jsonify({'error': 'Invalid role'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if query_db("SELECT id FROM users WHERE username=?", [username], one=True):
        return jsonify({'error': 'Username already taken'}), 409
    if query_db("SELECT id FROM users WHERE email=?", [email], one=True):
        return jsonify({'error': 'Email already registered'}), 409
    hashed = generate_password_hash(password)
    uid = mutate_db("INSERT INTO users (username,email,password,role,skills,bio) VALUES (?,?,?,?,?,?)",
                    [username, email, hashed, role, skills, bio])
    notify(uid, f"Welcome to FreeLancer Pro, {username}! Your account is ready.", 'success')
    return jsonify({'message': 'Account created successfully', 'redirect': '/login'}), 201

@app.route('/api/login', methods=['POST'])
def api_login():
    d = request.get_json()
    email = d.get('email','').strip().lower()
    password = d.get('password','')
    user = query_db("SELECT * FROM users WHERE email=?", [email], one=True)
    if not user or not check_password_hash(user['password'], password):
        return jsonify({'error': 'Invalid email or password'}), 401
    session['user_id'] = user['id']
    return jsonify({'message': 'Login successful', 'redirect': '/dashboard', 'role': user['role']}), 200

@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'redirect': '/'}), 200

@app.route('/api/jobs', methods=['POST'])
@login_required
def api_post_job():
    user = current_user()
    if user['role'] != 'client':
        return jsonify({'error': 'Only clients can post jobs'}), 403
    d = request.get_json()
    title = d.get('title','').strip()
    description = d.get('description','').strip()
    skills = d.get('skills_required','').strip()
    budget = d.get('budget')
    deadline = d.get('deadline','').strip()
    if not all([title, description, skills, budget, deadline]):
        return jsonify({'error': 'All fields are required'}), 400
    try:
        budget = float(budget)
        if budget <= 0:
            raise ValueError
    except:
        return jsonify({'error': 'Invalid budget amount'}), 400
    fraud_score, fraud_level, fraud_reasons = analyze_fraud(title, description)
    jid = mutate_db("""
        INSERT INTO jobs (client_id,title,description,skills_required,budget,deadline,fraud_score,fraud_level,fraud_reasons)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, [user['id'], title, description, skills, budget, deadline,
          fraud_score, fraud_level, json.dumps(fraud_reasons)])
    if fraud_level == 'High':
        notify(user['id'], f"⚠️ Your job '{title}' was flagged as HIGH risk. Consider revising.", 'warning')
    return jsonify({'message': 'Job posted successfully', 'job_id': jid,
                    'fraud_level': fraud_level, 'fraud_score': fraud_score,
                    'fraud_reasons': fraud_reasons}), 201

@app.route('/api/jobs/<int:job_id>', methods=['DELETE'])
@login_required
def api_delete_job(job_id):
    user = current_user()
    job = query_db("SELECT * FROM jobs WHERE id=?", [job_id], one=True)
    if not job or job['client_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    mutate_db("DELETE FROM jobs WHERE id=?", [job_id])
    return jsonify({'message': 'Job deleted'}), 200

@app.route('/api/proposals', methods=['POST'])
@login_required
def api_submit_proposal():
    user = current_user()
    if user['role'] != 'freelancer':
        return jsonify({'error': 'Only freelancers can submit proposals'}), 403
    d = request.get_json()
    job_id = d.get('job_id')
    cover_letter = d.get('cover_letter','').strip()
    bid_amount = d.get('bid_amount')
    timeline = d.get('timeline','').strip()
    if not all([job_id, cover_letter, bid_amount, timeline]):
        return jsonify({'error': 'All fields are required'}), 400
    job = query_db("SELECT * FROM jobs WHERE id=? AND status='open'", [job_id], one=True)
    if not job:
        return jsonify({'error': 'Job not found or closed'}), 404
    if query_db("SELECT id FROM proposals WHERE job_id=? AND freelancer_id=?", [job_id, user['id']], one=True):
        return jsonify({'error': 'You have already applied to this job'}), 409
    pid = mutate_db("INSERT INTO proposals (job_id,freelancer_id,cover_letter,bid_amount,timeline) VALUES (?,?,?,?,?)",
                    [job_id, user['id'], cover_letter, bid_amount, timeline])
    notify(job['client_id'], f"New proposal from {user['username']} for '{job['title']}'", 'info')
    return jsonify({'message': 'Proposal submitted successfully', 'proposal_id': pid}), 201

@app.route('/api/proposals/<int:proposal_id>/accept', methods=['POST'])
@login_required
def api_accept_proposal(proposal_id):
    user = current_user()
    proposal = query_db("SELECT p.*, j.title as job_title, j.client_id FROM proposals p JOIN jobs j ON p.job_id=j.id WHERE p.id=?", [proposal_id], one=True)
    if not proposal or proposal['client_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    mutate_db("UPDATE proposals SET status='accepted' WHERE id=?", [proposal_id])
    mutate_db("UPDATE proposals SET status='rejected' WHERE job_id=? AND id!=?",
              [proposal['job_id'], proposal_id])
    mutate_db("UPDATE jobs SET status='in_progress' WHERE id=?", [proposal['job_id']])
    notify(proposal['freelancer_id'],
           f"🎉 Your proposal for '{proposal['job_title']}' was accepted!", 'success')
    return jsonify({'message': 'Proposal accepted'}), 200

@app.route('/api/proposals/<int:proposal_id>/reject', methods=['POST'])
@login_required
def api_reject_proposal(proposal_id):
    user = current_user()
    proposal = query_db("SELECT p.*, j.client_id, j.title as job_title FROM proposals p JOIN jobs j ON p.job_id=j.id WHERE p.id=?", [proposal_id], one=True)
    if not proposal or proposal['client_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    mutate_db("UPDATE proposals SET status='rejected' WHERE id=?", [proposal_id])
    notify(proposal['freelancer_id'],
           f"Your proposal for '{proposal['job_title']}' was not selected this time.", 'info')
    return jsonify({'message': 'Proposal rejected'}), 200

@app.route('/api/escrow/deposit', methods=['POST'])
@login_required
def api_escrow_deposit():
    user = current_user()
    if user['role'] != 'client':
        return jsonify({'error': 'Only clients can deposit'}), 403
    d = request.get_json()
    job_id = d.get('job_id')
    amount = d.get('amount')
    freelancer_id = d.get('freelancer_id')
    job = query_db("SELECT * FROM jobs WHERE id=? AND client_id=?", [job_id, user['id']], one=True)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if query_db("SELECT id FROM escrow WHERE job_id=? AND status='held'", [job_id], one=True):
        return jsonify({'error': 'Escrow already active for this job'}), 409
    try:
        amount = float(amount)
    except:
        return jsonify({'error': 'Invalid amount'}), 400
    if user['balance'] < amount:
        return jsonify({'error': f'Insufficient balance. Your balance: ${user["balance"]:.2f}'}), 400
    mutate_db("UPDATE users SET balance=balance-? WHERE id=?", [amount, user['id']])
    eid = mutate_db("INSERT INTO escrow (job_id,client_id,freelancer_id,amount) VALUES (?,?,?,?)",
                    [job_id, user['id'], freelancer_id, amount])
    if freelancer_id:
        notify(freelancer_id, f"💰 ${amount:.2f} has been placed in escrow for '{job['title']}'", 'success')
    return jsonify({'message': f'${amount:.2f} deposited into escrow', 'escrow_id': eid}), 201

@app.route('/api/escrow/<int:escrow_id>/release', methods=['POST'])
@login_required
def api_escrow_release(escrow_id):
    user = current_user()
    escrow = query_db("SELECT e.*, j.title as job_title FROM escrow e JOIN jobs j ON e.job_id=j.id WHERE e.id=?", [escrow_id], one=True)
    if not escrow or escrow['client_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    if escrow['status'] != 'held':
        return jsonify({'error': 'Escrow already released or cancelled'}), 400
    mutate_db("UPDATE escrow SET status='released', released_at=CURRENT_TIMESTAMP WHERE id=?", [escrow_id])
    if escrow['freelancer_id']:
        mutate_db("UPDATE users SET balance=balance+? WHERE id=?", [escrow['amount'], escrow['freelancer_id']])
        notify(escrow['freelancer_id'],
               f"💸 ${escrow['amount']:.2f} released to your account for '{escrow['job_title']}'!", 'success')
    mutate_db("UPDATE jobs SET status='completed' WHERE id=?", [escrow['job_id']])
    return jsonify({'message': f'${escrow["amount"]:.2f} released to freelancer'}), 200

@app.route('/api/escrow/<int:escrow_id>/refund', methods=['POST'])
@login_required
def api_escrow_refund(escrow_id):
    user = current_user()
    escrow = query_db("SELECT * FROM escrow WHERE id=?", [escrow_id], one=True)
    if not escrow or escrow['client_id'] != user['id']:
        return jsonify({'error': 'Not authorized'}), 403
    if escrow['status'] != 'held':
        return jsonify({'error': 'Escrow is not in held status'}), 400
    mutate_db("UPDATE escrow SET status='refunded', released_at=CURRENT_TIMESTAMP WHERE id=?", [escrow_id])
    mutate_db("UPDATE users SET balance=balance+? WHERE id=?", [escrow['amount'], user['id']])
    return jsonify({'message': f'${escrow["amount"]:.2f} refunded to your account'}), 200

@app.route('/api/ai/generate-proposal', methods=['POST'])
@login_required
def api_generate_proposal():
    user = current_user()
    if user['role'] != 'freelancer':
        return jsonify({'error': 'Only freelancers can use this'}), 403
    d = request.get_json()
    job_id = d.get('job_id')
    job = query_db("SELECT * FROM jobs WHERE id=?", [job_id], one=True)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    proposal_text = generate_proposal(
        job['title'], job['description'], job['skills_required'],
        user['username'], user['skills'] or ''
    )
    return jsonify({'proposal': proposal_text}), 200

@app.route('/api/ai/match-freelancers/<int:job_id>', methods=['GET'])
@login_required
def api_match_freelancers(job_id):
    user = current_user()
    job = query_db("SELECT * FROM jobs WHERE id=? AND client_id=?", [job_id, user['id']], one=True)
    if not job:
        return jsonify({'error': 'Not authorized'}), 403
    freelancers = query_db("SELECT * FROM users WHERE role='freelancer'")
    matched = match_freelancers(job['skills_required'], [dict(f) for f in freelancers])
    return jsonify({'matches': matched}), 200

@app.route('/api/profile', methods=['PUT'])
@login_required
def api_update_profile():
    user = current_user()
    d = request.get_json()
    bio = d.get('bio','').strip()
    skills = d.get('skills','').strip()
    mutate_db("UPDATE users SET bio=?, skills=? WHERE id=?", [bio, skills, user['id']])
    return jsonify({'message': 'Profile updated successfully'}), 200

@app.route('/api/notifications', methods=['GET'])
@login_required
def api_get_notifications():
    user = current_user()
    notifs = query_db("""
        SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 20
    """, [user['id']])
    return jsonify({'notifications': [dict(n) for n in notifs]}), 200

@app.route('/api/notifications/read', methods=['POST'])
@login_required
def api_mark_notifications_read():
    user = current_user()
    mutate_db("UPDATE notifications SET is_read=1 WHERE user_id=?", [user['id']])
    return jsonify({'message': 'All marked as read'}), 200

@app.route('/api/stats', methods=['GET'])
@login_required
def api_stats():
    user = current_user()
    if user['role'] == 'client':
        total_jobs = query_db("SELECT COUNT(*) as c FROM jobs WHERE client_id=?", [user['id']], one=True)['c']
        active_jobs = query_db("SELECT COUNT(*) as c FROM jobs WHERE client_id=? AND status='open'", [user['id']], one=True)['c']
        total_proposals = query_db("SELECT COUNT(*) as c FROM proposals p JOIN jobs j ON p.job_id=j.id WHERE j.client_id=?", [user['id']], one=True)['c']
        escrow_held = query_db("SELECT COALESCE(SUM(amount),0) as s FROM escrow WHERE client_id=? AND status='held'", [user['id']], one=True)['s']
        return jsonify({'total_jobs': total_jobs, 'active_jobs': active_jobs, 'total_proposals': total_proposals, 'escrow_held': escrow_held, 'balance': user['balance']})
    else:
        applied = query_db("SELECT COUNT(*) as c FROM proposals WHERE freelancer_id=?", [user['id']], one=True)['c']
        accepted = query_db("SELECT COUNT(*) as c FROM proposals WHERE freelancer_id=? AND status='accepted'", [user['id']], one=True)['c']
        available = query_db("SELECT COUNT(*) as c FROM jobs WHERE status='open'", one=True)['c']
        return jsonify({'applied': applied, 'accepted': accepted, 'available_jobs': available, 'balance': user['balance']})

if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)