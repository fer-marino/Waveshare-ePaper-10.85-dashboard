#!/usr/bin/python3
# -*- coding:utf-8 -*-
import sys
import os
import time
import logging
import threading
import requests
import io
import gc
import socket
import resource
import signal
import json
import asyncio
import pickle
import subprocess
import math
import calendar
import urllib.parse
import re
import importlib.util
from collections import deque
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
from logging.handlers import RotatingFileHandler

# --- GMAIL IMPORTS ---
# Optional: only the Gmail widget needs the Google client, and it is not in
# the Debian package dependencies, so a missing install must not stop startup.
try:
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    GMAIL_AVAILABLE = True
except ImportError:
    GMAIL_AVAILABLE = False

# --- SYSTEM LIMITS ---
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
except Exception as e:
    print(f"Failed to set rlimit: {e}")

# --- PATHS ---
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
LIB_DIR = os.path.join(BASE_DIR, 'lib')
FONT_DIR = os.path.join(BASE_DIR, 'fnt')
ICON_DIR = os.path.join(BASE_DIR, 'icons')

# --- CONFIG LOADING ---
# Settings live outside the code so a package can ship without anyone's
# credentials in it, and so upgrading never overwrites local values. First
# match wins; everything below falls back to the defaults defined here.
CONFIG_PATHS = [
    os.environ.get('EPAPER_CONFIG', ''),
    '/etc/epaper-dashboard/config.py',
    os.path.join(BASE_DIR, 'config.py'),
]

_cfg = {}
CONFIG_FILE = None
for _path in CONFIG_PATHS:
    if _path and os.path.exists(_path):
        try:
            _spec = importlib.util.spec_from_file_location('epaper_config', _path)
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _cfg = {k: v for k, v in vars(_mod).items() if not k.startswith('_')}
            CONFIG_FILE = _path
        except Exception as _e:
            print(f"Failed to load config {_path}: {_e}")
        break


def conf(name, default):
    return _cfg.get(name, default)


# Writable state (tokens, logs). Kept apart from the code so the package
# directory can stay read-only.
STATE_DIR = conf('STATE_DIR', BASE_DIR)
try:
    os.makedirs(STATE_DIR, exist_ok=True)
except OSError:
    STATE_DIR = BASE_DIR

LOG_FILE = os.path.join(STATE_DIR, 'dashboard.log')

# --- WIDGET TOGGLES --- (override any of these in the config file)
ENABLE_STRAVA = conf('ENABLE_STRAVA', False)  # paid tier only
ENABLE_BAMBU = conf('ENABLE_BAMBU', False)
ENABLE_ROBOROCK = conf('ENABLE_ROBOROCK', False)
ENABLE_ANTIGRAVITY = conf('ENABLE_ANTIGRAVITY', False)
ENABLE_CODEX = conf('ENABLE_CODEX', False)
ENABLE_CLAUDE = conf('ENABLE_CLAUDE', False)
ENABLE_SPOTIFY = conf('ENABLE_SPOTIFY', False)
# Replaces the Gmail slot with DSL line stats read straight off the router.
ENABLE_FRITZBOX = conf('ENABLE_FRITZBOX', True)
# Top-left slot: a phrase a day in the local language.
ENABLE_PHRASE = conf('ENABLE_PHRASE', True)

# --- API ENDPOINTS ---
API_ENDPOINTS = {
    'weather': 'https://api.open-meteo.com/v1/forecast',
    'aqi': 'https://air-quality-api.open-meteo.com/v1/air-quality',
    'strava_token': 'https://www.strava.com/oauth/token',
    'strava_auth': 'https://www.strava.com/oauth/authorize',
    'strava_activities': 'https://www.strava.com/api/v3/athlete/activities',
    'btc': 'https://api.coingecko.com/api/v3/coins/bitcoin/market_chart',
    'eth': 'https://api.coingecko.com/api/v3/coins/ethereum/market_chart',
    'lastfm': 'http://ws.audioscrobbler.com/2.0/'
}

# --- CONFIGURATION ---
LOCATION_LAT = conf('LOCATION_LAT', 50.1109)
LOCATION_LON = conf('LOCATION_LON', 8.6821)

PRINTER_CONF = conf('PRINTER_CONF', {'IP': '', 'SERIAL': '', 'ACCESS_CODE': ''})

ROBOROCK_CONF = conf('ROBOROCK_CONF', {'EMAIL': ''})

FRITZBOX_CONF = conf('FRITZBOX_CONF', {'HOST': 'fritz.box'})

LASTFM_CONF = conf('LASTFM_CONF', {'API_KEY': '', 'USERNAME': ''})

STRAVA_CONF = {'TOKEN_FILE': os.path.join(STATE_DIR, 'strava_token.json')}

# --- FILES & SCOPES ---
GMAIL_TOKEN_PATH = os.path.join(STATE_DIR, 'token.json')
ROBOROCK_TOKEN_FILE = os.path.join(STATE_DIR, 'roborock_session.pkl')
ROBOROCK_STATS_FILE = os.path.join(STATE_DIR, 'roborock_stats.json')
GMAIL_SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

if os.path.exists(LIB_DIR):
    sys.path.append(LIB_DIR)

# --- GPIO PIN FACTORY ---
# Prefer the lgpio backend: the native/sysfs factory is broken on Raspberry Pi
# OS Bookworm (kernel 6.x) and fails to export the pins. If lgpio is not
# available (older OS images), fall back to gpiozero's default instead of
# hard-crashing on import.
#
# On import, lgpio creates its ".lgd-nfy*" notification pipe in the current
# working directory. Import from /tmp so it never depends on the permissions
# or location of the project directory, then restore the working directory.
_prev_cwd = os.getcwd()
try:
    os.chdir('/tmp')
    from gpiozero import Device
    from gpiozero.pins.lgpio import LGPIOFactory
    Device.pin_factory = LGPIOFactory()
except Exception as _e:
    print(f"lgpio pin factory unavailable ({_e}); using gpiozero default")
finally:
    os.chdir(_prev_cwd)

try:
    from waveshare_epd import epd10in85
    import bambulabs_api as bl
    from roborock.web_api import RoborockApiClient
    from roborock.devices.device_manager import create_device_manager, UserParams
except ImportError:
    pass

# --- LOGGING ---
logging.getLogger("bambulabs_api").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)
logging.getLogger("roborock").setLevel(logging.CRITICAL)
logging.getLogger("aiomqtt").setLevel(logging.CRITICAL)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=1)
file_handler.setFormatter(formatter)

logger.handlers.clear()
logger.addHandler(console_handler)
logger.addHandler(file_handler)

# --- DRIVER DEBUG ---
# Set True to log every stage of the e-paper driver (SPI load per controller,
# refresh, and how long BUSY stays low) to pinpoint where a partial update
# stalls. Only raises the driver's own logger; the rest of the app stays INFO.
EPD_DEBUG = False
if EPD_DEBUG:
    logging.getLogger("waveshare_epd").setLevel(logging.DEBUG)

# --- PANEL RE-INIT STRATEGY ---
# The single-core Pi Zero 1 latches the panel BUSY line after the first partial
# update, so it needs a hardware reset (init_Part) before every frame. Faster
# multi-core boards (Zero 2 W, Pi 3/4/5) do not have this issue, so we skip the
# extra re-init there to avoid needless latency and ghosting. Detected by core
# count: Zero 1 = 1 core, everything newer = 4+.
PANEL_REINIT_EACH_FRAME = (os.cpu_count() or 1) < 2

# --- REFRESH CADENCE ---
# This is the 4-color (G) panel, which has no partial-refresh mode: every update
# is a full refresh taking ~18s and visibly flashing the whole screen. Waveshare
# warns that refreshing large e-paper too often causes ghosting and can damage
# the panel, so we update far less frequently than the B/W build's 60s loop.
REFRESH_INTERVAL_SEC = conf('REFRESH_INTERVAL_SEC', 300)

# --- PANEL PALETTE ---
# The (G) panel renders exactly four colors. getbuffer() quantizes with
# Floyd-Steinberg dithering, so anything NOT one of these four gets dithered into
# speckle - which looks terrible on text. Always draw with these exact values.
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)

icon_cache = {}
global_printer = None


class HardwareTimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise HardwareTimeoutError("Hardware Busy-Wait Timeout")


# --- ROBUST NETWORK MANAGER ---
class NetworkManager:
    def __init__(self):
        self.session = None
        self.create_session()

    def create_session(self):
        if self.session:
            try:
                self.session.close()
            except:
                pass
        gc.collect()
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5, pool_maxsize=10,
            max_retries=requests.adapters.Retry(total=1, backoff_factor=0.5)
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def get_json(self, url, headers=None, data=None, method='GET', timeout=10):
        try:
            if self.session is None: self.create_session()
            if method == 'POST':
                resp = self.session.post(url, headers=headers, data=data, timeout=timeout)
            else:
                resp = self.session.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self.create_session()
            return None

    def get_image(self, url, timeout=15):
        try:
            if self.session is None: self.create_session()
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except Exception as e:
            self.create_session()
            return None


net = NetworkManager()


