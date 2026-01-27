import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime

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
DOUBLE_LIFT_WINDOW = 0.8    # time allowed for more lifts; action fires when this window expires
LONG_LIFT_MIN = 1.5         # long lift behaves like normal pause/no skip

PREV_RESTART_THRESHOLD = 5.0

FULL_STOP_AFTER = 20 * 60

# Finish detection polling
MPV_FINISH_POLL_INTERVAL = 0.25

# After finish-full-stop, when user does needle-up/needle-down:
# scan RFID for up to this many seconds to restart (same/new record)
RFID_SCAN_BURST_SECONDS = 5.0

# Audiobook feature
AUDIOBOOK_MARKER = "audiobook.json"
AUDIOBOOK_AUTOSAVE_SECONDS = 60.0

# Audiobook reset gesture
RESET_LIFT_COUNT = 5
RESET_GESTURE_MAX_TOTAL = 6.0  # must complete 5 quick lifts within this total time
# ==========================================================


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


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


def _safe_read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _safe_write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class MPVController:
    """
    Headless mpv controller with IPC.
    - pause/resume reliable
    - can seek to resume position
    - supports audiobook progress via audiobook.json
    """

    def __init__(self):
        self.rfid_map = self._load_rfid_map()

        # Cache used for non-audiobook resume (same behavior as before)
        self.playback_cache = {
            "target": None,   # mapped target path (folder/file/playlist)
            "file": None,     # current file path
            "time_pos": 0.0,  # seconds
        }

        # Current context
        self.current_target = None  # mapped path string from rfid.json (expanded in resolve)
        self.current_is_audiobook = False
        self.current_audiobook_file = None  # full path to audiobook.json (if audiobook)

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

        for _ in range(80):
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
        """
        Returns (play_arg, expanded_target_path, playlist_files or None)
        """
        p = os.path.expanduser(target_path)

        if os.path.isdir(p):
            files = _list_audio_files(p)
            if not files:
                return None, None, None
            _write_m3u(files, MPV_PLAYLIST_TMP)
            return MPV_PLAYLIST_TMP, p, files

        if os.path.isfile(p):
            ext = os.path.splitext(p)[1].lower()
            if ext in PLAYLIST_EXTS or ext in AUDIO_EXTS:
                return p, p, None

        return None, None, None

    def _try_restore_playlist_entry(self, wanted_path: str, max_steps=400):
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

    # -----------------------
    # Audiobook helpers
    # -----------------------
    def _audiobook_marker_path(self, folder: str):
        return os.path.join(folder, AUDIOBOOK_MARKER)

    def _is_audiobook_folder(self, folder: str) -> bool:
        return os.path.isdir(folder) and os.path.isfile(self._audiobook_marker_path(folder))

    def ensure_audiobook_marker(self, folder: str):
        """
        Create audiobook.json if missing, initialized to first file, time_pos=0.
        Does not overwrite existing.
        """
        if not os.path.isdir(folder):
            return
        marker = self._audiobook_marker_path(folder)
        if os.path.exists(marker):
            return
        files = _list_audio_files(folder)
        if not files:
            return
        rel_first = os.path.relpath(files[0], folder)
        data = {
            "type": "audiobook",
            "version": 1,
            "current_file": rel_first,
            "time_pos": 0.0,
            "updated_at": _now_iso(),
        }
        _safe_write_json(marker, data)

    def read_audiobook_state(self, folder: str):
        marker = self._audiobook_marker_path(folder)
        data = _safe_read_json(marker) or {}
        # normalize
        if data.get("version") is None:
            data["version"] = 1
        if data.get("current_file") is None:
            data["current_file"] = ""
        if data.get("time_pos") is None:
            data["time_pos"] = 0.0
        return data

    def write_audiobook_state(self, folder: str, current_file_rel: str, time_pos: float):
        marker = self._audiobook_marker_path(folder)
        data = {
            "type": "audiobook",
            "version": 1,
            "current_file": current_file_rel,
            "time_pos": float(time_pos),
            "updated_at": _now_iso(),
        }
        _safe_write_json(marker, data)

    def reset_audiobook_state(self, folder: str):
        """
        Reset progress to beginning (first track, 0s). Creates marker if needed.
        """
        self.ensure_audiobook_marker(folder)
        files = _list_audio_files(folder)
        if not files:
            return
        rel_first = os.path.relpath(files[0], folder)
        self.write_audiobook_state(folder, rel_first, 0.0)

    def store_audiobook_progress_if_active(self):
        """
        If current target is audiobook folder and mpv has a path+time, write audiobook.json.
        """
        if not self.current_is_audiobook or not self.current_target:
            return
        folder = self.current_target
        try:
            time_pos = self._get_property("time-pos")
            path = self._get_property("path")
            if path is None or time_pos is None:
                return
            try:
                time_pos = float(time_pos)
            except Exception:
                return

            # Make relative to folder (portable)
            try:
                rel = os.path.relpath(path, folder)
            except Exception:
                rel = os.path.basename(path)

            self.write_audiobook_state(folder, rel, time_pos)
            # (Optional) keep cache aligned
            self.playback_cache["target"] = folder
            self.playback_cache["file"] = path
            self.playback_cache["time_pos"] = time_pos
        except Exception as e:
            print(f"Audiobook progress save failed: {e}")

    # -----------------------
    # Playback API
    # -----------------------
    def play(self, rfid_id: str, force_restart_audiobook: bool = False):
        target_raw = self.rfid_map.get(str(rfid_id))
        if not target_raw:
            print(f"No local path mapped to RFID {rfid_id}")
            return

        play_arg, expanded_target, playlist_files = self._resolve_target_to_play_arg(target_raw)
        if not play_arg:
            print(f"Mapped path is not playable (missing files?): {target_raw}")
            return

        # Determine audiobook status
        is_audiobook = bool(playlist_files) and self._is_audiobook_folder(expanded_target)
        self.current_target = expanded_target
        self.current_is_audiobook = is_audiobook
        self.current_audiobook_file = self._audiobook_marker_path(expanded_target) if is_audiobook else None

        # For audiobooks, ensure marker exists
        if is_audiobook:
            self.ensure_audiobook_marker(expanded_target)

        with self._lock:
            print("Starting playback")
            self._command("loadfile", play_arg, "replace")

            # Audiobook resume logic
            if is_audiobook:
                folder = expanded_target
                if force_restart_audiobook:
                    self.reset_audiobook_state(folder)

                state = self.read_audiobook_state(folder)
                rel = state.get("current_file") or ""
                resume_time = float(state.get("time_pos") or 0.0)

                wanted_full = None
                if rel:
                    wanted_full = os.path.normpath(os.path.join(folder, rel))

                if wanted_full:
                    self._try_restore_playlist_entry(wanted_full)

                if resume_time > 0.5:
                    print(f"Audiobook resume at {resume_time:.1f}s")
                    self._command("seek", resume_time, "absolute", "exact")

                self._set_property("pause", False)
                return

            # Non-audiobook: keep old cache resume behavior
            if target_raw == self.playback_cache.get("target") or expanded_target == self.playback_cache.get("target"):
                resume_time = float(self.playback_cache.get("time_pos") or 0.0)
                cached_file = self.playback_cache.get("file")
                if cached_file:
                    self._try_restore_playlist_entry(cached_file)
                if resume_time > 0.5:
                    print(f"Resuming at {resume_time:.1f}s")
                    self._command("seek", resume_time, "absolute", "exact")

            self._set_property("pause", False)
            self.playback_cache["target"] = expanded_target

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
        """
        Next track with wrap-around:
        - playlist: normal next, wrap to first at end
        - single file: restart from beginning
        """
        with self._lock:
            count = self._get_property("playlist-count")
            pos = self._get_property("playlist-pos")

            try:
                count = int(count) if count is not None else 0
                pos = int(pos) if pos is not None else 0
            except Exception:
                count, pos = 0, 0

            if count <= 1:
                self._command("seek", 0, "absolute", "exact")
                return

            if pos >= count - 1:
                self._set_property("playlist-pos", 0)
                self._command("seek", 0, "absolute", "exact")
                return

            self._command("playlist-next", "force")

    def restart_or_prev(self, threshold_seconds: float = PREV_RESTART_THRESHOLD):
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
        """
        Store playback cache always; also write audiobook.json if audiobook active.
        """
        try:
            time_pos = self._get_property("time-pos")
            path = self._get_property("path")
            if time_pos is None:
                time_pos = 0.0
            self.playback_cache["time_pos"] = float(time_pos or 0.0)
            self.playback_cache["file"] = path
            self.playback_cache["target"] = self.current_target or self.playback_cache.get("target")
            print(
                f"Stored playback: target={self.playback_cache.get('target')}, "
                f"file={path}, time={self.playback_cache['time_pos']:.1f}s"
            )
        except Exception as e:
            print(f"Failed to store playback: {e}")

        # audiobook save
        try:
            self.store_audiobook_progress_if_active()
        except Exception:
            pass

    # -------- finish detection helpers --------

    def is_idle(self) -> bool:
        try:
            v1 = self._get_property("idle-active")
            if v1 is not None:
                return bool(v1)
            v2 = self._get_property("core-idle")
            return bool(v2)
        except Exception:
            return False

    def has_loaded_path(self) -> bool:
        try:
            p = self._get_property("path")
            return bool(p)
        except Exception:
            return False


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
    - Needle down (magnet present): spin + (resume if media loaded)
    - Needle up (magnet lost): stop motor + pause

    Gestures while a record is active:
    - 1 quick lift -> NEXT (fires after DOUBLE_LIFT_WINDOW)
    - 2 quick lifts -> PREVIOUS (fires after DOUBLE_LIFT_WINDOW)
    - 5 quick lifts (audiobook only) -> RESET audiobook progress + restart immediately

    Full stop:
    - Magnet missing >= FULL_STOP_AFTER -> full stop, requires re-scan
    - Playback finished -> full stop + motor stop + requires needle cycle, then RFID scan burst
    """

    def __init__(self, player: MPVController, motor: StepperMotor, rfid: SimpleMFRC522, hall_sensor: DigitalInputDevice):
        self.player = player
        self.motor = motor
        self.rfid = rfid
        self.hall_sensor = hall_sensor

        self.current_rfid = None

        self._magnet_present = None
        self._lift_start_time = None

        # gesture state
        self._quick_lift_count = 0
        self._gesture_first_time = None
        self._gesture_deadline = None  # when to decide between single/double
        self._full_stop_done = False

        # finish lock
        self._require_magnet_cycle = False
        self._saw_magnet_lost_after_finish = False

        # finish detection
        self._next_finish_poll = 0.0
        self._was_playing = False

        # audiobook autosave
        self._next_audiobook_save = 0.0

    def _reset_gesture(self):
        self._quick_lift_count = 0
        self._gesture_first_time = None
        self._gesture_deadline = None

    def _scan_rfid_burst(self, seconds: float):
        deadline = time.time() + seconds
        while time.time() < deadline:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id:
                return str(rfid_id)
            time.sleep(0.05)
        return None

    def _maybe_fire_gesture_action(self, now: float):
        """
        Fire NEXT/PREV when the lift window expires.
        Avoids breaking the 5-lift reset by delaying single/double until window expires.
        """
        if self.current_rfid is None:
            self._reset_gesture()
            return

        if self._gesture_deadline is None or now < self._gesture_deadline:
            return

        # window expired -> decide action
        if self._quick_lift_count == 1:
            print("Gesture: single quick lift -> NEXT track")
            self.player.next_track()
            self.player.resume()

        elif self._quick_lift_count == 2:
            print("Gesture: double quick lift -> PREVIOUS (restart-or-prev)")
            self.player.restart_or_prev(PREV_RESTART_THRESHOLD)
            self.player.resume()

        # 3-4 do nothing
        self._reset_gesture()

    def _trigger_finish_full_stop(self):
        print("Playback finished → FULL STOP (motor stop, needle cycle + re-scan required)")

        # Save progress once at end
        if self.player.current_is_audiobook and self.player.current_target:
            try:
                self.player.store_audiobook_progress_if_active()
                # and reset audiobook.json to beginning (your preference)
                self.player.reset_audiobook_state(self.player.current_target)
                print("Audiobook finished → progress reset to beginning")
            except Exception as e:
                print(f"Audiobook finish handling failed: {e}")

        self.player.stop()
        self.motor.stop()
        self.current_rfid = None
        self._was_playing = False
        self._reset_gesture()

        self._require_magnet_cycle = True
        self._saw_magnet_lost_after_finish = False

    def update(self):
        now = time.time()
        magnet_detected = bool(self.hall_sensor.value)

        # Fire pending single/double if deadline passed
        self._maybe_fire_gesture_action(now)

        # init
        if self._magnet_present is None:
            self._magnet_present = magnet_detected
            if magnet_detected:
                self.motor.start()
            return

        # finish-lock mode
        if self._require_magnet_cycle:
            if not magnet_detected:
                if not self._saw_magnet_lost_after_finish:
                    print("Finish lock: magnet removed (ok). Now put it back to restart via RFID.")
                self._saw_magnet_lost_after_finish = True
                self.motor.stop()
                self.player.pause()
                self._lift_start_time = now if self._lift_start_time is None else self._lift_start_time
            else:
                if self._saw_magnet_lost_after_finish:
                    print("Finish lock cleared: magnet returned. Scanning RFID to restart.")
                    self._require_magnet_cycle = False
                    self._saw_magnet_lost_after_finish = False

                    self.motor.start()

                    rfid_id = self._scan_rfid_burst(RFID_SCAN_BURST_SECONDS)
                    if rfid_id:
                        print(f"RFID after finish: {rfid_id} → play")
                        self.current_rfid = rfid_id
                        self.player.play(rfid_id)
                        self._was_playing = False
                        self._next_audiobook_save = now + AUDIOBOOK_AUTOSAVE_SECONDS
                    else:
                        print("No RFID detected after finish (staying silent, motor stop).")
                        self.current_rfid = None
                        self.motor.stop()

                    self._lift_start_time = None
                    self._magnet_present = magnet_detected
                    return
                else:
                    return

        # Magnet returned
        if magnet_detected and not self._magnet_present:
            lift_duration = 0.0
            if self._lift_start_time is not None:
                lift_duration = now - self._lift_start_time

            print(f"Magnet detected → start (lift duration {lift_duration:.2f}s)")
            self.motor.start()

            # Only resume if a record is active
            if self.current_rfid is not None:
                self.player.resume()

            # Quick lift tracking (gesture)
            if lift_duration <= SHORT_LIFT_MAX and self.current_rfid is not None:
                # Start / continue gesture sequence
                if self._gesture_first_time is None:
                    self._gesture_first_time = now
                    self._quick_lift_count = 0

                # If sequence took too long, reset and start anew
                if (now - self._gesture_first_time) > RESET_GESTURE_MAX_TOTAL:
                    self._reset_gesture()
                    self._gesture_first_time = now

                self._quick_lift_count += 1
                print(f"Quick lift #{self._quick_lift_count}")

                # Audiobook reset: 5 quick lifts
                if self.player.current_is_audiobook and self.player.current_target:
                    if self._quick_lift_count >= RESET_LIFT_COUNT and (now - self._gesture_first_time) <= RESET_GESTURE_MAX_TOTAL:
                        print("Gesture: 5 quick lifts -> RESET AUDIOBOOK + restart")
                        try:
                            self.player.reset_audiobook_state(self.player.current_target)
                            # restart immediately from beginning
                            self.player.play(self.current_rfid, force_restart_audiobook=True)
                            self._was_playing = False
                            self._next_audiobook_save = now + AUDIOBOOK_AUTOSAVE_SECONDS
                        except Exception as e:
                            print(f"Audiobook reset failed: {e}")
                        self._reset_gesture()
                        self._lift_start_time = None
                        self._full_stop_done = False
                        self._magnet_present = magnet_detected
                        return

                # Delay single/double decision until no further lifts occur
                self._gesture_deadline = now + DOUBLE_LIFT_WINDOW

            elif lift_duration >= LONG_LIFT_MIN:
                self._reset_gesture()
            else:
                self._reset_gesture()

            self._lift_start_time = None
            self._full_stop_done = False

        # Magnet lost
        elif (not magnet_detected) and self._magnet_present:
            print("Magnet lost → stop motor + pause")
            self.motor.stop()
            self.player.pause()

            # For audiobooks, save immediately on lift
            if self.player.current_is_audiobook:
                self.player.store_audiobook_progress_if_active()

            self._lift_start_time = now
            self._full_stop_done = False
            self._was_playing = False

        self._magnet_present = magnet_detected

        # Full stop if magnet missing too long
        if (not magnet_detected) and self._lift_start_time is not None and (not self._full_stop_done):
            if (now - self._lift_start_time) >= FULL_STOP_AFTER:
                print("Magnet missing for 20 minutes → FULL STOP (re-scan required)")
                self.player.stop()
                self.current_rfid = None
                self._was_playing = False
                self._reset_gesture()
                self._full_stop_done = True

        # Audiobook autosave every 60s while playing and needle down
        if magnet_detected and self.current_rfid is not None and self.player.current_is_audiobook:
            if now >= self._next_audiobook_save:
                self.player.store_audiobook_progress_if_active()
                self._next_audiobook_save = now + AUDIOBOOK_AUTOSAVE_SECONDS

        # Reliable finish detection: playing -> idle transition while magnet present
        if magnet_detected and self.current_rfid is not None and now >= self._next_finish_poll:
            self._next_finish_poll = now + MPV_FINISH_POLL_INTERVAL

            idle = self.player.is_idle()
            loaded = self.player.has_loaded_path()

            if (not idle) and loaded:
                self._was_playing = True

            if self._was_playing and idle:
                self._trigger_finish_full_stop()
                return

        # RFID handling while spinning
        if magnet_detected:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and str(rfid_id) != str(self.current_rfid):
                print(f"RFID changed: {rfid_id}")
                self.current_rfid = str(rfid_id)
                self._was_playing = False
                self._reset_gesture()
                self.player.play(str(rfid_id))
                self._next_audiobook_save = now + AUDIOBOOK_AUTOSAVE_SECONDS


def main():
    print("Starting Record Player (LOCAL FILES via mpv) + Hall Gestures + Audiobooks")
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