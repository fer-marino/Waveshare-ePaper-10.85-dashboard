# Waveshare-ePaper-10.85 Dashboard (Orange Pi / 4-colour fork)

An e-ink dashboard for the Waveshare 10.85" e-Paper HAT+, showing weather, air quality, the time, smart-home devices and AI usage limits on one screen.

This is a fork of [czuryk/Waveshare-ePaper-10.85-dashboard](https://github.com/czuryk/Waveshare-ePaper-10.85-dashboard). The upstream project targets a Raspberry Pi Zero with the black/white panel. This fork has diverged a lot:

| | Upstream | This fork |
|---|---|---|
| Board | Raspberry Pi Zero 1W / 2W | **Orange Pi Zero 2W** (Allwinner H618, Armbian) |
| Panel | 10.85" black/white | **10.85" 4-colour (G)**: black, white, yellow, red |
| GPIO | gpiozero / lgpio | **libgpiod v2** (`/dev/gpiochip1`) and SPI bus 1 |
| Refresh | Partial refresh every 60 s | Full refresh every **300 s** (the colour panel has no partial refresh) |
| Configuration | Edit the top of `main.py` | Separate **`config.py`**, kept out of git |
| Running | `tmux` | **systemd service**, or a **Debian package** built by CI |
| Column 1 | Strava, printer, Roborock / AI usage, fallback | **Phrase of the day** or ping, printer, Roborock / AI usage, **Coinbase 24h movers** |
| Column 3 bottom slot | Gmail | **Fritz!Box DSL line stats** (Gmail still available) |

> The panel code is now Orange Pi-specific. `lib/waveshare_epd/epdconfig.py` hard-codes Orange Pi Zero 2W pin numbers, the GPIO chip and the SPI bus. To run it on a Raspberry Pi, change those values (or use upstream's `epdconfig.py`).

---

## Screen layout

The panel is 1360×480, split into three columns. Only the four colours the panel can show are used. Anything else gets dithered, and dithered text is unreadable.

**Column 1: four stacked widgets**
1. **Phrase of the day**: a daily German phrase with its English translation (`ENABLE_PHRASE`). If Strava is enabled it takes this slot instead. With both disabled, the slot shows **internet quality** (current ping plus a latency history bar chart).
2. **Bambu Lab printer**: status, progress bar, remaining time and layer count. The progress bar is hidden while the printer is offline, idle or finished.
3. **Roborock vacuum**: battery, status and cleaned area. The slot falls back to Antigravity usage, then Codex usage, then **system load** (CPU, free RAM and a CPU history chart), depending on what's enabled.
4. **Coinbase 24h movers**: the top four gainers and losers across Coinbase USD pairs, filtered to pairs with more than $2M of 24h volume so illiquid tokens don't dominate. No API key needed.

**Column 2: weather** (Open-Meteo, no API key)
- Current conditions as a drawn weather glyph, plus temperature, humidity, pressure, and wind direction and speed.
- UV index and AQI, highlighted in red at UV ≥ 6 or AQI ≥ 50.
- A 4-hour forecast.

**Column 3**
- A large clock and the date.
- **Claude Code usage** (5-hour and 7-day limits with reset times), or **Spotify** now playing (via Last.fm). With neither enabled, it shows **time progress** bars for the day, month and year.
- **Fritz!Box DSL**: sync rate, current downstream throughput, ping and line uptime. With `ENABLE_FRITZBOX = False`, this slot shows the **Gmail** unread count instead.

---

## Hardware

* [Orange Pi Zero 2W](http://www.orangepi.org/html/hardWare/computerAndMicrocontrollers/details/Orange-Pi-Zero-2W.html)
* [Waveshare 10.85" e-Paper HAT+ (G)](https://www.waveshare.com/10.85inch-e-paper-hat-plus-g.htm), the 4-colour version

### Wiring

The HAT plugs into the 40-pin header. These are the pins in use, verified with a multimeter against the Orange Pi Zero 2W pin table:

| Signal | Physical pin | SoC pin | GPIO line (`gpiochip1`) |
|---|---|---|---|
| RST | 11 | PH2 | 226 |
| PWR | 12 | PI1 | 257 |
| BUSY | 18 | PH4 | 228 |
| DC | 22 | PI6 | 262 |
| CS (master) | 24 | PH5 | 229 |
| CS (slave) | 26 | PH9 | 233 |
| MOSI / SCLK | 19 / 23 | SPI1 | `/dev/spidev1.0` |

The panel is driven as two 680-px halves, each with its own chip select. Both chip selects are toggled manually as GPIOs, so SPI1 must be enabled **without** its hardware CS (see below).

**BUSY workaround:** on the original unit, the BUSY line doesn't make contact, so the driver waits fixed times instead of reading it (about 25 s per refresh). If your BUSY line works, set `TRUST_BUSY = True` in `lib/waveshare_epd/epd10in85.py` for faster refreshes. `FAST_REFRESH` in the same file picks the fast waveform (less flashing, slightly more ghosting).

---

## Installation

Tested on **Armbian (Debian 13 trixie), kernel 6.18, Python 3.13**.

### 1. Enable SPI1 without chip select

Create an overlay that enables `spidev` on SPI1 without claiming a CS pin:

```dts
/dts-v1/;
/plugin/;

/ {
    compatible = "allwinner,sun50i-h616", "allwinner,sun50i-h618";

    fragment@0 {
        target = <&spi1>;
        __overlay__ {
            status = "okay";
            #address-cells = <1>;
            #size-cells = <0>;
            pinctrl-names = "default";
            pinctrl-0 = <&spi1_pins>;

            spidev@0 {
                compatible = "rohm,dh2228fv";
                status = "okay";
                reg = <0>;
                spi-max-frequency = <4000000>;
            };
        };
    };
};
```

Save it as `spi1-nocs-spidev.dts`, install it with Armbian's overlay tool, and reboot:

```shell
sudo armbian-add-overlay spi1-nocs-spidev.dts
sudo reboot
```

This adds `user_overlays=spi1-nocs-spidev` to `/boot/armbianEnv.txt`. After the reboot, `/dev/spidev1.0` should exist.

### 2. System packages

```shell
sudo apt update
sudo apt install -y git python3-pip python3-pil python3-numpy python3-requests python3-spidev python3-libgpiod
```

`python3-libgpiod` must be **v2** (Debian 13 ships 2.2). The driver uses the v2 API (`gpiod.request_lines`).

### 3. Python packages

`main.py` imports the Google API client at startup, so install it even if you don't use Gmail. The rest are only needed for their widgets:

```shell
pip3 install --break-system-packages google-api-python-client google-auth-httplib2 google-auth-oauthlib
pip3 install --break-system-packages python-roborock aiomqtt   # Roborock
pip3 install --break-system-packages paho-mqtt                 # Bambu Lab
```

`bambulabs_api` is bundled in `lib/`. The startup message `lgpio pin factory unavailable (No module named 'gpiozero')` is harmless: gpiozero isn't used for the panel on this board.

### 4. Get the code and configure

```shell
git clone https://github.com/fer-marino/Waveshare-ePaper-10.85-dashboard.git ~/dashboard
cd ~/dashboard
cp packaging/config.example.py config.py
nano config.py
```

### 5. First run (interactive logins)

Claude, Strava, Roborock and Gmail need a one-time login in the terminal. Run the dashboard once in the foreground, complete the prompts, and stop it with `Ctrl+C`:

```shell
sudo python3 main.py
```

### 6. Run it as a service

```shell
sudo cp packaging/epaper-dashboard.service /etc/systemd/system/
```

The shipped unit expects the Debian package layout (`/opt/epaper-dashboard`). For a git checkout, edit `WorkingDirectory` and `ExecStart` in the copy to point at your checkout (for example `/root/dashboard`), and remove the `EPAPER_CONFIG` line. Then enable and start it:

```shell
sudo systemctl daemon-reload
sudo systemctl enable --now epaper-dashboard
journalctl -u epaper-dashboard -f
```

The unit stops the app with `SIGINT`, so it can power the panel down and release the GPIOs cleanly.

### Alternative: Debian package

Every push builds an `epaper-dashboard_<version>_all.deb` in GitHub Actions. Tagging `v*` attaches it to a release. To build it locally:

```shell
packaging/build-deb.sh 0.1.0
sudo apt install ./dist/epaper-dashboard_0.1.0_all.deb
```

The package installs:
* the code in `/opt/epaper-dashboard`
* the config in `/etc/epaper-dashboard/config.py`, registered as a conffile so upgrades keep your edits
* state (tokens, sessions, the log) in `/var/lib/epaper-dashboard`
* the service `epaper-dashboard.service`, enabled but not started

The package never contains credentials or local state; CI fails the build if any are found. It doesn't pull in the pip packages from step 3, so install those separately.

---

## Configuration

Settings are read from the first of these files that exists:

1. the path in `$EPAPER_CONFIG`
2. `/etc/epaper-dashboard/config.py`
3. `config.py` next to `main.py`

Anything you leave out falls back to the default in `main.py`. `config.py` is in `.gitignore` because it holds credentials. Start from `packaging/config.example.py`.

| Setting | Default | Purpose |
|---|---|---|
| `ENABLE_PHRASE` | `True` | Phrase of the day, top-left slot |
| `ENABLE_STRAVA` | `False` | Strava stats, replaces the phrase (paid Strava tier only) |
| `ENABLE_BAMBU` | `False` | Bambu Lab printer |
| `ENABLE_ROBOROCK` | `False` | Roborock vacuum |
| `ENABLE_ANTIGRAVITY` / `ENABLE_CODEX` | `False` | AI usage, used when Roborock is off |
| `ENABLE_CLAUDE` | `False` | Claude Code usage |
| `ENABLE_SPOTIFY` | `False` | Now playing via Last.fm, used when Claude is off |
| `ENABLE_FRITZBOX` | `True` | Fritz!Box DSL stats instead of Gmail |
| `LOCATION_LAT` / `LOCATION_LON` | Frankfurt | Location for weather, AQI and UV |
| `REFRESH_INTERVAL_SEC` | `300` | Seconds between frames |
| `STATE_DIR` | next to `main.py` | Where tokens, sessions and the log are written |
| `PRINTER_CONF` | empty | `IP`, `SERIAL`, `ACCESS_CODE` |
| `ROBOROCK_CONF` | empty | `EMAIL` |
| `FRITZBOX_CONF` | `fritz.box` | `HOST` |
| `LASTFM_CONF` | empty | `API_KEY`, `USERNAME` |

**Refresh interval:** each refresh on the 4-colour panel is a full-screen flash lasting about 12 s (fast waveform) to 18 s (full waveform). Waveshare warns that refreshing large panels too often causes ghosting and can damage them, so don't go much below 300 s.

### Widget setup

#### Fritz!Box
No credentials needed. The DSL sync rate and throughput come from the router's TR-064/IGD endpoints, which don't require a login. Make sure *Transmit status information over UPnP* is enabled on the Fritz!Box (Home Network → Network → Network Settings). Set `FRITZBOX_CONF['HOST']` if the router isn't reachable as `fritz.box`.

#### Bambu Lab 3D printer
You don't need LAN-only mode. On the printer, open **Settings → Network**, note the **IP address**, **serial number** and **access code**, and put them in `PRINTER_CONF`. Give the printer a fixed IP in your router.

#### Roborock
Put your account email in `ROBOROCK_CONF`. On the first run the app asks for the one-time code that Roborock emails you, then saves the session to `roborock_session.pkl` in `STATE_DIR`.

#### Claude Code
On the first run, the app prints an authorization URL. Open it in a browser and click **Authorize**. You'll land on a dead `localhost` page: copy the whole URL (the one containing `code=...`) back into the terminal. Tokens are saved to `claude_creds.json` next to `main.py`, and usage is refreshed every 10 minutes into `usage.json`.

#### Codex
The dashboard reuses the official Codex CLI's tokens. Run `codex login` on any machine, copy `~/.codex/auth.json` next to `codex.py`, and set `ENABLE_CODEX = True`. The app refreshes and rotates the tokens in that file automatically. You need a paid ChatGPT plan that includes Codex.

#### Strava
Paid Strava accounts only. Create an API application in your Strava settings. On the first run, enter the client ID and secret, open the printed URL, authorize, and paste the `code=...` from the dead `localhost` page back into the terminal. The token is saved to `strava_token.json`.

#### Spotify (via Last.fm)
Connect Spotify to Last.fm, create a Last.fm API key, and fill in `LASTFM_CONF`. A free Last.fm account is enough.

#### Gmail
Only used when `ENABLE_FRITZBOX = False`. Create a Google Cloud project, enable the Gmail API, create an OAuth client ID of type *Desktop app*, and save it as `credentials.json` next to `main.py`. The first run asks you to grant read-only access and saves `token.json`.

---

## How it works

* **Background fetching:** data is fetched in background threads, each service on its own interval (Fritz!Box every 60 s, Claude and Codex every 10 min, the printer every 5–15 s, and so on). A slow or unreachable service never blocks the others.
* **Rendering:** the main loop renders from a thread-safe data store every `REFRESH_INTERVAL_SEC`. At startup it waits up to 45 s for the first weather data, so the first frame isn't blank.
* **Watchdogs:** panel operations run under a `SIGALRM` watchdog. If the panel is unplugged or stuck at startup, the process exits with an error and systemd restarts it after 30 s, instead of the display silently freezing.
* **Errors:** an error while drawing a frame is logged with its full traceback to the journal and to `dashboard.log` (rotated at 1 MB), and the loop tries again on the next cycle.

---

## Troubleshooting

```shell
systemctl status epaper-dashboard
journalctl -u epaper-dashboard -n 100
tail -f ~/dashboard/dashboard.log     # or /var/lib/epaper-dashboard/dashboard.log for the .deb
```

* **`e-Paper busy H` / `busy release` lines:** normal. They mark each panel refresh.
* **The same error every refresh, and the screen doesn't change:** a widget is failing while drawing. The traceback in the log names the line.
* **`/dev/spidev1.0` missing:** the SPI overlay isn't loaded. Check `user_overlays` in `/boot/armbianEnv.txt`.
* **Garbled or half-drawn frames:** the panel is being written while it's still busy. Increase `REFRESH_SETTLE_SEC` in `lib/waveshare_epd/epd10in85.py`, or fix the BUSY line and set `TRUST_BUSY = True`.

---

## Credits

Based on [czuryk/Waveshare-ePaper-10.85-dashboard](https://github.com/czuryk/Waveshare-ePaper-10.85-dashboard). Upstream also has a [3D-printed case](https://makerworld.com/en/models/2322517-epaper-dashboard-waveshare-10-85) and an [assembly video](https://youtu.be/H964RpaJvu0), both designed around the Raspberry Pi Zero.