# --- GLOBAL DATA STORE ---
class DataStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.weather = {}
        self.aqi = 0
        self.strava = {
            'rides': 0, 'total_distance': 0,
            'rides_curr': 0, 'distance_curr': 0,
            'rides_prev': 0, 'distance_prev': 0,
            'bike_total': 0, 'hike_total': 0
        }
        self.printer = {'status': 'OFFLINE'}
        self.gmail_unread = 0
        self.spotify = {'status': 'PAUSED', 'text': '', 'cover': None}
        self.claude = {'error': False, 'five_hour': {}, 'seven_day': {}}
        self.antigravity = {'error': False, 'models': []}
        self.codex = {'error': False, 'five_hour': {}, 'seven_day': {}}
        self.roborock = {
            'status': 'OFFLINE', 'battery': 0, 'is_cleaning': False,
            'current_area': 0.0, 'ref_area': 0.0, 'pct': 0.0, 'last_date': '-'
        }
        self.sysload = {'cpu': 0, 'ram_free': 0, 'history': deque(maxlen=30)}
        self.crypto = {'btc': 0, 'eth': 0, 'btc_hist': [], 'eth_hist': []}
        # Top 24h movers from Coinbase: [(symbol, pct_change), ...]
        self.movers = {'gainers': [], 'losers': []}
        self.ping = {'current': 0, 'history': deque(maxlen=50)}
        self.fritz = {'ok': False, 'down_max': 0, 'up_max': 0,
                      'down_now': 0, 'up_now': 0, 'uptime': 0}

        self.last_update = {
            'weather': 0, 'strava': 0, 'printer': 0, 'gmail': 0,
            'spotify': 0, 'crypto': 0, 'sysload': 0, 'ping': 0,
            'claude': 0, 'antigravity': 0, 'codex': 0,
            'movers': 0, 'fritz': 0
        }


data_store = DataStore()


# --- HELPERS ---
def fetch_fritzbox():
    """DSL sync rate and live throughput from the router over TR-064/IGD.

    This is the real line capacity - a speedtest from the Pi only ever measures
    its own 2.4GHz wifi (~20 Mbps), which is far below what the line actually
    syncs at. The IGD endpoints used here need no authentication.
    """
    host = FRITZBOX_CONF.get('HOST', 'fritz.box')
    IFC = 'urn:schemas-upnp-org:service:WANCommonInterfaceConfig:1'
    CONN = 'urn:schemas-upnp-org:service:WANIPConnection:1'
    envelope = ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:{a} xmlns:u="{n}"/></s:Body></s:Envelope>')

    def call(path, ns, action):
        r = requests.post(
            f'http://{host}:49000{path}',
            data=envelope.format(a=action, n=ns),
            headers={'Content-Type': 'text/xml; charset="utf-8"',
                     'SoapAction': f'{ns}#{action}'},
            timeout=6)
        r.raise_for_status()
        return dict(re.findall(r'<(New[A-Za-z0-9]+)>([^<]*)</\1>', r.text))

    try:
        link = call('/igdupnp/control/WANCommonIFC1', IFC, 'GetCommonLinkProperties')
        addon = call('/igdupnp/control/WANCommonIFC1', IFC, 'GetAddonInfos')
        # Uptime lives on WANIPConnection, not on the interface-config service.
        status = call('/igdupnp/control/WANIPConn1', CONN, 'GetStatusInfo')
    except Exception:
        return None

    def num(d, k):
        try:
            return int(d.get(k, 0) or 0)
        except ValueError:
            return 0

    if link.get('NewPhysicalLinkStatus') != 'Up':
        return {'ok': False, 'down_max': 0, 'up_max': 0,
                'down_now': 0, 'up_now': 0, 'uptime': 0}

    return {
        'ok': True,
        'down_max': num(link, 'NewLayer1DownstreamMaxBitRate'),
        'up_max': num(link, 'NewLayer1UpstreamMaxBitRate'),
        'down_now': num(addon, 'NewByteReceiveRate') * 8,
        'up_now': num(addon, 'NewByteSendRate') * 8,
        'uptime': num(status, 'NewUptime'),
    }


