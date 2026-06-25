#!/usr/bin/env python3
"""
Wilson — a push-button Raspberry Pi home assistant.

Buttons: weather / dad joke / mp3 / affirmation.
Double-press any button: open the NASA ISS live stream fullscreen.
Display: a fullscreen animated face on HDMI (pygame).
On boot: an actuator presses the TV's power button on; 5 min later, off.

ARCHITECTURE (this is the important part — read before editing)
---------------------------------------------------------------
Everything that "does something" (speaking, playing music, opening the
stream) runs on ONE single worker thread, fed by a queue. This is the
core design choice that keeps the code simple and prevents Wilson from
"talking over himself": because only one action thread ever runs at a
time, two button presses can't both be speaking at once. A new action
signals the current one to stop (via a threading.Event), then runs.

GPIO callbacks therefore do almost nothing: they debounce, decide
single-vs-double press, and hand an Action to the controller. They never
block and never speak directly — RPi.GPIO callbacks must return promptly
or the library starts dropping button edges.

The main thread does only two things: render the face at 30fps, and
watch for quit. All shared state lives in the Controller behind one lock.
"""

import os
import sys
import time
import json
import queue
import random
import threading
import subprocess
import urllib.request

import api_keys
import pygame
import RPi.GPIO as GPIO

try:
    import websocket  # `websocket-client`; talks to Chromium's debug port
except ImportError:
    websocket = None

# ════════════════════════════════════════════════════════════════════
# CONFIG
# ════════════════════════════════════════════════════════════════════

PIN_WEATHER, PIN_JOKE, PIN_MUSIC, PIN_AFFIRMATION = 17, 27, 22, 23
BUTTON_PINS = (PIN_WEATHER, PIN_JOKE, PIN_MUSIC, PIN_AFFIRMATION)

PIN_ACTUATOR_EXTEND, PIN_ACTUATOR_RETRACT = 24, 25
ACTUATOR_EXTEND_SECONDS  = 0.7
ACTUATOR_RETRACT_SECONDS = 1.5
TV_AUTO_OFF_SECONDS      = 5 * 60

MP3_FILE       = "/home/admin/Desktop/wilson/forest_track.mp3"
WEATHER_LAT    = "38.9072"
WEATHER_LON    = "-77.0369"
API_NINJAS_KEY = api_keys.NINJAS_KEY
ESPEAK_SPEED   = 145

DOUBLE_PRESS_WINDOW   = 0.5    # max gap between taps to count as a double-press
HARD_DEBOUNCE_SECONDS = 0.25   # software debounce (RPi.GPIO's is unreliable on Pi 5)
IDLE_TO_STREAM_SECONDS = 20    # after this long idle, auto-open the NASA stream

YOUTUBE_VIDEO_ID  = "uwXgcTc8oY8"
YOUTUBE_WATCH_URL = f"https://www.youtube.com/watch?v={YOUTUBE_VIDEO_ID}"
CDP_PORT          = 9222
CDP_PROFILE_DIR   = "/tmp/wilson_chromium_profile"

GREETINGS = [
    "Hello there.",
    "Good to see you.",
    "Hi Pete.",
    "Systems online.",
    "Good morning. Or afternoon. Or evening. I have no idea what time it is.",
    "It is I, the thin thread connecting you to reality.",
    "I'm up.",
    "Greetings, Pete.",
    "Hey there, friend.",
    "Hola amigo!",
    "This had better be important.",
    "Press a button, I'm bored.",
    "Ah, if it isn't my favorite charity case.",
    "Hello! It's a great day to push buttons.",
    "Online and operational.",
    "What shall we do today?",
    "Hello, Demon!",
    "What's up freak",
]

# ════════════════════════════════════════════════════════════════════
# FACES
# ════════════════════════════════════════════════════════════════════
# Each face is rendered character-by-character into fixed-width cells
# (see Display.render), so glyphs of differing widths never overlap and
# the face never jitters horizontally between frames.

