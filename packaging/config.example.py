# e-Paper dashboard configuration.
#
# Anything omitted falls back to the default in main.py, so this only needs the
# values you actually want to change. Installed as a dpkg conffile: package
# upgrades will not overwrite your edits.

# --- WIDGETS ---
ENABLE_STRAVA = False        # paid Strava tier only
ENABLE_BAMBU = False
ENABLE_ROBOROCK = False
ENABLE_ANTIGRAVITY = False
ENABLE_CODEX = False
ENABLE_CLAUDE = False
ENABLE_SPOTIFY = False
ENABLE_FRITZBOX = True       # DSL line stats in place of the Gmail slot
ENABLE_PHRASE = True         # phrase of the day, top-left slot

# --- LOCATION (weather, AQI, UV, daylight) ---
LOCATION_LAT = 50.1109
LOCATION_LON = 8.6821

# --- REFRESH ---
# 4-colour panels have no partial refresh: every update is a full ~12s flash,
# and refreshing large e-paper too often causes ghosting. Keep this generous.
# Black/white panels can go much lower.
REFRESH_INTERVAL_SEC = 300

# --- STATE ---
# Where tokens, session files and the log are written.
STATE_DIR = '/var/lib/epaper-dashboard'

# --- DEVICES ---
# Bambu Lab printer: values are on the printer under Settings -> Network.
PRINTER_CONF = {
    'IP': '',
    'SERIAL': '',
    'ACCESS_CODE': '',
}

# Roborock: the account email. First run prompts for an emailed OTP.
ROBOROCK_CONF = {
    'EMAIL': '',
}

# Fritz!Box: read over TR-064/IGD, no credentials needed.
FRITZBOX_CONF = {
    'HOST': 'fritz.box',
}

# Spotify now-playing is read via Last.fm.
LASTFM_CONF = {
    'API_KEY': '',
    'USERNAME': '',
}
