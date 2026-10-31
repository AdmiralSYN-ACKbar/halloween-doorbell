#!/usr/bin/env python3
"""
Halloween costume doorbell.

Flow:  G4 Doorbell Pro spots a person (UniFi Protect event feed, watched from this PC)
       -> snapshot from the doorbell (Protect Integration API)
       -> Claude (or OpenAI) identifies the costume(s); nobody dressed up = stay quiet
       -> "I see a ...!" in an ElevenLabs voice (Ilinca and Malyx take turns), played on the Sonos
          from this PC (Hubitat's built-in voice if that fails), and the doorbell screen shows the
          first costume

Setup (Windows PowerShell):
  pip install anthropic openai requests websocket-client soco
  Put keys and settings in a .env file next to this script (KEY=value per line,
  e.g. ANTHROPIC_API_KEY=sk-ant-... and OPENAI_API_KEY=sk-...), or set them as
  environment variables, then:
  python costume_doorbell.py

  Identify a saved photo (no camera, no speaker):  python costume_doorbell.py --test-image sample.jpg
  ...or several photos as one visit's burst:       python costume_doorbell.py --test-image a.jpg b.jpg c.jpg
  Pretend someone walked up:          curl.exe http://localhost:8088/ring
  Pretend the button was pressed:     curl.exe http://localhost:8088/press
  Test without speaking (prints only): $env:DRY_RUN = "1"; python costume_doorbell.py
  Use OpenAI instead of Claude:         $env:BACKEND = "openai"; python costume_doorbell.py
  Use the local GPU instead of Claude:  $env:BACKEND = "ollama"; python costume_doorbell.py
"""

import argparse
import base64
import html
import itertools
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Read KEY=value lines into the environment; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_dotenv(Path(__file__).with_name(".env"))
sys.stdout.reconfigure(encoding="utf-8")  # the 🔊 log lines break a cp1252 console or log file

import ssl  # noqa: E402

import anthropic  # noqa: E402  (after .env so the clients see their API keys)
import openai  # noqa: E402
import requests  # noqa: E402
import soco  # noqa: E402
import urllib3  # noqa: E402
import websocket  # noqa: E402  (pip install websocket-client)
from soco.snapshot import Snapshot  # noqa: E402

urllib3.disable_warnings()  # UniFi consoles use a self-signed cert on the LAN
soco.config.REQUEST_TIMEOUT = 5  # a Roam with a flat battery shouldn't hold up the other speaker

# ---------------------------------------------------------------- CONFIG ----
BACKEND = os.getenv("BACKEND", "claude")  # "claude", "openai" or "ollama" (local GPU)
# Tried if BACKEND errors or times out: by default the other cloud provider (skipped if its
# API key isn't set). "none" = stay quiet instead.
FALLBACK = os.getenv("FALLBACK", {"claude": "openai", "openai": "claude"}.get(BACKEND, "none"))
MODEL_TIMEOUT_SEC = float(os.getenv("MODEL_TIMEOUT_SEC", "10"))  # per attempt; kids won't wait long

# API keys come from ANTHROPIC_API_KEY / OPENAI_API_KEY (environment or .env); never put them here.
MODEL = os.getenv("MODEL", "claude-opus-5-5")  # owner's pick after the night samples
# Thinking depth. On real night samples Opus at "low" matched Sonnet "high" for accuracy,
# answered in ~2.4s (max 2.6s), and never invented costumes from printed clothes.
EFFORT = os.getenv("EFFORT", "low")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6.1-sol")  # OpenAI's "balanced" tier, closest to Opus
OPENAI_EFFORT = os.getenv("OPENAI_EFFORT", "low")

# Local option: Ollama on the 3090 box (qwen3.6:27b reads images and fits in 24GB)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:27b")

# UniFi Protect: Settings -> Control Plane -> Integrations -> create API key
PROTECT_HOST = os.getenv("PROTECT_HOST", "192.168.1.1")  # your UDM / UNVR / Cloud Key
PROTECT_API_KEY = os.getenv("PROTECT_API_KEY", "your-protect-api-key")
DOORBELL_CAMERA_ID = os.getenv("DOORBELL_CAMERA_ID", "")  # leave blank to auto-find
LCD_SECONDS = float(os.getenv("LCD_SECONDS", "15"))  # doorbell screen text; 0 = leave the screen alone
LCD_MAX = 40  # Protect rejects longer lcdMessage text
LCD_HOME_FILE = Path(__file__).with_name("lcd_home.json")  # what the screen goes back to

# Hubitat: install the Maker API app, add the Sonos device to it
HUBITAT_HOST = os.getenv("HUBITAT_HOST", "192.168.1.50")
MAKER_APP_ID = os.getenv("MAKER_APP_ID", "123")
MAKER_TOKEN = os.getenv("MAKER_TOKEN", "your-maker-api-token")
SONOS_DEVICE_ID = os.getenv("SONOS_DEVICE_ID", "456")  # one ID, or several: "355,365"
SPEAK_VOLUME = os.getenv("SPEAK_VOLUME", "")  # "60" for all, "70,40" per speaker; blank = current volume
# Hubitat TTS voice, used only when ElevenLabs fails. Ruth (Amazon's newer, natural-sounding US voice) beat the
# older ones (Matthew, Joanna, Salli...) when auditioned; blank = the hub's default
SPEAK_VOICE = os.getenv("SPEAK_VOICE", "Ruth")

