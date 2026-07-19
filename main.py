import os
import re
import time
import requests
from flask import Flask, jsonify
from flask_cors import CORS
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ---------- CONFIG ----------
BASE_URL = os.environ.get("PANEL_BASE_URL", "http://51.89.99.105/NumberPanel")
USERNAME = os.environ.get("PANEL_USER", "dtz786")
PASSWORD = os.environ.get("PANEL_PASS", "dtz786")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE_URL,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/plain, */*"
}

# ---------- STATE ----------
session = None
sesskey = None
last_login = 0
login_lock = Lock()
otp_cache = {"data": [], "timestamp": 0}
cache_lock = Lock()

consecutive_failures = 0
FAILURE_THRESHOLD = 5
BREAKER_TIMEOUT = 30

# ---------- LOGGING ----------
log_messages = []
MAX_LOGS = 100

def add_log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_messages.append(f"[{timestamp}] {message}")
    if len(log_messages) > MAX_LOGS:
        log_messages.pop(0)

# ---------- FULL COUNTRY MAP (complete – truncated for brevity) ----------
# (keep the same COUNTRY_MAP and FLAG_MAP as before – they are unchanged)

# ---------- HELPERS ----------
def get_country(phone_digits):
    for length in range(4, 0, -1):
        prefix = phone_digits[:length]
        if prefix in COUNTRY_MAP:
            return COUNTRY_MAP[prefix]
    return None

def clean_number(raw):
    digits = re.sub(r'\D', '', raw)
    if not digits or len(digits) < 7:
        return None
    info = get_country(digits)
    if info:
        cc = info['code'].replace('+', '')
        rest = digits[len(cc):] if digits.startswith(cc) else digits
        if len(rest) < 7:
            return None
        phone = info['code'] + rest
        country = info['name']
    else:
        phone = '+' + digits
        country = 'Unknown'
    flag = FLAG_MAP.get(country, '🌍')
    return {'phone': phone, 'country': country, 'flag': flag}

def is_likely_phone(text):
    digits = re.sub(r'\D', '', text)
    return len(digits) >= 7

def extract_otp(text):
    if not text:
        return None
    clean = re.sub(r'\n', ' ', text).strip()
    patterns = [
        r'#\s*(\d{4,8})',
        r'(?:code|otp|verification\s*code|confirm\s*code|auth\s*code)\s*(?:is|:)?\s*(\d{4,8})',
        r'your\s+whatsapp\s+code\s*:\s*(\d{4,8})',
        r'(?<![0-9+])(\d{4,8})(?![0-9])',
        r'(\d{3,4})[\- ](\d{3,4})'
    ]
    for pat in patterns:
        m = re.search(pat, clean, re.I)
        if m:
            if pat == patterns[-1] and len(m.groups()) == 2:
                combined = m.group(1) + m.group(2)
                if 4 <= len(combined) <= 8:
                    return combined
            else:
                if pat == patterns[3]:
                    val = m.group(1)
                    if re.match(r'^(584|1|7|8|9)', val) and len(val) >= 10:
                        continue
                return m.group(1)
    return None

# ---------- SESSION MANAGEMENT ----------
def create_session_with_retries():
    sess = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
    sess.mount('http://', adapter)
    sess.mount('https://', adapter)
    return sess

def extract_sesskey_from_html(html):
    """Try multiple patterns to find sesskey in the HTML."""
    patterns = [
        r'data_smscdr\.php[^"]*sesskey=([^&"\s]+)',
        r'data_smsnumbers\.php[^"]*sesskey=([^&"\s]+)',
        r'sesskey=([^&\s"\']+)',
        r'var\s+sesskey\s*=\s*["\']([^"\']+)["\'];',
        r'SESSKEY\s*[:=]\s*["\']?([a-zA-Z0-9+/=]+)["\']?',
        r'"sesskey":"([^"]+)"'   # sometimes in JSON
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def fetch_sesskey():
    global sesskey
    # Try to get sesskey from multiple pages
    pages = [
        f"{BASE_URL}/client/SMSCDRStats",
        f"{BASE_URL}/client/MySMSNumbers",
        f"{BASE_URL}/agent/SMSCDRStats",  # fallback to agent if exists
    ]
    for url in pages:
        try:
            headers = {**HEADERS, "Referer": url}
            if session and session.cookies:
                headers["Cookie"] = '; '.join([f"{k}={v}" for k, v in session.cookies.items()])
            resp = session.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                add_log(f"Sesskey check on {url} returned {resp.status_code}")
                continue
            found = extract_sesskey_from_html(resp.text)
            if found:
                sesskey = found
                add_log(f"Sesskey found on {url}: {sesskey}")
                return True
            else:
                add_log(f"No sesskey pattern on {url}")
        except Exception as e:
            add_log(f"Error fetching sesskey from {url}: {e}")
    # If we get here, no sesskey found
    sesskey = None
    add_log("Sesskey could not be found on any page – will continue without it")
    return False

def login():
    global session, sesskey, last_login
    with login_lock:
        if session and (time.time() - last_login) < 3600:
            add_log("Login skipped – session still valid")
            return True
        add_log("Starting login...")
        try:
            session = create_session_with_retries()
            login_paths = ["/login", "/sign-in"]
            success = False

            for login_path in login_paths:
                try:
                    login_url = f"{BASE_URL}{login_path}"
                    r1 = session.get(login_url, headers=HEADERS, timeout=10)
                    if r1.status_code in (503, 403):
                        add_log(f"Login page {login_path} returned {r1.status_code}, waiting 3s")
                        time.sleep(3)
                        continue
                    if r1.status_code != 200:
                        add_log(f"Login page {login_path} returned {r1.status_code}, skipping")
                        continue

                    html = r1.text
                    captcha_match = re.search(r'What is (\d+) \+ (\d+) = \?', html)
                    if not captcha_match:
                        add_log(f"No captcha on {login_path}, skipping")
                        continue
                    ans = int(captcha_match[1]) + int(captcha_match[2])
                    add_log(f"Captcha answer: {ans}")

                    signin_url = f"{BASE_URL}/signin"
                    data = {"username": USERNAME, "password": PASSWORD, "capt": str(ans)}
                    headers_post = {**HEADERS, "Content-Type": "application/x-www-form-urlencoded"}

                    r2 = session.post(
                        signin_url,
                        data=data,
                        headers=headers_post,
                        allow_redirects=False,
                        timeout=10
                    )
                    add_log(f"POST /signin status: {r2.status_code}")

                    if r2.status_code in (503, 403):
                        add_log(f"Signin {r2.status_code}, waiting 3s")
                        time.sleep(3)
                        continue

                    if r2.status_code in (302, 301):
                        last_login = time.time()
                        add_log(f"Login successful ({r2.status_code})")
                        success = True
                        break
                    elif r2.status_code == 200:
                        html2 = r2.text.lower()
                        if "logout" in html2 or "dashboard" in html2:
                            last_login = time.time()
                            add_log("Login successful (200 with logout/dashboard)")
                            success = True
                            break
                except Exception as e:
                    add_log(f"Error with {login_path}: {e}")

            if not success:
                add_log("All login paths failed")
                session = None
                return False

            # Try to fetch sesskey (optional)
            fetch_sesskey()
            return True
        except Exception as e:
            add_log(f"Login failed: {e}")
            session = None
            sesskey = None
            return False

def ensure_session():
    if not session or (time.time() - last_login) > 3600:
        add_log("Session expired, re-logging...")
        return login()
    return True

# ---------- FETCH NUMBERS ----------
def fetch_numbers():
    if not ensure_session():
        add_log("ensure_session failed in fetch_numbers")
        return []
    params = {
        "frange": "",
        "fclient": "",
        "sEcho": "1",
        "iDisplayStart": "0",
        "iDisplayLength": "-1",
        "_": int(time.time() * 1000)
    }
    if sesskey:
        params["sesskey"] = sesskey
    try:
        url = f"{BASE_URL}/client/res/data_smsnumbers.php"
        headers = {**HEADERS, "Referer": f"{BASE_URL}/client/MySMSNumbers"}
        if session and session.cookies:
            headers["Cookie"] = '; '.join([f"{k}={v}" for k, v in session.cookies.items()])
        resp = session.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            add_log(f"Numbers fetch HTTP {resp.status_code}")
            return []
        data = resp.json()
        result = []
        for row in data.get("aaData", []):
            if len(row) < 4:
                continue
            raw = None
            for col in row:
                if is_likely_phone(col):
                    raw = col.strip()
                    break
            if not raw:
                if len(row) > 2:
                    raw = row[2].strip()
                elif len(row) > 1:
                    raw = row[1].strip()
                else:
                    raw = row[0].strip()
            if not raw:
                continue
            cleaned = clean_number(raw)
            if cleaned:
                result.append({
                    "raw": raw,
                    "e164": cleaned['phone'],
                    "country": cleaned['country'],
                    "flag": cleaned['flag']
                })
            else:
                result.append({"raw": raw, "e164": None, "country": "Unknown", "flag": "🌍"})
        add_log(f"Fetched {len(result)} numbers")
        return result
    except Exception as e:
        add_log(f"Numbers error: {e}")
        return []

# ---------- FETCH OTPs ----------
def fetch_otps(limit=100):
    if not ensure_session():
        add_log("ensure_session failed in fetch_otps")
        return []
    today = time.strftime("%Y-%m-%d")
    params = {
        "fdate1": f"{today} 00:00:00",
        "fdate2": f"{today} 23:59:59",
        "frange": "",
        "fnum": "",
        "fcli": "",
        "fgdate": "",
        "fgmonth": "",
        "fgrange": "",
        "fgnumber": "",
        "fgcli": "",
        "fg": "0",
        "sEcho": "1",
        "iDisplayStart": "0",
        "iDisplayLength": str(limit),
        "_": int(time.time() * 1000)
    }
    if sesskey:
        params["sesskey"] = sesskey
    try:
        url = f"{BASE_URL}/client/res/data_smscdr.php"
        headers = {**HEADERS, "Referer": f"{BASE_URL}/client/SMSCDRStats"}
        if session and session.cookies:
            headers["Cookie"] = '; '.join([f"{k}={v}" for k, v in session.cookies.items()])
        resp = session.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            add_log(f"OTP fetch HTTP {resp.status_code}")
            return []
        try:
            data = resp.json()
        except ValueError:
            add_log(f"OTP response is not JSON – likely HTML (sesskey missing or invalid). Preview: {resp.text[:200]}")
            return []
        if not data.get("aaData"):
            add_log("OTP response has no aaData")
            return []
        rows = data["aaData"]
        rows.sort(key=lambda x: x[0] if x and len(x) > 0 else '', reverse=True)
        result = []
        for row in rows:
            if len(row) < 6:
                continue
            number = row[2].strip() if row[2] else ''
            message = row[5].strip() if row[5] else ''
            if not number or not message:
                continue
            otp = extract_otp(message)
            if not otp:
                continue
            service = row[3].strip() if len(row) > 3 and row[3] else 'Unknown'
            timestamp = row[0] if row[0] else ''
            cleaned = clean_number(number)
            country = cleaned['country'] if cleaned else 'Unknown'
            flag = cleaned['flag'] if cleaned else '🌍'
            result.append({
                "number": number,
                "otp": otp,
                "service": service,
                "message": message[:300],
                "timestamp": timestamp,
                "country": country,
                "flag": flag
            })
            if len(result) >= 10:
                break
        add_log(f"Fetched {len(result)} OTPs")
        return result
    except Exception as e:
        add_log(f"OTP error: {e}")
        return []

# ---------- CACHED OTPs ----------
def get_cached_otps():
    with cache_lock:
        now = time.time()
        if otp_cache["data"] and (now - otp_cache["timestamp"]) < 10:
            return otp_cache["data"]
        fresh = fetch_otps(100)
        if fresh:
            otp_cache["data"] = fresh
            otp_cache["timestamp"] = now
            return fresh
        return otp_cache["data"]

# ---------- DEBUG ENDPOINT ----------
@app.route("/debug")
def debug():
    if not ensure_session():
        return jsonify({"error": "Not logged in"}), 500
    url = f"{BASE_URL}/client/res/data_smsnumbers.php"
    params = {"sEcho": "1", "iDisplayStart": "0", "iDisplayLength": "3"}
    headers = {**HEADERS, "Referer": f"{BASE_URL}/client/MySMSNumbers"}
    if session and session.cookies:
        headers["Cookie"] = '; '.join([f"{k}={v}" for k, v in session.cookies.items()])
    resp = session.get(url, headers=headers, params=params, timeout=10)
    if resp.status_code != 200:
        return jsonify({"error": f"HTTP {resp.status_code}"})
    try:
        data = resp.json()
        return jsonify({
            "status": resp.status_code,
            "raw_data": data
        })
    except:
        return jsonify({
            "status": resp.status_code,
            "raw_text": resp.text[:2000]
        })

# ---------- LOGS ENDPOINT ----------
@app.route("/logs")
def logs():
    return jsonify({
        "logs": log_messages,
        "count": len(log_messages)
    })

# ---------- ROUTES ----------
@app.route("/")
def root():
    return jsonify({
        "message": "NumberPanel API – Fixed for new panel",
        "endpoints": ["/numbers", "/sms", "/debug", "/logs"],
        "status": "online"
    })

@app.route("/numbers")
def numbers():
    try:
        data = fetch_numbers()
        return jsonify({"success": True, "count": len(data), "numbers": data})
    except Exception as e:
        add_log(f"/numbers error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/sms")
def sms():
    try:
        data = get_cached_otps()
        return jsonify({"success": True, "count": len(data), "otps": data})
    except Exception as e:
        add_log(f"/sms error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True, port=8000)
