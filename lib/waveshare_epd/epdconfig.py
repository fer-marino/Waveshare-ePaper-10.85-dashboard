import os
import time
import spidev

try:
    import gpiod
    from gpiod.line import Direction, Value
    _HAS_GPIOD_V2 = hasattr(gpiod, 'request_lines')
except ImportError:
    gpiod = None
    _HAS_GPIOD_V2 = False


def _detect_board():
    for path in ('/proc/device-tree/model', '/proc/cpuinfo'):
        try:
            with open(path, 'rb') as f:
                text = f.read().decode(errors='ignore')
            if 'Raspberry Pi' in text or 'BCM2' in text:
                return 'raspberrypi'
            if 'OrangePi' in text or 'Orange Pi' in text:
                return 'orangepi'
        except OSError:
            pass
    return 'orangepi'


BOARD = _detect_board()

if BOARD == 'raspberrypi':
    # Raspberry Pi 40-pin header (BCM GPIO numbering, SPI0)
    EPD_RST_PIN   = 17    # physical pin 11
    EPD_DC_PIN    = 25    # physical pin 22
    EPD_BUSY_PIN  = 24    # physical pin 18
    EPD_PWR_PIN   = 18    # physical pin 12
    EPD_CS_M_PIN  = 8     # physical pin 24 (SPI0 CE0) - manually toggled GPIO
    EPD_CS_S_PIN  = 7     # physical pin 26 (SPI0 CE1) - manually toggled GPIO
    CHIP = '/dev/gpiochip0'
    SPI_BUS = 0
else:
    # Verified pin-by-pin with a multimeter against the official Orange Pi
    # Zero 2W 40-pin table (Xunlong user manual + wiringOP), after reworking
    # several cold solder joints found during that verification.
    EPD_RST_PIN   = 226   # physical pin 11 (PH2)
    EPD_DC_PIN    = 262   # physical pin 22 (PI6)
    EPD_BUSY_PIN  = 228   # physical pin 18 (PH4)
    EPD_PWR_PIN   = 257   # physical pin 12 (PI1)
    EPD_CS_M_PIN  = 229   # physical pin 24 (PH5) - manually toggled GPIO
    EPD_CS_S_PIN  = 233   # physical pin 26 (PH9) - manually toggled GPIO
    CHIP = '/dev/gpiochip1'
    SPI_BUS = 1

_out_req = None
_in_req = None
_gpiozero_outs = None
_gpiozero_busy = None
_spi = None
_spi_opened = False


def _ensure_gpio():
    global _out_req, _in_req, _gpiozero_outs, _gpiozero_busy
    if _HAS_GPIOD_V2:
        if _out_req is None:
            _out_req = gpiod.request_lines(CHIP, consumer='epd10in85g', config={
                EPD_RST_PIN:  gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                EPD_DC_PIN:   gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                EPD_PWR_PIN:  gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE),
                EPD_CS_M_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
                EPD_CS_S_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
            })
        if _in_req is None:
            _in_req = gpiod.request_lines(CHIP, consumer='epd10in85g', config={
                EPD_BUSY_PIN: gpiod.LineSettings(direction=Direction.INPUT),
            })
    else:
        # Fallback to gpiozero on older Raspberry Pi OS (e.g. Bookworm with libgpiod v1)
        if _gpiozero_outs is None:
            import gpiozero
            _gpiozero_outs = {
                EPD_RST_PIN:  gpiozero.DigitalOutputDevice(EPD_RST_PIN, initial_value=False),
                EPD_DC_PIN:   gpiozero.DigitalOutputDevice(EPD_DC_PIN, initial_value=False),
                EPD_PWR_PIN:  gpiozero.DigitalOutputDevice(EPD_PWR_PIN, initial_value=False),
                EPD_CS_M_PIN: gpiozero.DigitalOutputDevice(EPD_CS_M_PIN, initial_value=True),
                EPD_CS_S_PIN: gpiozero.DigitalOutputDevice(EPD_CS_S_PIN, initial_value=True),
            }
            _gpiozero_busy = gpiozero.DigitalInputDevice(EPD_BUSY_PIN, pull_up=False)


def digital_write(pin, value):
    _ensure_gpio()
    if _HAS_GPIOD_V2:
        _out_req.set_value(pin, Value.ACTIVE if value else Value.INACTIVE)
    else:
        if value:
            _gpiozero_outs[pin].on()
        else:
            _gpiozero_outs[pin].off()


def digital_read(pin):
    _ensure_gpio()
    if _HAS_GPIOD_V2:
        if pin == EPD_BUSY_PIN:
            return 1 if _in_req.get_value(pin) == Value.ACTIVE else 0
        return 1 if _out_req.get_value(pin) == Value.ACTIVE else 0
    else:
        if pin == EPD_BUSY_PIN:
            return 1 if _gpiozero_busy.value else 0
        return 1 if _gpiozero_outs[pin].value else 0


def delay_ms(delaytime):
    time.sleep(delaytime / 1000.0)


def spi_writebyte(data):
    if isinstance(data, int):
        data = [data]
    _spi.writebytes(data)


def spi_writebyte2(buf, length):
    _spi.writebytes2(list(buf[:length]))


def module_init():
    global _spi, _spi_opened
    _ensure_gpio()
    digital_write(EPD_PWR_PIN, 1)

    if not _spi_opened:
        _spi = spidev.SpiDev()
        _spi.open(SPI_BUS, 0)
        _spi.max_speed_hz = 4000000
        _spi.mode = 0b00
        try:
            # Both CS pins are toggled manually via GPIO; tell the kernel SPI
            # driver not to assert a hardware chip-select line if supported.
            _spi.no_cs = True
        except OSError:
            pass
        _spi_opened = True

    return 0


def module_exit(cleanup=False):
    global _out_req, _in_req, _gpiozero_outs, _gpiozero_busy, _spi_opened
    if _spi:
        _spi.close()
    _spi_opened = False

    digital_write(EPD_RST_PIN, 0)
    digital_write(EPD_DC_PIN, 0)
    digital_write(EPD_PWR_PIN, 0)

    if cleanup:
        if _out_req:
            _out_req.release()
            _out_req = None
        if _in_req:
            _in_req.release()
            _in_req = None
        if _gpiozero_outs:
            for dev in _gpiozero_outs.values():
                dev.close()
            _gpiozero_outs = None
        if _gpiozero_busy:
            _gpiozero_busy.close()
            _gpiozero_busy = None