# ElevenLabs voices take turns, one whole announcement each; the PC serves the clip and the Sonos play it.
# No ELEVENLABS_API_KEY (or a failure) = Hubitat's built-in voice above.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICES = {  # name: (voice id, model, how "I see ...!" is delivered)
    "ilinca": ("FcZStbCG9g9QxDlJioSD", "eleven_multilingual_v2", "I see {}!"),  # Ilinca, Vampire Countess
    # Malyx, demon. v3 can also do "[whispers] I seeee... [evil laugh] {}!", but that's a ~6s clip
    "malyx": ("vfaqCOvlrKi4Zp7C2IAm", "eleven_v3", "I see {}!"),
}
VOICES = os.getenv("VOICES", "ilinca,malyx")  # the order they take turns in
TTS_TIMEOUT_SEC = float(os.getenv("TTS_TIMEOUT_SEC", "10"))
SONOS_IPS = os.getenv("SONOS_IPS", "192.168.1.60")  # one or several: "a,b"; same order as SPEAK_VOLUME
AUDIO_PORT = int(os.getenv("AUDIO_PORT", "8089"))  # the Sonos fetch clips from this PC here (firewall rule)
# Played on the Sonos when the doorbell button is pressed; blank = off. The doorbell itself plays Ilinca's
# laugh-ilinca-c (4.9s) as its Protect voice recording, so Malyx waits RING_DELAY_SEC to answer her (the
# Sonos adds ~1s of its own before it starts).
RING_SOUND = os.getenv("RING_SOUND", str(Path(__file__).with_name("voice-samples") / "laugh-malyx-b.mp3"))
RING_DELAY_SEC = float(os.getenv("RING_DELAY_SEC", "4"))

LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")  # /ring is only for manual tests on this PC
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8088"))
SNAPSHOT_DELAY_SEC = float(os.getenv("SNAPSHOT_DELAY_SEC", "0"))  # first frame right on detection
# Frames per visit, sent together so the model can use whichever shows each costume best
# (1 = a single snapshot). At 0.75s a walking kid moves about a step between frames; Protect
# returns a fresh frame each time (checked at 0.5s and 0.75s; each fetch takes ~0.15-0.3s).
BURST_FRAMES = max(1, int(os.getenv("BURST_FRAMES", "3")))
BURST_GAP_SEC = float(os.getenv("BURST_GAP_SEC", "0.75"))
RECHECK_SEC = float(os.getenv("RECHECK_SEC", "10"))  # while anyone is on the porch, look again this often
REPEAT_WINDOW_SEC = float(os.getenv("REPEAT_WINDOW_SEC", "120"))  # a costume seen this recently isn't re-announced
PRESENCE_MAX_SEC = float(os.getenv("PRESENCE_MAX_SEC", "900"))  # forget a detection Protect never closed
FALLBACK_LINE = os.getenv("FALLBACK_LINE", "")  # said when something breaks; blank = stay quiet
DRY_RUN = os.getenv("DRY_RUN") == "1"
# Visit log, OFF by default because the frames show visitors. LOG_VISITS picks what's kept
# under LOG_DIR, listed on LOG_DIR/index.html: "text" (each check's result), "images" (the frames),
# "audio" (the spoken clips), a mix like "text,audio", or "all" (also "1"). Images or audio include the text.
_LOG_PARTS = {p.strip().lower() for p in os.getenv("LOG_VISITS", "").split(",")} - {"", "0", "off"}
if _LOG_PARTS & {"1", "all"}:
    _LOG_PARTS = {"text", "images", "audio"}
LOG_VISITS = bool(_LOG_PARTS)
LOG_IMAGES = "images" in _LOG_PARTS
LOG_AUDIO = "audio" in _LOG_PARTS
LOG_DIR = Path(os.getenv("LOG_DIR", str(Path(__file__).with_name("visit-log"))))
# -----------------------------------------------------------------------------

PROMPT = """This is a doorbell camera photo on Halloween night.
Identify the Halloween costumes being worn by the people at the door.

Reply with ONLY a short phrase that completes the sentence "I see ___!", e.g.:
  a witch
  Princess Anna and a dinosaur
  a ghost, Spider-Man and friends
  a pirate and a trick or treater

Rules:
- When a costume is a recognizable character, use the character's name
  (e.g. "Elsa", "Spider-Man"); otherwise name the costume ("a witch").
- Someone in a costume you can't identify is "a trick or treater".
- Ignore anyone who isn't dressed up for Halloween.
- Name at most 2 costumes. If 3 or more people are dressed up, name the 2
  most obvious and add "and friends".
- Use the costume or character name only. Never describe the person's body,
  face, age, or anything other than the costume.
- If nobody is dressed up for Halloween, or no one is visible,
  reply with exactly: NONE"""


def build_prompt(frames: int) -> str:
    if frames == 1:
        return PROMPT
    return (f"There are {frames} photos below, taken about {BURST_GAP_SEC:g} seconds apart by the same "
            "doorbell camera as the same visitors walk up. Treat them as one moment: answer once for "
            "everyone, using whichever photo shows each costume best.\n\n" + PROMPT)


PROTECT =f"https://{PROTECT_HOST}/proxy/protect/integration/v1"
HEADERS = {"X-API-KEY": PROTECT_API_KEY, "Accept": "application/json"}

# No SDK retries: a retry after a timeout comes too late for the visitor at the door.
claude_client = (anthropic.Anthropic(timeout=MODEL_TIMEOUT_SEC, max_retries=0)
                 if os.getenv("ANTHROPIC_API_KEY") else None)
openai_client = (openai.OpenAI(timeout=MODEL_TIMEOUT_SEC, max_retries=0)
                 if os.getenv("OPENAI_API_KEY") else None)
