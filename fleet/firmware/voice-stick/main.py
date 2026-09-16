# voice-stick v1 — kids' voice assistant endpoint (profile: copil1)
# M5StickS3 (UiFlow2 MicroPython) -> voice-gateway on 0lympus (192.168.0.100:8095)
#
# Hold KEY1 (front button, BtnA) to talk; release to send.
# KEY2 (side button, BtnB) replays the last answer; on the admin stick a LONG hold
# of KEY2 (>=1.2s) instead TOGGLES the 0lympus TV output (config.TV_ON_URL -> tv-trigger :8099):
# lit -> tv-off, dark -> tv-on. Hold it BEFORE powering the TV off (2026-09-16 freeze).
# Recovery: hold KEY2 during the boot splash -> exits to REPL before anything runs.

import M5
from M5 import Widgets
import network, time, gc, struct, binascii, sys
import requests2
import config

BLACK = 0x000000
WHITE = 0xFFFFFF
RED = 0xFF3030
GREEN = 0x30FF60
YELLOW = 0xFFD030
GREY = 0x808080
DARKRED = 0x400000

CHUNK_SAMPLES = 4000  # 0.25 s @ 16 kHz

_sta = None
_last_audio = None
_last_activity = 0
_dimmed = False


# ---------- display helpers ----------

def _translit(s):
    for a, b in (("ă", "a"), ("â", "a"), ("î", "i"), ("ș", "s"),
                 ("ț", "t"), ("ş", "s"), ("ţ", "t"),
                 ("Ă", "A"), ("Â", "A"), ("Î", "I"), ("Ș", "S"),
                 ("Ț", "T"), ("Ş", "S"), ("Ţ", "T")):
        s = s.replace(a, b)
    return s


def _wrap(s, width=34):
    lines = []
    cur = ""
    for w in s.split():
        if len(cur) + len(w) + (1 if cur else 0) <= width:
            cur = (cur + " " + w) if cur else w
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _title(text, color):
    Widgets.Label(text, 6, 4, 1.0, color, BLACK, Widgets.FONTS.DejaVu18)


def screen_idle():
    Widgets.fillScreen(BLACK)
    _title("JARVIS", GREEN)
    try:
        Widgets.Label("%d%%" % M5.Power.getBatteryLevel(), 205, 8, 1.0, GREY, BLACK,
                      Widgets.FONTS.DejaVu12)
    except Exception:
        pass
    Widgets.Label("Tine apasat butonul mare", 6, 52, 1.0, WHITE, BLACK, Widgets.FONTS.DejaVu12)
    Widgets.Label("ca sa vorbesti cu mine", 6, 70, 1.0, WHITE, BLACK, Widgets.FONTS.DejaVu12)
    Widgets.Label("KEY2 = repeta raspunsul", 6, 120, 1.0, GREY, BLACK, Widgets.FONTS.DejaVu9)


def screen_recording():
    Widgets.fillScreen(BLACK)
    _title("Te ascult!", RED)
    Widgets.Label("Da drumul cand ai terminat", 6, 52, 1.0, WHITE, BLACK, Widgets.FONTS.DejaVu12)


def screen_thinking(secs):
    Widgets.fillScreen(BLACK)
    _title("Ma gandesc...", YELLOW)
    Widgets.Label("Am auzit %d secunde" % secs, 6, 52, 1.0, WHITE, BLACK, Widgets.FONTS.DejaVu12)
    Widgets.Label("(poate dura un minut)", 6, 70, 1.0, GREY, BLACK, Widgets.FONTS.DejaVu12)


def screen_reply(text):
    Widgets.fillScreen(BLACK)
    lines = _wrap(_translit(text))
    y = 6
    for ln in lines[:9]:
        Widgets.Label(ln, 6, y, 1.0, WHITE, BLACK, Widgets.FONTS.DejaVu12)
        y += 15
    if len(lines) > 9:
        Widgets.Label("...", 6, y, 1.0, GREY, BLACK, Widgets.FONTS.DejaVu12)


def screen_error(msg):
    Widgets.fillScreen(DARKRED)
    _title("Ups!", WHITE)
    y = 52
    for ln in _wrap(_translit(msg), 30)[:4]:
        Widgets.Label(ln, 6, y, 1.0, WHITE, DARKRED, Widgets.FONTS.DejaVu12)
        y += 17
    _beep(200, 350)
    time.sleep_ms(1800)


