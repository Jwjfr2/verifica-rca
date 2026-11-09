"""Apple Vision OCR backend for the RCA captcha (macOS only, on-device, no model download).

`ocr_vision.swift` is compiled into `build/vision_ocr` on first use and then kept as a
long-lived process (`--serve`), so recognition costs ~30-45 ms per image instead of paying
the model warm-up on every call.

Measured on dataset/ (labels.json) at 2x: 16/20 exact, no wrong reading — correct reads
come back with confidence 1.00, the one bad read (a noisy image) with 0.30, the rest are
empty (abstention). ~25 ms/image.
"""
import atexit
import os
import re
import subprocess
import sys
import threading

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SWIFT = os.path.join(HERE, "ocr_vision.swift")
BIN = os.path.join(HERE, "build", "vision_ocr")
SCALE = 2

_proc = None
_lock = threading.Lock()


def is_blank(path, black_frac=0.6):
    """True for the unreadable 'black canvas' variant (~20% of requests).

    The renderer draws black glyphs on a black, transparent canvas: the PNG is RGBA and
    essentially all pixels are near-black, with no legible text.
    """
    im = Image.open(path)
    if im.mode != "RGBA":
        return False
    hist = im.convert("L").histogram()
    return sum(hist[:30]) / (im.width * im.height) > black_frac


def available():
    return sys.platform == "darwin" and os.path.exists(SWIFT)


def build(force=False):
    """Compile the Swift helper (needs the Xcode command line tools)."""
    if os.path.exists(BIN) and not force:
        return BIN
    os.makedirs(os.path.dirname(BIN), exist_ok=True)
    subprocess.run(["swiftc", "-O", SWIFT, "-o", BIN], check=True)
    return BIN


def _daemon():
    global _proc
    if _proc is not None and _proc.poll() is None:
        return _proc
    binary = build()
    env = {**os.environ, "OCR_SCALE": str(SCALE)}
    _proc = subprocess.Popen([binary, "--serve"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, bufsize=1, env=env)
    return _proc


@atexit.register
def _close():
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.stdin.close()
            _proc.wait(timeout=2)
        except Exception:
            _proc.kill()
    _proc = None


def _parse(line):
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 4 or parts[1] == "ERR":
        return None, 0.0
    try:
        conf = float(parts[2])
    except ValueError:
        conf = 0.0
    digits = re.sub(r"\D", "", parts[3])
    if len(digits) != 4:
        return None, 0.0
    return digits, conf


_WARM = os.path.join("/tmp", "rca_vision_warm.png")


def prime():
    """Start the daemon and pay the one-off model warm-up now (call it in a thread)."""
    if not available():
        return
    with _lock:
        try:
            proc = _daemon()
            if not os.path.exists(_WARM):
                Image.new("RGB", (40, 20), "white").save(_WARM)
            proc.stdin.write(_WARM + "\n")
            proc.stdin.flush()
            proc.stdout.readline()
        except Exception:
            globals()["_proc"] = None


def read(path, timeout=10):
    """Return (digits, confidence) or (None, 0.0) when Vision finds nothing usable."""
    if not available():
        return None, 0.0
    with _lock:
        try:
            proc = _daemon()
            proc.stdin.write(os.path.abspath(path) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        except Exception:
            globals()["_proc"] = None
            return None, 0.0
    return _parse(line)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--build":
        print("built", build(force=True))
        sys.exit(0)
    for p in args:
        print(p, read(p))
