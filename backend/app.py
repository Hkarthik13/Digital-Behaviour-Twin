import os
import json
import threading
import platform
import hashlib
import secrets
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt

from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS
from pymongo import MongoClient
import pandas as pd
import numpy as np
from werkzeug.security import generate_password_hash, check_password_hash
from flask_jwt_extended import (
    JWTManager, create_access_token, create_refresh_token,
    jwt_required, get_jwt_identity
)
from datetime import timedelta, datetime
from functools import wraps
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

from services.report_service import generate_ai_feedback
from services.email_service import send_alert_email
from ml.rule_engine import rule_engine
from ml.predictor import predict_all
from ml.feature_builder import build_features
from ml.isolation_model import predict_anomaly, train_isolation_model
from ml.insight_engine import get_best_focus_hours
from routes.activity_routes import activity_bp
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.pagesizes import A4

app = Flask(__name__) 
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRACKER_TOKEN_FILE = os.path.normpath(os.path.join(BASE_DIR, "..", "tracker", "token.json"))
LEGACY_ACTIVE_TOKEN_FILE = os.path.join(BASE_DIR, "active_token.txt")
JWT_SECRET_FILE = os.path.join(BASE_DIR, ".jwt_secret")
DEFAULT_JWT_SECRET_PLACEHOLDER = "replace-me-with-a-strong-local-secret"
UTC_TZ = ZoneInfo("UTC")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata").strip() or "Asia/Kolkata"
try:
    APP_TZ = ZoneInfo(APP_TIMEZONE)
except Exception:
    APP_TZ = UTC_TZ


def get_admin_emails():
    raw = os.getenv("ADMIN_EMAILS", "")
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def is_admin_email(email: str) -> bool:
    return (email or "").strip().lower() in get_admin_emails()


def as_local_time(dt_value):
    if not isinstance(dt_value, datetime):
        return dt_value
    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=UTC_TZ)
    return dt_value.astimezone(APP_TZ)


def format_local_time(dt_value, fmt="%Y-%m-%d %H:%M:%S"):
    local_dt = as_local_time(dt_value)
    if not isinstance(local_dt, datetime):
        return None
    return local_dt.strftime(fmt)


def local_now():
    return datetime.now(UTC_TZ).astimezone(APP_TZ)


