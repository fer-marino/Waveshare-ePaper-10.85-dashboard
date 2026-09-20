import time
import spidev
import gpiod
from gpiod.line import Direction, Value

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

_out_req = None
_in_req = None
_spi = None
_spi_opened = False


def _ensure_gpio():
    global _out_req, _in_req
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


def digital_write(pin, value):
    _ensure_gpio()
    _out_req.set_value(pin, Value.ACTIVE if value else Value.INACTIVE)


def digital_read(pin):
    _ensure_gpio()
    if pin == EPD_BUSY_PIN:
        return 1 if _in_req.get_value(pin) == Value.ACTIVE else 0
    return 1 if _out_req.get_value(pin) == Value.ACTIVE else 0


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
        _spi.open(1, 0)
        _spi.max_speed_hz = 4000000
        _spi.mode = 0b00
        _spi_opened = True

    return 0


def module_exit(cleanup=False):
    global _out_req, _in_req, _spi_opened
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