# ---------- audio helpers ----------

def _beep(freq, ms):
    try:
        M5.Speaker.begin()
        M5.Speaker.setVolume(config.VOLUME)
        M5.Speaker.tone(freq, ms)
        time.sleep_ms(ms + 30)
        M5.Speaker.end()
    except Exception:
        pass


def record_while_held():
    screen_recording()
    _beep(880, 100)  # "talk now" cue (speaker must be done before mic starts)
    M5.Mic.begin()
    chunks = []
    max_chunks = config.MAX_RECORD_S * config.SAMPLE_RATE // CHUNK_SAMPLES
    bar_w = 228
    while True:
        M5.update()
        if not M5.BtnA.isPressed() or len(chunks) >= max_chunks:
            break
        buf = bytearray(CHUNK_SAMPLES * 2)
        M5.Mic.record(buf, config.SAMPLE_RATE, False)
        chunks.append(buf)
        while M5.Mic.isRecording() == 2:
            time.sleep_ms(5)
        M5.Lcd.fillRect(6, 100, bar_w * len(chunks) // max_chunks, 10, RED)
    while M5.Mic.isRecording():
        time.sleep_ms(10)
    M5.Mic.end()
    return b"".join(chunks) if chunks else b""


def play(audio):
    global _last_audio
    _last_audio = audio
    M5.Speaker.begin()
    M5.Speaker.setVolume(config.VOLUME)
    try:
        M5.Speaker.playWav(audio)
    except Exception:
        rate = 22050
        if len(audio) > 28:
            try:
                rate = struct.unpack_from("<I", audio, 24)[0]
            except Exception:
                pass
        M5.Speaker.playRaw(audio[44:], rate, False)
    while M5.Speaker.isPlaying():
        M5.update()
        if M5.BtnA.isPressed():  # interrupt playback to ask something new
            M5.Speaker.stop()
            break
        time.sleep_ms(50)
    M5.Speaker.end()


# ---------- network ----------

def wifi_connect(show=True):
    global _sta
    if _sta is None:
        _sta = network.WLAN(network.STA_IF)
        _sta.active(True)
    if _sta.isconnected():
        return True
    if config.WIFI_SSID == "CHANGEME":
        screen_error("Lipseste parola de WiFi (config.py)")
        return False
    if show:
        Widgets.fillScreen(BLACK)
        _title("Ma conectez la WiFi...", YELLOW)
    _sta.connect(config.WIFI_SSID, config.WIFI_PSK)
    for _ in range(40):  # 20 s
        if _sta.isconnected():
            return True
        time.sleep_ms(500)
    return False


def _wav(audio):
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(audio), b"WAVE",
                      b"fmt ", 16, 1, 1, config.SAMPLE_RATE, config.SAMPLE_RATE * 2,
                      2, 16, b"data", len(audio))
    return hdr + audio


def _hget(headers, name):
    name = name.lower()
    for k in headers:
        if k.lower() == name:
            return headers[k]
    return ""


def _rfc2047_decode(s):
    if not s:
        return ""
    out = []
    for tok in s.split():
        if tok.startswith("=?") and tok.endswith("?="):
            try:
                parts = tok.split("?")  # ['=', charset, 'b', payload, '=']
                if parts[2].lower() == "b":
                    out.append(binascii.a2b_base64(parts[3]).decode("utf-8"))
                    continue
            except Exception:
                pass
        out.append(tok)
    return " ".join(out)


def post_turn(wav):
    resp = requests2.post(config.SERVER + config.VOICE_PATH, data=wav,
                          headers={"Content-Type": "audio/wav"},
                          timeout=config.HTTP_TIMEOUT_S)
    try:
        if resp.status_code == 422:
            raise ValueError("not understood")
        if resp.status_code != 200:
            raise OSError("HTTP %d" % resp.status_code)
        reply = _rfc2047_decode(_hget(resp.headers, "X-Reply-Text"))
        audio = resp.content
    finally:
        resp.close()
    return reply, audio