def fetch_coinbase_movers(min_notional=2_000_000, count=4):
    """Top 24h gainers/losers across Coinbase USD pairs.

    One bulk request covers every product. Pairs are filtered by 24h notional
    volume first - without that the list is dominated by illiquid tokens whose
    triple-digit swings mean nothing.
    """
    data = net.get_json('https://api.exchange.coinbase.com/products/stats')
    if not data:
        return None
    rows = []
    for pid, st in data.items():
        if not pid.endswith('-USD'):
            continue
        day = (st or {}).get('stats_24hour') or {}
        try:
            op = float(day['open'])
            last = float(day['last'])
            vol = float(day.get('volume', 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if op <= 0 or last <= 0 or vol * last < min_notional:
            continue
        rows.append((pid[:-4], (last - op) / op * 100.0))
    if not rows:
        return None
    rows.sort(key=lambda r: r[1], reverse=True)
    return {'gainers': rows[:count], 'losers': rows[-count:][::-1]}


# Practical German for everyday life here - admin, shops, doctors, trades,
# transport. Picked by day-of-year so it is stable across refreshes and turns
# over once a day; the list is long enough not to repeat for a couple of months.
GERMAN_PHRASES = [
    ("Kann ich bitte die Rechnung haben?", "Could I have the bill, please?"),
    ("Ich haette gerne einen Termin.", "I would like an appointment."),
    ("Koennen Sie das bitte wiederholen?", "Could you repeat that, please?"),
    ("Ich verstehe das leider nicht.", "I'm afraid I don't understand."),
    ("Sprechen Sie bitte langsamer.", "Please speak more slowly."),
    ("Wo finde ich das Buergeramt?", "Where do I find the citizens' office?"),
    ("Ich moechte mich anmelden.", "I'd like to register my address."),
    ("Haben Sie das auch in Groesse M?", "Do you have this in size M?"),
    ("Kann ich mit Karte zahlen?", "Can I pay by card?"),
    ("Nur Bargeld, oder?", "Cash only, right?"),
    ("Wann haben Sie geoeffnet?", "When are you open?"),
    ("Ist das noch frei?", "Is this seat still free?"),
    ("Koennen Sie mir helfen?", "Could you help me?"),
    ("Ich brauche eine Quittung.", "I need a receipt."),
    ("Wo ist die naechste Haltestelle?", "Where is the nearest stop?"),
    ("Faehrt dieser Zug nach Frankfurt?", "Does this train go to Frankfurt?"),
    ("Der Zug hat Verspaetung.", "The train is delayed."),
    ("Ich habe meinen Anschluss verpasst.", "I missed my connection."),
    ("Gibt es hier WLAN?", "Is there wifi here?"),
    ("Wie lange dauert das ungefaehr?", "Roughly how long will that take?"),
    ("Das ist mir zu teuer.", "That's too expensive for me."),
    ("Koennen Sie mir einen Rabatt geben?", "Could you give me a discount?"),
    ("Ich moechte das zurueckgeben.", "I'd like to return this."),
    ("Haben Sie eine Tuete?", "Do you have a bag?"),
    ("Ich bin nur am Schauen.", "I'm just looking."),
    ("Wo ist der Ausgang?", "Where is the exit?"),
    ("Entschuldigung, wo ist die Toilette?", "Excuse me, where is the toilet?"),
    ("Ich habe einen Termin um drei.", "I have an appointment at three."),
    ("Mir geht es nicht gut.", "I'm not feeling well."),
    ("Ich habe Kopfschmerzen.", "I have a headache."),
    ("Brauche ich ein Rezept?", "Do I need a prescription?"),
    ("Ist das rezeptfrei?", "Is that available without prescription?"),
    ("Wo ist die Notaufnahme?", "Where is the emergency room?"),
    ("Koennen Sie das bitte aufschreiben?", "Could you write that down?"),
    ("Ich rufe spaeter zurueck.", "I'll call back later."),
    ("Koennen Sie mir das erklaeren?", "Could you explain that to me?"),
    ("Das habe ich nicht bestellt.", "I didn't order this."),
    ("Die Heizung funktioniert nicht.", "The heating isn't working."),
    ("Das Wasser laeuft nicht ab.", "The water isn't draining."),
    ("Wann kommt der Handwerker?", "When is the repairman coming?"),
    ("Ich habe den Schluessel vergessen.", "I forgot the key."),
    ("Der Aufzug ist kaputt.", "The lift is broken."),
    ("Wo kann ich den Muell hinbringen?", "Where can I take the rubbish?"),
    ("Wann wird der Muell abgeholt?", "When is the rubbish collected?"),
    ("Das ist Restmuell, oder?", "That's general waste, right?"),
    ("Ich moechte ein Paket abholen.", "I'd like to collect a parcel."),
    ("Haben Sie eine Sendungsnummer?", "Do you have a tracking number?"),
    ("Koennen Sie das bitte unterschreiben?", "Could you sign this, please?"),
    ("Ich habe eine Frage zur Rechnung.", "I have a question about the bill."),
    ("Die Abrechnung stimmt nicht.", "The invoice isn't correct."),
    ("Wann ist die Zahlung faellig?", "When is the payment due?"),
    ("Ich moechte kuendigen.", "I'd like to cancel my contract."),
    ("Gibt es eine Kuendigungsfrist?", "Is there a notice period?"),
    ("Koennen Sie mir das schriftlich geben?", "Can I get that in writing?"),
    ("Ich warte noch auf eine Antwort.", "I'm still waiting for a reply."),
    ("Das passt mir gut.", "That works well for me."),
    ("Passt es Ihnen am Montag?", "Does Monday suit you?"),
    ("Ich bin gleich da.", "I'll be right there."),
    ("Tut mir leid, ich bin zu spaet.", "Sorry, I'm running late."),
    ("Machen Sie sich keine Sorgen.", "Don't worry about it."),
    ("Vielen Dank fuer Ihre Hilfe.", "Thank you very much for your help."),
    ("Schoenen Feierabend!", "Have a nice evening after work!"),
    ("Einen schoenen Tag noch!", "Have a nice day!"),
    ("Bis naechste Woche.", "See you next week."),
    ("Wie meinen Sie das?", "How do you mean that?"),
    ("Das ist kein Problem.", "That's no problem."),
    ("Koennten Sie kurz warten?", "Could you wait a moment?"),
    ("Ich melde mich bei Ihnen.", "I'll get in touch with you."),
    ("Was empfehlen Sie?", "What do you recommend?"),
    ("Zum Mitnehmen, bitte.", "To take away, please."),
    ("Stimmt so.", "Keep the change."),
    ("Getrennt oder zusammen?", "Paying separately or together?"),
]


def phrase_of_the_day():
    return GERMAN_PHRASES[datetime.now().timetuple().tm_yday % len(GERMAN_PHRASES)]


def wrap_text(draw, text, font, max_width, max_lines=2):
    """Greedy word wrap, ellipsising anything past max_lines."""
    words = text.split()
    lines, cur = [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines:
        last = lines[-1]
        while last and draw.textlength(last + "...", font=font) > max_width:
            last = last[:-1]
        consumed = sum(len(l.split()) for l in lines)
        if consumed < len(words):
            lines[-1] = last + "..."
    return lines


def ping_printer(ip):
    try:
        result = subprocess.run(
            ['ping', '-c', '1', '-W', '1', ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return result.returncode == 0
    except:
        return False


def get_cached_icon(name, size, is_white=False):
    key = f"{name}_{size[0]}x{size[1]}_{'white' if is_white else 'black'}"
    if key not in icon_cache:
        path = os.path.join(ICON_DIR, f"{name}.bmp")
        if os.path.exists(path):
            try:
                with Image.open(path) as f_img:
                    # LANCZOS keeps 40x40 source art from turning to mush when
                    # it is scaled up (the weather glyph is drawn at 90x90).
                    img = f_img.convert("L").resize(size, Image.LANCZOS)
                    img = ImageOps.invert(img)
                    # Hard threshold, NOT convert("1"): that dithers by default,
                    # which sprays the anti-aliased edges of this grayscale art
                    # into speckle and makes every icon look dusty on the panel.
                    icon_cache[key] = img.point(lambda v: 255 if v > 110 else 0, mode="1")
            except:
                return None
        else:
            icon_cache[key] = None
    return icon_cache.get(key)


def time_until(iso_str):
    if not iso_str: return "N/A"
    try:
        # Handling the explicit +00:00 timezone format
        target = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        diff = target - now
        if diff.total_seconds() < 0: return "Resetting..."
        hours, rem = divmod(diff.total_seconds(), 3600)
        days, hours = divmod(hours, 24)
        if days > 0:
            return f"{int(days)}d {int(hours)}h"
        else:
            minutes = rem // 60
            return f"{int(hours)}h {int(minutes)}m"
    except Exception:
        return "N/A"


# --- AUTH & FETCH THREADS ---

def auth_claude():
    global ENABLE_CLAUDE
    if not ENABLE_CLAUDE: return
    try:
        import claude
        success = claude.interactive_auth()
        if not success:
            ENABLE_CLAUDE = False
            print("Claude widget is disabled.")
    except ImportError:
        print("claude.py not found. Claude widget disabled.")
        ENABLE_CLAUDE = False


def auth_codex():
    global ENABLE_CODEX
    if not ENABLE_CODEX: return
    # codex.py has no interactive login; it just needs a valid auth.json that
    # the user provides. Disable the widget if either file is missing.
    if not os.path.exists(os.path.join(BASE_DIR, 'codex.py')):
        print("codex.py not found. Codex widget disabled.")
        ENABLE_CODEX = False
        return
    if not os.path.exists(os.path.join(BASE_DIR, 'auth.json')):
        print("auth.json not found. Codex widget disabled.")
        ENABLE_CODEX = False


def auth_antigravity():
    global ENABLE_ANTIGRAVITY
    if not ENABLE_ANTIGRAVITY: return
    try:
        import antigravity
        success = antigravity.interactive_auth()
        if not success:
            ENABLE_ANTIGRAVITY = False
            print("Antigravity widget is disabled.")
    except ImportError:
        print("antigravity.py not found. Antigravity widget disabled.")
        ENABLE_ANTIGRAVITY = False


def auth_strava():
    global ENABLE_STRAVA
    if not ENABLE_STRAVA: return

    if os.path.exists(STRAVA_CONF['TOKEN_FILE']):
        return

    print("\n--- STRAVA CONFIGURATION REQUIRED ---")
    c_id = input("Enter Strava Client ID (or press Enter to disable): ").strip()
    if not c_id:
        print("Strava is disabled. Fallback widget (System Load) will be used.\n")
        ENABLE_STRAVA = False
        return

    c_secret = input("Enter Strava Client Secret: ").strip()

    auth_url = (
        f"{API_ENDPOINTS['strava_auth']}?"
        f"client_id={c_id}&"
        f"response_type=code&"
        f"redirect_uri=http://localhost&"
        f"approval_prompt=force&"
        f"scope=activity:read_all"
    )

    print("\n[!] To get a token with the correct permissions, open this link in your browser:\n")
    print(f"--> {auth_url} <--\n")
    print("Click 'Authorize'. You will be redirected to an empty/error page (localhost).")
    print("Look at the address bar. Copy the 'code' parameter.")

    code_input = input("Enter the 'code' from the URL (or paste the full URL): ").strip()

    if not code_input:
        print("Authorization cancelled. Strava is disabled.\n")
        ENABLE_STRAVA = False
        return

    if 'code=' in code_input:
        try:
            parsed = urllib.parse.urlparse(code_input)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get('code', [code_input])[0]
        except:
            code = code_input.split('code=')[1].split('&')[0]
    else:
        code = code_input

    print("Fetching Access Token...")
    data = {'client_id': c_id, 'client_secret': c_secret, 'code': code, 'grant_type': 'authorization_code'}

    try:
        resp = requests.post(API_ENDPOINTS['strava_token'], data=data)
        resp.raise_for_status()
        token_data = resp.json()
        token_data['client_id'] = c_id
        token_data['client_secret'] = c_secret

        with open(STRAVA_CONF['TOKEN_FILE'], 'w') as f:
            json.dump(token_data, f, indent=4)
        print("Strava Authorization Successful!\n")
    except Exception as e:
        print(f"Failed to fetch Strava tokens: {e}")
        ENABLE_STRAVA = False


def fetch_strava_data():
    if not os.path.exists(STRAVA_CONF['TOKEN_FILE']): return None
    with open(STRAVA_CONF['TOKEN_FILE'], 'r') as f:
        token_data = json.load(f)

    c_id = token_data.get('client_id')
    c_secret = token_data.get('client_secret')

    if time.time() > token_data.get('expires_at', 0):
        data = {'client_id': c_id, 'client_secret': c_secret, 'grant_type': 'refresh_token',
                'refresh_token': token_data.get('refresh_token')}
        new_token = net.get_json(API_ENDPOINTS['strava_token'], data=data, method='POST')
        if new_token and 'access_token' in new_token:
            new_token['client_id'] = c_id
            new_token['client_secret'] = c_secret
            token_data = new_token
            with open(STRAVA_CONF['TOKEN_FILE'], 'w') as f:
                json.dump(token_data, f, indent=4)
        else:
            return None

    access_token = token_data['access_token']

    now_year = datetime.now().year
    start_curr_ts = datetime(now_year, 1, 1).timestamp()
    start_prev_ts = datetime(now_year - 1, 1, 1).timestamp()
    end_prev_ts = datetime(now_year - 1, 12, 31, 23, 59, 59).timestamp()

    page = 1
    total_rides, total_dist = 0, 0
    rides_curr, dist_curr = 0, 0
    rides_prev, dist_prev = 0, 0
    bike_total, hike_total = 0, 0

    headers = {"Authorization": f"Bearer {access_token}"}

    while True:
        url = f"{API_ENDPOINTS['strava_activities']}?page={page}&per_page=100"
        activities = net.get_json(url, headers=headers)
        if not activities: break

        for act in activities:
            t = act.get('type')
            d = act.get('distance', 0)
            act_time = datetime.strptime(act['start_date'], "%Y-%m-%dT%H:%M:%SZ").timestamp()

            if t in ['Ride', 'VirtualRide', 'EBikeRide', 'GravelRide', 'MountainBikeRide']:
                total_rides += 1
                total_dist += d
                bike_total += d
                if act_time >= start_curr_ts:
                    rides_curr += 1
                    dist_curr += d
                elif start_prev_ts <= act_time <= end_prev_ts:
                    rides_prev += 1
                    dist_prev += d
            elif t in ['Hike', 'Walk']:
                hike_total += d

        if len(activities) < 100: break
        page += 1

    return {
        "rides": total_rides,
        "total_distance": round(total_dist / 1000, 1),
        "rides_curr": rides_curr,
        "distance_curr": round(dist_curr / 1000, 1),
        "rides_prev": rides_prev,
        "distance_prev": round(dist_prev / 1000, 1),
        "bike_total": round(bike_total / 1000, 1),
        "hike_total": round(hike_total / 1000, 1)
    }


def auth_roborock(email):
    global ENABLE_ROBOROCK
    if not ENABLE_ROBOROCK: return None

    if os.path.exists(ROBOROCK_TOKEN_FILE):
        try:
            with open(ROBOROCK_TOKEN_FILE, "rb") as f:
                return pickle.load(f)
        except:
            pass

    print("\n--- ROBOROCK AUTHORIZATION REQUIRED ---")

    async def _do_auth():
        web_api = RoborockApiClient(username=email)
        await web_api.request_code()
        code = input(f"Enter 6-digit Roborock auth code sent to {email} (or press Enter to disable): ").strip()
        if not code: return None
        user_data = await web_api.code_login(code)
        with open(ROBOROCK_TOKEN_FILE, "wb") as f: pickle.dump(user_data, f)
        print("Roborock Authorization Successful!\n")
        return user_data

    if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        user_data = asyncio.run(_do_auth())
        if not user_data:
            print("Roborock is disabled. Fallback widget (Ping) will be used.\n")
            ENABLE_ROBOROCK = False
        return user_data
    except Exception as e:
        print(f"Failed to auth Roborock: {e}")
        ENABLE_ROBOROCK = False
        return None


def roborock_update_thread(user_data, email):
    if not ENABLE_ROBOROCK or not user_data: return

    async def _loop():
        ref_area, last_date = 0.0, "-"
        if os.path.exists(ROBOROCK_STATS_FILE):
            try:
                with open(ROBOROCK_STATS_FILE, "r") as f:
                    stats = json.load(f)
                    ref_area, last_date = stats.get("ref_area", 0.0), stats.get("last_date", "-")
            except:
                pass

        user_params = UserParams(username=email, user_data=user_data)
        device_manager = await create_device_manager(user_params)

        short_states = {
            5: "Clean", 6: "Return", 8: "Charge", 10: "Pause",
            17: "Spot", 18: "Room", 22: "Empty", 23: "Wash",
            26: "ToWash", 29: "Map"
        }

        while True:
            try:
                devices = await device_manager.get_devices()
                if devices and devices[0].v1_properties:
                    device = devices[0]
                    status_trait = device.v1_properties.status
                    await status_trait.refresh()
                    current_area = (status_trait.clean_area / 1000000) if status_trait.clean_area else 0

                    is_cleaning = status_trait.state in [5, 6, 10, 17, 18, 22, 23, 26, 29]
                    status_str = short_states.get(status_trait.state, f"S:{status_trait.state}")

                    if not is_cleaning and current_area > 0 and current_area != ref_area:
                        ref_area = current_area
                        last_date = datetime.now().strftime("%d %b %H:%M")
                        with open(ROBOROCK_STATS_FILE, "w") as f: json.dump(
                            {"ref_area": ref_area, "last_date": last_date}, f)

                    pct = (current_area / ref_area) * 100 if is_cleaning and ref_area > 0 else 0.0

                    with data_store.lock:
                        data_store.roborock = {
                            'status': status_str, 'battery': status_trait.battery,
                            'is_cleaning': is_cleaning, 'current_area': current_area,
                            'ref_area': ref_area, 'pct': pct, 'last_date': last_date
                        }
            except Exception as e:
                logging.error(f"Roborock error: {e}")
            await asyncio.sleep(60)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_loop())


def update_data_thread():
    global global_printer

    if ENABLE_BAMBU:
        try:
            global_printer = bl.Printer(PRINTER_CONF['IP'], PRINTER_CONF['ACCESS_CODE'], PRINTER_CONF['SERIAL'])
        except Exception as e:
            logging.error(f"Bambu init error: {e}")
            global_printer = None

    is_connected = False

    while True:
        now = time.time()

        if now - data_store.last_update['weather'] > 600:
            weather_url = f"{API_ENDPOINTS['weather']}?latitude={LOCATION_LAT}&longitude={LOCATION_LON}&current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m,weather_code,is_day,uv_index&hourly=temperature_2m,precipitation_probability,weather_code,cloud_cover&timezone=auto&forecast_days=2"
            aqi_url = f"{API_ENDPOINTS['aqi']}?latitude={LOCATION_LAT}&longitude={LOCATION_LON}&current=european_aqi&timezone=auto"
            w_data = net.get_json(weather_url)
            a_data = net.get_json(aqi_url)
            with data_store.lock:
                if w_data: data_store.weather = w_data
                if a_data and 'current' in a_data: data_store.aqi = a_data['current'].get('european_aqi', 0)
            data_store.last_update['weather'] = now

        if ENABLE_STRAVA:
            if now - data_store.last_update['strava'] > 900:
                s_data = fetch_strava_data()
                if s_data:
                    with data_store.lock: data_store.strava = s_data
                data_store.last_update['strava'] = now
        else:
            if now - data_store.last_update['sysload'] > 30:
                try:
                    with open('/proc/loadavg', 'r') as f:
                        cpu = float(f.read().split()[0]) * 10
                    with open('/proc/meminfo', 'r') as f:
                        lines = f.readlines()
                        free = int(lines[1].split()[1]) // 1024
                    with data_store.lock:
                        data_store.sysload['cpu'] = min(int(cpu), 100)
                        data_store.sysload['ram_free'] = free
                        data_store.sysload['history'].append(min(int(cpu), 100))
                except:
                    pass
                data_store.last_update['sysload'] = now

        if ENABLE_BAMBU:
            update_interval = 5 if is_connected else 15
            if now - data_store.last_update['printer'] > update_interval:
                is_alive = ping_printer(PRINTER_CONF['IP'])
                if is_alive:
                    try:
                        if not is_connected and global_printer:
                            global_printer.connect()
                            time.sleep(1)
                            is_connected = True
                        if global_printer:
                            status = global_printer.get_state()
                            if status and status != "UNKNOWN":
                                with data_store.lock:
                                    data_store.printer = {
                                        'status': status,
                                        'percentage': global_printer.get_percentage(),
                                        'remaining_time': global_printer.get_time(),
                                        'layers': f"{global_printer.current_layer_num()}/{global_printer.total_layer_num()}"
                                    }
                    except Exception as e:
                        is_connected = False
                        with data_store.lock:
                            data_store.printer['status'] = 'OFFLINE'
                        try:
                            if global_printer: global_printer.disconnect()
                        except:
                            pass
                else:
                    if is_connected:
                        is_connected = False
                        try:
                            global_printer.disconnect()
                        except:
                            pass
                    with data_store.lock:
                        data_store.printer['status'] = 'OFFLINE'
                data_store.last_update['printer'] = now
        # Crypto has its own slot now, so fetch it regardless of the printer.
        if True:
            if now - data_store.last_update['crypto'] > 600:
                btc_url = f"{API_ENDPOINTS['btc']}?vs_currency=usd&days=7"
                eth_url = f"{API_ENDPOINTS['eth']}?vs_currency=usd&days=7"
                btc_data = net.get_json(btc_url)
                eth_data = net.get_json(eth_url)
                with data_store.lock:
                    if btc_data:
                        prices = [p[1] for p in btc_data.get('prices', [])]
                        if prices:
                            data_store.crypto['btc'] = int(prices[-1])
                            data_store.crypto['btc_hist'] = prices[::len(prices) // 50][:50]
                    if eth_data:
                        prices = [p[1] for p in eth_data.get('prices', [])]
                        if prices:
                            data_store.crypto['eth'] = int(prices[-1])
                            data_store.crypto['eth_hist'] = prices[::len(prices) // 50][:50]
                data_store.last_update['crypto'] = now

        if ENABLE_FRITZBOX and now - data_store.last_update['fritz'] > 60:
            fb = fetch_fritzbox()
            if fb:
                with data_store.lock:
                    data_store.fritz = fb
            data_store.last_update['fritz'] = now

        if now - data_store.last_update['movers'] > 600:
            mv = fetch_coinbase_movers()
            if mv:
                with data_store.lock:
                    data_store.movers = mv
            data_store.last_update['movers'] = now

        if True:  # ping drives the top-left widget regardless of other toggles
            if now - data_store.last_update['ping'] > 20:
                try:
                    out = subprocess.check_output(['ping', '-c', '1', '-W', '1', '8.8.8.8']).decode('utf-8')
                    ms = float(out.split('time=')[1].split(' ms')[0])
                except:
                    ms = 0
                with data_store.lock:
                    data_store.ping['current'] = int(ms)
                    data_store.ping['history'].append(int(ms))
                data_store.last_update['ping'] = now

        # The Gmail slot is only drawn when the Fritz!Box widget is off.
        if GMAIL_AVAILABLE and not ENABLE_FRITZBOX and now - data_store.last_update['gmail'] > 300:
            try:
                creds = None
                if os.path.exists(GMAIL_TOKEN_PATH):
                    creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_PATH, GMAIL_SCOPES)
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        with open(GMAIL_TOKEN_PATH, 'w') as t: t.write(creds.to_json())
                if creds and creds.valid:
                    service = build('gmail', 'v1', credentials=creds, cache_discovery=False)
                    label_info = service.users().labels().get(userId='me', id='INBOX').execute()
                    with data_store.lock: data_store.gmail_unread = label_info.get('messagesUnread', 0)
            except:
                pass
            data_store.last_update['gmail'] = now

        # Claude Data Fetching (Run external script every 10 min)
        if ENABLE_CLAUDE and now - data_store.last_update['claude'] > 600:
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, 'claude.py')], capture_output=True, timeout=30)
                usage_path = os.path.join(BASE_DIR, 'usage.json')
                if os.path.exists(usage_path):
                    with open(usage_path, 'r') as f:
                        usage_data = json.load(f)
                    with data_store.lock:
                        data_store.claude = usage_data
                        if "error" in usage_data and "five_hour" not in usage_data:
                            data_store.claude['error'] = True
                        else:
                            data_store.claude['error'] = False
                else:
                    with data_store.lock:
                        data_store.claude['error'] = True
            except Exception as e:
                logging.error(f"Claude update error: {e}")
                with data_store.lock:
                    data_store.claude['error'] = True
            data_store.last_update['claude'] = now

        # Codex Data Fetching (Run external script every 10 min)
        if ENABLE_CODEX and now - data_store.last_update['codex'] > 600:
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, 'codex.py'), '--once'],
                               capture_output=True, timeout=30)
                usage_path = os.path.join(BASE_DIR, 'codex_usage.json')
                if os.path.exists(usage_path):
                    with open(usage_path, 'r') as f:
                        usage_data = json.load(f)
                    with data_store.lock:
                        data_store.codex = usage_data
                        # codex.py signals failure with utilization = -1.0 (not an
                        # "error" key like claude.py), so check both.
                        sd = usage_data.get('seven_day', {})
                        if usage_data.get('error') or 'seven_day' not in usage_data \
                                or sd.get('utilization', -1) < 0:
                            data_store.codex['error'] = True
                        else:
                            data_store.codex['error'] = False
                else:
                    with data_store.lock:
                        data_store.codex['error'] = True
            except Exception as e:
                logging.error(f"Codex update error: {e}")
                with data_store.lock:
                    data_store.codex['error'] = True
            data_store.last_update['codex'] = now

        if ENABLE_ANTIGRAVITY and now - data_store.last_update['antigravity'] > 60:
            try:
                subprocess.run([sys.executable, os.path.join(BASE_DIR, 'antigravity.py')], capture_output=True, timeout=30)
                limits_path = os.path.join(BASE_DIR, 'limits.json')
                if os.path.exists(limits_path):
                    with open(limits_path, 'r', encoding='utf-8') as f:
                        limits_data = json.load(f)
                    with data_store.lock:
                        data_store.antigravity = limits_data
                        if "error" in limits_data:
                            data_store.antigravity['error'] = True
                        else:
                            data_store.antigravity['error'] = False
                else:
                    with data_store.lock:
                        data_store.antigravity['error'] = True
            except Exception as e:
                logging.error(f"Antigravity update error: {e}")
                with data_store.lock:
                    data_store.antigravity['error'] = True
            data_store.last_update['antigravity'] = now

        if ENABLE_SPOTIFY and now - data_store.last_update['spotify'] > 20:
            url = f"{API_ENDPOINTS['lastfm']}?method=user.getrecenttracks&user={LASTFM_CONF['USERNAME']}&api_key={LASTFM_CONF['API_KEY']}&format=json&limit=2&rnd={int(now)}"
            s_data = net.get_json(url, timeout=5)
            if s_data:
                try:
                    tracks = s_data.get('recenttracks', {}).get('track', [])
                    if isinstance(tracks, dict): tracks = [tracks]
                    if tracks:
                        current_track = tracks[0]
                        is_playing = current_track.get('@attr', {}).get('nowplaying') == 'true'
                        if is_playing:
                            track_name = current_track.get('name', 'Unknown')
                            artist = current_track.get('artist', {}).get('#text', 'Unknown')
                            img_url = ""
                            for img in current_track.get('image', []):
                                if img.get('size') == 'extralarge': img_url = img.get('#text', '')
                            cover_dithered = None
                            if img_url:
                                img_bytes = net.get_image(img_url)
                                if img_bytes:
                                    img_pil = Image.open(io.BytesIO(img_bytes)).convert("L").resize((120, 120))
                                    enhancer = ImageEnhance.Contrast(img_pil)
                                    img_pil = enhancer.enhance(3.0)
                                    cover_dithered = img_pil.convert("1", dither=Image.NONE)
                            with data_store.lock:
                                data_store.spotify = {'status': 'PLAYING', 'text': f"{artist} - {track_name}",
                                                      'cover': cover_dithered}
                        else:
                            with data_store.lock:
                                data_store.spotify = {'status': 'PAUSED', 'text': '', 'cover': None}
                except:
                    pass
            data_store.last_update['spotify'] = now

        gc.collect()
        time.sleep(1)


