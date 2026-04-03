import os
import json
import time
import uuid
import hashlib
import platform
import threading
import tkinter as tk
import requests
import atexit
import base64
from datetime import datetime, timedelta

import pytesseract
from dotenv import load_dotenv
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
load_dotenv(ENV_FILE)

BASE_URL     = os.getenv("API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
INTERVAL     = 5
OCR_INTERVAL = 30
HB_INTERVAL  = 30

AUTH_STATE_FILE = os.path.join(os.path.dirname(__file__), "token.json")
LEGACY_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "backend", "active_token.txt")

incognito_state = {"active": False}
auth_state = {
    "access_token": None,
    "access_expires_at": None,
    "refresh_token": None,
}

DEVICE_ID_FILE = "device_id.txt"

# ─────────────────────────────────────────────
# BLOCKER STATE & HOSTS FILE LOGIC
# ─────────────────────────────────────────────
blocker_state = {
    "enabled":           True,
    "threshold":         70,
    "sites":             [],
    "currently_blocked": False,
    "grace_until":       None,
    "last_config_fetch": None,
    "config_ttl_secs":   60,
}

HOSTS_FILE         = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_MARKER_START = "# === Digital Behaviour Twin Blocker START ==="
HOSTS_MARKER_END   = "# === Digital Behaviour Twin Blocker END ==="


def _decode_jwt_exp(token: str):
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")).decode("utf-8"))
        exp = data.get("exp")
        return datetime.fromtimestamp(exp) if exp else None
    except Exception:
        return None