_lock = threading.Lock()  # guards _present and _announced
_present: dict[str, float] = {}  # Protect person detections still open -> when we last heard about each
_announced: dict[str, float] = {}  # costume (normalized) -> when it was last seen after being announced
_wake = threading.Event()  # set by a new arrival (or /ring) to start checking
_lcd_lock = threading.Lock()
_lcd_timer: threading.Timer | None = None
_log_lock = threading.Lock()
_turn = itertools.count()  # whose turn it is among VOICES
_clips_lock = threading.Lock()
_clips: dict[str, bytes] = {}  # the last few spoken clips, by URL name
_clip_host = ""  # the PC's LAN address once the clip server is up
_play_lock = threading.Lock()  # one clip on the Sonos at a time
_ring_lock = threading.Lock()  # held while the ring sound plays; presses meanwhile are ignored
_event_types: set[str] = set()  # Protect event types seen so far (printed once each)


def find_doorbell_id() -> str:
    """Return the first connected camera whose model looks like a doorbell."""
    r = requests.get(f"{PROTECT}/cameras", headers=HEADERS, verify=False, timeout=5)
    r.raise_for_status()
    for cam in r.json():
        name = f"{cam.get('modelKey', '')} {cam.get('name', '')} {cam.get('type', '')}".lower()
        # Old doorbells can linger in Protect as DISCONNECTED; skip them.
        if "doorbell" in name and cam.get("state") == "CONNECTED":
            print(f"Using camera '{cam.get('name')}' ({cam['id']})")
            return cam["id"]
    raise RuntimeError("No doorbell camera found; set DOORBELL_CAMERA_ID manually.")


def get_snapshot(camera_id: str) -> bytes:
    r = requests.get(
        f"{PROTECT}/cameras/{camera_id}/snapshot",
        params={"highQuality": "true"},
        headers={"X-API-KEY": PROTECT_API_KEY},
        verify=False,
        timeout=5,
    )
    r.raise_for_status()
    return r.content


def get_burst(camera_id: str) -> list[bytes]:
    """BURST_FRAMES snapshots, BURST_GAP_SEC apart, after SNAPSHOT_DELAY_SEC. Kept in memory only."""
    first = time.time() + SNAPSHOT_DELAY_SEC
    frames = []
    for i in range(BURST_FRAMES):
        wait = first + i * BURST_GAP_SEC - time.time()  # fixed schedule, so slow fetches don't drift
        if wait > 0:
            time.sleep(wait)
        try:
            frames.append(get_snapshot(camera_id))
        except Exception as e:  # one missed frame shouldn't cost the visit
            print(f"   frame {i + 1} failed: {e!r}")
    if not frames:
        raise RuntimeError("No snapshots from the doorbell")
    return frames


def image_type(b64: str) -> str:
    # Protect snapshots are JPEG; --test-image may also be given a PNG
    return "image/png" if b64.startswith("iVBOR") else "image/jpeg"


def ask_claude(images: list[str]) -> str:
    content = []
    for i, b64 in enumerate(images):
        if len(images) > 1:
            content.append({"type": "text", "text": f"Photo {i + 1}:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": image_type(b64), "data": b64}})
    content.append({"type": "text", "text": build_prompt(len(images))})
    # Claude thinks before answering, and thinking counts against max_tokens,
    # so leave plenty of room; the answer itself is a few words.
    msg = claude_client.messages.create(
        model=MODEL,
        max_tokens=1000,
        output_config={"effort": EFFORT},
        messages=[{"role": "user", "content": content}],
    )
    if msg.stop_reason != "end_turn":  # e.g. "max_tokens" or "refusal": no usable answer
        print(f"Claude stopped early: {msg.stop_reason}")
        return ""
    return "".join(b.text for b in msg.content if b.type == "text")


def ask_openai(images: list[str]) -> str:
    content = []
    for i, b64 in enumerate(images):
        if len(images) > 1:
            content.append({"type": "input_text", "text": f"Photo {i + 1}:"})
        content.append({"type": "input_image", "detail": "high", "image_url": f"data:{image_type(b64)};base64,{b64}"})
    content.append({"type": "input_text", "text": build_prompt(len(images))})
    # Reasoning tokens count against max_output_tokens, as thinking does for Claude.
    resp = openai_client.responses.create(
        model=OPENAI_MODEL,
        max_output_tokens=1000,
        reasoning={"effort": OPENAI_EFFORT},
        input=[{"role": "user", "content": content}],
    )
    if resp.status != "completed":  # e.g. "incomplete" (ran out of tokens): no usable answer
        print(f"OpenAI stopped early: {resp.status}")
        return ""
    return resp.output_text


def ask_ollama(images: list[str]) -> str:
    r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=60, json={
        "model": OLLAMA_MODEL,
        "stream": False,
        "think": False,  # qwen3.6 thinks by default; a few-word answer doesn't need it
        "keep_alive": "24h",  # keep the model loaded in VRAM all night
        "options": {"temperature": 0, "num_predict": 100},
        "messages": [{"role": "user", "content": build_prompt(len(images)), "images": images}],
    })
    r.raise_for_status()
    return r.json()["message"]["content"]


ASK = {"claude": ask_claude, "openai": ask_openai, "ollama": ask_ollama}


def backend_label(backend: str) -> str:
    return {"claude": f"Claude {MODEL}, effort {EFFORT}",
            "openai": f"OpenAI {OPENAI_MODEL}, effort {OPENAI_EFFORT}",
            "ollama": f"Ollama {OLLAMA_MODEL}"}.get(backend, backend)