FACES = {
    "boot": ["(⇀‿‿↼)", "(⇀‿‿↼)", "(≖‿‿≖)", "(≖‿‿≖)", "(◕‿‿◕)", "(◕‿‿◕)"],
    "idle": ["(⚆⚆ )", "(☉☉ )"],
    "loading": ["(☼‿‿☼)"],
    "talk": ["(⇀‿o‿↼)", "(⇀‿O‿↼)", "(⇀‿O‿↼)", "(⇀‿o‿↼)"],
    "weather": ["(⇀☐‿☐↼)", "(⇀☐o☐↼)", "(⇀☐O☐↼)", "(⇀☐o☐↼)"],
    "joke": ["(⇀^o^↼)", "(⇀^O^↼)", "(⇀^o^↼)"],
    "affirmation": ["(⇀◠‿◠↼)", "(⇀◠o◠↼)", "(⇀◠O◠↼)", "(⇀◠o◠↼)"],
    "music": ["(⌐■_■)", "(⌐■_■)", "(⌐■_■)", "(⌐■_■)", "(⌐■_■)", "(⌐■_■)", "(⌐■_■)", "(⌐■_■)", 
              "(■_■)", "(■_■)", "(■_■)", "(■_■)", "(■_■)", "(■_■)", 
              "(■_■¬)", "(■_■¬)", "(■_■¬)", "(■_■¬)", "(■_■¬)", "(■_■¬)", "(■_■¬)", "(■_■¬)", 
              "(■‿■¬)", "(■‿■¬)", "(■‿■¬)", "(■‿■¬)",  "(■‿■¬)", "(■‿■¬)", 
              "(■_■)", "(■_■)", "(■_■)", "(■_■)"],
}

# ════════════════════════════════════════════════════════════════════
# DISPLAY
# ════════════════════════════════════════════════════════════════════