def _load_refresh_token_from_disk():
    if not os.path.exists(AUTH_STATE_FILE):
        return None
    try:
        with open(AUTH_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        token = (data.get("refresh_token") or "").strip()
        return token or None
    except Exception as e:
        print(f"[Auth] Could not read auth state: {e}")
        return None


def _clear_auth_state():
    auth_state["access_token"] = None
    auth_state["access_expires_at"] = None
    auth_state["refresh_token"] = None
    for path in (AUTH_STATE_FILE, LEGACY_TOKEN_FILE):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


def _set_access_token(token: str):
    auth_state["access_token"] = token
    auth_state["access_expires_at"] = _decode_jwt_exp(token)


def get_valid_access_token(force_refresh: bool = False):
    now = datetime.now()
    current_token = auth_state.get("access_token")
    current_exp = auth_state.get("access_expires_at")
    if not force_refresh and current_token and current_exp and current_exp > (now + timedelta(seconds=30)):
        return current_token

    refresh_token = _load_refresh_token_from_disk()
    auth_state["refresh_token"] = refresh_token
    if not refresh_token:
        auth_state["access_token"] = None
        auth_state["access_expires_at"] = None
        return None

    try:
        response = requests.post(
            f"{BASE_URL}/auth/refresh",
            headers={"Authorization": f"Bearer {refresh_token}"},
            timeout=10
        )
        if response.status_code == 200:
            access_token = response.json().get("access_token")
            if access_token:
                _set_access_token(access_token)
                return access_token
        elif response.status_code in [401, 422]:
            print("[Auth] Refresh token invalid or expired. Waiting for a fresh login...")
            _clear_auth_state()
            return None
        else:
            print(f"[Auth] Refresh failed with status {response.status_code}")
    except Exception as e:
        print(f"[Auth] Refresh request failed: {e}")
    return None


def authorized_request(method: str, path: str, token: str = None, retry_on_unauthorized: bool = True, **kwargs):
    request_headers = dict(kwargs.pop("headers", {}) or {})
    access_token = token or get_valid_access_token()
    if not access_token:
        return None

    request_headers["Authorization"] = f"Bearer {access_token}"
    response = requests.request(method, f"{BASE_URL}{path}", headers=request_headers, **kwargs)

    if retry_on_unauthorized and response.status_code in [401, 422]:
        access_token = get_valid_access_token(force_refresh=True)
        if not access_token:
            return response
        request_headers["Authorization"] = f"Bearer {access_token}"
        response = requests.request(method, f"{BASE_URL}{path}", headers=request_headers, **kwargs)

    return response


def _hosts_read() -> str:
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[Blocker] Cannot read hosts: {e}")
        return ""


def _hosts_write(content: str) -> bool:
    try:
        with open(HOSTS_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except PermissionError:
        print("[Blocker] ⚠️  Permission denied — run tracker.pyw as Administrator to enable blocking.")
        return False
    except Exception as e:
        print(f"[Blocker] Write error: {e}")
        return False


def _strip_blocker_section(content: str) -> str:
    lines  = content.splitlines(keepends=True)
    result = []
    inside = False
    for line in lines:
        if HOSTS_MARKER_START in line:
            inside = True
            continue
        if HOSTS_MARKER_END in line:
            inside = False
            continue
        if not inside:
            result.append(line)
    return "".join(result)


def apply_block(sites: list) -> bool:
    if not sites:
        return False
    content = _hosts_read()
    content = _strip_blocker_section(content)
    block_lines = [f"\n{HOSTS_MARKER_START}\n"]
    for site in sites:
        site = site.strip().lower()
        if not site:
            continue
        block_lines.append(f"127.0.0.1  {site}\n")
        if not site.startswith("www."):
            block_lines.append(f"127.0.0.1  www.{site}\n")
    block_lines.append(f"{HOSTS_MARKER_END}\n")
    new_content = content + "".join(block_lines)
    success     = _hosts_write(new_content)
    if success:
        print(f"[Blocker] 🔒 Blocked {len(sites)} sites in hosts file.")
    return success


def remove_block() -> bool:
    content     = _hosts_read()
    new_content = _strip_blocker_section(content)
    success     = _hosts_write(new_content)
    if success:
        print("[Blocker] 🔓 All sites unblocked.")
    return success


def fetch_blocker_config(token: str):
    try:
        res = authorized_request(
            "GET",
            "/blocker/config",
            token=token,
            timeout=8
        )
        if res is None:
            return
        if res.status_code == 200:
            data = res.json()
            blocker_state["enabled"]   = data.get("enabled", True)
            blocker_state["threshold"] = data.get("risk_threshold", 70)
            blocker_state["sites"]     = data.get("sites", [])
            grace_str = data.get("grace_until")
            if grace_str:
                try:
                    blocker_state["grace_until"] = datetime.strptime(grace_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    blocker_state["grace_until"] = None
            else:
                blocker_state["grace_until"] = None
            blocker_state["last_config_fetch"] = datetime.now()
            print(f"[Blocker] Config synced — threshold: {blocker_state['threshold']}, "
                  f"sites: {len(blocker_state['sites'])}, enabled: {blocker_state['enabled']}")
    except Exception as e:
        print(f"[Blocker] Config fetch error: {e}")


def report_block_status(token: str, blocked: bool):
    try:
        authorized_request(
            "POST",
            "/blocker/report-status",
            token=token,
            json={"blocked": blocked},
            headers={"Content-Type": "application/json"},
            timeout=8
        )
    except Exception:
        pass


def evaluate_blocker(token: str, risk_score: int):
    if not blocker_state["enabled"]:
        if blocker_state["currently_blocked"]:
            remove_block()
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
        return

    last_fetch = blocker_state.get("last_config_fetch")
    ttl        = blocker_state["config_ttl_secs"]
    if last_fetch is None or (datetime.now() - last_fetch).total_seconds() > ttl:
        fetch_blocker_config(token)

    grace = blocker_state.get("grace_until")
    if grace and datetime.now() < grace:
        if blocker_state["currently_blocked"]:
            remove_block()
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
            print(f"[Blocker] 🕐 Grace period active until {grace.strftime('%H:%M:%S')}")
        return

    threshold    = blocker_state["threshold"]
    sites        = blocker_state["sites"]
    should_block = risk_score >= threshold and bool(sites)

    if should_block and not blocker_state["currently_blocked"]:
        success = apply_block(sites)
        if success:
            blocker_state["currently_blocked"] = True
            report_block_status(token, True)
            threading.Thread(
                target=show_attractive_alert,
                args=(
                    f"🔒 WEBSITE BLOCKER ACTIVATED\n\n"
                    f"Risk score: {risk_score}/100 (threshold: {threshold})\n\n"
                    f"{len(sites)} distracting sites have been blocked.\n"
                    f"Focus up and your score will drop! 💪"
                ),
                daemon=True
            ).start()
            send_whatsapp_tracker_async(
                f"🔒 *Sites Blocked Automatically*\n\n"
                f"Risk score hit *{risk_score}/100* (threshold: {threshold}).\n"
                f"{len(sites)} sites blocked on your PC.\n\n"
                f"Stay focused — they'll unblock when your score drops! 💪"
            )

    elif not should_block and blocker_state["currently_blocked"]:
        success = remove_block()
        if success:
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
            print(f"[Blocker] ✅ Risk dropped to {risk_score} — sites unblocked.")
            send_whatsapp_tracker_async(
                f"✅ *Sites Unblocked!*\n\n"
                f"Risk score back to *{risk_score}/100*.\n"
                f"Well done! Keep it up 🎯"
            )


def cleanup_on_exit():
    if blocker_state["currently_blocked"]:
        print("[Blocker] Tracker exiting — removing hosts block...")
        remove_block()

atexit.register(cleanup_on_exit)


# ─────────────────────────────────────────────
# DELTA TRACKER
# ─────────────────────────────────────────────
class DeltaTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.prod  = 0
        self.dist  = 0

    def add(self, activity_type: str, seconds: int):
        with self._lock:
            if   activity_type == "productive":  self.prod += seconds
            elif activity_type == "distracting": self.dist += seconds

    def flush(self) -> dict:
        with self._lock:
            result    = {"productive": self.prod, "distracting": self.dist}
            self.prod = 0
            self.dist = 0
        return result

delta_tracker = DeltaTracker()


# ─────────────────────────────────────────────
# WHATSAPP ALERT HELPER
# ─────────────────────────────────────────────
def send_whatsapp_from_tracker(message: str) -> bool:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except: pass

    phone_num = os.getenv("WHATSAPP_PHONE", "").strip().replace(" ", "").replace("-", "")
    api_key   = os.getenv("WHATSAPP_API_KEY", "").strip()

    if not phone_num or not api_key:
        return False

    try:
        url    = "https://api.callmebot.com/whatsapp.php"
        params = {"phone": phone_num, "text": message, "apikey": api_key}
        resp   = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200 and "Message queued" in resp.text:
            print(f"[WhatsApp-Tracker] ✅ Sent: {message[:50]}...")
            return True
        print(f"[WhatsApp-Tracker] ❌ {resp.status_code}: {resp.text[:80]}")
        return False
    except Exception as e:
        print(f"[WhatsApp-Tracker] Error: {e}")
        return False


def send_whatsapp_tracker_async(message: str):
    threading.Thread(
        target=send_whatsapp_from_tracker,
        args=(message,),
        daemon=True
    ).start()


def send_telegram_from_tracker(message: str) -> bool:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except:
        pass

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return False

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=15
        )
        if resp.status_code == 200 and resp.json().get("ok"):
            print(f"[Telegram-Tracker] Sent: {message[:50]}...")
            return True
        print(f"[Telegram-Tracker] Failed {resp.status_code}: {resp.text[:80]}")
        return False
    except Exception as e:
        print(f"[Telegram-Tracker] Error: {e}")
        return False


def send_telegram_tracker_async(message: str):
    threading.Thread(
        target=send_telegram_from_tracker,
        args=(message,),
        daemon=True
    ).start()


send_whatsapp_from_tracker = send_telegram_from_tracker
send_whatsapp_tracker_async = send_telegram_tracker_async


# ─────────────────────────────────────────────
# VOICE ALERT
# ─────────────────────────────────────────────
def speak_alert(message):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty('rate', 160)
        engine.setProperty('volume', 1.0)
        engine.say(message)
        engine.runAndWait()
    except Exception as e:
        print(f"[Voice Alert Error] {e}")


# ─────────────────────────────────────────────
# POPUP ALERT
# ─────────────────────────────────────────────
def show_attractive_alert(message):
    threading.Thread(
        target=speak_alert,
        args=("Hey! Wake up! You have been distracted for too long. Get back to work!",),
        daemon=True
    ).start()

    root = tk.Tk()
    root.withdraw()
    top  = tk.Toplevel(root)
    top.title("🚨 DISTRACTION ALERT 🚨")
    top.geometry("600x300")
    top.configure(bg="#EF4444")
    top.attributes('-topmost', True)
    top.update_idletasks()
    x = (top.winfo_screenwidth()  - top.winfo_reqwidth())  // 2
    y = (top.winfo_screenheight() - top.winfo_reqheight()) // 2
    top.geometry(f"+{x}+{y}")
    lbl = tk.Label(top, text=message, font=("Arial", 14, "bold"),
                   bg="#EF4444", fg="white", wraplength=520, justify="center")
    lbl.pack(expand=True, pady=20)
    def close_window(): top.destroy(); root.destroy()
    btn = tk.Button(top, text="I WILL FOCUS NOW!", font=("Arial", 14, "bold"),
                    bg="white", fg="#EF4444", command=close_window,
                    padx=20, pady=10, cursor="hand2")
    btn.pack(pady=20)
    root.mainloop()


# ─────────────────────────────────────────────
# INCOGNITO MODE TRAY (updated with blocker status)
# ─────────────────────────────────────────────
def create_tray_window():
    tray = tk.Tk()
    tray.title("Digital Twin Tracker")
    tray.geometry("240x140+10+10")
    tray.attributes('-topmost', True)
    tray.resizable(False, False)
    tray.configure(bg="#1e1e2e")

    status_label = tk.Label(tray, text=f"🟢 {DEVICE_NAME[:22]}",
                             font=("Arial", 9, "bold"), bg="#1e1e2e", fg="#10B981")
    status_label.pack(pady=(6, 0))

    sync_label = tk.Label(tray, text="🔄 Syncing to server...",
                          font=("Arial", 8), bg="#1e1e2e", fg="#A78BFA")
    sync_label.pack(pady=(0, 2))

    blocker_label = tk.Label(tray, text="🔓 Blocker: standby",
                              font=("Arial", 8), bg="#1e1e2e", fg="#6b7280")
    blocker_label.pack(pady=(0, 2))

    wa_status = tk.Label(tray, text="📨 Telegram: checking...",
                         font=("Arial", 7), bg="#1e1e2e", fg="#6b7280")
    wa_status.pack(pady=(0, 2))

    def check_wa_status():
        try:
            from dotenv import load_dotenv
            load_dotenv(ENV_FILE)
        except: pass
        chat_id   = os.getenv("TELEGRAM_CHAT_ID", "")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if chat_id and bot_token:
            wa_status.config(text=f"📨 Telegram: {chat_id[-4:]} ✅", fg="#10B981")
        else:
            wa_status.config(text="📨 Telegram: not configured", fg="#6b7280")

    tray.after(2000, check_wa_status)

    def toggle_incognito():
        incognito_state["active"] = not incognito_state["active"]
        if incognito_state["active"]:
            btn.config(text="🔴 Incognito ON",  bg="#EF4444", fg="white")
            status_label.config(text="🔴 Tracking Paused", fg="#EF4444")
            if blocker_state["currently_blocked"]:
                remove_block()
                blocker_state["currently_blocked"] = False
        else:
            btn.config(text="🟢 Incognito OFF", bg="#10B981", fg="white")
            status_label.config(text=f"🟢 {DEVICE_NAME[:22]}", fg="#10B981")
        print(f"[Tracker] Incognito: {incognito_state['active']}")

    btn = tk.Button(tray, text="🟢 Incognito OFF", font=("Arial", 9, "bold"),
                    bg="#10B981", fg="white", command=toggle_incognito,
                    relief="flat", padx=8, pady=3, cursor="hand2")
    btn.pack()

    def refresh_labels():
        if hasattr(create_tray_window, '_last_sync'):
            sync_label.config(text=create_tray_window._last_sync)
        if blocker_state["currently_blocked"]:
            blocker_label.config(
                text=f"🔒 Blocker: ACTIVE ({len(blocker_state['sites'])} sites)",
                fg="#EF4444"
            )
        elif blocker_state["enabled"]:
            blocker_label.config(
                text=f"🔓 Blocker: watching (>{blocker_state['threshold']})",
                fg="#10B981"
            )
        else:
            blocker_label.config(text="🔓 Blocker: disabled", fg="#6b7280")
        tray.after(5000, refresh_labels)

    tray.after(5000, refresh_labels)
    tray.mainloop()


# ─────────────────────────────────────────────
# DEVICE ID HELPERS
# ─────────────────────────────────────────────
def get_or_create_device_id() -> str:
    if os.path.exists(DEVICE_ID_FILE):
        try:
            with open(DEVICE_ID_FILE, "r") as f:
                did = f.read().strip()
                if did: return did
        except: pass
    try:
        import uuid as _uuid
        mac = hex(_uuid.getnode())
    except:
        mac = "unknown-mac"
    hostname = platform.node() or "unknown-host"
    system   = platform.system() or "unknown-os"
    raw      = f"{mac}-{hostname}-{system}"
    did      = "win-" + hashlib.sha256(raw.encode()).hexdigest()[:16]
    try:
        with open(DEVICE_ID_FILE, "w") as f:
            f.write(did)
    except: pass
    return did

def get_device_name() -> str:
    hostname = platform.node() or "My PC"
    return f"{hostname} (Windows)"

def get_device_type() -> str:
    sys = platform.system().lower()
    if sys == "windows": return "windows"
    if sys == "darwin":  return "mac"
    if sys == "linux":   return "linux"
    return "unknown"

DEVICE_ID   = get_or_create_device_id()
DEVICE_NAME = get_device_name()
DEVICE_TYPE = get_device_type()

print(f"[Device] ID: {DEVICE_ID}")
print(f"[Device] Name: {DEVICE_NAME}")
print(f"[Device] Type: {DEVICE_TYPE}")


# ─────────────────────────────────────────────
# ACTIVE WINDOW DETECTION
# ─────────────────────────────────────────────
def get_active_window():
    try:
        import win32gui
        window = win32gui.GetForegroundWindow()
        return win32gui.GetWindowText(window)
    except Exception:
        return ""


# ─────────────────────────────────────────────
# REGULAR ACTIVITY LOG
# ─────────────────────────────────────────────
def send_activity(token, app_name, duration_seconds):
    headers = {"Content-Type": "application/json"}
    try:
        response = authorized_request(
            "POST",
            "/activity/log",
            token=token,
            json={
                "app":         app_name,
                "duration":    duration_seconds,
                "device_id":   DEVICE_ID,
                "device_name": DEVICE_NAME
            },
            headers=headers,
            timeout=10
        )
        if response is None:
            return "UNAUTHORIZED", None
        if response.status_code in [401, 422]:
            return "UNAUTHORIZED", None
        if response.status_code != 200:
            return "ERROR", None

        response_data = response.json()
        activity_type = response_data.get("type", "neutral")
        delta_tracker.add(activity_type, duration_seconds)

        if "alert" in response_data:
            print("🚨 TRIGGERING ALERT POPUP!")
            threading.Thread(
                target=show_attractive_alert,
                args=(response_data["alert"],),
                daemon=True
            ).start()

        print(
            f"✅ [{DEVICE_NAME[:18]}] {app_name} ({duration_seconds}s) | "
            f"Risk: {response_data.get('risk_score','?')} | "
            f"Focus: {response_data.get('focus_level','?')} | "
            f"Type: {activity_type}"
        )
        return "SUCCESS", response_data

    except Exception as e:
        print(f"⚠️ Server not reachable: {e}")
        return "ERROR", None


# ─────────────────────────────────────────────
# DEVICE REGISTRATION
# ─────────────────────────────────────────────
def register_device(token) -> bool:
    headers = {"Content-Type": "application/json"}
    try:
        response = authorized_request(
            "POST",
            "/devices/register",
            token=token,
            json={"device_id": DEVICE_ID, "device_name": DEVICE_NAME, "device_type": DEVICE_TYPE},
            headers=headers,
            timeout=10
        )
        if response is None:
            return False
        if response.status_code in [401, 422]:
            _clear_auth_state()
            return False
        if response.status_code == 200:
            data     = response.json()
            is_new   = data.get("is_new", False)
            is_prim  = data.get("is_primary", False)
            status   = "🆕 Registered" if is_new else "♻️ Updated"
            primary  = " (Primary Device)" if is_prim else ""
            print(f"[Device Sync] {status}: {DEVICE_NAME}{primary}")
            create_tray_window._last_sync = f"✅ Device registered{primary}"
            if is_new:
                send_whatsapp_tracker_async(
                    f"🖥️ *New Device Connected!*\n\n"
                    f"*{DEVICE_NAME}* is now tracking your activity.\n"
                    f"Type: {DEVICE_TYPE.upper()}\n\n"
                    f"Your Digital Twin is watching! Stay focused 🎯"
                )
            return True
        else:
            print(f"[Device Sync] Registration error: {response.status_code}")
            return False
    except Exception as e:
        print(f"[Device Sync] Registration failed: {e}")
        return False


# ─────────────────────────────────────────────
# HEARTBEAT LOOP — now calls evaluate_blocker
# ─────────────────────────────────────────────
def send_heartbeat(token):
    headers = {"Content-Type": "application/json"}
    deltas  = delta_tracker.flush()
    try:
        response = authorized_request(
            "POST",
            "/devices/heartbeat",
            token=token,
            json={
                "device_id":         DEVICE_ID,
                "productive_delta":  deltas["productive"],
                "distracting_delta": deltas["distracting"]
            },
            headers=headers,
            timeout=10
        )
        if response is None:
            delta_tracker.add("productive",  deltas["productive"])
            delta_tracker.add("distracting", deltas["distracting"])
            return False, {}
        if response.status_code in [401, 422]:
            _clear_auth_state()
            delta_tracker.add("productive",  deltas["productive"])
            delta_tracker.add("distracting", deltas["distracting"])
            return False, {}
        if response.status_code == 200:
            data          = response.json()
            now_str       = datetime.now().strftime("%H:%M:%S")
            notifications = data.get("pending_notifications", [])
            prod_s        = deltas["productive"]
            dist_s        = deltas["distracting"]
            create_tray_window._last_sync = f"🔄 Synced {now_str}"
            print(f"[Heartbeat] ✅ {now_str} | +{prod_s}s prod, +{dist_s}s dist | Notifs: {len(notifications)}")

            for notif in notifications:
                msg  = notif.get("message", "")
                ntyp = notif.get("type", "info")
                if msg and ntyp in ["warning", "danger"]:
                    threading.Thread(
                        target=show_attractive_alert,
                        args=(f"📱 Cross-Device Alert:\n\n{msg}",),
                        daemon=True
                    ).start()
                elif msg:
                    print(f"[Notification] {msg}")
            return True, data
        elif response.status_code == 404:
            print("[Heartbeat] Device not found on server, re-registering...")
            return False, {}
        else:
            print(f"[Heartbeat] Server error: {response.status_code}")
            return True, {}
    except Exception as e:
        print(f"[Heartbeat] Failed: {e}")
        create_tray_window._last_sync = "⚠️ Sync failed"
        delta_tracker.add("productive",  deltas["productive"])
        delta_tracker.add("distracting", deltas["distracting"])
        return True, {}


def heartbeat_loop():
    device_registered = False
    last_hb_time      = 0
    while True:
        token = get_valid_access_token()
        if not token:
            device_registered = False
            time.sleep(5)
            continue
        if incognito_state["active"]:
            if blocker_state["currently_blocked"]:
                remove_block()
                blocker_state["currently_blocked"] = False
            time.sleep(HB_INTERVAL)
            continue
        now = time.time()
        if not device_registered:
            success = register_device(token)
            if success:
                device_registered = True
                fetch_blocker_config(token)
            else:
                time.sleep(10)
                continue
        if now - last_hb_time >= HB_INTERVAL:
            result, hb_data = send_heartbeat(token)
            if not result:
                device_registered = False
            else:
                # ── BLOCKER EVALUATION ──────────────────────────
                try:
                    rs = authorized_request(
                        "GET",
                        "/twin/recommendation",
                        token=token,
                        timeout=8
                    )
                    if rs is not None and rs.status_code == 200:
                        risk = rs.json().get("risk_score", 0)
                        evaluate_blocker(token, risk)
                except Exception as be:
                    print(f"[Blocker] Risk fetch error: {be}")
            last_hb_time = now
        time.sleep(5)


# ─────────────────────────────────────────────
# OCR SCREEN VERIFICATION
# ─────────────────────────────────────────────
def take_ocr_screenshot(token, app_name):
    try:
        from PIL import ImageGrab
        import pytesseract
        screenshot = ImageGrab.grab()
        w, h       = screenshot.size
        screenshot = screenshot.resize((w // 2, h // 2))
        ocr_text   = pytesseract.image_to_string(screenshot)
        if not ocr_text.strip():
            print("[OCR] Screen is blank — skipping")
            return
        headers = {"Content-Type": "application/json"}
        response = authorized_request(
            "POST",
            "/activity/log-ocr",
            token=token,
            json={"ocr_text": ocr_text[:3000], "app": app_name, "duration": OCR_INTERVAL},
            headers=headers, timeout=15
        )
        if response is None:
            print("[OCR] No valid login session for OCR sync.")
            return
        if response.status_code == 200:
            data   = response.json()
            result = data.get("ocr_result", "?")
            words  = data.get("word_count", 0)
            emoji  = {"productive": "✅", "distracted": "⚠️", "idle": "💤"}.get(result, "❓")
            print(f"[OCR] {emoji} {result.upper()} | Words: {words} | App: {app_name}")
        else:
            print(f"[OCR] Server returned {response.status_code}")
    except ImportError:
        print("[OCR] pytesseract or Pillow not installed.")
    except Exception as e:
        print(f"[OCR] Error: {e}")


# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
if __name__ == "__main__":

    print("=" * 60)
    print("🟢 Digital Behaviour Twin Tracker  v2.2 (Auto Blocker)")
    print("=" * 60)
    print(f"   Device ID  : {DEVICE_ID}")
    print(f"   Device Name: {DEVICE_NAME}")
    print(f"   Device Type: {DEVICE_TYPE}")
    print(f"   Activity log every    : {INTERVAL}s")
    print(f"   OCR screenshot every  : {OCR_INTERVAL}s")
    print(f"   Server heartbeat every: {HB_INTERVAL}s")
    print(f"   ⚠️  Run as Administrator for hosts file blocking!")

    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except: pass
    wa_configured = bool(os.getenv("TELEGRAM_CHAT_ID") and os.getenv("TELEGRAM_BOT_TOKEN"))
    print(f"   Telegram alerts: {'✅ Configured' if wa_configured else '❌ Not configured (.env missing)'}")
    print("   Waiting for user to login on dashboard...\n")

    create_tray_window._last_sync = "⏳ Waiting for login..."

    tray_thread = threading.Thread(target=create_tray_window, daemon=True)
    tray_thread.start()

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    last_activity = datetime.now()
    last_ocr      = datetime.now()

    while True:
        token = get_valid_access_token()
        if not token:
            time.sleep(3)
            last_activity = datetime.now()
            last_ocr      = datetime.now()
            continue
        if incognito_state["active"]:
            time.sleep(INTERVAL)
            last_activity = datetime.now()
            continue
        app_name = get_active_window()
        if not app_name or app_name.strip() == "" or app_name == "Windows Default Lock Screen":
            time.sleep(INTERVAL)
            last_activity = datetime.now()
            continue
        now       = datetime.now()
        elapsed   = round((now - last_activity).total_seconds())
        last_activity = now
        duration  = max(5, min(elapsed, 60))
        status, _ = send_activity(token, app_name, duration)
        if status == "UNAUTHORIZED":
            print("🔄 Token expired — waiting for re-login...")
            _clear_auth_state()
        ocr_elapsed = (now - last_ocr).total_seconds()
        if ocr_elapsed >= OCR_INTERVAL:
            last_ocr = now
            threading.Thread(target=take_ocr_screenshot, args=(token, app_name), daemon=True).start()
        time.sleep(INTERVAL)