# --- GRAPHICS FUNCTIONS ---
def draw_icon(draw, x, y, name, size=(40, 40), is_white=False, color=None):
    icon = get_cached_icon(name, size, is_white)
    ink = color if color is not None else (WHITE if is_white else BLACK)
    if icon:
        draw.bitmap((x, y), icon, fill=ink)
    else:
        draw.rectangle((x, y, x + size[0], y + size[1]), outline=ink)


def draw_sparkline(draw, x, y, data, max_items=50, width=400, height=60, color=BLACK, style="bar"):
    if not data: return
    max_val = max(data) if max(data) > 0 else 1
    step = width / max(max_items - 1, 1)

    if style == "line":
        points = []
        for i, val in enumerate(data):
            px = x + i * step
            py = y + height - (val / max_val) * height
            points.append((px, py))
        if len(points) > 1: draw.line(points, fill=color, width=2)
    elif style == "bar":
        bar_w = max(int(step) - 1, 1)
        for i, val in enumerate(data):
            bh = int((val / max_val) * height)
            bx = x + i * step
            by = y + height - bh
            draw.rectangle((bx, by, bx + bar_w, y + height), fill=color)


def _sun(draw, cx, cy, r, rays=True, unit=4):
    if rays:
        for i in range(8):
            a = math.radians(i * 45)
            draw.line((cx + math.cos(a) * r * 1.35, cy + math.sin(a) * r * 1.35,
                       cx + math.cos(a) * r * 1.9, cy + math.sin(a) * r * 1.9),
                      fill=YELLOW, width=max(2, int(unit)))
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=YELLOW)