def local_day_start_utc_naive(reference_dt=None):
    local_dt = as_local_time(reference_dt) if reference_dt else local_now()
    start_local = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC_TZ).replace(tzinfo=None)


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        email = get_jwt_identity()
        user = users.find_one({"email": email}) or {}
        if not (user.get("is_admin", False) or is_admin_email(email)):
            return jsonify({"msg": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper

def load_jwt_secret() -> str:
    configured_secret = os.getenv("JWT_SECRET_KEY", "").strip()
    if configured_secret and configured_secret != DEFAULT_JWT_SECRET_PLACEHOLDER:
        return configured_secret

    if os.path.exists(JWT_SECRET_FILE):
        try:
            with open(JWT_SECRET_FILE, "r", encoding="utf-8") as f:
                file_secret = f.read().strip()
            if file_secret:
                print(f"[Auth] Using persistent local JWT secret from {JWT_SECRET_FILE}.")
                return file_secret
        except Exception as e:
            print(f"[Auth] Failed reading {JWT_SECRET_FILE}: {e}")

    generated_secret = secrets.token_urlsafe(64)
    try:
        with open(JWT_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(generated_secret)
        print(f"[Auth] Generated persistent local JWT secret at {JWT_SECRET_FILE}.")
    except Exception as e:
        print(f"[Auth] Failed writing {JWT_SECRET_FILE}: {e}")
        print("[Auth] Falling back to an ephemeral JWT secret for this run.")
    return generated_secret


jwt_secret = load_jwt_secret()

app.config["JWT_SECRET_KEY"] = jwt_secret
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(days=7)
app.config["JWT_REFRESH_TOKEN_EXPIRES"] = timedelta(days=30)
jwt = JWTManager(app)


@jwt.invalid_token_loader
def invalid_token_callback(reason):
    return jsonify({"msg": "Invalid token", "detail": reason}), 401


@jwt.expired_token_loader
def expired_token_callback(jwt_header, jwt_payload):
    return jsonify({"msg": "Token expired"}), 401


@jwt.unauthorized_loader
def unauthorized_callback(reason):
    return jsonify({"msg": "Missing authorization", "detail": reason}), 401


@jwt.needs_fresh_token_loader
def fresh_token_callback(jwt_header, jwt_payload):
    return jsonify({"msg": "Fresh token required"}), 401


@jwt.revoked_token_loader
def revoked_token_callback(jwt_header, jwt_payload):
    return jsonify({"msg": "Token revoked"}), 401

app.register_blueprint(activity_bp)

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "digital_behaviour_twin")

client = MongoClient(MONGODB_URI)
db = client[MONGODB_DB_NAME]

users           = db["users"]
activities      = db["activities"]
twin            = db["behaviour_twin"]
alerts          = db["alerts"]
risk_scores     = db["risk_scores"]
ml_states       = db["ml_states"]
focus_sessions  = db["focus_sessions"]
goals           = db["goals"]
devices         = db["devices"]
device_sessions = db["device_sessions"]
sync_events     = db["sync_events"]
block_configs   = db["block_configs"]

scheduler = BackgroundScheduler()
scheduler.add_job(train_isolation_model, "interval", hours=24)

def mark_offline_devices():
    cutoff = datetime.now() - timedelta(minutes=3)
    devices.update_many(
        {"last_heartbeat": {"$lt": cutoff}, "is_online": True},
        {"$set": {"is_online": False}}
    )
scheduler.add_job(mark_offline_devices, "interval", minutes=2)
scheduler.start()


def persist_tracker_auth_state(refresh_token: str):
    try:
        os.makedirs(os.path.dirname(TRACKER_TOKEN_FILE), exist_ok=True)
        with open(TRACKER_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"refresh_token": refresh_token}, f)
        if os.path.exists(LEGACY_ACTIVE_TOKEN_FILE):
            os.remove(LEGACY_ACTIVE_TOKEN_FILE)
    except Exception as e:
        print("Tracker auth sync error:", e)


def clear_tracker_auth_state():
    for path in (TRACKER_TOKEN_FILE, LEGACY_ACTIVE_TOKEN_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception as e:
            print(f"Tracker auth cleanup error for {path}: {e}")


def send_telegram_alert(message: str, chat_id: str = None) -> bool:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    target_chat = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    if not bot_token or not target_chat:
        print("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env - skipping")
        return False

    try:
        import requests as req
        response = req.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": target_chat,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
        if response.status_code == 200 and response.json().get("ok"):
            print(f"[Telegram] Sent to {target_chat}: {message[:60]}...")
            return True
        print(f"[Telegram] Failed ({response.status_code}): {response.text[:120]}")
        return False
    except Exception as e:
        print(f"[Telegram] Exception: {e}")
        return False


def send_telegram_async(message: str, chat_id: str = None):
    threading.Thread(
        target=send_telegram_alert,
        args=(message, chat_id),
        daemon=True
    ).start()


# Backward-compatible alias so existing alert call sites keep working
send_whatsapp_alert = send_telegram_alert
send_whatsapp_async = send_telegram_async


# ─────────────────────────────────────────────
# 📱 WHATSAPP ALERT  (CallMeBot — FREE)
# ─────────────────────────────────────────────
def send_whatsapp_alert(message: str, phone: str = None) -> bool:
    phone_num = phone or os.getenv("WHATSAPP_PHONE", "")
    api_key   = os.getenv("WHATSAPP_API_KEY", "")

    if not phone_num or not api_key:
        print("[WhatsApp] WHATSAPP_PHONE or WHATSAPP_API_KEY not set in .env — skipping")
        return False

    phone_num = phone_num.strip().replace(" ", "").replace("-", "")
    if not phone_num.startswith("+"):
        print(f"[WhatsApp] Phone must start with + and country code, got: {phone_num}")
        return False

    try:
        import requests as req
        url    = "https://api.callmebot.com/whatsapp.php"
        params = {
            "phone":  phone_num,
            "text":   message,
            "apikey": api_key
        }
        resp = req.get(url, params=params, timeout=15)
        if resp.status_code == 200 and "Message queued" in resp.text:
            print(f"[WhatsApp] ✅ Sent to {phone_num}: {message[:60]}...")
            return True
        else:
            print(f"[WhatsApp] ❌ Failed ({resp.status_code}): {resp.text[:100]}")
            return False
    except Exception as e:
        print(f"[WhatsApp] Exception: {e}")
        return False


def send_whatsapp_async(message: str, phone: str = None):
    threading.Thread(
        target=send_whatsapp_alert,
        args=(message, phone),
        daemon=True
    ).start()


# Force all existing alert flows onto Telegram without rewriting every call site
send_whatsapp_alert = send_telegram_alert
send_whatsapp_async = send_telegram_async


# ─────────────────────────────────────────────
# WHATSAPP ALERT ROUTES
# ─────────────────────────────────────────────
@app.route("/telegram/test", methods=["POST"])
@jwt_required()
def test_telegram():
    email = get_jwt_identity()
    data = request.get_json() or {}
    chat_id = (data.get("chat_id", "") or "").strip()
    if chat_id:
        users.update_one({"email": email}, {"$set": {
            "telegram_chat_id": chat_id,
            "whatsapp_phone": chat_id
        }})

    user = users.find_one({"email": email}) or {}
    target_chat = chat_id or user.get("telegram_chat_id", "") or os.getenv("TELEGRAM_CHAT_ID", "")
    if not target_chat:
        return jsonify({"error": "No Telegram chat ID configured."}), 400

    msg = (
        "Digital Behaviour Twin - Test Alert\n\n"
        "Telegram alerts are working.\n"
        f"Account: {email}\n"
        f"Time: {datetime.now().strftime('%d %b %Y, %H:%M')}"
    )
    success = send_telegram_alert(msg, target_chat)
    if success:
        return jsonify({"msg": "Test Telegram message sent. Check your bot chat.", "chat_id": target_chat})
    return jsonify({"error": "Failed to send. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"}), 500


@app.route("/telegram/settings", methods=["GET", "POST"])
@jwt_required()
def telegram_settings():
    email = get_jwt_identity()

    if request.method == "POST":
        data = request.get_json() or {}
        chat_id = (data.get("chat_id", "") or "").strip()
        alerts_enabled = data.get("alerts_enabled", True)
        risk_threshold = data.get("risk_threshold", 70)
        users.update_one(
            {"email": email},
            {"$set": {
                "telegram_chat_id": chat_id,
                "telegram_alerts": alerts_enabled,
                "telegram_risk_threshold": risk_threshold,
                "whatsapp_phone": chat_id,
                "whatsapp_alerts": alerts_enabled,
                "whatsapp_risk_threshold": risk_threshold
            }},
            upsert=False
        )
        return jsonify({"msg": "Telegram settings saved!"})

    user = users.find_one({"email": email}) or {}
    return jsonify({
        "chat_id": user.get("telegram_chat_id", ""),
        "alerts_enabled": user.get("telegram_alerts", True),
        "risk_threshold": user.get("telegram_risk_threshold", 70),
        "global_chat_id_set": bool(os.getenv("TELEGRAM_CHAT_ID"))
    })


@app.route("/whatsapp/test", methods=["POST"])
@jwt_required()
def test_whatsapp():
    email = get_jwt_identity()
    data  = request.get_json()
    phone = data.get("phone", "")
    if phone:
        users.update_one({"email": email}, {"$set": {"whatsapp_phone": phone}})
    
    user = users.find_one({"email": email})
    phone_to_use = phone or user.get("whatsapp_phone", "") or os.getenv("WHATSAPP_PHONE", "")
    
    if not phone_to_use:
        return jsonify({"error": "No WhatsApp phone number configured."}), 400
    
    msg = (
        f"🤖 *Digital Behaviour Twin* - Test Alert\n\n"
        f"✅ WhatsApp alerts are working!\n"
        f"📧 Account: {email}\n"
        f"🕐 Time: {datetime.now().strftime('%d %b %Y, %H:%M')}"
    )
    success = send_whatsapp_alert(msg, phone_to_use)
    
    if success:
        return jsonify({"msg": "✅ Test WhatsApp sent! Check your phone.", "phone": phone_to_use})
    else:
        return jsonify({"error": "Failed to send. Check WHATSAPP_API_KEY in .env"}), 500


@app.route("/whatsapp/settings", methods=["GET", "POST"])
@jwt_required()
def whatsapp_settings():
    email = get_jwt_identity()
    
    if request.method == "POST":
        data  = request.get_json()
        phone = data.get("phone", "").strip()
        alerts_enabled = data.get("alerts_enabled", True)
        risk_threshold = data.get("risk_threshold", 70)
        
        users.update_one(
            {"email": email},
            {"$set": {
                "whatsapp_phone":   phone,
                "whatsapp_alerts":  alerts_enabled,
                "whatsapp_risk_threshold": risk_threshold
            }},
            upsert=False
        )
        return jsonify({"msg": "WhatsApp settings saved!"})
    
    user = users.find_one({"email": email}) or {}
    return jsonify({
        "phone":           user.get("whatsapp_phone", ""),
        "alerts_enabled":  user.get("whatsapp_alerts", True),
        "risk_threshold":  user.get("whatsapp_risk_threshold", 70),
        "global_phone_set": bool(os.getenv("WHATSAPP_PHONE"))
    })


# ─────────────────────────────────────────────
# FREE AI HELPER  (Groq — Llama 3.3 70B, free tier)
# ─────────────────────────────────────────────
def call_free_ai(system_prompt: str, messages: list, max_tokens: int = 500) -> str:
    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        try:
            import requests as req
            payload = {
                "model": "llama-3.3-70b-versatile",
                "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system_prompt}] + messages
            }
            resp = req.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json=payload, timeout=20
            )
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[Groq Exception] {e}")

    last_user_msg = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_msg = m["content"].lower()
            break

    prod_mins, dist_mins, risk, focus_lvl = 0, 0, 0, "Unknown"
    for line in system_prompt.split("\n"):
        if "productive time" in line.lower():
            try: prod_mins = int(''.join(filter(str.isdigit, line.split(":")[-1])))
            except: pass
        if "distracted time" in line.lower():
            try: dist_mins = int(''.join(filter(str.isdigit, line.split(":")[-1])))
            except: pass
        if "risk score" in line.lower():
            try: risk = int(''.join(filter(str.isdigit, line.split(":")[-1].split("/")[0])))
            except: pass
        if "focus level" in line.lower():
            focus_lvl = line.split(":")[-1].strip()

    if any(w in last_user_msg for w in ["how am i", "doing", "status"]):
        if risk > 60:
            return (f"⚠️ Your risk score is {risk}/100 — that's quite high! You've been productive for "
                    f"{prod_mins} mins but distracted for {dist_mins} mins today. "
                    f"Close those distracting tabs and get back on track. You can do this! 💪")
        elif prod_mins > dist_mins:
            return (f"🎯 You're doing great! {prod_mins} mins of focused work vs {dist_mins} mins distracted. "
                    f"Risk score: {risk}/100. Keep this momentum going!")
        else:
            return (f"📊 Today: {prod_mins} mins productive, {dist_mins} mins distracted. "
                    f"Your focus level is {focus_lvl}. Try a 25-minute Pomodoro session to boost your score!")

    if any(w in last_user_msg for w in ["plan", "study", "schedule"]):
        return (f"📅 Here's a 2-hour plan:\n\n• 0:00–0:25 → Deep work block\n• 0:25–0:30 → Break ☕\n"
                f"• 0:30–0:55 → Second focus block\n• 0:55–1:00 → Stretch 💧\n"
                f"• 1:00–1:25 → Review\n• 1:25–1:30 → Break\n• 1:30–2:00 → Final sprint 🚀\n\n"
                f"Risk: {risk}/100 — use Pomodoro timer!")

    return (f"🤖 Study Buddy here! {prod_mins} mins productive, {dist_mins} mins distracted (risk: {risk}/100).\n\n"
            f"Ask: 'How am I doing?', 'Give me a study plan', or 'Motivate me'!")


# ─────────────────────────────────────────────
# OCR VERIFICATION
# ─────────────────────────────────────────────
PRODUCTIVE_KEYWORDS = [
    "def ", "class ", "import ", "function", "return", "const ", "var ",
    "print(", "console.log", "SELECT", "INSERT", "git ",
    "abstract", "introduction", "methodology", "references", "theorem",
    "hypothesis", "conclusion", "chapter", "equation",
    "paragraph", "heading", "document", "report", "summary",
]
DISTRACTED_KEYWORDS = [
    "instagram", "facebook", "twitter", "tiktok", "netflix",
    "youtube", "reels", "subscribe", "followers", "trending",
    "memes", "shopping", "cart", "checkout",
]

def classify_ocr_text(text):
    if not text or len(text.strip()) < 20:
        return "idle"
    text_lower = text.lower()
    prod_hits = sum(1 for k in PRODUCTIVE_KEYWORDS if k.lower() in text_lower)
    dist_hits = sum(1 for k in DISTRACTED_KEYWORDS if k.lower() in text_lower)
    if prod_hits == 0 and dist_hits == 0:
        return "idle"
    return "distracted" if dist_hits > prod_hits else "productive"

@app.route("/activity/log-ocr", methods=["POST"])
@jwt_required()
def log_ocr_activity():
    email    = get_jwt_identity()
    data     = request.get_json()
    ocr_text = data.get("ocr_text", "")
    app_name = data.get("app", "Unknown")
    duration = data.get("duration", 30)
    ocr_result = classify_ocr_text(ocr_text)
    word_count = len(ocr_text.split())
    db["ocr_logs"].insert_one({
        "email": email, "app": app_name, "ocr_result": ocr_result,
        "word_count": word_count, "timestamp": datetime.now(), "duration": duration,
    })
    if ocr_result == "productive":
        bonus = int(duration * 0.2)
        twin.update_one({"email": email},
            {"$inc": {"productive_time": duration + bonus}, "$set": {"last_updated": datetime.now()}},
            upsert=True)
    elif ocr_result == "distracted":
        twin.update_one({"email": email},
            {"$inc": {"distracting_time": duration}, "$set": {"last_updated": datetime.now()}},
            upsert=True)
    return jsonify({"ocr_result": ocr_result, "word_count": word_count,
                    "app": app_name, "msg": f"Screen verified as: {ocr_result}"}), 200


@app.route("/twin/ocr-stats", methods=["GET"])
@jwt_required()
def ocr_stats():
    email = get_jwt_identity()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logs  = list(db["ocr_logs"].find({"email": email, "timestamp": {"$gte": today}}))
    total      = len(logs)
    productive = sum(1 for l in logs if l["ocr_result"] == "productive")
    distracted = sum(1 for l in logs if l["ocr_result"] == "distracted")
    idle       = sum(1 for l in logs if l["ocr_result"] == "idle")
    verified_pct = round((productive / total) * 100) if total > 0 else 0
    return jsonify({"total_scans": total, "verified_work": productive,
                    "verified_dist": distracted, "idle_scans": idle, "verified_pct": verified_pct})


# ─────────────────────────────────────────────
# APP CLASSIFICATION
# ─────────────────────────────────────────────
APP_CLASSIFY_FILE = "app_classifications.json"
DEFAULT_APP_CLASSIFICATIONS = {
    "productive": ["code", "visual studio", "pycharm", "docs", "github", "notion", "excel", "word", "jupyter"],
    "distracting": ["instagram", "youtube", "facebook", "twitter", "netflix", "reddit", "tiktok", "twitch"]
}

def load_app_classifications():
    if os.path.exists(APP_CLASSIFY_FILE):
        try:
            with open(APP_CLASSIFY_FILE, "r") as f:
                return json.load(f)
        except: pass
    return DEFAULT_APP_CLASSIFICATIONS

def save_app_classifications(data):
    with open(APP_CLASSIFY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def classify_app(app_name):
    classifications = load_app_classifications()
    app_lower = app_name.lower()
    for keyword in classifications.get("productive", []):
        if keyword in app_lower: return "productive"
    for keyword in classifications.get("distracting", []):
        if keyword in app_lower: return "distracting"
    return "neutral"

@app.route("/apps/classifications", methods=["GET"])
@jwt_required()
def get_classifications():
    return jsonify(load_app_classifications())

@app.route("/apps/classify", methods=["POST"])
@jwt_required()
def set_app_classification():
    data     = request.get_json()
    app_name = data.get("app", "").lower().strip()
    category = data.get("category", "neutral")
    if not app_name:
        return jsonify({"error": "App name required"}), 400
    classifications = load_app_classifications()
    for cat in ["productive", "distracting"]:
        if app_name in classifications.get(cat, []):
            classifications[cat].remove(app_name)
    if category in ["productive", "distracting"]:
        if app_name not in classifications[category]:
            classifications[category].append(app_name)
    save_app_classifications(classifications)
    return jsonify({"msg": f"'{app_name}' marked as {category}", "classifications": classifications})

@app.route("/apps/recent", methods=["GET"])
@jwt_required()
def recent_apps():
    email    = get_jwt_identity()
    pipeline = [
        {"$match": {"email": email}},
        {"$group": {"_id": "$app", "type": {"$last": "$type"}, "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20}
    ]
    apps = list(activities.aggregate(pipeline))
    return jsonify([{"app": a["_id"], "type": a["type"], "count": a["count"]} for a in apps])


# ─────────────────────────────────────────────
# MULTI-DEVICE SYNC
# ─────────────────────────────────────────────
def get_device_icon(device_type: str) -> str:
    icons = {
        "windows": "💻", "mac": "🍎", "linux": "🐧",
        "android": "📱", "ios": "📱", "web": "🌐", "unknown": "🖥️"
    }
    return icons.get(device_type.lower(), "🖥️")


@app.route("/devices/register", methods=["POST"])
@jwt_required()
def register_device():
    email = get_jwt_identity()
    data  = request.get_json()
    device_id   = data.get("device_id", "").strip()
    device_name = data.get("device_name", "Unknown Device").strip()
    device_type = data.get("device_type", "unknown").strip().lower()
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    existing = devices.find_one({"email": email, "device_id": device_id})
    now = datetime.now()
    if existing:
        devices.update_one(
            {"email": email, "device_id": device_id},
            {"$set": {"device_name": device_name, "device_type": device_type,
                      "is_online": True, "last_heartbeat": now, "last_seen": now}}
        )
        return jsonify({"msg": "Device updated", "device_id": device_id,
                        "device_name": device_name, "is_new": False})
    else:
        device_count = devices.count_documents({"email": email})
        if device_count >= 10:
            return jsonify({"error": "Maximum 10 devices allowed per account"}), 400
        devices.insert_one({
            "email": email, "device_id": device_id, "device_name": device_name,
            "device_type": device_type, "icon": get_device_icon(device_type),
            "is_online": True, "registered_at": now, "last_heartbeat": now,
            "last_seen": now, "total_productive_time": 0, "total_distracting_time": 0,
            "is_primary": device_count == 0
        })
        sync_events.insert_one({"email": email, "event": "device_registered",
                                 "device_id": device_id, "device_name": device_name,
                                 "timestamp": now})
        return jsonify({"msg": f"Device '{device_name}' registered successfully!",
                        "device_id": device_id, "device_name": device_name,
                        "is_new": True, "is_primary": device_count == 0})


@app.route("/devices/heartbeat", methods=["POST"])
@jwt_required()
def device_heartbeat():
    email = get_jwt_identity()
    data  = request.get_json()
    device_id = data.get("device_id", "")
    productive_delta  = data.get("productive_delta", 0)
    distracting_delta = data.get("distracting_delta", 0)
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    now = datetime.now()
    update_data = {"is_online": True, "last_heartbeat": now, "last_seen": now}
    inc_data = {}
    if productive_delta > 0:  inc_data["total_productive_time"]  = productive_delta
    if distracting_delta > 0: inc_data["total_distracting_time"] = distracting_delta
    update_query = {"$set": update_data}
    if inc_data: update_query["$inc"] = inc_data
    result = devices.update_one({"email": email, "device_id": device_id}, update_query)
    if result.matched_count == 0:
        return jsonify({"error": "Device not registered. Call /devices/register first."}), 404
    pending = list(db["device_notifications"].find(
        {"email": email, "device_id": device_id, "delivered": False}, {"_id": 0}))
    db["device_notifications"].update_many(
        {"email": email, "device_id": device_id, "delivered": False},
        {"$set": {"delivered": True}})
    return jsonify({"status": "ok", "server_time": local_now().isoformat(),
                    "pending_notifications": pending})


@app.route("/devices/list", methods=["GET"])
@jwt_required()
def list_devices():
    email = get_jwt_identity()
    device_list = list(devices.find({"email": email}, {"_id": 0}))
    today = local_day_start_utc_naive()
    for d in device_list:
        for key in ["registered_at", "last_heartbeat", "last_seen"]:
            if key in d and isinstance(d[key], datetime):
                d[key] = format_local_time(d[key], "%Y-%m-%d %H:%M:%S")
        today_logs = list(activities.find({"email": email,
                                           "device_id": d.get("device_id"),
                                           "timestamp": {"$gte": today}}))
        d["today_productive"]  = sum(l["duration"] for l in today_logs if l["type"] == "productive")
        d["today_distracting"] = sum(l["duration"] for l in today_logs if l["type"] == "distracting")
        d["today_logs_count"]  = len(today_logs)
    return jsonify({"devices": device_list, "total": len(device_list)})


@app.route("/devices/remove", methods=["POST"])
@jwt_required()
def remove_device():
    email     = get_jwt_identity()
    data      = request.get_json()
    device_id = data.get("device_id", "")
    if not device_id:
        return jsonify({"error": "device_id required"}), 400
    result = devices.delete_one({"email": email, "device_id": device_id})
    if result.deleted_count == 0:
        return jsonify({"error": "Device not found"}), 404
    sync_events.insert_one({"email": email, "event": "device_removed",
                             "device_id": device_id, "timestamp": datetime.now()})
    return jsonify({"msg": "Device removed successfully"})


@app.route("/devices/rename", methods=["POST"])
@jwt_required()
def rename_device():
    email     = get_jwt_identity()
    data      = request.get_json()
    device_id = data.get("device_id", "")
    new_name  = data.get("new_name", "").strip()
    if not device_id or not new_name:
        return jsonify({"error": "device_id and new_name required"}), 400
    result = devices.update_one({"email": email, "device_id": device_id},
                                 {"$set": {"device_name": new_name}})
    if result.matched_count == 0:
        return jsonify({"error": "Device not found"}), 404
    return jsonify({"msg": f"Renamed to '{new_name}'"})


@app.route("/devices/sync-summary", methods=["GET"])
@jwt_required()
def sync_summary():
    email = get_jwt_identity()
    today = local_day_start_utc_naive()
    device_list = list(devices.find({"email": email}, {"_id": 0}))
    all_logs    = list(activities.find({"email": email, "timestamp": {"$gte": today}}))
    total_productive  = sum(l["duration"] for l in all_logs if l["type"] == "productive")
    total_distracting = sum(l["duration"] for l in all_logs if l["type"] == "distracting")
    per_device = {}
    for log in all_logs:
        did = log.get("device_id", "unknown")
        if did not in per_device:
            per_device[did] = {"productive": 0, "distracting": 0, "neutral": 0}
        t = log.get("type", "neutral")
        if t in per_device[did]:
            per_device[did][t] += log["duration"]
    device_stats = []
    for d in device_list:
        did   = d.get("device_id", "")
        stats = per_device.get(did, {"productive": 0, "distracting": 0, "neutral": 0})
        device_stats.append({
            "device_id": did, "device_name": d.get("device_name", "Unknown"),
            "device_type": d.get("device_type", "unknown"), "icon": d.get("icon", "🖥️"),
            "is_online": d.get("is_online", False),
            "productive": stats["productive"], "distracting": stats["distracting"],
            "contribution_pct": round(
                (stats["productive"] / total_productive * 100) if total_productive > 0 else 0, 1)
        })
    online_count = sum(1 for d in device_list if d.get("is_online"))
    return jsonify({"total_productive": total_productive, "total_distracting": total_distracting,
                    "online_devices": online_count, "total_devices": len(device_list),
                    "device_stats": device_stats, "last_sync": local_now().strftime("%H:%M:%S")})


@app.route("/devices/sync-status", methods=["GET"])
@jwt_required()
def sync_status():
    email = get_jwt_identity()
    total  = devices.count_documents({"email": email})
    online = devices.count_documents({"email": email, "is_online": True})
    last_ev = sync_events.find_one({"email": email}, sort=[("timestamp", -1)])
    last_t  = format_local_time(last_ev["timestamp"], "%H:%M:%S") if last_ev else "Never"
    return jsonify({"online_devices": online, "total_devices": total,
                    "last_sync": last_t, "synced": online > 0})


@app.route("/devices/notify-all", methods=["POST"])
@jwt_required()
def notify_all_devices():
    email   = get_jwt_identity()
    data    = request.get_json()
    message = data.get("message", "")
    ntype   = data.get("type", "info")
    if not message:
        return jsonify({"error": "message required"}), 400
    online_devs = list(devices.find({"email": email, "is_online": True}))
    count = 0
    for d in online_devs:
        db["device_notifications"].insert_one({
            "email": email, "device_id": d["device_id"], "message": message,
            "type": ntype, "delivered": False, "created_at": datetime.now()
        })
        count += 1
    return jsonify({"msg": f"Notification sent to {count} online device(s)"})


# ─────────────────────────────────────────────
# TWIN UPDATE
# ─────────────────────────────────────────────
def update_twin(email, activity_type, duration, device_id=None):
    update_data = {"last_updated": datetime.now()}
    inc_data    = {}
    if activity_type == "productive":
        inc_data["productive_time"] = duration
    elif activity_type == "distracting":
        inc_data["distracting_time"] = duration
    update_query = {"$set": update_data}
    if inc_data:
        update_query["$inc"] = inc_data
    twin.update_one({"email": email}, update_query, upsert=True)

def calculate_risk_score(email):
    twin_data  = twin.find_one({"email": email})
    if not twin_data: return 0
    productive  = twin_data.get("productive_time", 0)
    distracting = twin_data.get("distracting_time", 0)
    total       = productive + distracting
    if total == 0: return 0
    base_risk   = (distracting / total) * 100
    last_24h    = datetime.now() - timedelta(hours=24)
    alert_count = alerts.count_documents({"email": email, "timestamp": {"$gte": last_24h}})
    alert_penalty = min(alert_count * 2, 15)
    risk = base_risk + alert_penalty
    if productive > (distracting * 2): risk = min(risk, 30)
    elif productive > distracting:     risk = min(risk, 49)
    risk = min(round(risk), 100)
    risk_scores.update_one(
        {"email": email},
        {"$set": {"email": email, "risk_score": risk, "last_updated": datetime.now()}},
        upsert=True)
    return risk

def check_distraction_alert(email):
    threshold_minutes = 20
    cooldown_minutes  = 20
    now          = datetime.now()
    window_start = now - timedelta(minutes=threshold_minutes)
    recent_logs  = list(activities.find({"email": email, "timestamp": {"$gte": window_start}}))
    total_distracting = sum(log["duration"] for log in recent_logs if log["type"] == "distracting")
    last_alert = alerts.find_one({"email": email, "reason": "Continuous Distraction",
                                  "timestamp": {"$gte": now - timedelta(minutes=cooldown_minutes)}})
    if (total_distracting / 60) >= threshold_minutes and not last_alert:
        alerts.insert_one({"email": email, "timestamp": now, "reason": "Continuous Distraction"})
        try: send_alert_email(email, "🚨 WAKE UP! You've been distracted for over 20 minutes. Get back to work!")
        except: pass
        
        user = users.find_one({"email": email}) or {}
        if user.get("whatsapp_alerts", True):
            twin_data  = twin.find_one({"email": email}) or {}
            prod_mins  = round(twin_data.get("productive_time", 0) / 60)
            dist_mins  = round(twin_data.get("distracting_time", 0) / 60)
            wa_msg = (
                f"🚨 *DISTRACTION ALERT*\n\n"
                f"You've been distracted for *20+ minutes* straight!\n\n"
                f"📊 Today's stats:\n"
                f"✅ Productive: {prod_mins} mins\n"
                f"❌ Distracted: {dist_mins} mins\n\n"
                f"💪 Close distracting apps and get back to work!"
            )
            send_whatsapp_async(wa_msg, user.get("whatsapp_phone"))
        return True
    return False

def detect_focus_session(email):
    recent_logs     = list(activities.find({"email": email}).sort("timestamp", -1).limit(320))
    productive_logs = 0
    allowed_neutral = 3
    for log in recent_logs:
        if log["type"] == "productive": productive_logs += 1
        elif log["type"] == "neutral" and allowed_neutral > 0:
            allowed_neutral -= 1
            productive_logs += 1
        else: break
    productive_minutes = (productive_logs * 5) / 60
    if productive_minutes < 25: return 0
    last_session = focus_sessions.find_one({"email": email}, sort=[("timestamp", -1)])
    if last_session and (datetime.now() - last_session["timestamp"]).seconds < 1800: return 0
    focus_sessions.insert_one({
        "email": email, "duration_minutes": round(productive_minutes, 2), "timestamp": datetime.now()
    })
    
    user = users.find_one({"email": email}) or {}
    if user.get("whatsapp_alerts", True):
        wa_msg = (
            f"🎯 *FOCUS SESSION COMPLETE!*\n\n"
            f"Amazing! You just completed a *{round(productive_minutes)} minute* deep focus session!\n\n"
            f"🏆 Keep up the great work!\n"
            f"⏰ Take a short break, then go again 💪"
        )
        send_whatsapp_async(wa_msg, user.get("whatsapp_phone"))
    
    return round(productive_minutes, 2)

def detect_app_switching(email):
    recent_logs = list(activities.find({"email": email}).sort("timestamp", -1).limit(6))
    if len(recent_logs) < 5: return False
    switches = sum(1 for i in range(len(recent_logs) - 1)
                   if recent_logs[i]["app"] != recent_logs[i+1]["app"])
    return switches >= 4


# ─────────────────────────────────────────────
# DAILY SUMMARY WHATSAPP
# ─────────────────────────────────────────────
def send_daily_summary_whatsapp(email: str):
    user = users.find_one({"email": email}) or {}
    if not user.get("whatsapp_alerts", True):
        return
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logs  = list(activities.find({"email": email, "timestamp": {"$gte": today}}))
    
    productive  = sum(l["duration"] for l in logs if l["type"] == "productive")
    distracting = sum(l["duration"] for l in logs if l["type"] == "distracting")
    prod_mins   = round(productive / 60)
    dist_mins   = round(distracting / 60)
    total       = productive + distracting
    focus_score = round((productive / total) * 100) if total > 0 else 0
    
    risk_data  = risk_scores.find_one({"email": email}) or {}
    risk       = risk_data.get("risk_score", 0)
    
    goal_doc   = goals.find_one({"email": email}) or {}
    goal_mins  = goal_doc.get("daily_goal", 240)
    goal_pct   = min(100, round((prod_mins / goal_mins) * 100)) if goal_mins > 0 else 0
    
    sessions   = focus_sessions.count_documents({"email": email, "timestamp": {"$gte": today}})
    
    emoji_score = "🔥" if focus_score >= 75 else "📈" if focus_score >= 50 else "📉"
    
    wa_msg = (
        f"📊 *Daily Summary - Digital Twin*\n"
        f"📅 {datetime.now().strftime('%d %b %Y')}\n\n"
        f"✅ Productive: *{prod_mins} mins*\n"
        f"❌ Distracted: *{dist_mins} mins*\n"
        f"{emoji_score} Focus Score: *{focus_score}%*\n"
        f"⚠️ Risk Level: *{risk}/100*\n"
        f"🎯 Goal Progress: *{goal_pct}%* ({prod_mins}/{goal_mins} mins)\n"
        f"🏆 Focus Sessions: *{sessions}*\n\n"
        f"{'Great day! Keep it up tomorrow! 🚀' if focus_score >= 70 else 'Tomorrow is a new chance to do better! 💪'}"
    )
    send_whatsapp_async(wa_msg, user.get("whatsapp_phone"))

scheduler.add_job(
    lambda: [send_daily_summary_whatsapp(u["email"])
             for u in users.find({"whatsapp_alerts": {"$ne": False}}, {"email": 1})],
    "cron", hour=21, minute=0
)


# ─────────────────────────────────────────────
# ML BACKGROUND PROCESSING
# ─────────────────────────────────────────────
def run_ml_in_background(email):
    try:
        ml_features    = build_features(email, activities)
        prediction     = predict_all(ml_features)
        focus_level    = prediction.get("focus_level", "Balanced")
        predicted_score = prediction.get("predicted_focus_score", 50)
        twin_data      = twin.find_one({"email": email})
        anomaly_result = predict_anomaly(
            twin_data.get("productive_time", 0) if twin_data else 0,
            twin_data.get("distracting_time", 0) if twin_data else 0
        )
        if anomaly_result == -1:
            alerts.insert_one({"email": email, "timestamp": datetime.now(),
                                "reason": "Anomaly detected in behaviour pattern"})
            user = users.find_one({"email": email}) or {}
            if user.get("whatsapp_alerts", True):
                wa_msg = (
                    f"🔍 *Behaviour Anomaly Detected*\n\n"
                    f"Your Digital Twin noticed an unusual pattern in your activity today.\n\n"
                    f"This might mean you're working differently than usual. "
                    f"Open the dashboard to see details."
                )
                send_whatsapp_async(wa_msg, user.get("whatsapp_phone"))
        
        ml_states.update_one(
            {"email": email},
            {"$set": {"email": email, "focus_level": focus_level,
                      "predicted_score": predicted_score, "last_updated": datetime.now()}},
            upsert=True)
    except Exception as e:
        print(f"[ML Background Error] {e}")


# ─────────────────────────────────────────────
# AUTH ROUTES
# ─────────────────────────────────────────────
@app.route("/")
def home():
    return render_template("dashboard.html")

@app.route("/login")
def login_page():
    return render_template("dashboard.html")

@app.route("/auth/register", methods=["POST"])
def register():
    data     = request.get_json()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"msg": "Email and password required"}), 400
    if users.find_one({"email": email}):
        return jsonify({"msg": "User already exists"}), 400
    hashed = generate_password_hash(password, method="pbkdf2:sha256")
    users.insert_one({
        "email": email,
        "password": hashed,
        "is_new_user": True,
        "is_admin": is_admin_email(email),
        "is_active": True,
        "whatsapp_alerts": True,
        "consent_given": False,
        "consent_version": None,
        "consent_accepted_at": None,
        "registered_at": datetime.now(),
    })
    return jsonify({"msg": "User registered successfully"})

@app.route("/auth/login", methods=["POST"])
def login():
    data     = request.get_json()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user     = users.find_one({"email": email})
    if not user:
        return jsonify({"msg": "No account found. Please register first."}), 401
    if user.get("is_active", True) is False:
        return jsonify({"msg": "This account has been disabled by admin."}), 403
    if not check_password_hash(user["password"], password):
        return jsonify({"msg": "Wrong password. Please try again."}), 401
    access_token = create_access_token(identity=email)
    refresh_token = create_refresh_token(identity=email)
    is_new = user.get("is_new_user", False)
    is_admin = user.get("is_admin", False) or is_admin_email(email)
    persist_tracker_auth_state(refresh_token)
    return jsonify({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "is_new_user": is_new,
        "consent_given": user.get("consent_given", False),
        "is_admin": is_admin
    })

@app.route("/auth/refresh", methods=["POST"])
@jwt_required(refresh=True)
def refresh_access_token():
    email = get_jwt_identity()
    access_token = create_access_token(identity=email)
    return jsonify({"access_token": access_token})

@app.route("/auth/logout", methods=["POST"])
def logout_backend():
    clear_tracker_auth_state()
    return jsonify({"msg": "Logged out"})

@app.route("/auth/setup", methods=["POST"])
@jwt_required()
def setup_user():
    email = get_jwt_identity()
    data  = request.get_json()
    consent_accepted = bool(data.get("consent_accepted", False))
    if not consent_accepted:
        return jsonify({"msg": "You must accept the consent form before continuing."}), 400
    goals.update_one(
        {"email": email},
        {"$set": {"email": email, "daily_goal": data.get("goal_minutes", 240),
                  "primary_focus": data.get("primary_focus", "Study")}},
        upsert=True)
    users.update_one({"email": email}, {"$set": {
        "is_new_user": False,
        "consent_given": True,
        "consent_version": "v1.0",
        "consent_accepted_at": datetime.now()
    }})
    
    whatsapp_phone = data.get("whatsapp_phone", "")
    if whatsapp_phone:
        users.update_one({"email": email}, {"$set": {"whatsapp_phone": whatsapp_phone}})
        wa_msg = (
            f"👋 *Welcome to Digital Behaviour Twin!*\n\n"
            f"🎯 Goal: {data.get('goal_minutes', 240)} mins/day\n"
            f"📚 Focus: {data.get('primary_focus', 'Study')}\n\n"
            f"Alerts are now active! I'll message you when:\n"
            f"🚨 You get distracted too long\n"
            f"🎉 You complete a focus session\n"
            f"📊 Daily summary at 9 PM\n\n"
            f"Let's crush your goals! 💪"
        )
        send_whatsapp_async(wa_msg, whatsapp_phone)
    
    return jsonify({"msg": "Setup complete"})

@app.route("/profile", methods=["GET"])
@jwt_required()
def profile():
    email  = get_jwt_identity()
    user   = users.find_one({"email": email})
    is_new = user.get("is_new_user", False) if user else False
    return jsonify({
        "user": email,
        "is_new_user": is_new,
        "consent_given": user.get("consent_given", False) if user else False,
        "is_admin": (user.get("is_admin", False) if user else False) or is_admin_email(email),
        "is_active": user.get("is_active", True) if user else False
    })


@app.route("/admin/summary", methods=["GET"])
@admin_required
def admin_summary():
    total_users = users.count_documents({})
    active_users = users.count_documents({"is_active": {"$ne": False}})
    admin_users = sum(
        1 for u in users.find({}, {"email": 1, "is_admin": 1})
        if u.get("is_admin", False) or is_admin_email(u.get("email", ""))
    )
    new_users = users.count_documents({"is_new_user": True})
    consented_users = users.count_documents({"consent_given": True})
    online_devices = devices.count_documents({"is_online": True})
    today = local_day_start_utc_naive()
    today_logs = activities.count_documents({"timestamp": {"$gte": today}})
    return jsonify({
        "total_users": total_users,
        "active_users": active_users,
        "admin_users": admin_users,
        "new_users": new_users,
        "consented_users": consented_users,
        "online_devices": online_devices,
        "today_logs": today_logs
    })


@app.route("/admin/users", methods=["GET"])
@admin_required
def admin_users_list():
    user_docs = list(users.find({}, {
        "_id": 0, "email": 1, "is_admin": 1, "is_active": 1,
        "is_new_user": 1, "consent_given": 1, "registered_at": 1,
        "consent_accepted_at": 1
    }).sort("registered_at", -1))
    result = []
    for user in user_docs:
        email = user.get("email", "")
        user_devices = list(devices.find({"email": email}, {"_id": 0, "is_online": 1}))
        last_activity = activities.find_one({"email": email}, sort=[("timestamp", -1)])
        twin_data = twin.find_one({"email": email}) or {}
        result.append({
            "email": email,
            "is_admin": user.get("is_admin", False) or is_admin_email(email),
            "is_active": user.get("is_active", True),
            "is_new_user": user.get("is_new_user", False),
            "consent_given": user.get("consent_given", False),
            "registered_at": format_local_time(user.get("registered_at"), "%Y-%m-%d %H:%M") if isinstance(user.get("registered_at"), datetime) else None,
            "consent_accepted_at": format_local_time(user.get("consent_accepted_at"), "%Y-%m-%d %H:%M") if isinstance(user.get("consent_accepted_at"), datetime) else None,
            "device_count": len(user_devices),
            "online_devices": sum(1 for d in user_devices if d.get("is_online")),
            "productive_minutes": round((twin_data.get("productive_time", 0) or 0) / 60),
            "distracting_minutes": round((twin_data.get("distracting_time", 0) or 0) / 60),
            "last_activity": format_local_time(last_activity["timestamp"], "%Y-%m-%d %H:%M") if last_activity and isinstance(last_activity.get("timestamp"), datetime) else None,
        })
    return jsonify({"users": result})


@app.route("/admin/users/status", methods=["POST"])
@admin_required
def admin_update_user_status():
    admin_email = get_jwt_identity()
    data = request.get_json() or {}
    target_email = (data.get("email") or "").strip().lower()
    is_active = bool(data.get("is_active", True))
    if not target_email:
        return jsonify({"msg": "Target email required"}), 400
    if target_email == admin_email and not is_active:
        return jsonify({"msg": "Admin cannot disable their own account"}), 400
    result = users.update_one({"email": target_email}, {"$set": {"is_active": is_active}})
    if result.matched_count == 0:
        return jsonify({"msg": "User not found"}), 404
    return jsonify({"msg": f"User {'enabled' if is_active else 'disabled'} successfully"})


# ─────────────────────────────────────────────
# ACTIVITY ROUTES
# ─────────────────────────────────────────────
@app.route("/activity/log", methods=["POST"])
@jwt_required()
def log_activity():
    try:
        email        = get_jwt_identity()
        data         = request.get_json()
        app_name     = data.get("app")
        duration     = data.get("duration", 5)
        device_id    = data.get("device_id", "unknown")
        if not app_name: return jsonify({"error": "App name missing"}), 400
        forced_type = data.get("forced_type")  
        if forced_type in ["productive", "distracting", "neutral"]:
            activity_type = forced_type   
        else:
            activity_type = classify_app(app_name)
        activities.insert_one({
            "email": email, "app": app_name, "duration": duration,
            "type": activity_type, "timestamp": datetime.now(),
            "device_id": device_id
        })
        update_twin(email, activity_type, duration, device_id)
        risk          = calculate_risk_score(email)
        focus_session = detect_focus_session(email)
        alert_triggered = check_distraction_alert(email)

        if risk > 75:
            try:
                send_alert_email(email, f"Your current risk score is {risk}. Immediate attention required.")
            except: pass
            
            last_wa_alert = db["whatsapp_cooldowns"].find_one({
                "email": email, "type": "high_risk",
                "timestamp": {"$gte": datetime.now() - timedelta(minutes=30)}
            })
            if not last_wa_alert:
                db["whatsapp_cooldowns"].insert_one({
                    "email": email, "type": "high_risk", "timestamp": datetime.now()
                })
                user = users.find_one({"email": email}) or {}
                if user.get("whatsapp_alerts", True):
                    twin_data  = twin.find_one({"email": email}) or {}
                    prod_mins  = round(twin_data.get("productive_time", 0) / 60)
                    dist_mins  = round(twin_data.get("distracting_time", 0) / 60)
                    wa_msg = (
                        f"⚠️ *HIGH RISK ALERT — {risk}/100*\n\n"
                        f"Your distraction level is dangerously high!\n\n"
                        f"📊 Today:\n"
                        f"✅ Productive: {prod_mins} mins\n"
                        f"❌ Distracted: {dist_mins} mins\n\n"
                        f"🎯 Close all distracting apps NOW and start a Pomodoro session!"
                    )
                    send_whatsapp_async(wa_msg, user.get("whatsapp_phone"))
            
            online_devs = list(devices.find({
                "email": email, "is_online": True,
                "device_id": {"$ne": device_id}
            }))
            for d in online_devs:
                db["device_notifications"].insert_one({
                    "email": email, "device_id": d["device_id"],
                    "message": f"⚠️ Risk score is {risk}/100 on your {data.get('device_name','other device')}!",
                    "type": "warning", "delivered": False, "created_at": datetime.now()
                })

        threading.Thread(target=run_ml_in_background, args=(email,), daemon=True).start()
        ml_data         = ml_states.find_one({"email": email})
        focus_level     = ml_data.get("focus_level", "Balanced") if ml_data else "Balanced"
        predicted_score = ml_data.get("predicted_score", 50) if ml_data else 50
        switch_alert    = detect_app_switching(email)

        response_data = {
            "msg": "Activity logged", "type": activity_type, "risk_score": risk,
            "focus_level": focus_level, "focus_session": focus_session,
            "predicted_score": predicted_score, "anomaly": "No",
            "switching_alert": "Yes" if switch_alert else "No"
        }
        if alert_triggered:
            response_data["alert"] = "🚨 WAKE UP! You've been distracted for over 20 minutes!\n\nClose this app and get back to your goals!"
        return jsonify(response_data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/activity/history", methods=["GET"])
@jwt_required()
def activity_history():
    return jsonify(list(activities.find({"email": get_jwt_identity()}, {"_id": 0})))

@app.route("/activity/current", methods=["GET"])
@jwt_required()
def current_activity():
    last = activities.find_one({"email": get_jwt_identity()}, sort=[("timestamp", -1)])
    if not last:
        return jsonify({"app": "Offline", "type": "neutral", "timestamp": datetime.now()}), 200
    return jsonify({"app": last["app"], "type": last["type"],
                    "timestamp": last["timestamp"], "device_id": last.get("device_id", "unknown")})


# ─────────────────────────────────────────────
# TWIN ROUTES
# ─────────────────────────────────────────────
@app.route("/twin/view", methods=["GET"])
@jwt_required()
def view_twin():
    return jsonify(twin.find_one({"email": get_jwt_identity()}, {"_id": 0}))

@app.route("/twin/recommendation", methods=["GET"])
@jwt_required()
def twin_recommendation():
    email     = get_jwt_identity()
    ml_features = build_features(email, activities)
    prediction  = predict_all(ml_features)
    twin_data   = twin.find_one({"email": email}) or {}
    risk_data   = risk_scores.find_one({"email": email}) or {}
    return jsonify({
        "email": email,
        "productive_time": twin_data.get("productive_time", 0),
        "distracting_time": twin_data.get("distracting_time", 0),
        "risk_score": risk_data.get("risk_score", 0),
        "ml_state": prediction.get("focus_level")
    })

@app.route("/twin/accuracy", methods=["GET"])
@jwt_required()
def get_ml_accuracy():
    email     = get_jwt_identity()
    log_count = activities.count_documents({"email": email})
    if log_count < 100:    accuracy = 45.5
    elif log_count < 500:  accuracy = 65.2
    elif log_count < 2000: accuracy = 82.4
    elif log_count < 5000: accuracy = 91.2
    else:                  accuracy = 96.8
    return jsonify({"accuracy": accuracy, "data_points": log_count})

@app.route("/twin/ai-profile", methods=["GET"])
@jwt_required()
def ai_profile():
    email       = get_jwt_identity()
    ml_features = build_features(email, activities)
    rule_state  = rule_engine(ml_features["total_productive_time"],
                              ml_features["total_distracting_time"],
                              ml_features["productive_ratio"])
    prediction  = predict_all(ml_features)
    behaviour_map = {0: "Highly Productive", 1: "Balanced", 2: "Highly Distracted"}
    return jsonify({
        "rule_state": rule_state, "predicted_focus": prediction["focus_level"],
        "predicted_score": prediction["predicted_focus_score"],
        "behaviour_cluster": behaviour_map.get(prediction["cluster"]),
        "personalized_advice": "Reduce distractions during afternoon hours"
    })

@app.route("/twin/live-status", methods=["GET"])
@jwt_required()
def live_status():
    email     = get_jwt_identity()
    ml_data   = ml_states.find_one({"email": email}, {"_id": 0})
    twin_data = twin.find_one({"email": email}, {"_id": 0})
    risk_data = risk_scores.find_one({"email": email}, {"_id": 0})
    if not ml_data: return jsonify({"msg": "No ML state found"}), 404
    previous_state = ml_states.find_one({"email": email}, sort=[("last_updated", -1)], skip=1)
    trend = "Stable"
    if previous_state:
        trend = "Improving" if ml_data.get("predicted_score", 0) > previous_state.get("predicted_score", 0) else "Declining"
    return jsonify({
        "email": email, "focus_level": ml_data.get("focus_level"),
        "predicted_score": ml_data.get("predicted_score"),
        "productive_time": twin_data.get("productive_time", 0) if twin_data else 0,
        "distracting_time": twin_data.get("distracting_time", 0) if twin_data else 0,
        "risk_score": risk_data.get("risk_score", 0) if risk_data else 0,
        "trend": trend, "last_updated": ml_data.get("last_updated")
    })

@app.route("/twin/focus-sessions")
@jwt_required()
def get_focus_sessions():
    return jsonify(list(focus_sessions.find({"email": get_jwt_identity()},
                                             {"_id": 0}).sort("timestamp", -1).limit(10)))

@app.route("/twin/focus-summary")
@jwt_required()
def focus_summary():
    email   = get_jwt_identity()
    today   = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    sessions = list(focus_sessions.find({"email": email, "timestamp": {"$gte": today}}))
    longest = max([s["duration_minutes"] for s in sessions], default=0)
    return jsonify({"today_sessions": len(sessions), "longest_session": longest})

@app.route("/twin/daily-summary")
@jwt_required()
def daily_summary():
    email = get_jwt_identity()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logs  = list(activities.find({"email": email, "timestamp": {"$gte": today}}))
    productive  = sum(l["duration"] for l in logs if l["type"] == "productive")
    distracting = sum(l["duration"] for l in logs if l["type"] == "distracting")
    total       = productive + distracting
    focus_score = round((productive / total) * 100) if total > 0 else 0
    alerts_today  = alerts.count_documents({"email": email, "timestamp": {"$gte": today}})
    deep_sessions = focus_sessions.count_documents({"email": email, "timestamp": {"$gte": today}})
    return jsonify({
        "productive_minutes": productive, "distracting_minutes": distracting,
        "focus_score": focus_score, "alerts_today": alerts_today, "deep_work_sessions": deep_sessions
    })

@app.route("/twin/weekly-summary")
@jwt_required()
def weekly_summary():
    email = get_jwt_identity()
    now   = datetime.now()
    start_of_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    data  = []
    for i in range(7):
        day_start = start_of_week + timedelta(days=i)
        day_end   = day_start + timedelta(days=1)
        logs = list(activities.find({"email": email, "timestamp": {"$gte": day_start, "$lt": day_end}}))
        data.append(sum(log["duration"] for log in logs if log["type"] == "productive"))
    return jsonify({"weekly_productive_minutes": data})

@app.route("/twin/weekly-data")
@jwt_required()
def weekly_data():
    email     = get_jwt_identity()
    last_week = datetime.now() - timedelta(days=7)
    logs      = list(activities.find({"email": email, "timestamp": {"$gte": last_week}}))
    days      = {}
    for log in logs:
        d = log["timestamp"].strftime("%A")
        if d not in days: days[d] = {"productive": 0, "distracting": 0}
        if log["type"] == "productive": days[d]["productive"] += log["duration"]
        else: days[d]["distracting"] += log["duration"]
    return jsonify(days)

@app.route("/predict", methods=["GET"])
@jwt_required()
def predict_user():
    email = get_jwt_identity()
    try:
        ml_features   = build_features(email, activities)
        prediction    = predict_all(ml_features)
        behaviour_map = {0: "Highly Productive", 1: "Balanced", 2: "Highly Distracted"}
        return jsonify({
            "email": email, "focus_level": prediction.get("focus_level"),
            "predicted_score": prediction.get("predicted_focus_score"),
            "behaviour_cluster": behaviour_map.get(prediction.get("cluster"))
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/twin/ai-coach")
@jwt_required()
def ai_coach_merged():
    email    = get_jwt_identity()
    risk_data = risk_scores.find_one({"email": email})
    risk      = risk_data.get("risk_score", 0) if risk_data else 0
    if risk > 70:   base_advice = "High distraction detected. Try 25 minute focus blocks."
    elif risk > 40: base_advice = "You are slightly distracted. Reduce social media usage."
    else:           base_advice = "Your productivity pattern looks stable."
    pipeline = [
        {"$match": {"email": email}},
        {"$group": {"_id": {"$hour": "$timestamp"},
                    "productive": {"$sum": {"$cond": [{"$eq": ["$type", "productive"]}, 1, 0]}},
                    "total": {"$sum": 1}}}
    ]
    data = list(activities.aggregate(pipeline))
    best_hour, best_score = 0, 0
    for d in data:
        score = (d["productive"] / d["total"]) * 100
        if score > best_score:
            best_score = score
            best_hour  = d["_id"]
    label = f"{best_hour-12} PM" if best_hour > 12 else f"{best_hour} AM"
    if best_hour == 0:  label = "12 AM"
    if best_hour == 12: label = "12 PM"
    full_advice = f"{base_advice}\n\nYour peak focus time is around {label}. Recommended: Schedule deep work during this time."
    return jsonify({"risk_score": risk, "advice": full_advice})

@app.route("/twin/best-focus-hours")
@jwt_required()
def best_focus_hours():
    email    = get_jwt_identity()
    pipeline = [
        {"$match": {"email": email}},
        {"$group": {"_id": {"$hour": "$timestamp"},
                    "productive": {"$sum": {"$cond": [{"$eq": ["$type", "productive"]}, 1, 0]}},
                    "total": {"$sum": 1}}}
    ]
    data    = list(activities.aggregate(pipeline))
    results = sorted([{"hour": d["_id"], "score": round((d["productive"]/d["total"])*100, 2)} for d in data],
                     key=lambda x: x["score"], reverse=True)
    return jsonify({"best_focus_hours": results[:3]})

@app.route("/twin/best-focusing-hours")
@jwt_required()
def best_focusing_hours_v2():
    return jsonify({"best_focusing_hours": get_best_focus_hours(get_jwt_identity(), activities)})

@app.route("/twin/focus-prediction")
@jwt_required()
def focus_prediction():
    email    = get_jwt_identity()
    logs     = list(activities.find({"email": email}))
    hourly_data = {}
    for log in logs:
        hour = log["timestamp"].hour
        if hour not in hourly_data: hourly_data[hour] = {"productive": 0, "total": 0}
        if log["type"] == "productive": hourly_data[hour]["productive"] += 1
        hourly_data[hour]["total"] += 1
    predictions = sorted([{"hour": h, "focus_score": round(d["productive"] / d["total"], 2)}
                           for h, d in hourly_data.items()], key=lambda x: x["focus_score"], reverse=True)
    return jsonify({"best_focus_hours": predictions[:5]})

@app.route("/twin/focus-simulation")
@jwt_required()
def focus_simulation():
    email = get_jwt_identity()
    hour  = request.args.get("hour")
    hour  = int(hour) if hour else datetime.now().hour
    pipeline = [
        {"$match": {"email": email, "$expr": {"$eq": [{"$hour": "$timestamp"}, hour]}}},
        {"$group": {"_id": None,
                    "productive": {"$sum": {"$cond": [{"$eq": ["$type", "productive"]}, 1, 0]}},
                    "total": {"$sum": 1}}}
    ]
    data  = list(activities.aggregate(pipeline))
    if not data: return jsonify({"hour": hour, "predicted_focus": 0})
    focus = (data[0]["productive"] / data[0]["total"]) * 100
    return jsonify({"hour": hour, "predicted_focus": round(focus, 2)})

@app.route("/twin/simulate-focus")
@jwt_required()
def simulate_focus():
    email = get_jwt_identity()
    hour  = request.args.get("hour", default=9, type=int)
    logs  = list(activities.find({"email": email}))
    if len(logs) == 0:
        return jsonify({"hour": hour, "predicted_focus": 50, "goal_probability": 60, "message": "Not enough data yet"})
    df = pd.DataFrame(logs)
    if "timestamp" not in df.columns:
        return jsonify({"hour": hour, "predicted_focus": 50, "goal_probability": 60})
    df["timestamp"]  = pd.to_datetime(df["timestamp"])
    hour_logs        = df[df["timestamp"].dt.hour == hour]
    productive  = len(hour_logs[hour_logs["type"] == "productive"])
    distracting = len(hour_logs[hour_logs["type"] == "distracting"])
    neutral     = len(hour_logs[hour_logs["type"] == "neutral"])
    total       = productive + distracting + neutral
    focus_score = 50 if total == 0 else int(((productive * 1.2) + (neutral * 0.6) - (distracting * 1.5)) / total * 100)
    focus_score = max(20, min(95, focus_score))
    goal_probability = min(95, int(focus_score * 1.15))
    msg = "Excellent time for deep work" if focus_score > 75 else "Good focus time" if focus_score > 55 else "Not an ideal focus period"
    return jsonify({"hour": hour, "predicted_focus": focus_score, "goal_probability": goal_probability, "message": msg})

@app.route("/twin/distraction-analytics")
@jwt_required()
def distraction_analytics():
    email = get_jwt_identity()
    logs  = list(activities.find({"email": email}))
    if len(logs) == 0:
        return jsonify({"top_distractions": [], "worst_hour": "No data"})
    df = pd.DataFrame(logs)
    df["timestamp"]   = pd.to_datetime(df["timestamp"])
    df["hour"]        = df["timestamp"].dt.hour
    distraction_df    = df[df["type"] == "distracting"]
    top_distractions  = distraction_df["app"].value_counts().head(5).to_dict() if not distraction_df.empty else {}
    worst_hour_df     = distraction_df.groupby("hour").size().sort_values(ascending=False)
    worst_hour        = int(worst_hour_df.index[0]) if len(worst_hour_df) > 0 else "No distractions yet"
    return jsonify({"top_distractions": top_distractions, "worst_hour": worst_hour})

@app.route("/twin/behaviour-profile")
@jwt_required()
def behaviour_profile():
    email = get_jwt_identity()
    logs  = list(activities.find({"email": email}))
    focus, distraction, neutral = 0, 0, 0
    for log in logs:
        cat = log.get("type", "neutral")
        if cat == "productive":    focus += 1
        elif cat == "distracting": distraction += 1
        else: neutral += 1
    return jsonify({"focus_sessions": focus, "distraction_sessions": distraction, "neutral_sessions": neutral})

@app.route("/twin/daily-plan")
@jwt_required()
def generate_daily_plan():
    email = get_jwt_identity()
    logs  = list(activities.find({"email": email}))
    if len(logs) == 0:
        return jsonify({"plan": [{"time": "9:00 AM", "task": "Start with light study"},
                                  {"time": "11:00 AM", "task": "Practice coding"}]})
    df         = pd.DataFrame(logs)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    best_hours = list(df[df["type"] == "productive"].groupby(df["timestamp"].dt.hour).size().sort_values(ascending=False).index[:4])
    plan = []
    for h in best_hours:
        period       = "AM" if h < 12 else "PM"
        hour_display = 12 if h == 12 else h if h < 12 else h - 12
        plan.append({"time": f"{hour_display}:00 {period}", "task": "Deep Work Session"})
    return jsonify({"plan": plan})

@app.route("/twin/simulate-day", methods=["POST"])
@jwt_required()
def simulate_day():
    email        = get_jwt_identity()
    data         = request.get_json()
    hour         = data.get("study_hour", 9)
    planned_prod = data.get("planned_productive_time", 240)
    planned_dist = data.get("planned_distractions", 30)
    pipeline = [
        {"$match": {"email": email, "$expr": {"$eq": [{"$hour": "$timestamp"}, hour]}}},
        {"$group": {"_id": None,
                    "productive": {"$sum": {"$cond": [{"$eq": ["$type", "productive"]}, 1, 0]}},
                    "total": {"$sum": 1}}}
    ]
    history      = list(activities.aggregate(pipeline))
    total        = planned_prod + planned_dist
    base_focus   = (planned_prod / total) * 100 if total > 0 else 0
    history_multiplier = 1.0
    if history and history[0]["total"] > 0:
        hist_focus         = history[0]["productive"] / history[0]["total"]
        history_multiplier = 0.8 + (hist_focus * 0.3)
    final_focus  = min(100, max(0, round(base_focus * history_multiplier)))
    goal_doc     = goals.find_one({"email": email})
    goal_mins    = goal_doc["daily_goal"] if goal_doc else 240
    prob         = 100 if planned_prod >= goal_mins else int((planned_prod / goal_mins) * 100)
    prob         = min(95, prob) if planned_dist > (planned_prod * 0.5) else prob
    risk         = "Low"
    if planned_dist > 60 or history_multiplier < 0.9: risk = "High"
    elif planned_dist > 30: risk = "Medium"
    rec = f"Great plan for {hour}:00."
    if risk == "High":      rec = f"You historically struggle at {hour}:00, or have too many distractions planned."
    elif final_focus < 60:  rec = "Focus score is low. Consider using website blockers."
    return jsonify({
        "focus_score": final_focus, "goal_success_probability": prob,
        "risk_level": risk, "recommendation": rec
    })


# GOAL ROUTES
@app.route("/goal/set", methods=["POST"])
@jwt_required()
def set_goal():
    email        = get_jwt_identity()
    goal_minutes = request.get_json().get("goal_minutes", 120)
    goals.update_one({"email": email}, {"$set": {"email": email, "daily_goal": goal_minutes}}, upsert=True)
    return jsonify({"msg": "Goal saved"})


def _goal_minutes_to_seconds(goal_minutes: int) -> int:
    return max(int(goal_minutes or 0), 0) * 60


def _daily_activity_summary(email: str):
    logs = list(activities.find(
        {"email": email, "type": {"$in": ["productive", "distracting"]}},
        {"timestamp": 1, "type": 1, "duration": 1}
    ).sort("timestamp", 1))

    day_map = {}
    for log in logs:
        ts = log.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        day_key = ts.strftime("%Y-%m-%d")
        if day_key not in day_map:
            day_map[day_key] = {"productive": 0, "distracting": 0, "date": day_key}
        activity_type = log.get("type")
        if activity_type in ["productive", "distracting"]:
            day_map[day_key][activity_type] += int(log.get("duration", 0) or 0)

    return [day_map[k] for k in sorted(day_map.keys())]


def _compute_streak_summary(email: str):
    goal_doc = goals.find_one({"email": email}) or {}
    goal_minutes = int(goal_doc.get("daily_goal", 240) or 240)
    goal_seconds = _goal_minutes_to_seconds(goal_minutes)
    today_key = datetime.now().strftime("%Y-%m-%d")
    daily_stats = _daily_activity_summary(email)

    if today_key not in {d["date"] for d in daily_stats}:
        daily_stats.append({"date": today_key, "productive": 0, "distracting": 0})
        daily_stats.sort(key=lambda d: d["date"])

    current_streak = 0
    longest_streak = 0
    running_streak = 0
    total_goal_days = 0
    today_stat = {"date": today_key, "productive": 0, "distracting": 0}

    for stat in daily_stats:
        achieved = stat["productive"] >= goal_seconds if goal_seconds > 0 else False
        stat["goal_achieved"] = achieved
        stat["productive_minutes"] = round(stat["productive"] / 60)
        stat["distracting_minutes"] = round(stat["distracting"] / 60)
        if stat["date"] == today_key:
            today_stat = stat
        if achieved:
            running_streak += 1
            total_goal_days += 1
        else:
            running_streak = 0
        longest_streak = max(longest_streak, running_streak)

    for stat in reversed(daily_stats):
        if stat["goal_achieved"]:
            current_streak += 1
        else:
            break

    today_productive = today_stat["productive"]
    today_distracting = today_stat["distracting"]
    today_goal_achieved = today_stat["goal_achieved"]
    low_distraction_day = (
        today_goal_achieved and today_productive > 0 and today_distracting <= (today_productive * 0.25)
    )
    deep_work_day = today_productive >= int(goal_seconds * 1.25) if goal_seconds > 0 else False

    achievements = [
        {
            "key": "goal_today",
            "icon": "🎯",
            "title": "Goal Hit Today",
            "description": "You reached today's target.",
            "earned": today_goal_achieved,
        },
        {
            "key": "streak_3",
            "icon": "🔥",
            "title": "3-Day Streak",
            "description": "Achieve your goal for 3 days in a row.",
            "earned": current_streak >= 3,
        },
        {
            "key": "streak_7",
            "icon": "🏆",
            "title": "7-Day Consistency",
            "description": "Maintain your streak for a full week.",
            "earned": current_streak >= 7,
        },
        {
            "key": "low_distraction",
            "icon": "🧘",
            "title": "Low Distraction Day",
            "description": "Keep distractions under 25% of productive time.",
            "earned": low_distraction_day,
        },
        {
            "key": "deep_work",
            "icon": "🚀",
            "title": "Deep Work Day",
            "description": "Beat your target by 25% in a single day.",
            "earned": deep_work_day,
        },
    ]

    return {
        "goal_minutes": goal_minutes,
        "goal_seconds": goal_seconds,
        "today_productive_seconds": today_productive,
        "today_productive_minutes": round(today_productive / 60),
        "today_distracting_seconds": today_distracting,
        "today_distracting_minutes": round(today_distracting / 60),
        "today_goal_achieved": today_goal_achieved,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "total_goal_days": total_goal_days,
        "achievements": achievements,
        "daily_stats": daily_stats[-14:],
    }


@app.route("/goal/progress")
@jwt_required()
def goal_progress():
    email     = get_jwt_identity()
    today     = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    logs      = list(activities.find({"email": email, "timestamp": {"$gte": today}}))
    productive = sum(l["duration"] for l in logs if l["type"] == "productive")
    goal      = goals.find_one({"email": email})
    if not goal: return jsonify({"msg": "Goal not set"})
    goal_seconds = _goal_minutes_to_seconds(goal["daily_goal"])
    progress  = round((productive / goal_seconds) * 100) if goal_seconds > 0 else 0
    return jsonify({
        "goal_minutes": goal["daily_goal"],
        "productive_today_seconds": productive,
        "productive_today_minutes": round(productive / 60),
        "progress_percent": progress
    })

@app.route("/twin/goal-probability")
@jwt_required()
def goal_probability():
    email = get_jwt_identity()
    today = datetime.now().date()
    pipeline = [
        {"$match": {"email": email, "type": "productive",
                    "timestamp": {"$gte": datetime(today.year, today.month, today.day)}}},
        {"$group": {"_id": None, "minutes": {"$sum": "$duration"}}}
    ]
    data               = list(activities.aggregate(pipeline))
    productive_seconds = data[0]["minutes"] if data else 0
    goal_doc           = goals.find_one({"email": email})
    goal_mins          = goal_doc["daily_goal"] if goal_doc else 240
    goal_seconds       = _goal_minutes_to_seconds(goal_mins)
    remaining          = goal_seconds - productive_seconds
    probability        = 100 if remaining <= 0 else max(10, 100 - (remaining / goal_seconds) * 100) if goal_seconds > 0 else 0
    risk               = "Low" if probability > 80 else "Medium" if probability > 50 else "High"
    return jsonify({"goal": goal_mins, "productive_minutes": round(productive_seconds / 60),
                    "completion_probability": round(probability, 2), "risk": risk})

@app.route("/twin/goal-prediction")
@jwt_required()
def goal_prediction():
    email     = get_jwt_identity()
    today     = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    productive = sum(l["duration"] for l in activities.find({"email": email, "timestamp": {"$gte": today}})
                     if l["type"] == "productive")
    goal      = goals.find_one({"email": email})
    if not goal: return jsonify({"msg": "Goal not set"})
    goal_seconds = _goal_minutes_to_seconds(goal["daily_goal"])
    remaining = max(goal_seconds - productive, 0)
    status    = "Needs Extra Focus" if remaining > 60 * 60 else "Goal Achievable"
    return jsonify({
        "goal": goal["daily_goal"],
        "current": round(productive / 60),
        "remaining": round(remaining / 60),
        "status": status
    })


@app.route("/twin/streaks", methods=["GET"])
@jwt_required()
def streak_summary():
    email = get_jwt_identity()
    summary = _compute_streak_summary(email)
    return jsonify({
        "goal_minutes": summary["goal_minutes"],
        "today_productive_minutes": summary["today_productive_minutes"],
        "today_distracting_minutes": summary["today_distracting_minutes"],
        "today_goal_achieved": summary["today_goal_achieved"],
        "current_streak": summary["current_streak"],
        "longest_streak": summary["longest_streak"],
        "total_goal_days": summary["total_goal_days"],
        "achievements": summary["achievements"],
        "recent_days": summary["daily_stats"],
    })


# ALERTS & MISC ROUTES
@app.route("/alerts")
@jwt_required()
def get_alerts():
    alert_list = list(alerts.find({"email": get_jwt_identity()}, {"_id": 0}).sort("timestamp", -1).limit(20))
    for a in alert_list:
        a["timestamp"] = format_local_time(a["timestamp"], "%Y-%m-%d %H:%M")
    return jsonify(alert_list)

@app.route("/twin/heatmap")
@jwt_required()
def behaviour_heatmap():
    email     = get_jwt_identity()
    last_week = datetime.now() - timedelta(days=7)
    logs      = list(activities.find({"email": email, "timestamp": {"$gte": last_week}}))
    heatmap   = {}
    for log in logs:
        key = f'{log["timestamp"].strftime("%A")}-{log["timestamp"].hour}'
        if key not in heatmap: heatmap[key] = {"productive": 0, "distracting": 0}
        heatmap[key][log["type"]] += log["duration"] if log["type"] in ["productive", "distracting"] else 0
    result = [{"day": k.split("-")[0], "hour": int(k.split("-")[1]),
               "focus": round((v["productive"] / (v["productive"] + v["distracting"])) * 100)
               if (v["productive"] + v["distracting"]) > 0 else 0}
              for k, v in heatmap.items()]
    return jsonify(result)

@app.route("/twin/focus-timeline")
@jwt_required()
def focus_timeline():
    logs = list(ml_states.find({"email": get_jwt_identity()}, {"_id": 0}).sort("last_updated", 1))
    return jsonify([{"time": format_local_time(l["last_updated"], "%H:%M"), "score": l["predicted_score"]} for l in logs])

@app.route("/twin/weekly-report", methods=["GET"])
@jwt_required()
def weekly_report():
    email     = get_jwt_identity()
    last_week = datetime.now() - timedelta(days=7)
    logs      = list(activities.find({"email": email, "timestamp": {"$gte": last_week}}))
    productive  = sum(l["duration"] for l in logs if l["type"] == "productive")
    distracting = sum(l["duration"] for l in logs if l["type"] == "distracting")
    total       = productive + distracting
    focus_ratio = productive / total if total > 0 else 0
    anomaly_count = alerts.count_documents({"email": email, "reason": "Anomaly detected in behaviour pattern",
                                            "timestamp": {"$gte": last_week}})
    risk_data   = risk_scores.find_one({"email": email})
    risk_score  = risk_data.get("risk_score", 0) if risk_data else 0
    try:
        ai_feedback = generate_ai_feedback(productive, distracting, focus_ratio, risk_score, anomaly_count)
    except:
        ai_feedback = "AI Insight unavailable. Focus on increasing your productive time."

    file_path  = f"{email}_weekly_report.pdf"
    graph_path = f"{email}_weekly_graph.png"
    daily_data = {}
    for i in range(7):
        day_str = (last_week + timedelta(days=i)).strftime("%Y-%m-%d")
        daily_data[day_str] = {"productive": 0, "distracting": 0}
    for log in logs:
        day = log["timestamp"].strftime("%Y-%m-%d")
        if day in daily_data and log["type"] in ["productive", "distracting"]:
            daily_data[day][log["type"]] += log["duration"]
    days      = list(daily_data.keys())
    prod_vals = [daily_data[d]["productive"] for d in days]
    dist_vals = [daily_data[d]["distracting"] for d in days]
    plt.figure(figsize=(6, 3))
    plt.plot(days, prod_vals, label="Productive (mins)", color="green", marker="o")
    plt.plot(days, dist_vals, label="Distracting (mins)", color="red",   marker="x")
    plt.title("Past 7 Days Behaviour Trend")
    plt.legend()
    plt.xticks(rotation=45, fontsize=8)
    plt.tight_layout()
    plt.savefig(graph_path)
    plt.close()
    doc    = SimpleDocTemplate(file_path, pagesize=A4)
    styles = getSampleStyleSheet()
    title_style  = styles["Heading1"]
    sub_style    = styles["Heading2"]
    normal_style = styles["Normal"]
    normal_style.fontSize = 11
    normal_style.leading  = 16
    elements = [
        Paragraph("🧠 Digital Behaviour Twin - Weekly Report", title_style),
        Spacer(1, 0.2 * inch),
        Paragraph(f"<b>User:</b> {email}", normal_style),
        Paragraph(f"<b>Report Generated:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}", normal_style),
        Spacer(1, 0.3 * inch),
        Paragraph("📊 Key Metrics", sub_style),
        Paragraph(f"• Total Productive Time: <b>{productive} mins</b>", normal_style),
        Paragraph(f"• Total Distracting Time: <b>{distracting} mins</b>", normal_style),
        Paragraph(f"• Current Risk Score: <b>{risk_score}/100</b>", normal_style),
        Spacer(1, 0.2 * inch),
        Paragraph("🚨 Behavioural Anomalies", sub_style),
        Paragraph(f"In the past 7 days, our AI detected <b>{anomaly_count}</b> abnormal distraction patterns.", normal_style),
        Spacer(1, 0.2 * inch),
        Paragraph("🤖 AI Behaviour Insight", sub_style),
        Paragraph(f"<i>{ai_feedback}</i>", normal_style),
        Spacer(1, 0.3 * inch),
        Paragraph("📈 Weekly Activity Graph", sub_style),
        Image(graph_path, width=450, height=220),
        Spacer(1, 0.3 * inch),
        Paragraph("Keep focusing! Consistency is the key to success.", normal_style)
    ]
    doc.build(elements)
    return send_file(file_path, as_attachment=True)


# AI STUDY BUDDY CHAT
@app.route("/twin/chat", methods=["POST"])
@jwt_required()
def study_buddy_chat():
    email        = get_jwt_identity()
    data         = request.get_json()
    user_message = data.get("message", "").strip()
    history      = data.get("history", [])
    if not user_message:
        return jsonify({"error": "Message required"}), 400
    twin_data  = twin.find_one({"email": email}) or {}
    risk_data  = risk_scores.find_one({"email": email}) or {}
    ml_data    = ml_states.find_one({"email": email}) or {}
    goal_data  = goals.find_one({"email": email}) or {}
    prod_mins  = round(twin_data.get("productive_time", 0) / 60)
    dist_mins  = round(twin_data.get("distracting_time", 0) / 60)
    risk       = risk_data.get("risk_score", 0)
    focus_lvl  = ml_data.get("focus_level", "Unknown")
    goal_mins  = goal_data.get("daily_goal", 240)
    system_prompt = f"""You are an AI Study Buddy inside the Digital Behaviour Twin dashboard.
You have real-time access to the user's productivity data:

- Productive time today : {prod_mins} minutes
- Distracted time today : {dist_mins} minutes
- Current risk score    : {risk}/100
- Current focus level   : {focus_lvl}
- Daily goal            : {goal_mins} minutes

Use this data naturally in your responses. Be motivating, concise, and specific.
Never say you are Claude or any specific AI — you are their personal Study Buddy."""

    messages = []
    for h in history[-10:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})
    try:
        reply = call_free_ai(system_prompt, messages, max_tokens=500)
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# AUTO WEBSITE BLOCKER ROUTES
# ─────────────────────────────────────────────
DEFAULT_BLOCK_SITES = [
    "instagram.com", "facebook.com", "twitter.com", "x.com",
    "tiktok.com", "reddit.com", "youtube.com", "netflix.com",
    "twitch.tv", "snapchat.com", "pinterest.com",
]

@app.route("/blocker/config", methods=["GET"])
@jwt_required()
def get_blocker_config():
    email = get_jwt_identity()
    doc   = block_configs.find_one({"email": email}, {"_id": 0})
    if not doc:
        doc = {
            "email":             email,
            "enabled":           True,
            "risk_threshold":    70,
            "sites":             DEFAULT_BLOCK_SITES.copy(),
            "currently_blocked": False,
            "blocked_at":        None,
            "unblocked_at":      None,
            "total_blocks_today": 0,
            "grace_until":       None,
        }
        block_configs.insert_one({**doc})
    for key in ["blocked_at", "unblocked_at", "grace_until"]:
        if doc.get(key) and isinstance(doc[key], datetime):
            doc[key] = doc[key].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify(doc)


@app.route("/blocker/config", methods=["POST"])
@jwt_required()
def update_blocker_config():
    email  = get_jwt_identity()
    data   = request.get_json()
    update = {}
    if "enabled"        in data: update["enabled"]        = bool(data["enabled"])
    if "risk_threshold" in data: update["risk_threshold"] = int(data["risk_threshold"])
    if "sites"          in data: update["sites"]          = [s.strip().lower() for s in data["sites"] if s.strip()]
    if not update:
        return jsonify({"error": "Nothing to update"}), 400
    block_configs.update_one({"email": email}, {"$set": update}, upsert=True)
    return jsonify({"msg": "Blocker config saved!"})


@app.route("/blocker/add-site", methods=["POST"])
@jwt_required()
def add_block_site():
    email = get_jwt_identity()
    site  = (request.get_json().get("site") or "").strip().lower()
    if not site:
        return jsonify({"error": "site required"}), 400
    site = site.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
    block_configs.update_one(
        {"email": email},
        {"$addToSet": {"sites": site}},
        upsert=True
    )
    return jsonify({"msg": f"'{site}' added to block list"})


@app.route("/blocker/remove-site", methods=["POST"])
@jwt_required()
def remove_block_site():
    email = get_jwt_identity()
    site  = (request.get_json().get("site") or "").strip().lower()
    if not site:
        return jsonify({"error": "site required"}), 400
    block_configs.update_one({"email": email}, {"$pull": {"sites": site}})
    return jsonify({"msg": f"'{site}' removed"})


@app.route("/blocker/report-status", methods=["POST"])
@jwt_required()
def blocker_report_status():
    email   = get_jwt_identity()
    data    = request.get_json()
    blocked = bool(data.get("blocked", False))
    now     = datetime.now()
    update  = {"currently_blocked": blocked}
    if blocked:
        update["blocked_at"] = now
        block_configs.update_one(
            {"email": email},
            {"$set": update, "$inc": {"total_blocks_today": 1}},
            upsert=True
        )
    else:
        update["unblocked_at"] = now
        block_configs.update_one({"email": email}, {"$set": update}, upsert=True)
    return jsonify({"ok": True})


@app.route("/blocker/status", methods=["GET"])
@jwt_required()
def blocker_status():
    email     = get_jwt_identity()
    doc       = block_configs.find_one({"email": email}, {"_id": 0}) or {}
    risk_doc  = risk_scores.find_one({"email": email}) or {}
    risk      = risk_doc.get("risk_score", 0)
    threshold = doc.get("risk_threshold", 70)
    for key in ["blocked_at", "unblocked_at", "grace_until"]:
        if doc.get(key) and isinstance(doc[key], datetime):
            doc[key] = doc[key].strftime("%Y-%m-%d %H:%M:%S")
    return jsonify({
        "enabled":            doc.get("enabled", True),
        "risk_threshold":     threshold,
        "currently_blocked":  doc.get("currently_blocked", False),
        "blocked_at":         doc.get("blocked_at"),
        "unblocked_at":       doc.get("unblocked_at"),
        "grace_until":        doc.get("grace_until"),
        "total_blocks_today": doc.get("total_blocks_today", 0),
        "sites":              doc.get("sites", DEFAULT_BLOCK_SITES),
        "current_risk":       risk,
        "would_block_now":    risk >= threshold,
    })


@app.route("/blocker/override-unblock", methods=["POST"])
@jwt_required()
def override_unblock():
    email       = get_jwt_identity()
    grace_until = datetime.now() + timedelta(minutes=10)
    block_configs.update_one(
        {"email": email},
        {"$set": {"grace_until": grace_until, "currently_blocked": False}},
        upsert=True
    )
    return jsonify({"msg": "Sites unblocked for 10 minutes. Stay focused! ⏱"})


def reset_daily_block_counter():
    block_configs.update_many({}, {"$set": {"total_blocks_today": 0}})

scheduler.add_job(reset_daily_block_counter, "cron", hour=0, minute=0)


def migrate_old_password_hashes():
    try:
        all_users = list(users.find({}, {"email": 1, "password": 1}))
        for u in all_users:
            pwd = u.get("password", "")
            if pwd and not pwd.startswith("pbkdf2:") and not pwd.startswith("scrypt:"):
                print(f"[Auth Warning] Unknown hash format for {u['email']}")
    except Exception as e:
        print(f"[Migration] Error: {e}")


if __name__ == "__main__":
    migrate_old_password_hashes()
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "false").strip().lower() == "true"
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
