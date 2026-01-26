import json
import os
import socket
import subprocess
import threading
import time

import RPi.GPIO as GPIO
from gpiozero import DigitalInputDevice, DigitalOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory
from mfrc522 import SimpleMFRC522

HALL_SENSOR_PIN = 17
STEPPER_PINS = [14, 15, 18, 23]
RFID_FILE = "rfid.json"

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
PLAYLIST_EXTS = {".m3u", ".m3u8"}

MPV_SOCKET = "/tmp/rfid_record_player_mpv.sock"
MPV_PLAYLIST_TMP = "/tmp/rfid_record_player_playlist.m3u"

# ==========================================================
# Gesture timing (seconds)  <-- tweak these to taste
# ==========================================================
SHORT_LIFT_MAX = 0.6        # quick lift must be <= this to count as a "gesture"
DOUBLE_LIFT_WINDOW = 0.8    # time allowed for a 2nd quick lift (double-lift)
LONG_LIFT_MIN = 1.5         # if lifted >= this, treat as normal pause (no track skip)

# Previous behavior (seconds)
# If you're > this many seconds into the track: "previous" restarts current.
# If you're <= this (e.g., right after restart), "previous" goes to previous track.
PREV_RESTART_THRESHOLD = 3.0
# ==========================================================


def _list_audio_files(folder: str):
    files = []
    for root, _, names in os.walk(folder):
        for name in names:
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTS:
                files.append(os.path.join(root, name))
    files.sort(key=lambda p: p.lower())
    return files