def _moon(draw, cx, cy, r):
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=YELLOW)
    # bite out of the disc to make a crescent
    draw.ellipse((cx - r * 1.45, cy - r * 1.15, cx + r * 0.55, cy + r * 0.85), fill=WHITE)


def _cloud(draw, cx, cy, w, colour=BLACK):
    h = w * 0.58
    draw.ellipse((cx - w * 0.52, cy - h * 0.20, cx - w * 0.02, cy + h * 0.58), fill=colour)
    draw.ellipse((cx - w * 0.22, cy - h * 0.62, cx + w * 0.34, cy + h * 0.42), fill=colour)
    draw.ellipse((cx + w * 0.02, cy - h * 0.16, cx + w * 0.52, cy + h * 0.58), fill=colour)
    draw.rectangle((cx - w * 0.46, cy + h * 0.10, cx + w * 0.46, cy + h * 0.58), fill=colour)


def draw_weather_glyph(draw, x, y, size, code, is_day=1):
    """Draw a weather condition as a real multi-colour glyph.

    The bundled icon BMPs are grayscale line art, so draw_icon() can only stamp
    them in a single flat colour - a sun behind a cloud comes out entirely
    yellow or entirely black. These shapes are simple enough to draw directly,
    which is the only way to get a yellow sun against a black cloud, or a yellow
    bolt on a storm cloud, out of a 4-colour panel.
    """
    s = size
    unit = max(2, s * 0.055)
    cx, cy = x + s / 2.0, y + s / 2.0

    if code == 0:                                   # clear
        (_sun if is_day else _moon)(draw, cx, cy, s * 0.26, *([True, unit] if is_day else []))
        return

    if code in (1, 2):                              # partly cloudy: sun + cloud
        _sun(draw, x + s * 0.62, y + s * 0.34, s * 0.17, True, unit * 0.8)
        _cloud(draw, cx - s * 0.04, cy + s * 0.14, s * 0.76)
        return

    if code == 3:                                   # overcast
        _cloud(draw, cx, cy, s * 0.88)
        return

    if code in (45, 48):                            # fog
        _cloud(draw, cx, cy - s * 0.12, s * 0.80)
        for i in range(3):
            yy = cy + s * (0.24 + i * 0.13)
            draw.line((x + s * 0.16, yy, x + s * 0.84, yy), fill=BLACK, width=int(unit))
        return

    if code in (95, 96, 99):                        # thunderstorm: yellow bolt
        _cloud(draw, cx, cy - s * 0.14, s * 0.82)
        bx, by = cx, cy + s * 0.16
        draw.polygon([(bx + s * 0.06, by), (bx - s * 0.13, by + s * 0.20),
                      (bx - s * 0.01, by + s * 0.20), (bx - s * 0.08, by + s * 0.40),
                      (bx + s * 0.16, by + s * 0.14), (bx + s * 0.02, by + s * 0.14)],
                     fill=YELLOW)
        return

    if code in (71, 73, 75, 85, 86):                # snow
        _cloud(draw, cx, cy - s * 0.12, s * 0.80)
        for i in range(3):
            fx = x + s * (0.26 + i * 0.24)
            fy = cy + s * 0.30
            r = s * 0.055
            draw.ellipse((fx - r, fy - r, fx + r, fy + r), fill=BLACK)
        return

    # everything else: rain. Red drops so they read against the black cloud.
    _cloud(draw, cx, cy - s * 0.12, s * 0.80)
    for i in range(3):
        dx = x + s * (0.28 + i * 0.22)
        dy = cy + s * 0.22
        draw.line((dx, dy, dx - s * 0.06, dy + s * 0.20), fill=RED, width=int(unit))


def weather_icon_color(name):
    """Tint a weather glyph to match what it depicts.

    The panel only has black/yellow/red, so: anything sun-like is yellow,
    storms are red (they are the condition worth noticing), and cloud/rain/snow
    stay black - a yellow raincloud just reads as broken.
    """
    if name in ("icon_sun", "icon_moon", "icon_partly-cloudy-day", "icon_night"):
        return YELLOW
    if name in ("icon_storm", "icon_cloud-lightning"):
        return RED
    return BLACK


def get_weather_icon(code, is_day=1):
    if code == 0:
        return "icon_sun" if is_day else "icon_moon"
    elif code in [1, 2]:
        return "icon_partly-cloudy-day"
    elif code == 3:
        return "icon_clouds"
    elif code in [45, 48]:
        return "icon_wind"
    elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
        return "icon_rain"
    elif code in [71, 73, 75, 85, 86]:
        return "icon_snow"
    elif code in [95, 96, 99]:
        return "icon_lightning"
    return "icon_sun"


