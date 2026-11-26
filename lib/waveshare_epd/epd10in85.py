"""
Compatibility shim: presents the black/white epd10in85 interface that main.py
expects, but drives the 4-color (G) panel underneath.

The dashboard was written for the B/W 10.85" panel, which reports width=1360 and
supports partial refresh. The G panel's driver reports width=680 (it addresses the
screen as two 680px halves) and has no partial refresh at all - 4-color e-paper
physically cannot do it. This shim reconciles both differences so main.py's layout
math and refresh calls keep working unchanged.
"""
import os
import sys
import time

# epd10in85g does a plain `import epdconfig`, so this package's own directory has
# to be importable in its own right, not just as a package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import epd10in85g
import epdconfig  # re-exported: main.py calls epd10in85.epdconfig.module_exit()


# The panel's fast waveform: noticeably less flashing and a shorter refresh, at
# the cost of some color saturation and slightly more ghosting. Set False to fall
# back to the full-quality waveform.
FAST_REFRESH = True

# --- BUSY LINE WORKAROUND ---
# On the Orange Pi unit, the BUSY pin had a bad header contact and floated
# "ready" forever (causing ReadBusyH() to return in ~0.1s while the panel was
# still refreshing). If ReadBusyH() actually waited for the hardware signal
# (> 1.0s on refresh, > 0.3s on init), we trust it; if it returned suspiciously
# fast (< threshold), we automatically fall back to the fixed settle delay so
# a floating BUSY pin never corrupts the frame.
TRUST_BUSY = epdconfig.BOARD == 'raspberrypi'
INIT_SETTLE_SEC = 2
REFRESH_SETTLE_SEC = 25


class EPD:
    def __init__(self):
        self._epd = epd10in85g.EPD()
        # main.py lays out against the full panel width, not the half-panel the
        # G driver reports.
        self.width = self._epd.width * 2
        self.height = self._epd.height

    def _settle(self, seconds, min_expected_busy=1.0):
        # Stands in for the BUSY handshake if TRUST_BUSY is False or if
        # ReadBusyH() returned suspiciously fast (floating/disconnected pin).
        waited = getattr(self._epd, 'last_busy_duration', 0.0)
        if not TRUST_BUSY or waited < min_expected_busy:
            remaining = max(0.0, seconds - waited)
            if remaining > 0:
                time.sleep(remaining)

    def init(self):
        rc = self._epd.Init_Fast() if FAST_REFRESH else self._epd.Init()
        self._settle(INIT_SETTLE_SEC, min_expected_busy=0.25)
        return rc

    def init_Part(self):
        # No partial-refresh mode exists on this panel; nothing to arm.
        return 0

    def Clear(self):
        self._epd.Clear()
        self._settle(REFRESH_SETTLE_SEC, min_expected_busy=2.0)

    def getbuffer(self, image):
        return self._epd.getbuffer(image)

    def display(self, buf):
        self._epd.display(buf)
        self._settle(REFRESH_SETTLE_SEC, min_expected_busy=2.0)

    def display_Partial(self, buf, x=0, y=0, w=None, h=None):
        # Falls back to a full refresh - the only kind this panel supports.
        self.display(buf)

    def sleep(self):
        self._epd.sleep()