def _write_m3u(path_list, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for p in path_list:
            f.write(p + "\n")


class MPVController:
    """
    Headless mpv controller with IPC.
    - pause/resume reliable
    - can seek to resume position
    - output goes to system default audio sink (Bluetooth if set as default)
    """

    def __init__(self):
        self.rfid_map = self._load_rfid_map()
        self.playback_cache = {
            "target": None,   # mapped target path (folder/file/playlist)
            "file": None,     # current file path
            "time_pos": 0.0,  # seconds
        }

        self._proc = None
        self._lock = threading.Lock()
        self._ensure_mpv()

    def _load_rfid_map(self):
        try:
            with open(RFID_FILE, "r") as f:
                data = json.load(f)
                if not data:
                    print("Warning: RFID map is empty.")
                return data
        except FileNotFoundError:
            print(f"Warning: RFID map file {RFID_FILE} not found.")
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
        return {}

    def _ensure_mpv(self):
        try:
            if os.path.exists(MPV_SOCKET):
                os.remove(MPV_SOCKET)
        except Exception:
            pass

        cmd = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--force-window=no",
            f"--input-ipc-server={MPV_SOCKET}",
            "--audio-display=no",
            "--terminal=no",
            "--msg-level=all=warn",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(50):
            if os.path.exists(MPV_SOCKET):
                return
            time.sleep(0.05)

        raise RuntimeError("mpv IPC socket did not appear. Is mpv installed and runnable?")

    def _send(self, command_obj, expect_reply=True, timeout=1.0):
        payload = (json.dumps(command_obj) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(MPV_SOCKET)
            s.sendall(payload)
            if not expect_reply:
                return None
            data = b""
            while b"\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            if not data:
                return None
            line = data.split(b"\n", 1)[0]
            try:
                return json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                return None

    def _command(self, *args):
        return self._send({"command": list(args)}, expect_reply=True)

    def _get_property(self, prop):
        resp = self._send({"command": ["get_property", prop]})
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    def _set_property(self, prop, value):
        return self._send({"command": ["set_property", prop, value]}, expect_reply=True)

    def _resolve_target_to_play_arg(self, target_path: str):
        p = os.path.expanduser(target_path)
        if os.path.isdir(p):
            files = _list_audio_files(p)
            if not files:
                return None
            _write_m3u(files, MPV_PLAYLIST_TMP)
            return MPV_PLAYLIST_TMP

        if os.path.isfile(p):
            ext = os.path.splitext(p)[1].lower()
            if ext in PLAYLIST_EXTS or ext in AUDIO_EXTS:
                return p

        return None

    def _try_restore_playlist_entry(self, wanted_path: str, max_steps=200):
        wanted_path = os.path.expanduser(wanted_path)
        cur = self._get_property("path")
        if cur == wanted_path:
            return True
        for _ in range(max_steps):
            self._command("playlist-next", "force")
            cur = self._get_property("path")
            if cur == wanted_path:
                return True
        return False

    def play(self, rfid_id: str):
        target = self.rfid_map.get(str(rfid_id))
        if not target:
            print(f"No local path mapped to RFID {rfid_id}")
            return

        play_arg = self._resolve_target_to_play_arg(target)
        if not play_arg:
            print(f"Mapped path is not playable (missing files?): {target}")
            return

        with self._lock:
            print("Starting playback")
            self._command("loadfile", play_arg, "replace")

            # Resume if same target and we have cached time
            if target == self.playback_cache.get("target"):
                resume_time = float(self.playback_cache.get("time_pos") or 0.0)
                cached_file = self.playback_cache.get("file")

                if cached_file:
                    self._try_restore_playlist_entry(cached_file)

                if resume_time > 0.5:
                    print(f"Resuming at {resume_time:.1f}s")
                    self._command("seek", resume_time, "absolute", "exact")

            self._set_property("pause", False)
            self.playback_cache["target"] = target

    def pause(self):
        with self._lock:
            self._set_property("pause", True)
            self.store_playback()

    def resume(self):
        with self._lock:
            self._set_property("pause", False)

    def stop(self):
        with self._lock:
            self.store_playback()
            self._command("stop")

    def next_track(self):
        with self._lock:
            self._command("playlist-next", "force")

    def prev_track(self):
        with self._lock:
            self._command("playlist-prev", "force")

    def restart_or_prev(self, threshold_seconds: float = PREV_RESTART_THRESHOLD):
        """
        If current position > threshold: restart current track.
        Else: go to previous track.
        """
        with self._lock:
            pos = self._get_property("time-pos")
            try:
                pos = float(pos) if pos is not None else 0.0
            except Exception:
                pos = 0.0

            if pos > threshold_seconds:
                print(f"Previous gesture: restart current track (pos={pos:.2f}s)")
                self._command("seek", 0, "absolute", "exact")
            else:
                print(f"Previous gesture: go to previous track (pos={pos:.2f}s)")
                self._command("playlist-prev", "force")

    def store_playback(self):
        try:
            time_pos = self._get_property("time-pos")
            path = self._get_property("path")
            if time_pos is None:
                time_pos = 0.0
            self.playback_cache["time_pos"] = float(time_pos or 0.0)
            self.playback_cache["file"] = path
            print(
                f"Stored playback: target={self.playback_cache.get('target')}, "
                f"file={path}, time={self.playback_cache['time_pos']:.1f}s"
            )
        except Exception as e:
            print(f"Failed to store playback: {e}")


class StepperMotor:
    STEP_SEQUENCE = [
        [1, 0, 0, 1],
        [1, 0, 0, 0],
        [1, 1, 0, 0],
        [0, 1, 0, 0],
        [0, 1, 1, 0],
        [0, 0, 1, 0],
        [0, 0, 1, 1],
        [0, 0, 0, 1],
    ]
    STEP_DELAY = 0.002

    def __init__(self):
        self.pins = [DigitalOutputDevice(pin) for pin in STEPPER_PINS]
        self._running = False
        self._thread = None

    def _run(self):
        print("Stepper motor thread started")
        while self._running:
            for step in self.STEP_SEQUENCE:
                for pin, value in zip(self.pins, step):
                    pin.value = value
                time.sleep(self.STEP_DELAY)
        self._stop_pins()
        print("Stepper motor thread stopped")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _stop_pins(self):
        for pin in self.pins:
            pin.off()


class RecordPlayer:
    """
    Hall sensor gestures (Option B):
    - Normal behavior: magnet lost pauses playback; magnet detected resumes.
    - Quick lift (<= SHORT_LIFT_MAX) then put back:
        - Single quick lift -> NEXT track (after DOUBLE_LIFT_WINDOW if no second lift)
        - Double quick lift -> PREVIOUS behavior:
            * if > PREV_RESTART_THRESHOLD into track -> restart current
            * else -> previous track
    - Long lift (>= LONG_LIFT_MIN): normal pause (no skip)
    """

    def __init__(self, player: MPVController, motor: StepperMotor, rfid: SimpleMFRC522, hall_sensor: DigitalInputDevice):
        self.player = player
        self.motor = motor
        self.rfid = rfid
        self.hall_sensor = hall_sensor

        self.current_rfid = None

        # Track magnet state transitions
        self._magnet_present = None
        self._lift_start_time = None

        # Gesture state
        self._short_lift_count = 0
        self._pending_single_deadline = None  # when to execute NEXT if no second lift arrives

    def _reset_gesture(self):
        self._short_lift_count = 0
        self._pending_single_deadline = None

    def _maybe_fire_pending_single(self, now: float):
        if self._pending_single_deadline is not None and now >= self._pending_single_deadline:
            if self._short_lift_count == 1:
                print("Gesture: single quick lift -> NEXT track")
                self.player.next_track()
                self.player.resume()
            self._reset_gesture()

    def update(self):
        now = time.time()
        magnet_detected = bool(self.hall_sensor.value)

        # Fire pending single-lift action if window elapsed
        self._maybe_fire_pending_single(now)

        # Initialize on first run
        if self._magnet_present is None:
            self._magnet_present = magnet_detected
            if magnet_detected:
                self.motor.start()
            return

        if magnet_detected and not self._magnet_present:
            # Magnet returned
            lift_duration = 0.0
            if self._lift_start_time is not None:
                lift_duration = now - self._lift_start_time

            print(f"Magnet detected → start (lift duration {lift_duration:.2f}s)")
            self.motor.start()
            self.player.resume()

            if lift_duration <= SHORT_LIFT_MAX:
                self._short_lift_count += 1
                print(f"Quick lift #{self._short_lift_count}")

                if self._short_lift_count == 1:
                    self._pending_single_deadline = now + DOUBLE_LIFT_WINDOW

                elif self._short_lift_count >= 2:
                    print("Gesture: double quick lift -> PREVIOUS (restart-or-prev)")
                    self.player.restart_or_prev(PREV_RESTART_THRESHOLD)
                    self.player.resume()
                    self._reset_gesture()

            elif lift_duration >= LONG_LIFT_MIN:
                self._reset_gesture()
            else:
                self._reset_gesture()

            self._lift_start_time = None

        elif (not magnet_detected) and self._magnet_present:
            # Magnet lost
            print("Magnet lost → stop motor + pause")
            self.motor.stop()
            self.player.pause()
            self._lift_start_time = now

        self._magnet_present = magnet_detected

        # RFID handling while spinning
        if magnet_detected:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and str(rfid_id) != str(self.current_rfid):
                print(f"RFID changed: {rfid_id}")
                self.current_rfid = rfid_id
                self._reset_gesture()
                self.player.play(str(rfid_id))


def main():
    print("Starting Record Player (LOCAL FILES via mpv) + Hall Gestures")
    player = MPVController()
    motor = StepperMotor()
    rfid = SimpleMFRC522()
    hall_sensor = DigitalInputDevice(HALL_SENSOR_PIN, pull_up=True, pin_factory=LGPIOFactory())

    rp = RecordPlayer(
        player=player,
        motor=motor,
        rfid=rfid,
        hall_sensor=hall_sensor,
    )

    try:
        while True:
            rp.update()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        motor.stop()
        GPIO.cleanup()


if __name__ == "__main__":
    main()