class Display:
    BLACK  = (0, 0, 0)
    GREEN  = (51, 255, 102)
    DGREEN = (30, 140, 60)

    def __init__(self):
        os.environ.setdefault("SDL_VIDEODRIVER", "x11")
        os.environ.setdefault("DISPLAY", ":0")
        pygame.init()
        info = pygame.display.Info()
        self.w = info.current_w or 1920
        self.h = info.current_h or 1080
        self.screen = pygame.display.set_mode(
            (self.w, self.h), pygame.FULLSCREEN | pygame.NOFRAME)
        pygame.display.set_caption("Assistant")
        pygame.mouse.set_visible(False)

        self.face_font = pygame.font.SysFont(
            "DejaVu Sans", max(176, int((self.h // 6) * 1.43)))
        self.text_font = pygame.font.SysFont(
            "DejaVu Sans", max(32, self.h // 22))

        # One cell wide enough for the widest glyph used anywhere, so the
        # per-character grid never lets two glyphs collide.
        widest = max(
            self.face_font.size(ch)[0]
            for frames in FACES.values() for s in frames for ch in s
        )
        self.cell_w = int(widest * 1.25)

    def render(self, face, caption):
        self.screen.fill(self.BLACK)

        row_left = self.w // 2 - (self.cell_w * len(face)) // 2
        cy = self.h // 2 - self.h // 10
        for i, ch in enumerate(face):
            if ch == " ":
                continue
            surf = self.face_font.render(ch, True, self.GREEN)
            cx = row_left + i * self.cell_w + self.cell_w // 2
            self.screen.blit(surf, surf.get_rect(center=(cx, cy)))

        if caption:
            self._render_caption(caption)
        pygame.display.flip()

    def _render_caption(self, caption):
        max_w = int(self.w * 0.85)
        lines, line = [], ""
        for word in caption.split():
            test = (line + " " + word).strip()
            if self.text_font.size(test)[0] <= max_w:
                line = test
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)

        y = self.h // 2 + self.h // 3
        step = self.text_font.get_height() + 6
        for i, l in enumerate(lines[:6]):
            surf = self.text_font.render(l, True, self.DGREEN)
            self.screen.blit(surf, surf.get_rect(center=(self.w // 2, y + i * step)))

# ════════════════════════════════════════════════════════════════════
# HARDWARE HELPERS
# ════════════════════════════════════════════════════════════════════

def fetch_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())

def press_tv_power_button():
    """Extend the actuator to push the TV power button, then retract.
    Fixed durations — the L298N has no current/position feedback, so it
    cannot detect resistance; if it stalls against the button early it
    simply holds there (safe briefly) until the extend time elapses."""
    GPIO.output(PIN_ACTUATOR_EXTEND, GPIO.HIGH)
    GPIO.output(PIN_ACTUATOR_RETRACT, GPIO.LOW)
    time.sleep(ACTUATOR_EXTEND_SECONDS)
    GPIO.output(PIN_ACTUATOR_EXTEND, GPIO.LOW)
    GPIO.output(PIN_ACTUATOR_RETRACT, GPIO.HIGH)
    time.sleep(ACTUATOR_RETRACT_SECONDS)
    GPIO.output(PIN_ACTUATOR_RETRACT, GPIO.LOW)

# ════════════════════════════════════════════════════════════════════
# YOUTUBE STREAM (Chromium kiosk + CDP for fullscreen & cursor hiding)
# ════════════════════════════════════════════════════════════════════

class Stream:
    """Opens the NASA ISS live stream in Chromium kiosk mode and uses the
    Chrome DevTools Protocol (CDP) to put the video into YouTube's own
    native fullscreen.

    The mechanism that actually works here (after trying several that
    didn't): send a *trusted* 'f' keypress via CDP's Input domain. The
    things that failed and why —
      • requestFullscreen() over CDP / JS .click() on the FS button:
        rejected, because they aren't "user gestures" and YouTube's
        fullscreen is gesture-gated.
      • xdotool 'f': couldn't reach the display — this Pi's graphical
        session is a tty-typed session xdotool can't target.
      • CSS stretching the <video> to 100vw/100vh: collapsed the player
        to 0×0 and showed a white screen, because it fought YouTube's
        own layout JS.
    CDP's Input.dispatchKeyEvent synthesizes a genuine browser-level
    input event, which satisfies the gesture requirement — confirmed to
    flip document.fullscreenElement and resize the video to full screen.
    (The cursor is hidden separately and globally by unclutter, started
    in main() — not here.)"""

    def __init__(self):
        self.proc = None
        self._fullscreen_ready = threading.Event()

    def is_open(self):
        return self.proc is not None and self.proc.poll() is None

    def open(self):
        if self.is_open():
            return
        self._fullscreen_ready.clear()
        self.proc = subprocess.Popen(
            [
                "chromium", "--kiosk", "--noerrdialogs", "--disable-infobars",
                "--password-store=basic",
                "--autoplay-policy=no-user-gesture-required",
                f"--remote-debugging-port={CDP_PORT}",
                # Newer Chromium returns "403 Forbidden" on CDP WebSocket
                # connections unless the origin is explicitly allowed.
                "--remote-allow-origins=*",
                f"--user-data-dir={CDP_PROFILE_DIR}",
                YOUTUBE_WATCH_URL,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Keep the pygame "Assistant" window (showing the loading face) in
        # FRONT while Chromium loads behind it — otherwise Chromium's kiosk
        # window pops to the top mid-load and the user sees YouTube's raw
        # loading page. _go_fullscreen_when_ready raises Chromium only once
        # the video is actually fullscreen, so the swap looks instant.
        # We re-raise the pygame window a few times over the first second
        # because Chromium may take a moment to map its window and try to
        # come forward.
        threading.Thread(target=self._keep_face_in_front, daemon=True).start()
        threading.Thread(target=self._go_fullscreen_when_ready, daemon=True).start()

    def _keep_face_in_front(self):
        # Hold the loading face in front until fullscreen is ready (or the
        # stream closes, or a safety timeout). Stops promptly once
        # _fullscreen_ready is set, so it never covers the playing video.
        for _ in range(40):   # safety cap ~10s
            if not self.is_open() or self._fullscreen_ready.is_set():
                return
            _run(["wmctrl", "-a", "Assistant"])
            time.sleep(0.25)

    def close(self):
        if self.is_open():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        # Bring the pygame window back to the front (Chromium was on top).
        _run(["wmctrl", "-a", "Assistant"])

    def _go_fullscreen_when_ready(self):
        if websocket is None:
            print("[stream] websocket-client not installed; "
                  "run: pip install websocket-client --break-system-packages")
            self._fullscreen_ready.set()
            return
        ws_url = self._wait_for_target()
        if not ws_url or not self.is_open():
            self._fullscreen_ready.set()
            return
        try:
            ws = websocket.create_connection(ws_url, timeout=5)
            _id = [0]

            def send(method, params=None):
                _id[0] += 1
                ws.send(json.dumps({"id": _id[0], "method": method,
                                    "params": params or {}}))
                return json.loads(ws.recv())

            def evaluate(expr):
                r = send("Runtime.evaluate",
                         {"expression": expr, "returnByValue": True})
                return r.get("result", {}).get("result", {}).get("value")

            key = {"key": "f", "code": "KeyF",
                   "windowsVirtualKeyCode": 70, "nativeVirtualKeyCode": 70}

            # The keypress only "takes" once the video is actually loaded
            # and playing — firing it the instant the tab appears (which
            # is what failed before) is too early. So we poll until the
            # video reports it has real dimensions / data, then send the
            # 'f' keypress, and retry the whole thing until
            # document.fullscreenElement is actually set (or we give up).
            engaged = False
            for attempt in range(40):   # ~40 * 0.5s = 20s max for the video to load
                if not self.is_open():
                    break
                ready = evaluate(
                    "(function(){var v=document.querySelector('video');"
                    "return !!(v && v.readyState>=2 && v.videoWidth>0);})()")
                if ready:
                    # Send a trusted 'f' keypress to trigger fullscreen.
                    # IMPORTANT: do NOT focus the <video> element first.
                    # Focusing the raw video routes the keypress to the
                    # video element's native fullscreen, which fullscreens
                    # the *document* and leaves the video at its small
                    # in-page size. Leaving focus alone lets YouTube's own
                    # page-level handler catch 'f' and scale the video to
                    # fill the screen.
                    send("Input.dispatchKeyEvent", {"type": "keyDown", **key})
                    send("Input.dispatchKeyEvent", {"type": "keyUp", **key})
                    time.sleep(0.6)
                    if evaluate("!!document.fullscreenElement"):
                        engaged = True
                        break
                time.sleep(0.5)

            print(f"[stream] fullscreen engaged: {engaged} (after {attempt + 1} checks)")
            ws.close()

            # Only NOW bring Chromium to the front — the video is already
            # fullscreen, so the loading face (held in front until this
            # point by _keep_face_in_front) swaps straight to the playing
            # video with no visible YouTube loading page in between. Set
            # the ready flag FIRST so the face-keeper stops re-raising the
            # pygame window before we raise Chromium. We match Chromium's
            # window by the video title in its title bar.
            self._fullscreen_ready.set()
            if engaged and self.is_open():
                _run(["wmctrl", "-a", "International Space Station"])
        except Exception as e:
            print(f"[stream] CDP error: {e!r}")
            self._fullscreen_ready.set()  # don't leave the face-keeper spinning

    def _wait_for_target(self, max_wait=20, interval=0.5):
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://localhost:{CDP_PORT}/json", timeout=2) as r:
                    for t in json.loads(r.read().decode()):
                        if YOUTUBE_VIDEO_ID in t.get("url", ""):
                            return t.get("webSocketDebuggerUrl")
            except Exception:
                pass
            time.sleep(interval)
        print("[stream] timed out waiting for the YouTube tab")
        return None

def _run(cmd):
    """Best-effort fire-and-forget external command; never raises."""
    try:
        subprocess.run(cmd, timeout=3,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════
# CONTROLLER  — owns all state; runs ONE serialized action worker
# ════════════════════════════════════════════════════════════════════

class Controller:
    def __init__(self, display):
        self.display = display
        self.stream = Stream()

        self._lock = threading.Lock()
        self._face_set = "idle"
        self._frame = 0
        self._caption = ""

        # The single action queue + worker. Only ONE action runs at a
        # time, which is what structurally prevents overlapping speech.
        self._queue = queue.Queue()
        self._stop = threading.Event()   # signals the running action to bail
        self._action_ran = False         # set when a real action is submitted;
                                          # read+cleared by the main loop to reset
                                          # the once-per-idle-session auto-open lock
        threading.Thread(target=self._worker_loop, daemon=True).start()

    # ---- state accessors (all behind the lock) ----------------------

    def set_face(self, face_set, caption=None):
        with self._lock:
            if face_set != self._face_set:
                self._face_set = face_set
                self._frame = 0
            if caption is not None:
                self._caption = caption

    def advance_frame(self):
        with self._lock:
            self._frame += 1

    def set_frame(self, n):
        with self._lock:
            self._frame = n

    def snapshot(self):
        with self._lock:
            frames = FACES[self._face_set]
            return frames[self._frame % len(frames)], self._caption

    def current_face_set(self):
        with self._lock:
            return self._face_set

    # ---- queueing ---------------------------------------------------

    def submit(self, name):
        """Ask for an action by name. Signals any currently-running
        action to stop, then enqueues this one. Because the worker is
        single-threaded, the new action can't start until the old one
        has actually returned — so they never overlap."""
        self._stop.set()
        self._queue.put(name)

    def consume_action_ran_flag(self):
        """Read-and-clear the 'a real action ran' flag. Used by the main
        loop to reset the once-per-idle-session auto-open lock: after the
        user actually does something, the idle-stream timeout is allowed
        to fire again on a later idle stretch."""
        with self._lock:
            ran = self._action_ran
            self._action_ran = False
        return ran

    def interrupt(self):
        """Stop the currently-running action without queueing a new one
        (used when opening the stream, which isn't a worker action)."""
        self._stop.set()

    def _worker_loop(self):
        while True:
            name = self._queue.get()
            # Coalesce: if more presses are already queued, skip to the
            # last one rather than running a backlog.
            while not self._queue.empty():
                name = self._queue.get_nowait()
            self._stop.clear()
            try:
                ACTIONS[name](self)
            except Exception as e:
                print(f"[action {name}] error: {e!r}")
            # A real (non-boot) action just ran to completion → re-arm the
            # once-per-idle-session auto-open, so it can fire again on a
            # later idle stretch. Tying this to the worker ACTUALLY running
            # an action (rather than to submit() being called) is robust:
            # dismissing the stream or a stray bounce never reaches here,
            # so it can't accidentally re-arm the auto-open.
            if name != "boot":
                with self._lock:
                    self._action_ran = True
            if self._queue.empty():
                self.set_face("idle", caption="")

    # ---- primitives used by actions ---------------------------------

    def speak(self, text, face_set="talk"):
        """Speak via espeak, animating `face_set`, interruptible: if
        another action is submitted, self._stop is set and we kill espeak
        and return early so the new action can take over cleanly."""
        self.set_face(face_set, caption=text)
        proc = subprocess.Popen(
            ["espeak", "-s", str(ESPEAK_SPEED), "-v", "en", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while proc.poll() is None:
            if self._stop.is_set():
                proc.terminate()
                return
            self.advance_frame()
            time.sleep(0.18)

    def play_music(self):
        if not os.path.isfile(MP3_FILE):
            self.speak(f"I could not find the music file at {MP3_FILE}")
            return
        self.set_face("music", caption="")
        proc = subprocess.Popen(["mpg123", "-q", "-f", "80000", MP3_FILE])
        while proc.poll() is None:
            if self._stop.is_set():
                proc.terminate()
                return
            self.advance_frame()
            time.sleep(0.25)

# ════════════════════════════════════════════════════════════════════
# ACTIONS  — each takes the controller; runs on the single worker thread
# ════════════════════════════════════════════════════════════════════

def action_weather(c):
    try:
        data = fetch_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
            "&current=temperature_2m,weathercode,windspeed_10m"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            "&temperature_unit=fahrenheit&windspeed_unit=mph"
            "&forecast_days=1&timezone=auto")
        cur, day = data["current"], data["daily"]
        codes = {
            0: "clear skies", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
            45: "foggy", 48: "icy fog", 51: "light drizzle", 53: "drizzle",
            55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
            71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
            80: "showers", 81: "heavy showers", 82: "violent showers",
            85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms",
            96: "thunderstorms with hail", 99: "severe hail",
        }
        msg = (f"Right now it is {round(cur['temperature_2m'])} degrees with "
               f"{codes.get(cur['weathercode'], 'mixed conditions')} and winds at "
               f"{round(cur['windspeed_10m'])} miles per hour. Today's high is "
               f"{round(day['temperature_2m_max'][0])}, low is "
               f"{round(day['temperature_2m_min'][0])}, with a "
               f"{day['precipitation_probability_max'][0]} percent chance of precipitation.")
    except Exception as e:
        msg = f"Sorry, I could not retrieve the weather. {e}"
    c.speak(msg, face_set="weather")

def action_joke(c):
    try:
        data = fetch_json("https://api.api-ninjas.com/v1/dadjokes",
                          headers={"X-Api-Key": API_NINJAS_KEY})
        joke = data[0]["joke"]
    except Exception as e:
        joke = f"I had a joke about UDP, but you might not get it. Also the API failed: {e}"
    c.speak(joke, face_set="joke")

def action_affirmation(c):
    try:
        msg = fetch_json("https://www.affirmations.dev/").get(
            "affirmation", "You are doing great.")
    except Exception as e:
        msg = f"You are amazing. Also the API had trouble: {e}"
    c.speak(msg, face_set="affirmation")

def action_music(c):
    c.play_music()

def action_boot(c):
    """Boot animation (sleeping → awakening → awake) then a greeting.
    Queued like any other action so it runs on the same single worker."""
    for i in range(len(FACES["boot"])):
        if c._stop.is_set():
            break
        c.set_face("boot")
        c.set_frame(i)
        time.sleep(1.0)
    c.speak(random.choice(GREETINGS))

ACTIONS = {
    "weather": action_weather,
    "joke": action_joke,
    "affirmation": action_affirmation,
    "music": action_music,
    "boot": action_boot,
}

PIN_TO_ACTION = {
    PIN_WEATHER: "weather",
    PIN_JOKE: "joke",
    PIN_MUSIC: "music",
    PIN_AFFIRMATION: "affirmation",
}

# ════════════════════════════════════════════════════════════════════
# BUTTON INPUT  — debounce + single/double-press, then hand to controller
# ════════════════════════════════════════════════════════════════════

class Buttons:
    def __init__(self, controller):
        self.c = controller
        self._last_edge = {p: 0.0 for p in BUTTON_PINS}
        self._pending = {p: None for p in BUTTON_PINS}  # threading.Timer per pin
        self._lock = threading.Lock()

        GPIO.setmode(GPIO.BCM)
        for p in BUTTON_PINS:
            GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            GPIO.add_event_detect(p, GPIO.FALLING, callback=self._on_edge,
                                  bouncetime=120)
        GPIO.setup(PIN_ACTUATOR_EXTEND, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(PIN_ACTUATOR_RETRACT, GPIO.OUT, initial=GPIO.LOW)

    def _on_edge(self, channel):
        # Software debounce — RPi.GPIO's bouncetime is unreliable on Pi 5,
        # and a bounce misread as a 2nd press would fake a double-press.
        now = time.time()
        with self._lock:
            if now - self._last_edge[channel] < HARD_DEBOUNCE_SECONDS:
                return
            self._last_edge[channel] = now

        # Stream open? Any press just closes it.
        if self.c.stream.is_open():
            self.c.stream.close()
            self.c.set_face("idle", caption="")
            return

        with self._lock:
            pending = self._pending[channel]
            if pending is not None:
                # Second press within the window → double-press → stream.
                pending.cancel()
                self._pending[channel] = None
                double = True
            else:
                double = False

        if double:
            # Stop whatever's running, show the loading face, open stream.
            self.c.interrupt()
            self.c.set_face("loading", caption="")
            self.c.stream.open()
            return

        # First press: wait DOUBLE_PRESS_WINDOW to see if a second follows.
        t = threading.Timer(DOUBLE_PRESS_WINDOW, self._fire_single, args=(channel,))
        with self._lock:
            self._pending[channel] = t
        t.start()

    def _fire_single(self, channel):
        with self._lock:
            self._pending[channel] = None
        self.c.submit(PIN_TO_ACTION[channel])

# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    display = Display()
    controller = Controller(display)
    Buttons(controller)

    # Hide the mouse cursor globally. On X11 (which this Pi now runs,
    # after switching off labwc/Wayland), unclutter works reliably —
    # "-idle 0" hides the pointer immediately and keeps it hidden,
    # including over Chromium's fullscreen video. This replaces all the
    # earlier page-level / CDP cursor tricks, which couldn't touch the
    # compositor-drawn cursor under Wayland. Launched with Popen (not
    # _run) because unclutter is a long-running daemon — _run uses
    # subprocess.run, which would block here forever waiting for it.
    try:
        subprocess.Popen(["unclutter", "-idle", "0", "-root"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[main] couldn't start unclutter (cursor may show): {e!r}")

    # Turn the TV on now; schedule it off later. Both off the main thread.
    threading.Thread(target=press_tv_power_button, daemon=True).start()
    off = threading.Timer(TV_AUTO_OFF_SECONDS, press_tv_power_button)
    off.daemon = True
    off.start()

    time.sleep(2)

    # Boot animation + greeting, queued like any other action.
    controller.submit("boot")

    clock = pygame.time.Clock()
    last_tick = time.time()
    idle_since = None        # timestamp the face last entered idle state
    auto_opened = False      # did the idle-timeout already fire this session?
    try:
        while True:
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (
                        event.type == pygame.KEYDOWN
                        and event.key == pygame.K_ESCAPE):
                    raise KeyboardInterrupt

            is_idle = controller.current_face_set() == "idle"

            # Advance the idle animation on a slow clock. Action faces
            # (talk/music/boot) animate themselves from the worker thread.
            if is_idle and time.time() - last_tick >= 2.0:
                controller.advance_frame()
                last_tick = time.time()

            # Auto-open the NASA stream after IDLE_TO_STREAM_SECONDS of
            # continuous idle — but only ONCE per idle session. After the
            # user dismisses it (a button press), we do NOT auto-reopen on
            # the next idle stretch; auto_opened stays True until the user
            # actually triggers an action, which clears it (see
            # Controller.submit). A double-press still opens the stream
            # any time, since that path calls stream.open() directly.
            if is_idle and not controller.stream.is_open():
                if idle_since is None:
                    idle_since = time.time()
                elif (not auto_opened
                        and time.time() - idle_since >= IDLE_TO_STREAM_SECONDS):
                    controller.set_face("loading", caption="")
                    controller.stream.open()
                    auto_opened = True
            else:
                idle_since = None

            # An action having run clears the once-per-session lock, so the
            # next idle stretch can auto-open again.
            if controller.consume_action_ran_flag():
                auto_opened = False

            face, caption = controller.snapshot()
            display.render(face, caption)
            clock.tick(30)
    except KeyboardInterrupt:
        pass
    finally:
        controller.stream.close()
        GPIO.cleanup()
        pygame.quit()
        sys.exit(0)

if __name__ == "__main__":
    main()