def fallback_ready() -> bool:
    has_key = {"claude": claude_client, "openai": openai_client, "ollama": True}.get(FALLBACK)
    return FALLBACK != BACKEND and bool(has_key)


def ask_model(images: list[str], info: dict) -> str:
    """Ask BACKEND, or FALLBACK if it fails; notes which one answered in info["backend"]."""
    try:
        info["backend"] = backend_label(BACKEND)
        return ASK[BACKEND](images)
    except Exception as e:  # outage, overload, timeout, bad model id
        if not fallback_ready():
            raise
        print(f"{BACKEND} failed ({e.__class__.__name__}); trying {backend_label(FALLBACK)}")
        info["backend"] = f"{backend_label(FALLBACK)} (fallback after {e.__class__.__name__})"
        return ASK[FALLBACK](images)


def identify_costumes(frames: list[bytes], info: dict | None = None) -> str | None:
    """The costume phrase for one visit's frames, or None when nobody is dressed up (or the answer is unusable).
    Fills info (if given) with the backend used and its raw answer, for the visit log."""
    info = {} if info is None else info
    text = ask_model([base64.b64encode(f).decode() for f in frames], info).strip()
    info["raw"] = text
    text = text.splitlines()[0].strip(" .!\"'") if text else ""
    if text.lower().startswith("i see "):  # models sometimes repeat the sentence start
        text = text[6:].strip()
    if not text or text.upper() == "NONE" or len(text) > 80:
        return None
    return text


def maker_url(path: str) -> str:
    return f"http://{HUBITAT_HOST}/apps/api/{MAKER_APP_ID}/devices/{path}"


def current_volume(device: str) -> str:
    r = requests.get(maker_url(device), params={"access_token": MAKER_TOKEN}, timeout=5)
    r.raise_for_status()
    attrs = {a["name"]: a.get("currentValue") for a in r.json().get("attributes", [])}
    return str(attrs.get("volume") or 50)


def voice_names() -> list[str]:
    return [v.strip() for v in VOICES.split(",") if v.strip() in ELEVEN_VOICES] if ELEVENLABS_API_KEY else []


def eleven_clip(line: str, name: str) -> bytes:
    """MP3 of the line in an ElevenLabs voice; "I see X!" gets that voice's delivery."""
    voice_id, model, style = ELEVEN_VOICES[name]
    m = re.fullmatch(r"I see (.+)!", line)
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", timeout=TTS_TIMEOUT_SEC,
                      headers={"xi-api-key": ELEVENLABS_API_KEY}, params={"output_format": "mp3_44100_128"},
                      json={"text": style.format(m[1]) if m else line, "model_id": model})
    r.raise_for_status()
    return r.content


class ClipServer(BaseHTTPRequestHandler):
    """Hands the Sonos the latest clips, by unguessable name, from memory."""

    def do_GET(self):
        with _clips_lock:
            clip = _clips.get(self.path.rsplit("/", 1)[-1])
        if not clip:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(clip)))
        self.end_headers()
        self.wfile.write(clip)

    def log_message(self, *args):
        pass


def clip_url(clip: bytes) -> str:
    """Serve the clip on the LAN (starting the server on first use) and return its URL."""
    global _clip_host
    with _clips_lock:
        if not _clip_host:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:  # the PC's address facing the Sonos
                s.connect((SONOS_IPS.split(",")[0].strip(), 1400))
                host = s.getsockname()[0]
            server = ThreadingHTTPServer((host, AUDIO_PORT), ClipServer)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            _clip_host = host
        name = f"{secrets.token_hex(8)}.mp3"
        _clips[name] = clip
        while len(_clips) > 5:
            del _clips[next(iter(_clips))]
    return f"http://{_clip_host}:{AUDIO_PORT}/voice/{name}"


