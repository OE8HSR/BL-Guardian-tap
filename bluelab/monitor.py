"""Serial reader for the Guardian Monitor debug stream."""

import time
from dataclasses import dataclass
from datetime import datetime

import serial
from serial.tools import list_ports

# 9600 8N1. The firmware emits one CR-terminated line about every 20 seconds.
BAUD = 9600
# FTDI vendor id on the red USB-UART board.
FTDI_VID = 0x0403


class GuardianError(Exception):
    """The monitor port could not be opened or a reading could not be taken."""


@dataclass(frozen=True)
class Reading:
    """One measurement line from the monitor.

    Column order matches the firmware header EC, T, mV, p. ``tick`` increments
    about once per second. ``raw_a`` and ``raw_b`` are still unidentified.
    The trailing flags are the firmware state tokens (for example ``ECx``,
    ``Tx``, ``pHx``).
    """

    tick: int
    ec: float
    temperature_c: float
    ph_mv: float
    ph: float
    raw_a: str
    raw_b: str
    status: str
    ec_flag: str
    temp_flag: str
    ph_flag: str
    received_at: datetime

    @property
    def celsius(self):
        """Temperature in degrees Celsius. Same value as ``temperature_c``."""
        return self.temperature_c

    @property
    def fahrenheit(self):
        """Temperature in degrees Fahrenheit."""
        return self.temperature_c * 9.0 / 5.0 + 32.0

    def temperature(self, unit="C"):
        """Return the temperature in ``C`` or ``F``."""
        key = unit.strip().lower().replace("°", "")
        if key in ("c", "celsius"):
            return self.celsius
        if key in ("f", "fahrenheit"):
            return self.fahrenheit
        raise ValueError("Temperature unit must be 'C' or 'F'.")

    @property
    def cf(self):
        """Conductivity factor. Bluelab uses 1 EC = 10 CF."""
        return self.ec * 10.0

    @property
    def ppm_500(self):
        """TDS on the 500 scale. 1 EC = 500 ppm."""
        return self.ec * 500.0

    @property
    def ppm_700(self):
        """TDS on the 700 scale. 1 EC = 700 ppm."""
        return self.ec * 700.0

    def conductivity(self, unit="EC"):
        """Return conductivity as ``EC``, ``CF``, ``ppm500`` or ``ppm700``."""
        key = unit.strip().lower().replace(" ", "")
        if key == "ec":
            return self.ec
        if key == "cf":
            return self.cf
        if key in ("ppm500", "tds500"):
            return self.ppm_500
        if key == "ppm700":
            return self.ppm_700
        raise ValueError("Conductivity unit must be 'EC', 'CF', 'ppm500' or 'ppm700'.")

    def __str__(self):
        stamp = self.received_at.strftime("%H:%M:%S")
        return (
            f"{stamp}  "
            f"pH {self.ph:.2f}   "
            f"EC {self.ec:.2f}   "
            f"{self.temperature_c:.2f} C   "
            f"{self.ph_mv:.1f} mV   "
            f"{self.status}  "
            f"{self.ec_flag} {self.temp_flag} {self.ph_flag}"
        )


def find_port():
    """Return the FTDI serial device, or the only USB serial device."""
    matches = []
    for port in list_ports.comports():
        device = port.device
        if port.vid == FTDI_VID or "usbserial" in device or "usbmodem" in device:
            matches.append(device)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise GuardianError("No USB serial port found. Pass the device path to Guardian().")
    raise GuardianError("More than one USB serial port: " + ", ".join(matches))


def parse_line(line):
    """Parse one measurement line. Banner lines that start with '#' are ignored."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    fields = line.split("\t")
    if len(fields) < 12:
        return None
    try:
        return Reading(
            tick=int(fields[0]),
            ec=float(fields[2]),
            temperature_c=float(fields[3]),
            ph_mv=float(fields[4]),
            ph=float(fields[5]),
            raw_a=fields[6].strip(),
            raw_b=fields[7].strip(),
            status=fields[8].strip(),
            ec_flag=fields[9].strip(),
            temp_flag=fields[10].strip(),
            ph_flag=fields[11].strip(),
            received_at=datetime.now(),
        )
    except ValueError:
        return None


def _pop_line(buf):
    """Split the first CR or LF-terminated line off ``buf``."""
    for sep in (b"\r\n", b"\n", b"\r"):
        if sep in buf:
            raw, rest = buf.split(sep, 1)
            return raw, rest
    return None, buf


class Guardian:
    """Connection to a Guardian Monitor on the USB-UART tap.

    The monitor pushes readings on its own. ``read`` blocks until the next
    one arrives. Iterating the connection yields each new reading once::

        with Guardian() as monitor:
            for reading in monitor:
                print(reading)
    """

    def __init__(self, port=None, baud=BAUD):
        self.port = port or find_port()
        self.baud = baud
        self._ser = None
        self._buf = b""

    def open(self):
        """Open the serial port. DTR and RTS stay released."""
        if self._ser is not None and self._ser.is_open:
            return self
        self._ser = serial.Serial(self.port, self.baud, timeout=0.2)
        self._ser.dtr = False
        self._ser.rts = False
        self._buf = b""
        return self

    def close(self):
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def __enter__(self):
        return self.open()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self, timeout=None):
        """Block until the next measurement.

        ``timeout`` is in seconds. ``None`` waits indefinitely. Returns
        ``None`` when the timeout expires before a measurement arrives.
        """
        if self._ser is None or not self._ser.is_open:
            raise GuardianError("Monitor is not open.")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return None
            # macOS sometimes reports the FTDI port as ready and then returns
            # no bytes. Treat that as an empty read and keep waiting.
            try:
                chunk = self._ser.read(256)
            except serial.SerialException:
                time.sleep(0.05)
                continue
            if chunk:
                self._buf += chunk
            while True:
                raw, self._buf = _pop_line(self._buf)
                if raw is None:
                    break
                reading = parse_line(raw.decode("ascii", errors="replace"))
                if reading is not None:
                    return reading

    def __iter__(self):
        while True:
            yield self.read()