def tv_wake():
    """Long-press KEY2 -> ask the host to light the 0lympus TV (tv-trigger :8099).
    No-op unless config.TV_ON_URL is set (admin stick only). Never raises: a failure
    here must not take down the voice loop."""
    url = getattr(config, "TV_ON_URL", None)
    if not url:
        return
    Widgets.fillScreen(BLACK)
    _title("Pornesc TV...", YELLOW)
    try:
        if not wifi_connect(show=False):
            _title("TV: fara WiFi", RED)
        else:
            resp = requests2.get(url, timeout=25)
            try:
                ok = resp.status_code == 200
                body = resp.content.decode().strip() if ok else ("HTTP %d" % resp.status_code)
            finally:
                resp.close()
            _title("TV pornit" if ok else "TV: " + body[:14], GREEN if ok else RED)
    except Exception:
        _title("TV: eroare", RED)
    time.sleep_ms(1200)
    screen_idle()


# ---------- turn logic ----------

def turn():
    global _last_audio
    _last_audio = None  # free PSRAM before the next big buffers
    gc.collect()
    audio = record_while_held()
    secs = len(audio) // (config.SAMPLE_RATE * 2)
    if len(audio) < int(config.MIN_RECORD_S * config.SAMPLE_RATE) * 2:
        screen_idle()
        return
    screen_thinking(secs)
    if not wifi_connect(show=False):
        screen_error("Nu am WiFi. Mai incearca!")
        screen_idle()
        return
    try:
        gc.collect()
        reply, reply_audio = post_turn(_wav(audio))
        del audio
        gc.collect()
        screen_reply(reply if reply else "(fara raspuns)")
        if reply_audio and len(reply_audio) > 44:
            play(reply_audio)
    except ValueError:
        screen_error("Nu te-am auzit bine. Mai incearca!")
    except Exception as e:
        print("turn failed:", e)
        screen_error("Serverul nu raspunde. Mai incearca!")
    screen_idle()


def replay():
    if _last_audio:
        play(_last_audio)


# ---------- main ----------

def _wake():
    global _last_activity, _dimmed
    _last_activity = time.ticks_ms()
    if _dimmed:
        Widgets.setBrightness(config.FULL_BRIGHT)
        _dimmed = False


def main():
    global _last_activity, _dimmed
    M5.begin()
    Widgets.setRotation(1)
    Widgets.setBrightness(config.FULL_BRIGHT)
    Widgets.fillScreen(BLACK)
    _title("voice-stick v1", GREEN)
    Widgets.Label("KEY2 acum = iesire", 6, 52, 1.0, GREY, BLACK, Widgets.FONTS.DejaVu12)
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < 2000:  # anti-brick grace window
        M5.update()
        if M5.BtnB.isPressed():
            Widgets.Label("Iesire la REPL", 6, 70, 1.0, RED, BLACK, Widgets.FONTS.DejaVu12)
            sys.exit()
        time.sleep_ms(20)

    if not wifi_connect():
        screen_error("Nu ma pot conecta la WiFi")
    screen_idle()
    _last_activity = time.ticks_ms()

    # KEY2: short press = replay. On the admin stick (TV_ON_URL set) a long hold
    # (>=1.2s) instead wakes the 0lympus TV. Without TV_ON_URL the original
    # replay-on-press behaviour is kept unchanged (kids stick).
    TV_URL = getattr(config, "TV_ON_URL", None)
    btnb_t0 = 0
    btnb_fired = False
    while True:
        M5.update()
        if M5.BtnA.isPressed():
            _wake()
            turn()
            _last_activity = time.ticks_ms()
        elif TV_URL and M5.BtnB.wasPressed():
            btnb_t0 = time.ticks_ms()
            btnb_fired = False
        elif TV_URL and M5.BtnB.isPressed():
            if (not btnb_fired) and btnb_t0 and time.ticks_diff(time.ticks_ms(), btnb_t0) >= 1200:
                btnb_fired = True
                _wake()
                tv_wake()
                _last_activity = time.ticks_ms()
        elif TV_URL and M5.BtnB.wasReleased():
            if not btnb_fired:
                _wake()
                replay()
            btnb_t0 = 0
            _last_activity = time.ticks_ms()
        elif (not TV_URL) and M5.BtnB.wasPressed():
            _wake()
            replay()
            _last_activity = time.ticks_ms()
        elif not _dimmed and time.ticks_diff(time.ticks_ms(), _last_activity) > config.IDLE_DIM_S * 1000:
            Widgets.setBrightness(config.DIM_LEVEL)
            _dimmed = True
        time.sleep_ms(20)


main()
