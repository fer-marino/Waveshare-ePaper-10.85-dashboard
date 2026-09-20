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
# On this unit the BUSY line is not making contact between the HAT and the panel,
# so it floats and reads "ready" forever. The driver's ReadBusyH() therefore
# returns instantly, the refresh command is fired and we return while the panel
# is still working - which paints a garbled frame.
#
# Everything else (power, SPI, CS, DC, RST) is verified good, so we substitute
# fixed waits for the handshake. Timings come from measuring this panel while
# BUSY still worked: ~12s for a fast-waveform refresh, ~18s full. The margin is
# deliberate; overshooting only costs time, undershooting corrupts the image.
# Set TRUST_BUSY = True once the BUSY connection is repaired.
TRUST_BUSY = False
INIT_SETTLE_SEC = 2
REFRESH_SETTLE_SEC = 25


class EPD:
    def __init__(self):
        self._epd = epd10in85g.EPD()
        # main.py lays out against the full panel width, not the half-panel the
        # G driver reports.
        self.width = self._epd.width * 2
        self.height = self._epd.height

    @staticmethod
    def _settle(seconds):
        # Stands in for the BUSY handshake while that line is disconnected.
        if not TRUST_BUSY:
            time.sleep(seconds)

    def init(self):
        rc = self._epd.Init_Fast() if FAST_REFRESH else self._epd.Init()
        self._settle(INIT_SETTLE_SEC)
        return rc

    def init_Part(self):
        # No partial-refresh mode exists on this panel; nothing to arm.
        return 0

    def Clear(self):
        self._epd.Clear()
        self._settle(REFRESH_SETTLE_SEC)

    def getbuffer(self, image):
        return self._epd.getbuffer(image)

    def display(self, buf):
        self._epd.display(buf)
        self._settle(REFRESH_SETTLE_SEC)

    def display_Partial(self, buf, x=0, y=0, w=None, h=None):
        # Falls back to a full refresh - the only kind this panel supports.
        self.display(buf)

    def sleep(self):
        self._epd.sleep()