def render_screen(epd, fonts):
    Himage = Image.new('RGB', (epd.width, epd.height), WHITE)
    draw = ImageDraw.Draw(Himage)

    if not data_store.lock.acquire(timeout=2.0): return Himage
    try:
        weather = data_store.weather.copy()
        aqi = data_store.aqi
        strava = data_store.strava.copy()
        printer = data_store.printer.copy()
        rob = data_store.roborock.copy()
        gmail_unread = data_store.gmail_unread
        spotify = data_store.spotify.copy()
        claude = data_store.claude.copy()
        antigravity = data_store.antigravity.copy()
        codex = data_store.codex.copy()
        sysload = data_store.sysload.copy()
        crypto = data_store.crypto.copy()
        ping = data_store.ping.copy()
        movers = data_store.movers.copy()
        fritz = data_store.fritz.copy()
    finally:
        data_store.lock.release()

    col_w = epd.width // 3

    # --- COLUMN 1 (Widgets) ---
    col1_x = 20

    # Four stacked slots across the 480px height, ~113px each including its
    # separator. Tighter than the original three-slot layout, so the crypto
    # widget at the bottom is drawn in a compact two-row form.
    y1, y2, y3, y4 = 15, 128, 241, 354
    SEP1, SEP2, SEP3 = 118, 231, 344

    # Widget 1: Strava or Internet Quality
    if ENABLE_STRAVA:
        draw_icon(draw, col1_x, y1, "icon_strava", (50, 50), color=RED)
        draw.text((col1_x + 60, y1), "STRAVA STATS", font=fonts['28'], fill=BLACK)

        now_y = datetime.now().year
        draw.text((col1_x + 60, y1 + 30),
                  f"{now_y}: {strava.get('distance_curr', 0)} km | {now_y - 1}: {strava.get('distance_prev', 0)} km",
                  font=fonts['20'], fill=BLACK)
        draw.text((col1_x + 60, y1 + 52),
                  f"Total: {strava.get('total_distance', 0)} km | {strava.get('rides', 0)} acts", font=fonts['20'],
                  fill=BLACK)

        draw_icon(draw, col1_x + 60, y1 + 74, "icon_bike", (26, 26), color=RED)
        draw.text((col1_x + 92, y1 + 76), f"{strava.get('bike_total', 0)} km", font=fonts['20'], fill=BLACK)

        draw_icon(draw, col1_x + 210, y1 + 74, "icon_hike", (26, 26), color=RED)
        draw.text((col1_x + 242, y1 + 76), f"{strava.get('hike_total', 0)} km", font=fonts['20'], fill=BLACK)

    elif ENABLE_PHRASE:
        # Speech bubble, drawn rather than tinted: a flat yellow body with a
        # black outline reads far better than yellow line art would.
        # The slot is only ~100px tall and this stacks four rows, so the
        # vertical rhythm is tight: header, two German lines, one English.
        draw.ellipse((col1_x, y1 + 2, col1_x + 20, y1 + 22), fill=YELLOW, outline=BLACK, width=2)
        draw.text((col1_x + 28, y1 + 1), "PHRASE OF THE DAY", font=fonts['20'], fill=BLACK)

        _de, _en = phrase_of_the_day()
        _w = col_w - 60
        _yy = y1 + 28
        for _line in wrap_text(draw, _de, fonts['24'], _w, max_lines=2):
            draw.text((col1_x, _yy), _line, font=fonts['24'], fill=BLACK)
            _yy += 22
        for _line in wrap_text(draw, _en, fonts['20'], _w, max_lines=1):
            draw.text((col1_x, _yy + 2), _line, font=fonts['20'], fill=RED)

    else:
        draw_icon(draw, col1_x, y1, "icon_wifi", (50, 50), color=RED)
        draw.text((col1_x + 60, y1), f"Internet Quality: {ping['current']} ms", font=fonts['28'], fill=BLACK)
        draw_sparkline(draw, col1_x + 60, y1 + 56, list(ping['history']), max_items=50, width=350, height=34,
                       style="bar", color=RED)

    draw.line((col1_x, SEP1, col_w - 20, SEP1), fill=RED, width=2)

    # Widget 2: Bambu printer. Crypto has its own slot now, so there is no
    # fallback here - the slot simply stays empty when the printer is disabled.
    if ENABLE_BAMBU:
        p_status = str(printer.get('status', 'OFFLINE')).upper()
        draw_icon(draw, col1_x, y2, "icon_3d", (50, 50), color=RED)
        draw.text((col1_x + 60, y2), f"PRINTER: {p_status}", font=fonts['28'], fill=BLACK)
        if p_status not in ["OFFLINE", "UNKNOWN", "FINISH", "IDLE"]:
            percent = printer.get('percentage', 0)
            draw.rectangle((col1_x + 60, y2 + 38, col1_x + 390, y2 + 56), outline=BLACK)
            fill_w = int(326 * min(max(percent, 0), 100) / 100)
            if fill_w > 0:
                draw.rectangle((col1_x + 62, y2 + 40, col1_x + 62 + fill_w, y2 + 54), fill=YELLOW)
            draw.text((col1_x + 60, y2 + 62),
                      f"{percent}% | Rem: {printer.get('remaining_time', '0')}m | {printer.get('layers', '0/0')} L",
                      font=fonts['20'], fill=BLACK)

    draw.line((col1_x, SEP2, col_w - 20, SEP2), fill=RED, width=2)

    # Widget 3: Roborock / Antigravity / Codex / System load
    if ENABLE_ROBOROCK:
        draw_icon(draw, col1_x, y3, "icon_roborock", (50, 50), color=RED)
        draw.text((col1_x + 60, y3), f"Bat: {rob['battery']}% | {rob['status']}", font=fonts['28'], fill=BLACK)
        if rob['is_cleaning']:
            draw.text((col1_x + 60, y3 + 34), f"Clean: {rob['current_area']:.1f} m2 ({rob['pct']:.0f}%)",
                      font=fonts['24'], fill=BLACK)
            clamped_pct = min(rob['pct'], 100)
            draw.rectangle((col1_x + 60, y3 + 64, col1_x + 390, y3 + 82), outline=BLACK)
            fill_w = int(326 * min(max(clamped_pct, 0), 100) / 100)
            if fill_w > 0:
                draw.rectangle((col1_x + 62, y3 + 66, col1_x + 62 + fill_w, y3 + 80), fill=YELLOW)
        else:
            draw.text((col1_x + 60, y3 + 34), f"Last: {rob['last_date']} | {rob['ref_area']:.1f} m2", font=fonts['24'],
                      fill=BLACK)
    elif ENABLE_ANTIGRAVITY:
        draw_icon(draw, col1_x, y3, "icon_cpu", (50, 50), color=RED)
        draw.text((col1_x + 60, y3), "ANTIGRAVITY USAGE", font=fonts['28'], fill=BLACK)

        if antigravity.get('error'):
            draw.text((col1_x + 60, y3 + 34), "Error loading data", font=fonts['20'], fill=BLACK)
        else:
            models = antigravity.get('models', [])
            opus = next((m for m in models if m.get('modelId') == 'claude-opus-4-6-thinking'), None)
            gemini = next((m for m in models if m.get('modelId') == 'gemini-3-pro-high'), None)

            y_off = y3 + 32
            for m_data in (opus, gemini):
                if m_data:
                    label = "Opus 4.6" if m_data.get('modelId') == 'claude-opus-4-6-thinking' else "Gemini 3Pro"
                    pct = m_data.get('usedPercentage', 0)
                    rem_time = time_until(m_data.get('resetDate'))

                    draw.text((col1_x + 60, y_off), f"{label} {pct}% | In {rem_time}", font=fonts['20'], fill=BLACK)

                    bx, bw, bh = col1_x + 60, 330, 13
                    draw.rectangle((bx, y_off + 22, bx + bw, y_off + 22 + bh), outline=BLACK, width=2)
                    fill_w = int((bw - 4) * min(pct / 100.0, 1.0))
                    if fill_w > 0: draw.rectangle((bx + 2, y_off + 24, bx + 2 + fill_w, y_off + 22 + bh - 2), fill=YELLOW)

                    y_off += 40
    elif ENABLE_CODEX:
        draw_icon(draw, col1_x, y3, "icon_cpu", (50, 50), color=RED)
        draw.text((col1_x + 60, y3), "CODEX AI USAGE", font=fonts['28'], fill=BLACK)

        if codex.get('error'):
            draw.text((col1_x + 60, y3 + 34), "Codex Usage Error", font=fonts['20'], fill=BLACK)
        else:
            # 7-Day limit only: OpenAI has disabled the 5-hour window for now.
            pct_7d = codex.get('seven_day', {}).get('utilization', 0)
            resets_7d = codex.get('seven_day', {}).get('resets_at')
            rem_7d = time_until(resets_7d)

            draw.text((col1_x + 60, y3 + 34), f"7-Day Limit: {round(pct_7d)}% (In {rem_7d})",
                      font=fonts['20'], fill=BLACK)
            bx, bw, bh = col1_x + 60, 330, 13
            draw.rectangle((bx, y3 + 64, bx + bw, y3 + 64 + bh), outline=BLACK, width=2)
            fill_w = int((bw - 4) * min(pct_7d / 100.0, 1.0))
            if fill_w > 0:
                draw.rectangle((bx + 2, y3 + 66, bx + 2 + fill_w, y3 + 64 + bh - 2), fill=YELLOW)
    else:
        draw_icon(draw, col1_x, y3, "icon_cpu", (50, 50), color=RED)
        draw.text((col1_x + 60, y3), f"SYSTEM LOAD: {sysload['cpu']}%", font=fonts['28'], fill=BLACK)
        draw.text((col1_x + 60, y3 + 34), f"RAM Free: {sysload['ram_free']} MB", font=fonts['20'], fill=BLACK)
        draw_sparkline(draw, col1_x + 60, y3 + 60, list(sysload['history']), max_items=30, width=350, height=26,
                       style="bar", color=RED)

    draw.line((col1_x, SEP3, col_w - 20, SEP3), fill=RED, width=2)

    # Widget 4: Coinbase 24h movers - four columns, gainers over losers.
    draw_icon(draw, col1_x, y4, "icon_btc", (26, 26), color=YELLOW)
    draw.text((col1_x + 34, y4 + 1), "COINBASE 24h MOVERS", font=fonts['20'], fill=BLACK)

    _cell_w = 98
    # Yellow is unreadable as small text on this panel - it only works as a
    # large solid fill. So direction is carried by a chunky yellow/red triangle
    # and the numbers stay high-contrast.
    for _row, (_key, _colour, _up) in enumerate((('gainers', YELLOW, True), ('losers', RED, False))):
        _entries = movers.get(_key) or []
        _ry = y4 + 28 + _row * 40
        for _i in range(4):
            _cx = col1_x + _i * _cell_w
            if _i >= len(_entries):
                draw.text((_cx, _ry), "-", font=fonts['20'], fill=BLACK)
                continue
            _sym, _pct = _entries[_i]
            # Only a couple of coins ship an icon (BTC, ETH); fall back to the
            # ticker text for everything else.
            _tx = _cx
            if os.path.exists(os.path.join(ICON_DIR, f"icon_{_sym.lower()}.bmp")):
                draw_icon(draw, _cx, _ry + 1, f"icon_{_sym.lower()}", (16, 16), color=BLACK)
                _tx = _cx + 20
            draw.text((_tx, _ry), _sym[:6], font=fonts['20'], fill=BLACK)

            _ax, _ay = _cx + 5, _ry + 22
            if _up:
                draw.polygon([(_ax, _ay + 11), (_ax + 10, _ay + 11), (_ax + 5, _ay)], fill=YELLOW)
                draw.polygon([(_ax, _ay + 11), (_ax + 10, _ay + 11), (_ax + 5, _ay)], outline=BLACK)
            else:
                draw.polygon([(_ax, _ay), (_ax + 10, _ay), (_ax + 5, _ay + 11)], fill=RED)
            draw.text((_cx + 20, _ry + 18), f"{abs(_pct):.1f}%", font=fonts['20'],
                      fill=BLACK if _up else RED)

    draw.line((col_w, 10, col_w, 470), fill=RED, width=2)

    # --- COLUMN 2 (Weather) ---
    col2_x = col_w + 20

    if 'current' in weather:
        cur = weather['current']
        temp = cur.get('temperature_2m', 0)
        hum = cur.get('relative_humidity_2m', 0)
        pres = cur.get('surface_pressure', 0)
        w_code = cur.get('weather_code', 0)
        wind_dir = cur.get('wind_direction_10m', 0)
        wind_spd = cur.get('wind_speed_10m', 0)
        is_day = cur.get('is_day', 1)
        uv_index = cur.get('uv_index', 0.0)

        temp_rounded = math.floor(temp + 0.5)

        draw_weather_glyph(draw, col2_x, 20, 90, w_code, is_day)
        draw.text((col2_x + 100, 10), f"{temp_rounded}°C", font=fonts['80'], fill=RED)

        uv_x, uv_y = col2_x + 320, 25
        uv_rounded = math.floor(uv_index + 0.5)
        draw.text((uv_x, uv_y), "UV", font=fonts['28'], fill=BLACK)
        uv_val_str = str(uv_rounded)
        try:
            bbox = draw.textbbox((0, 0), uv_val_str, font=fonts['60'])
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            tw, th = draw.textsize(uv_val_str, font=fonts['60'])

        uv_val_x, uv_val_y = uv_x + 45, 5
        if uv_rounded >= 6:
            pad = 5
            draw.rectangle((uv_val_x - pad, uv_val_y - pad + 10, uv_val_x + tw + pad, uv_val_y + th + pad), fill=RED)
            draw.text((uv_val_x, uv_val_y), uv_val_str, font=fonts['60'], fill=WHITE)
        else:
            draw.text((uv_val_x, uv_val_y), uv_val_str, font=fonts['60'], fill=BLACK)

        draw.text((col2_x + 100, 95), f"Humidity: {hum}%", font=fonts['20'], fill=BLACK)
        draw.text((col2_x + 100, 120), f"Press: {pres} hPa", font=fonts['20'], fill=BLACK)

        draw.line((col2_x, 140, col2_x + col_w - 40, 140), fill=RED, width=2)

        y_c2 = 160
        draw_icon(draw, col2_x + 5, y_c2, "icon_wind", (30, 30), color=RED)

        cx, cy, r = col2_x + 80, y_c2 + 80, 60
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=BLACK, width=2)

        for angle in range(0, 360, 45):
            rad_tick = math.radians(angle)
            inner_r = r - 8 if angle % 90 == 0 else r - 4
            tx1, ty1 = cx + inner_r * math.cos(rad_tick), cy + inner_r * math.sin(rad_tick)
            tx2, ty2 = cx + r * math.cos(rad_tick), cy + r * math.sin(rad_tick)
            draw.line((tx1, ty1, tx2, ty2), fill=RED, width=2)

        draw.text((cx - 8, cy - r - 22), "N", font=fonts['20'], fill=BLACK)
        draw.text((cx - 8, cy + r + 4), "S", font=fonts['20'], fill=BLACK)
        draw.text((cx + r + 6, cy - 10), "E", font=fonts['20'], fill=BLACK)
        draw.text((cx - r - 24, cy - 10), "W", font=fonts['20'], fill=BLACK)

        rad_arrow = math.radians(wind_dir - 90)
        tip_x = cx + (r - 12) * math.cos(rad_arrow)
        tip_y = cy + (r - 12) * math.sin(rad_arrow)
        base_angle = math.radians(150)
        left_x = cx + 20 * math.cos(rad_arrow + base_angle)
        left_y = cy + 20 * math.sin(rad_arrow + base_angle)
        right_x = cx + 20 * math.cos(rad_arrow - base_angle)
        right_y = cy + 20 * math.sin(rad_arrow - base_angle)
        draw.polygon([(tip_x, tip_y), (left_x, left_y), (right_x, right_y)], fill=RED)
        draw.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=RED)

        spd_text = f"{wind_spd} km/h"
        try:
            bbox = draw.textbbox((0, 0), spd_text, font=fonts['20'])
            tw = bbox[2] - bbox[0]
        except AttributeError:
            tw = draw.textsize(spd_text, font=fonts['20'])[0]

        draw.text((cx - tw / 2, cy + 25), spd_text, font=fonts['20'], fill=BLACK)

        aqi_x = col2_x + 180
        draw.text((aqi_x, y_c2 + 10), "AIR QUALITY", font=fonts['20'], fill=BLACK)
        draw.text((aqi_x, y_c2 + 55), "AQI:", font=fonts['28'], fill=BLACK)

        aqi_str = str(aqi)
        try:
            bbox = draw.textbbox((0, 0), aqi_str, font=fonts['80'])
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            tw, th = draw.textsize(aqi_str, font=fonts['80'])

        val_x, val_y = aqi_x + 80, y_c2 + 66

        if aqi >= 50:
            pad = 20
            draw.rectangle((val_x - pad, val_y - pad + 15, val_x + tw + pad, val_y + th + pad - 5), fill=RED)
            draw.text((val_x, val_y), aqi_str, font=fonts['80'], fill=WHITE)
        else:
            draw.text((val_x, val_y), aqi_str, font=fonts['80'], fill=BLACK)

        draw.line((col2_x, 320, col2_x + col_w - 40, 320), fill=RED, width=2)

        hourly = weather.get('hourly', {})
        times = hourly.get('time', [])
        temps = hourly.get('temperature_2m', [])
        codes = hourly.get('weather_code', [])

        cur_iso = datetime.now().strftime("%Y-%m-%dT%H:00")
        try:
            start_idx = times.index(cur_iso) + 1
        except:
            start_idx = 0

        for i in range(4):
            idx = start_idx + i
            if idx < len(times):
                off_x = col2_x + (i * 105)
                draw.text((off_x + 10, 340), f"{times[idx].split('T')[1][:5]}", font=fonts['24'], fill=BLACK)
                draw_weather_glyph(draw, off_x + 15, 375, 60, codes[idx], 1)
                f_temp = math.floor(temps[idx] + 0.5)
                draw.text((off_x + 15, 440), f"{f_temp}°C", font=fonts['24'], fill=RED)

    draw.line((col_w * 2, 10, col_w * 2, 470), fill=RED, width=2)

    # --- COLUMN 3 (Time, Claude/Spotify/Progress, Gmail) ---
    col3_x = col_w * 2 + 30
    dt = datetime.now()

    # 1. Time & Date
    draw.text((col3_x, 10), dt.strftime("%H:%M"), font=fonts['clock'], fill=RED)

    date_str = dt.strftime("%d %B %Y")
    day_str = dt.strftime("%a").upper()

    draw.text((col3_x, 170), date_str, font=fonts['32'], fill=BLACK)
    draw.text((col3_x + 340, 170), day_str, font=fonts['32'], fill=BLACK)

    draw.line((col3_x, 220, epd.width - 20, 220), fill=RED, width=2)

    # 2. Claude AI OR Spotify OR Time Progress
    sp_y = 240
    # Clear background for widget
    draw.rectangle((col3_x, sp_y, col3_x + 420, sp_y + 130), fill=WHITE)

    if ENABLE_CLAUDE:
        draw_icon(draw, col3_x, sp_y - 2, "icon_claude", (34, 34), color=RED)
        draw.text((col3_x + 44, sp_y), "CLAUDE AI USAGE", font=fonts['28'], fill=BLACK)

        if claude.get('error'):
            draw.text((col3_x, sp_y + 50), "Claude Usage Error", font=fonts['24'], fill=BLACK)
        else:
            # 5-Hour Limit
            pct_5h = claude.get('five_hour', {}).get('utilization', 0)
            resets_5h = claude.get('five_hour', {}).get('resets_at')
            rem_5h = time_until(resets_5h)

            draw.text((col3_x, sp_y + 40), f"5-Hour Limit: {pct_5h}% (Resets in {rem_5h})", font=fonts['20'], fill=BLACK)
            bx, bw, bh = col3_x, 400, 15
            draw.rectangle((bx, sp_y + 65, bx + bw, sp_y + 65 + bh), outline=BLACK, width=2)
            fill_w = int((bw - 4) * min(pct_5h / 100.0, 1.0))
            if fill_w > 0: draw.rectangle((bx + 2, sp_y + 67, bx + 2 + fill_w, sp_y + 65 + bh - 2), fill=YELLOW)

            # 7-Day Limit
            pct_7d = claude.get('seven_day', {}).get('utilization', 0)
            resets_7d = claude.get('seven_day', {}).get('resets_at')
            rem_7d = time_until(resets_7d)

            draw.text((col3_x, sp_y + 90), f"7-Day Limit: {pct_7d}% (Resets in {rem_7d})", font=fonts['20'], fill=BLACK)
            draw.rectangle((bx, sp_y + 115, bx + bw, sp_y + 115 + bh), outline=BLACK, width=2)
            fill_w = int((bw - 4) * min(pct_7d / 100.0, 1.0))
            if fill_w > 0: draw.rectangle((bx + 2, sp_y + 117, bx + 2 + fill_w, sp_y + 115 + bh - 2), fill=YELLOW)

    elif ENABLE_SPOTIFY:
        if spotify['cover']:
            Himage.paste(spotify['cover'], (col3_x, sp_y))
        else:
            draw_icon(draw, col3_x, sp_y, "icon_spotify", (120, 120), color=RED)

        status_ico = "icon_play" if spotify['status'] == 'PLAYING' else "icon_pause"
        draw_icon(draw, col3_x + 140, sp_y + 10, status_ico, (30, 30))

        if spotify['status'] == 'PLAYING':
            words = spotify['text'].split(' - ')
            artist = words[0] if len(words) > 0 else "Unknown"
            track = words[1] if len(words) > 1 else ""
            draw.text((col3_x + 180, sp_y + 10), artist[:20], font=fonts['28'], fill=BLACK)
            draw.text((col3_x + 140, sp_y + 50), track[:25], font=fonts['24'], fill=BLACK)

    else:
        # Fallback: Time Progress
        tp_y = sp_y
        draw.text((col3_x, tp_y), "TIME PROGRESS", font=fonts['28'], fill=BLACK)

        day_pct = (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400.0
        days_in_m = calendar.monthrange(dt.year, dt.month)[1]
        month_pct = (dt.day - 1 + (dt.hour / 24.0)) / days_in_m
        days_in_y = 366 if calendar.isleap(dt.year) else 365
        year_pct = (dt.timetuple().tm_yday - 1 + (dt.hour / 24.0)) / days_in_y

        def draw_prog(y_offset, label, pct):
            draw.text((col3_x, tp_y + y_offset), label, font=fonts['24'], fill=BLACK)
            bx = col3_x + 110
            bw = 200
            bh = 20
            draw.rectangle((bx, tp_y + y_offset + 2, bx + bw, tp_y + y_offset + bh + 2), outline=BLACK, width=2)
            if pct > 0:
                fill_w = int((bw - 4) * min(pct, 1.0))
                if fill_w > 0:
                    draw.rectangle((bx + 2, tp_y + y_offset + 4, bx + 2 + fill_w, tp_y + y_offset + bh), fill=YELLOW)
            draw.text((bx + bw + 15, tp_y + y_offset), f"{int(pct * 100)}%", font=fonts['24'], fill=BLACK)

        draw_prog(40, "DAY", day_pct)
        draw_prog(75, "MONTH", month_pct)
        draw_prog(110, "YEAR", year_pct)

    draw.line((col3_x, 380, epd.width - 20, 380), fill=RED, width=2)

    # 3. DSL line (Fritz!Box) or Gmail
    gm_y = 400
    if ENABLE_FRITZBOX:
        draw_icon(draw, col3_x, gm_y + 2, "icon_wifi", (44, 44), color=RED)
        if fritz.get('ok'):
            _dn = fritz['down_max'] / 1e6
            _up = fritz['up_max'] / 1e6
            draw.text((col3_x + 56, gm_y), f"DSL {_dn:.0f} / {_up:.0f} Mbps",
                      font=fonts['28'], fill=BLACK)

            # Latency belongs here too: the line can sync at full rate while
            # routing is terrible, and sync rate alone would not show that.
            _hrs = fritz['uptime'] // 3600
            _now_mbps = fritz['down_now'] / 1e6
            draw.text((col3_x + 56, gm_y + 34),
                      f"Now {_now_mbps:.1f} Mbps | {ping['current']} ms | Up {_hrs // 24}d {_hrs % 24}h",
                      font=fonts['20'], fill=BLACK)
        else:
            draw.text((col3_x + 56, gm_y + 8), "DSL link down", font=fonts['28'], fill=RED)
    else:
        draw_icon(draw, col3_x, gm_y, "icon_mail", (60, 60), color=RED)
        draw.text((col3_x + 80, gm_y + 10), f"Unread Inbox: {gmail_unread}", font=fonts['35'], fill=BLACK)

    return Himage


# --- MAIN LOOP ---
def main():
    auth_strava()
    auth_claude()
    auth_antigravity()
    auth_codex()
    roborock_user_data = auth_roborock(ROBOROCK_CONF['EMAIL'])

    signal.signal(signal.SIGALRM, timeout_handler)
    epd = None

    try:
        epd = epd10in85.EPD()
        # Startup talks to the panel OUTSIDE the main loop's watchdog. If the
        # panel is unplugged, half-seated or wedged, its BUSY line never
        # releases and we would block here forever while systemd still reports
        # the unit "active" - a wall display frozen on a stale frame with no
        # error anywhere. Arm the watchdog so a dead panel exits non-zero and
        # lets systemd retry us instead.
        signal.alarm(120)
        epd.init()
        epd.Clear()
        signal.alarm(0)
        time.sleep(1)
        epd.init_Part()

        def load_font(name, size):
            return ImageFont.truetype(os.path.join(FONT_DIR, name), size)

        fonts = {
            '20': load_font('Aldrich-Regular.ttc', 20),
            '24': load_font('Aldrich-Regular.ttc', 24),
            '28': load_font('Aldrich-Regular.ttc', 28),
            '32': load_font('Aldrich-Regular.ttc', 32),
            '35': load_font('Aldrich-Regular.ttc', 35),
            '40': load_font('Aldrich-Regular.ttc', 40),
            '60': load_font('Aldrich-Regular.ttc', 60),
            '80': load_font('Aldrich-Regular.ttc', 80),
            'clock': load_font('advanced_led_board-7.ttc', 180),
        }

        t_data = threading.Thread(target=update_data_thread)
        t_data.daemon = True
        t_data.start()

        if ENABLE_ROBOROCK:
            t_robo = threading.Thread(target=roborock_update_thread, args=(roborock_user_data, ROBOROCK_CONF['EMAIL']))
            t_robo.daemon = True
            t_robo.start()

        # Let the fetch threads land their first results before the opening
        # frame. Without this the first paint shows an empty weather column, and
        # on this panel the next refresh is REFRESH_INTERVAL_SEC away - so a
        # blank column would sit on the wall for minutes.
        _wait_until = time.time() + 45
        while time.time() < _wait_until:
            with data_store.lock:
                if 'current' in data_store.weather:
                    break
            time.sleep(1)
        else:
            logging.warning("First weather fetch did not arrive in time; painting anyway")

        refresh_counter = 0

        while True:
            start_time = time.time()
            try:
                # CPU-bound rendering runs OUTSIDE the hardware watchdog: on a
                # Pi Zero 1 it can be slow, but it never hangs, so a slow render
                # must not trigger a reboot and throw away the frame.
                image = render_screen(epd, fonts)
                buf = epd.getbuffer(image)

                if refresh_counter >= 600:
                    logging.info("Full Refresh cycle")
                    signal.alarm(90)  # full refresh flashes the whole panel, it is slow
                    epd.init()
                    epd.display(buf)
                    time.sleep(2)
                    epd.init_Part()
                    signal.alarm(0)
                    refresh_counter = 0
                else:
                    logging.debug("Display refresh")
                    signal.alarm(90)  # no partial mode here: every refresh is a full ~18s one
                    # On the Pi Zero 1 the panel reliably completes only the
                    # FIRST partial after an init: afterwards the controller
                    # latches BUSY low forever. init_Part() does a hardware reset
                    # (RST pin) that pulls it out of that state, so re-arm before
                    # every frame. Skipped on faster boards (see the flag above).
                    if PANEL_REINIT_EACH_FRAME:
                        epd.init_Part()
                    epd.display_Partial(buf, 0, 0, epd.width, epd.height)
                    signal.alarm(0)
                    refresh_counter += 1

                del image
                del buf
                if refresh_counter % 10 == 0: gc.collect()

            except HardwareTimeoutError:
                logging.critical("HARDWARE HANG DETECTED!")
                signal.alarm(0)
                logging.shutdown()
                os.execv(sys.executable, ['python'] + sys.argv)
            except OSError as e:
                signal.alarm(0)
                if e.errno == 24:
                    os.execv(sys.executable, ['python'] + sys.argv)
            except Exception as e:
                signal.alarm(0)
                logging.error(f"Unexpected error in main: {e}", exc_info=True)

            elapsed = time.time() - start_time
            sleep_time = max(5, REFRESH_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)

    except HardwareTimeoutError:
        # Panel never released BUSY during startup. Exit non-zero so systemd
        # retries rather than leaving the unit "active" on a frozen frame.
        logging.critical(
            "Panel did not respond during startup (BUSY never released). "
            "Check the ribbon cables and the HAT seating."
        )
        try:
            signal.alarm(0)
            epd10in85.epdconfig.module_exit(cleanup=True)
        except Exception:
            pass
        sys.exit(1)
    except KeyboardInterrupt:
        try:
            signal.alarm(0)
            epd10in85.epdconfig.module_exit(cleanup=True)
        except:
            pass
        exit()


if __name__ == '__main__':
    main()
