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
from tkinter import messagebox

import pytesseract
from dotenv import load_dotenv
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "backend", ".env")
load_dotenv(ENV_FILE)

BASE_URL     = os.getenv("API_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
INTERVAL     = 5
OCR_INTERVAL = 30
HB_INTERVAL  = 10

AUTH_STATE_FILE = os.path.join(os.path.dirname(__file__), "token.json")
LEGACY_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", "backend", "active_token.txt")

incognito_state = {"active": False}
auth_state = {
    "access_token": None,
    "access_expires_at": None,
    "refresh_token": None,
}
auth_prompt_lock = threading.Lock()
auth_prompt_state = {"active": False}

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
    "focus_lock_until":  None,
    "focus_lock_reason": None,
    "last_config_fetch": None,
    "config_ttl_secs":   10,
}
app_classification_state = {
    "productive": [],
    "distracting": [],
    "last_fetch": None,
    "ttl_secs": 30,
    "last_blocked_title": "",
    "last_blocked_at": None,
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


def _save_auth_state(refresh_token: str, email: str = ""):
    try:
        payload = {
            "refresh_token": refresh_token,
            "email": (email or "").strip().lower(),
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with open(AUTH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"[Auth] Could not save auth state: {e}")


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


def _show_tracker_login_window():
    result = {"email": None, "password": None}

    root = tk.Tk()
    root.title("Digital Twin Tracker Login")
    win_w = 560
    win_h = 560
    root.geometry(f"{win_w}x{win_h}")
    root.minsize(540, 540)
    root.resizable(True, True)
    root.configure(bg="#08111f")
    root.attributes("-topmost", True)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - win_w) // 2
    y = max((root.winfo_screenheight() - win_h) // 2, 30)
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")

    shell = tk.Frame(root, bg="#08111f")
    shell.pack(fill="both", expand=True, padx=14, pady=14)

    card = tk.Frame(shell, bg="#0f172a", highlightbackground="#1e293b", highlightthickness=1)
    card.pack(fill="both", expand=True)

    hero = tk.Frame(card, bg="#14b8a6", height=80)
    hero.pack(fill="x")
    hero.pack_propagate(False)

    tk.Label(
        hero,
        text="Digital Behaviour Twin",
        font=("Arial", 12, "bold"),
        bg="#14b8a6",
        fg="#06202a",
    ).pack(anchor="w", padx=22, pady=(16, 2))

    tk.Label(
        hero,
        text="Tracker sign in",
        font=("Arial", 20, "bold"),
        bg="#14b8a6",
        fg="#ffffff",
    ).pack(anchor="w", padx=22)

    content = tk.Frame(card, bg="#0f172a")
    content.pack(fill="both", expand=True, padx=20, pady=16)

    tk.Label(
        content,
        text="Sign in with the same account you use in the web dashboard.",
        font=("Arial", 10),
        bg="#0f172a",
        fg="#cbd5e1",
        wraplength=480,
        justify="left",
    ).pack(anchor="w")

    tk.Label(
        content,
        text=f"Connected server: {BASE_URL}",
        font=("Arial", 9),
        bg="#0f172a",
        fg="#67e8f9",
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(8, 12))

    features = tk.Frame(content, bg="#111c2f")
    features.pack(fill="x", pady=(0, 12))
    for bullet in (
        "Auto syncs app activity to your live dashboard",
        "Keeps this device linked to your account",
        "Stores the tracker session locally on this laptop",
    ):
        tk.Label(
            features,
            text=f"  {bullet}",
            font=("Arial", 9),
            bg="#111c2f",
            fg="#dbeafe",
            anchor="w",
            padx=10,
            pady=3,
        ).pack(fill="x")

    form = tk.Frame(content, bg="#0f172a")
    form.pack(fill="x")

    tk.Label(form, text="Email address", font=("Arial", 10, "bold"), bg="#0f172a", fg="#e2e8f0").pack(anchor="w")
    email_entry = tk.Entry(
        form,
        font=("Arial", 11),
        relief="flat",
        bg="#f8fafc",
        fg="#0f172a",
        insertbackground="#0f172a",
    )
    email_entry.pack(fill="x", pady=(6, 10), ipady=7)

    tk.Label(form, text="Password", font=("Arial", 10, "bold"), bg="#0f172a", fg="#e2e8f0").pack(anchor="w")
    password_entry = tk.Entry(
        form,
        font=("Arial", 11),
        show="*",
        relief="flat",
        bg="#f8fafc",
        fg="#0f172a",
        insertbackground="#0f172a",
    )
    password_entry.pack(fill="x", pady=(6, 8), ipady=7)

    show_password = tk.BooleanVar(value=False)

    def toggle_password():
        password_entry.config(show="" if show_password.get() else "*")

    tk.Checkbutton(
        form,
        text="Show password",
        variable=show_password,
        command=toggle_password,
        font=("Arial", 9),
        bg="#0f172a",
        fg="#cbd5e1",
        activebackground="#0f172a",
        activeforeground="#ffffff",
        selectcolor="#0f172a",
    ).pack(anchor="w", pady=(0, 8))

    tk.Label(
        form,
        text="Pomodoro blocking works for distracting websites/domains. Run tracker as Administrator for reliable hosts-file blocking.",
        font=("Arial", 8),
        bg="#0f172a",
        fg="#94a3b8",
        wraplength=480,
        justify="left",
    ).pack(anchor="w", pady=(0, 8))

    status_label = tk.Label(
        content,
        text="",
        font=("Arial", 9),
        bg="#0f172a",
        fg="#fca5a5",
        wraplength=480,
        justify="left",
    )
    status_label.pack(anchor="w", pady=(2, 8))

    def submit():
        email = email_entry.get().strip().lower()
        password = password_entry.get()
        if not email or not password:
            status_label.config(text="Email and password both required.")
            return
        result["email"] = email
        result["password"] = password
        root.destroy()

    def cancel():
        root.destroy()

    button_row = tk.Frame(content, bg="#0f172a")
    button_row.pack(fill="x", pady=(6, 0))

    tk.Button(
        button_row,
        text="Connect tracker",
        command=submit,
        bg="#10b981",
        fg="white",
        activebackground="#059669",
        activeforeground="white",
        font=("Arial", 11, "bold"),
        padx=18,
        pady=8,
        cursor="hand2",
        relief="flat",
    ).pack(side="left")

    tk.Button(
        button_row,
        text="Cancel",
        command=cancel,
        bg="#334155",
        fg="white",
        activebackground="#475569",
        activeforeground="white",
        font=("Arial", 10, "bold"),
        padx=18,
        pady=8,
        cursor="hand2",
        relief="flat",
    ).pack(side="right")

    email_entry.focus_set()
    root.bind("<Return>", lambda event: submit())
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result


def _prompt_tracker_login():
    with auth_prompt_lock:
        if _load_refresh_token_from_disk():
            return True
        if auth_prompt_state["active"]:
            return False
        auth_prompt_state["active"] = True

    try:
        while True:
            creds = _show_tracker_login_window()
            email = (creds.get("email") or "").strip().lower()
            password = creds.get("password") or ""
            if not email or not password:
                print("[Auth] Tracker login cancelled. Waiting before retry...")
                create_tray_window._last_sync = "Login cancelled"
                return False

            try:
                response = requests.post(
                    f"{BASE_URL}/auth/login",
                    json={"email": email, "password": password},
                    headers={"Content-Type": "application/json"},
                    timeout=15,
                )
            except Exception as e:
                print(f"[Auth] Tracker login request failed: {e}")
                messagebox.showerror("Tracker Login Failed", f"Could not reach server.\n\n{e}")
                create_tray_window._last_sync = "Login failed"
                continue

            try:
                data = response.json()
            except Exception:
                data = {}

            if response.status_code == 200:
                access_token = data.get("access_token")
                refresh_token = data.get("refresh_token")
                if not access_token or not refresh_token:
                    messagebox.showerror(
                        "Tracker Login Failed",
                        "Server response did not include the required login tokens.",
                    )
                    create_tray_window._last_sync = "Login failed"
                    continue
                _save_auth_state(refresh_token, email)
                auth_state["refresh_token"] = refresh_token
                _set_access_token(access_token)
                create_tray_window._last_sync = "Tracker connected"
                print(f"[Auth] Tracker login successful for {email}")
                return True

            error_msg = data.get("msg") or f"Login failed with status {response.status_code}"
            print(f"[Auth] Tracker login rejected: {error_msg}")
            messagebox.showerror("Tracker Login Failed", error_msg)
            create_tray_window._last_sync = "Login rejected"
    finally:
        with auth_prompt_lock:
            auth_prompt_state["active"] = False


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
        if not force_refresh:
            _prompt_tracker_login()
            refresh_token = _load_refresh_token_from_disk()
            auth_state["refresh_token"] = refresh_token
            if refresh_token:
                return get_valid_access_token(force_refresh=True)
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
            print("[Auth] Refresh token invalid or expired. Opening tracker login...")
            _clear_auth_state()
            if not force_refresh:
                _prompt_tracker_login()
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
            focus_lock_str = data.get("focus_lock_until")
            if focus_lock_str:
                try:
                    blocker_state["focus_lock_until"] = datetime.strptime(focus_lock_str, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    blocker_state["focus_lock_until"] = None
            else:
                blocker_state["focus_lock_until"] = None
            blocker_state["focus_lock_reason"] = data.get("focus_lock_reason")
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


def fetch_app_classifications(token: str):
    last_fetch = app_classification_state.get("last_fetch")
    if last_fetch and (datetime.now() - last_fetch).total_seconds() < app_classification_state["ttl_secs"]:
        return
    try:
        res = authorized_request("GET", "/apps/classifications", token=token, timeout=8)
        if res is None or res.status_code != 200:
            return
        data = res.json()
        app_classification_state["productive"] = [str(v).lower() for v in data.get("productive", [])]
        app_classification_state["distracting"] = [str(v).lower() for v in data.get("distracting", [])]
        app_classification_state["last_fetch"] = datetime.now()
    except Exception as e:
        print(f"[AppBlock] Classification fetch error: {e}")


def get_active_window_details():
    try:
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return hwnd, title, pid
    except Exception:
        return None, "", None


def classify_window_title(title: str) -> str:
    lower_title = (title or "").strip().lower()
    if not lower_title:
        return "neutral"
    for keyword in app_classification_state.get("distracting", []):
        if keyword and keyword in lower_title:
            return "distracting"
    for keyword in app_classification_state.get("productive", []):
        if keyword and keyword in lower_title:
            return "productive"
    return "neutral"


def enforce_focus_lock_app_block(token: str):
    focus_lock_until = blocker_state.get("focus_lock_until")
    if not (focus_lock_until and datetime.now() < focus_lock_until):
        return

    fetch_app_classifications(token)
    hwnd, title, _pid = get_active_window_details()
    if not hwnd or not title:
        return

    lower_title = title.lower()
    if "digital twin tracker login" in lower_title or "digital behaviour twin" in lower_title:
        return

    if classify_window_title(title) != "distracting":
        return

    now = datetime.now()
    if (
        app_classification_state.get("last_blocked_title") == title
        and app_classification_state.get("last_blocked_at")
        and (now - app_classification_state["last_blocked_at"]).total_seconds() < 4
    ):
        return

    try:
        import win32con
        import win32gui
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        app_classification_state["last_blocked_title"] = title
        app_classification_state["last_blocked_at"] = now
        print(f"[AppBlock] Focus lock blocked distracting window: {title[:80]}")
    except Exception as e:
        print(f"[AppBlock] Failed blocking window '{title[:60]}': {e}")


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


def evaluate_blocker(token: str, risk_score: int):
    focus_lock_until = blocker_state.get("focus_lock_until")
    focus_lock_active = bool(focus_lock_until and datetime.now() < focus_lock_until)

    if not blocker_state["enabled"] and not focus_lock_active:
        if blocker_state["currently_blocked"]:
            remove_block()
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
        return

    last_fetch = blocker_state.get("last_config_fetch")
    ttl = blocker_state["config_ttl_secs"]
    if last_fetch is None or (datetime.now() - last_fetch).total_seconds() > ttl:
        fetch_blocker_config(token)
        focus_lock_until = blocker_state.get("focus_lock_until")
        focus_lock_active = bool(focus_lock_until and datetime.now() < focus_lock_until)

    grace = blocker_state.get("grace_until")
    if grace and datetime.now() < grace and not focus_lock_active:
        if blocker_state["currently_blocked"]:
            remove_block()
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
            print(f"[Blocker] Grace period active until {grace.strftime('%H:%M:%S')}")
        return

    threshold = blocker_state["threshold"]
    sites = blocker_state["sites"]
    should_block = bool(sites) and (focus_lock_active or risk_score >= threshold)
    status_reason = (
        f"Pomodoro focus lock until {focus_lock_until.strftime('%H:%M:%S')}"
        if focus_lock_active and focus_lock_until
        else f"Risk score: {risk_score}/100 (threshold: {threshold})"
    )

    if should_block and not blocker_state["currently_blocked"]:
        success = apply_block(sites)
        if success:
            blocker_state["currently_blocked"] = True
            report_block_status(token, True)
            threading.Thread(
                target=show_attractive_alert,
                args=(
                    f"WEBSITE BLOCKER ACTIVATED\n\n"
                    f"{status_reason}\n\n"
                    f"{len(sites)} distracting sites have been blocked.\n"
                    f"Focus up and your score will drop!"
                ),
                daemon=True
            ).start()
            send_whatsapp_tracker_async(
                f"*Sites Blocked Automatically*\n\n"
                f"{status_reason}.\n"
                f"{len(sites)} sites blocked on your PC.\n\n"
                f"Stay focused while the lock is active."
            )
    elif not should_block and blocker_state["currently_blocked"]:
        success = remove_block()
        if success:
            blocker_state["currently_blocked"] = False
            report_block_status(token, False)
            print(f"[Blocker] Risk dropped to {risk_score} - sites unblocked.")
            send_whatsapp_tracker_async(
                f"*Sites Unblocked!*\n\n"
                f"Risk score back to *{risk_score}/100*.\n"
                f"You can use distracting apps again."
            )


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
        try:
            fetch_blocker_config(token)
            enforce_focus_lock_app_block(token)
        except Exception as app_block_err:
            print(f"[AppBlock] Loop error: {app_block_err}")
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
    print(f"   API Base URL: {BASE_URL}")
    print("   Tracker will open its own login window if needed.\n")

    create_tray_window._last_sync = "⏳ Waiting for tracker login..."

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