def play_on_sonos(clip: bytes) -> None:
    """Play the clip on every Sonos at once, then put back whatever each was doing."""
    url = clip_url(clip)
    seconds = len(clip) * 8 / 128_000  # mp3_44100_128
    volumes = [v.strip() for v in SPEAK_VOLUME.split(",") if v.strip()]
    errors = []

    def play_one(i: int, ip: str) -> None:
        try:
            speaker = soco.SoCo(ip)
            snap = Snapshot(speaker)
            snap.snapshot()
            if volumes:
                speaker.volume = int(volumes[min(i, len(volumes) - 1)])
            speaker.play_uri(url, title="Costume doorbell")
            time.sleep(seconds + 1)
            snap.restore()
        except Exception as e:
            errors.append(f"Sonos {ip}: {e!r}")

    ips = [ip.strip() for ip in SONOS_IPS.split(",") if ip.strip()]
    threads = [threading.Thread(target=play_one, args=(i, ip)) for i, ip in enumerate(ips)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors and len(errors) == len(ips):
        raise RuntimeError("; ".join(errors))  # nobody heard it
    for err in errors:
        print(f"Play failed on {err}")


def speak(line: str, voice: str = "", info: dict | None = None) -> None:
    """Say the line on every Sonos: in the next ElevenLabs voice (or the one named), else Hubitat's voice.

    With info, records which voice spoke and keeps the ElevenLabs clip for the visit log."""
    info = {} if info is None else info
    names = voice_names()
    name = voice if voice in ELEVEN_VOICES else (names[next(_turn) % len(names)] if names and not voice else "")
    print(f"🔊 {line}" + (f"  ({name})" if name else ""))
    if DRY_RUN:
        return
    if name:
        try:
            start = time.time()
            info["clip"] = eleven_clip(line, name)
            info["voice"] = name
            print(f"   voice ready in {time.time() - start:.1f}s")
            with _play_lock:
                play_on_sonos(info["clip"])
            return
        except Exception as e:
            print(f"ElevenLabs voice failed ({e!r}); using Hubitat's voice")
            info["voice_error"] = repr(e)
    info["voice"] = "Hubitat"
    speak_hubitat(line, voice if voice and not name else SPEAK_VOICE)


def speak_hubitat(line: str, voice: str) -> None:
    """Have every configured Sonos say the line at the same time, in Hubitat's TTS voice."""
    devices = [d.strip() for d in SONOS_DEVICE_ID.split(",") if d.strip()]
    volumes = [v.strip() for v in SPEAK_VOLUME.split(",") if v.strip()]
    errors = []

    def speak_one(i: int, device: str) -> None:
        # Maker API splits command arguments on commas, so the text can't contain any.
        # speak(text, volume, voice) is positional: a voice needs a volume before it.
        args = [urllib.parse.quote(line.replace(",", ""), safe="")]
        if volumes or voice:
            args.append(volumes[min(i, len(volumes) - 1)] if volumes else current_volume(device))
        if voice:
            args.append(urllib.parse.quote(voice, safe=""))
        url = maker_url(f"{device}/speak/{','.join(args)}")
        try:
            requests.get(url, params={"access_token": MAKER_TOKEN}, timeout=5).raise_for_status()
        except Exception as e:
            errors.append(f"Sonos {device}: {e!r}")

    threads = [threading.Thread(target=speak_one, args=(i, d)) for i, d in enumerate(devices)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors and len(errors) == len(devices):
        raise RuntimeError("; ".join(errors))  # nobody heard it
    for err in errors:
        print(f"Speak failed on {err}")


def screen_line(costume: str) -> str:
    """'Princess Anna and a dinosaur' -> 'I SEE PRINCESS ANNA!', fitted to the doorbell screen."""
    first = re.split(r",\s*|\s+and\s+", costume)[0].strip()
    return f"I SEE {first[:LCD_MAX - 7]}!".upper()


def is_our_message(msg: dict) -> bool:
    return msg.get("type") == "CUSTOM_MESSAGE" and (msg.get("text") or "").startswith("I SEE ")


def get_lcd() -> dict:
    r = requests.get(f"{PROTECT}/cameras/{DOORBELL_CAMERA_ID}", headers=HEADERS, verify=False, timeout=5)
    r.raise_for_status()
    return r.json().get("lcdMessage") or {}


def set_lcd(msg: dict) -> None:
    r = requests.patch(f"{PROTECT}/cameras/{DOORBELL_CAMERA_ID}", headers=HEADERS,
                       json={"lcdMessage": msg}, verify=False, timeout=5)
    r.raise_for_status()


def remember_home_screen() -> None:
    """Save what the screen shows now (e.g. your Halloween image), or put it back if a crash left our text up."""
    current = get_lcd()
    if is_our_message(current):
        restore_screen()
    elif current:
        LCD_HOME_FILE.write_text(json.dumps(current))


def restore_screen() -> None:
    with _lcd_lock:
        if LCD_HOME_FILE.exists():
            try:
                set_lcd(json.loads(LCD_HOME_FILE.read_text()))
            except Exception as e:
                print(f"Couldn't restore the doorbell screen: {e!r}")


def show_on_doorbell(costume: str) -> None:
    """Put the first costume on the doorbell screen for LCD_SECONDS, then restore the home screen."""
    global _lcd_timer
    text = screen_line(costume)
    print(f"📺 {text}")
    if DRY_RUN or not LCD_SECONDS:
        return
    if not (_lcd_timer and _lcd_timer.is_alive()):  # our text isn't up already
        remember_home_screen()  # picks up any image change made in Protect since the last ring
    with _lcd_lock:
        set_lcd({"type": "CUSTOM_MESSAGE", "text": text, "resetAt": None})
        if _lcd_timer:
            _lcd_timer.cancel()
        _lcd_timer = threading.Timer(LCD_SECONDS, restore_screen)
        _lcd_timer.daemon = True
        _lcd_timer.start()


def split_costumes(phrase: str) -> tuple[list[str], bool]:
    """'a witch, Spider-Man and friends' -> (['a witch', 'Spider-Man'], True)."""
    parts = [p.strip() for p in re.split(r",\s*|\s+and\s+", phrase) if p.strip()]
    return [p for p in parts if p.lower() != "friends"], any(p.lower() == "friends" for p in parts)


def costume_key(costume: str) -> str:
    return re.sub(r"^(a|an|the)\s+", "", costume.lower()).strip()


def announcement(costumes: list[str], friends: bool) -> str:
    """At most 2 costumes, plus "and friends" for anyone else."""
    names, more = costumes[:2], friends or len(costumes) > 2
    if len(names) == 1:
        return f"I see {names[0]}{' and friends' if more else ''}!"
    return f"I see {names[0]}, {names[1]} and friends!" if more else f"I see {names[0]} and {names[1]}!"


def check_porch(source: str) -> None:
    """One look at the porch: burst -> model -> announce only costumes not seen in the last REPEAT_WINDOW_SEC."""
    start = time.time()
    print(f"{time.strftime('%H:%M:%S')} {source}")
    frames: list[bytes] = []  # images stay in memory only, unless LOG_VISITS includes images
    info: dict = {"time": start, "source": source}
    try:
        frames = get_burst(DOORBELL_CAMERA_ID)
        captured = time.time()
        costume = identify_costumes(frames, info)
        info.update(costume=costume, capture_s=round(captured - start, 1), model_s=round(time.time() - captured, 1))
        print(f"   {len(frames)} frame(s) in {info['capture_s']}s, model {info['model_s']}s")
        if not costume:
            print("   No costume; staying quiet")
            return
        costumes, friends = split_costumes(costume)
        now = time.time()
        with _lock:
            new = [c for c in costumes if now - _announced.get(costume_key(c), 0) > REPEAT_WINDOW_SEC]
            for c in costumes:  # "seen" restarts the repeat window, so a lingering group stays quiet
                _announced[costume_key(c)] = now
        if not new:
            info["note"] = f"Nothing new (already announced: {costume})"
            print(f"   {info['note']}; staying quiet")
            return
        line = announcement(new, friends and len(new) == len(costumes))  # friends only when the whole group is new
        try:
            show_on_doorbell(new[0])
            info["screen"] = screen_line(new[0])
        except Exception as e:  # the screen is a bonus; never let it stop the speech
            print(f"Doorbell screen failed: {e!r}")
        speak(line, info=info)
        info["spoken"] = line
    except Exception as e:  # nobody is waiting on an answer, so stay quiet unless told otherwise
        print(f"Error: {e!r}")
        info["error"] = repr(e)
        if FALLBACK_LINE:
            try:
                speak(FALLBACK_LINE, info=info)
                info["spoken"] = FALLBACK_LINE
            except Exception as e2:
                print(f"Couldn't speak fallback either: {e2!r}")
    finally:
        info["total_s"] = round(time.time() - start, 1)
        print(f"   ({info['total_s']}s)")
        if LOG_VISITS:
            log_visit(info, frames)
        frames.clear()


def someone_present() -> bool:
    with _lock:
        stale = [k for k, seen in _present.items() if time.time() - seen > PRESENCE_MAX_SEC]
        for k in stale:  # Protect never closed it (e.g. a missed message); stop watching for it
            del _present[k]
        return bool(_present)


def porch_watcher() -> None:
    """Check on every new arrival, then every RECHECK_SEC for as long as anyone is still on the porch.

    One check at a time, so it never talks over itself; arrivals during a check are picked up by the next one.
    """
    while True:
        _wake.wait()
        source = "Person at the door"
        while True:
            _wake.clear()
            started = time.time()
            check_porch(source)
            if not someone_present() and not _wake.is_set():
                break  # porch is empty
            time.sleep(max(0.0, started + RECHECK_SEC - time.time()))
            source = "New arrival" if _wake.is_set() else "Re-check (someone still at the door)"


# ------------------------------------------------------------ visit log ----
def log_visit(info: dict, frames: list[bytes]) -> None:
    """Save a visit's record (plus frames and spoken clip, if those are logged) under LOG_DIR, then rebuild
    LOG_DIR/index.html."""
    clip = info.pop("clip", None)  # always out of the record: it's bytes
    clip = clip if LOG_AUDIO else None
    frames = frames if LOG_IMAGES else []
    try:
        with _log_lock:
            when = time.localtime(info["time"])
            day = LOG_DIR / time.strftime("%Y-%m-%d", when)
            day.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%H%M%S", when)
            info["frames"] = []
            for i, frame in enumerate(frames):
                path = day / f"{stamp}-{i + 1}.jpg"
                path.write_bytes(frame)
                info["frames"].append(f"{day.name}/{path.name}")
            if clip:
                path = day / f"{stamp}-{info.get('voice', 'voice')}.mp3"
                path.write_bytes(clip)
                info["audio"] = f"{day.name}/{path.name}"
            with (LOG_DIR / "visits.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(info) + "\n")
            build_log_page()
    except Exception as e:  # the log is a bonus; never let it break a visit
        print(f"Visit log failed: {e!r}")


LOG_STYLE = """
:root { --bg: #f4f3f8; --card: #fff; --ink: #1d1a2b; --muted: #625e75; --line: #dddae7;
  --said: #1f7a4a; --said-bg: #dcf1e4; --quiet: #625e75; --quiet-bg: #ecebf2;
  --skip: #9a6400; --skip-bg: #fbecd0; --err: #b3261e; --err-bg: #f9dedb; color-scheme: light; }
@media (prefers-color-scheme: dark) { :root { --bg: #15131f; --card: #1e1b2b; --ink: #ece9f5;
  --muted: #a39fb8; --line: #322e44; --said: #6fd39c; --said-bg: #173326; --quiet: #a39fb8;
  --quiet-bg: #26233a; --skip: #f0c060; --skip-bg: #3a2f14; --err: #ff8a80; --err-bg: #3d1c1a;
  color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
main { max-width: 1000px; margin: 0 auto; padding: 28px 16px 60px; display: grid; gap: 14px; }
h1 { font-size: 28px; margin: 0; }
.sub { color: var(--muted); margin: 0; }
.counts { display: flex; flex-wrap: wrap; gap: 8px; }
.pill { border-radius: 999px; padding: 3px 12px; font-weight: 600; font-size: 13px; }
.said { color: var(--said); background: var(--said-bg); }
.quiet { color: var(--quiet); background: var(--quiet-bg); }
.skip { color: var(--skip); background: var(--skip-bg); }
.err { color: var(--err); background: var(--err-bg); }
article { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; display: grid; gap: 8px; }
.head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; }
.time { font-weight: 700; font-variant-numeric: tabular-nums; }
.line { font-size: 19px; font-weight: 700; }
.meta { color: var(--muted); font-size: 13px; }
.frames { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 8px; }
.frames img { width: 100%; border-radius: 6px; border: 1px solid var(--line); display: block; }
code { font-family: Consolas, ui-monospace, monospace; font-size: 13px; }
"""


def build_log_page() -> None:
    """Write LOG_DIR/index.html from visits.jsonl, newest first. Refreshes itself every 30s."""
    visits = [json.loads(line) for line in (LOG_DIR / "visits.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = {"said": 0, "quiet": 0, "err": 0}
    cards = []
    for v in sorted(visits, key=lambda v: v["time"], reverse=True):  # visits are written when they finish
        when = time.strftime("%a %b %d, %H:%M:%S", time.localtime(v["time"]))
        if v.get("error"):
            kind, label, line = "err", "Error", v["error"]
        elif v.get("spoken"):
            kind, label, line = "said", "Announced", v["spoken"]
        else:
            kind, label, line = "quiet", "Stayed quiet", v.get("note") or "No costume seen"
        kinds[kind] += 1
        meta = []
        if v.get("voice"):
            meta.append(f"Voice: {html.escape(v['voice'])}"
                        + (f" (ElevenLabs failed: <code>{html.escape(v['voice_error'])}</code>)" if v.get("voice_error") else ""))
        if v.get("backend"):
            meta.append(f"Model: {html.escape(v['backend'])}")
        if "raw" in v:
            meta.append(f"Raw answer: <code>{html.escape(v['raw'] or '(empty)')}</code>")
        if v.get("screen"):
            meta.append(f"Doorbell screen: <code>{html.escape(v['screen'])}</code>")
        if "capture_s" in v:
            meta.append(f"Frames {v['capture_s']}s · model {v['model_s']}s · total {v['total_s']}s")
        elif "total_s" in v:
            meta.append(f"Total {v['total_s']}s")
        frames = "".join(f'<a href="{html.escape(p)}"><img src="{html.escape(p)}" alt="Frame {i + 1}" loading="lazy"></a>'
                         for i, p in enumerate(v.get("frames", [])))
        cards.append(
            f'<article><div class="head"><span class="time">{when}</span><span class="pill {kind}">{label}</span>'
            f'<span class="meta">{html.escape(v["source"])}</span></div>'
            f'<div class="line">{html.escape(line)}</div>'
            + (f'<audio controls preload="none" src="{html.escape(v["audio"])}"></audio>' if v.get("audio") else "")
            + (f'<div class="meta">{" · ".join(meta)}</div>' if meta else "")
            + (f'<div class="frames">{frames}</div>' if frames else "") + "</article>")
    counts = (f'<span class="pill said">{kinds["said"]} announced</span><span class="pill quiet">{kinds["quiet"]} quiet</span>'
              f'<span class="pill err">{kinds["err"]} errors</span>')
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="refresh" content="30"><title>Doorbell Visit Log</title><style>{LOG_STYLE}</style></head>'
            f'<body><main><h1>Doorbell Visit Log</h1>'
            f'<p class="sub">Newest first. Updated {time.strftime("%H:%M:%S")}; this page refreshes every 30 seconds.</p>'
            f'<div class="counts">{counts}</div>{"".join(cards)}</main></body></html>')
    (LOG_DIR / "index.html").write_text(page, encoding="utf-8")


def track_person(msg: dict) -> bool:
    """Keep _present up to date from a Protect event; True when a new person detection starts.

    Events look like {"type": "add"|"update", "item": {"id", "type": "smartDetectZone",
    "device", "smartDetectTypes": ["person"], "end": <set once it's over>, ...}}. The doorbell
    also sees the street, so vehicles are ignored.
    """
    item = msg.get("item") or {}
    if (item.get("type") != "smartDetectZone" or item.get("device") != DOORBELL_CAMERA_ID
            or "person" not in (item.get("smartDetectTypes") or [])):
        return False
    with _lock:
        if item.get("end"):
            _present.pop(item.get("id"), None)
            return False
        new = item.get("id") not in _present
        _present[item.get("id")] = time.time()
    return new


def is_ring(msg: dict) -> bool:
    """A doorbell button press: {"type": "add", "item": {"type": "ring", "device": <doorbell id>, ...}}."""
    item = msg.get("item") or {}
    return msg.get("type") == "add" and item.get("type") == "ring" and item.get("device") == DOORBELL_CAMERA_ID


def play_ring_sound() -> None:
    """Play RING_SOUND on the Sonos; presses while it's still playing are ignored (kids mash the button)."""
    if not RING_SOUND or not _ring_lock.acquire(blocking=False):
        return
    try:
        print(f"{time.strftime('%H:%M:%S')} Doorbell pressed: playing {Path(RING_SOUND).name} in {RING_DELAY_SEC:g}s")
        clip = Path(RING_SOUND).read_bytes()
        time.sleep(RING_DELAY_SEC)
        if not DRY_RUN:
            with _play_lock:
                play_on_sonos(clip)
    except Exception as e:
        print(f"Ring sound failed: {e!r}")
    finally:
        _ring_lock.release()


def watch_detections() -> None:
    """Follow Protect's live event feed forever, reconnecting whenever it drops."""
    url = f"wss://{PROTECT_HOST}/proxy/protect/integration/v1/subscribe/events"
    while True:
        try:
            ws = websocket.create_connection(url, header=[f"X-API-KEY: {PROTECT_API_KEY}"],
                                             sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=60)
            print("Watching the doorbell for people")
            while True:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    ws.ping()  # quiet porch; make sure the connection is still alive
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = f"{msg.get('type')}/{(msg.get('item') or {}).get('type')}"
                if kind not in _event_types:  # shows what Protect actually sends, e.g. the first ring
                    _event_types.add(kind)
                    print(f"   (first Protect event of type {kind})")
                if is_ring(msg):
                    threading.Thread(target=play_ring_sound, daemon=True).start()
                if track_person(msg):
                    _wake.set()
        except Exception as e:
            print(f"Event feed dropped ({e!r}); reconnecting in 5s")
            with _lock:
                _present.clear()  # we may have missed "end" messages while disconnected
            time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/press":  # pretend the button was pressed
            threading.Thread(target=play_ring_sound, daemon=True).start()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.split("?")[0] != "/ring":
            self.send_response(404)
            self.end_headers()
            return
        _wake.set()  # one check now (more only if Protect sees someone)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_POST = do_GET

    def log_message(self, *args):
        pass


def test_image(paths: list[str]) -> None:
    """Identify saved photo(s) and print the line, without the camera or the speaker.
    Several paths are treated as one visit's burst, in order."""
    frames = [Path(p).read_bytes() for p in paths]
    fallback = backend_label(FALLBACK) if fallback_ready() else "none"
    print(f"Backend: {backend_label(BACKEND)}  (fallback: {fallback}), {len(frames)} frame(s)")
    start = time.time()
    costume = identify_costumes(frames)
    print(f"🔊 I see {costume}!" if costume else "(no costume: stays quiet)")
    print(f"   ({time.time() - start:.1f}s)")


def list_cameras() -> None:
    """Print each Protect camera, to check the doorbell auto-detect or set DOORBELL_CAMERA_ID."""
    r = requests.get(f"{PROTECT}/cameras", headers=HEADERS, verify=False, timeout=5)
    r.raise_for_status()
    for cam in r.json():
        print(f"{cam.get('name')!r}: id={cam.get('id')} modelKey={cam.get('modelKey')} "
              f"type={cam.get('type')} state={cam.get('state')}")


def test_camera() -> None:
    """Take a burst from the doorbell now, identify it and print the line. Images are never saved."""
    camera_id = DOORBELL_CAMERA_ID or find_doorbell_id()
    start = time.time()
    frames = get_burst(camera_id)
    got = time.time()
    print(f"Burst: {len(frames)} frame(s), {sum(len(f) for f in frames) // 1024} KB in {got - start:.1f}s")
    costume = identify_costumes(frames)
    del frames
    print(f"🔊 I see {costume}!" if costume else "(no costume: stays quiet)")
    print(f"   (identify {time.time() - got:.1f}s, total {time.time() - start:.1f}s)")
    if not costume:
        return
    show_on_doorbell(costume)
    if _lcd_timer:
        print(f"   (doorbell screen goes back in {LCD_SECONDS:.0f}s)")
        _lcd_timer.join()  # wait so the home screen is restored before we exit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Halloween costume doorbell")
    parser.add_argument("--test-image", metavar="PATH", nargs="+",
                        help="identify this photo (several = one visit's burst) and print the line, then exit")
    parser.add_argument("--list-cameras", action="store_true",
                        help="list the Protect cameras, then exit")
    parser.add_argument("--test-camera", action="store_true",
                        help="snapshot the doorbell, identify it and print the line, then exit")
    parser.add_argument("--say", metavar="TEXT",
                        help="have the Sonos speaker(s) say TEXT, then exit")
    parser.add_argument("--voice", metavar="NAME", default="",
                        help=f"with --say: an ElevenLabs voice ({', '.join(ELEVEN_VOICES)}) or a Hubitat TTS "
                             "voice name (default: the next voice in VOICES)")
    parser.add_argument("--save-clip", metavar="PATH",
                        help="with --say: save the ElevenLabs clip to PATH instead of playing it")
    args = parser.parse_args()
    if args.say and args.save_clip:
        name = args.voice if args.voice in ELEVEN_VOICES else (voice_names() or list(ELEVEN_VOICES))[0]
        start = time.time()
        Path(args.save_clip).write_bytes(eleven_clip(args.say, name))
        print(f"{name}: saved {args.save_clip} in {time.time() - start:.1f}s (not played)")
        raise SystemExit
    if args.say:
        start = time.time()
        speak(args.say, args.voice)
        print(f"   (sent in {time.time() - start:.1f}s)")
        raise SystemExit
    if args.test_image:
        test_image(args.test_image)
        raise SystemExit
    if args.list_cameras:
        list_cameras()
        raise SystemExit
    if args.test_camera:
        test_camera()
        raise SystemExit
    if not DOORBELL_CAMERA_ID:
        DOORBELL_CAMERA_ID = find_doorbell_id()
    if LCD_SECONDS:
        try:
            remember_home_screen()
        except Exception as e:
            print(f"Couldn't read the doorbell screen: {e!r}")
    threading.Thread(target=watch_detections, daemon=True).start()
    threading.Thread(target=porch_watcher, daemon=True).start()
    print(f"Manual test: curl.exe http://localhost:{LISTEN_PORT}/ring  (dry run: {DRY_RUN})")
    print(f"Voices: {', '.join(voice_names()) or 'Hubitat built-in'} (taking turns)")
    print(f"Button press plays: {Path(RING_SOUND).name if RING_SOUND else 'nothing (RING_SOUND is off)'}"
          f"  (test: curl.exe http://localhost:{LISTEN_PORT}/press)")
    kept = ["results"] + ["frames"] * LOG_IMAGES + ["spoken clips"] * LOG_AUDIO
    print(f"Visit log: {f'ON, saving {", ".join(kept)} to {LOG_DIR}' if LOG_VISITS else 'off (nothing is saved)'}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
