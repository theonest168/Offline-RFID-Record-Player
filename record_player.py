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
    - pause/resume is reliable
    - can seek to resume position
    - audio output goes to system default (Bluetooth speaker if configured as default sink)
    """

    def __init__(self):
        self.rfid_map = self._load_rfid_map()
        self.playback_cache = {
            # last "target" can be a folder/file/playlist path (string)
            "target": None,
            # resume info
            "file": None,
            "time_pos": 0.0,
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

    def reload_rfid_map(self):
        # optional helper if you edit rfid.json while running
        self.rfid_map = self._load_rfid_map()

    def _ensure_mpv(self):
        # Clean up old socket
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

        # Wait briefly for socket
        for _ in range(50):
            if os.path.exists(MPV_SOCKET):
                return
            time.sleep(0.05)

        raise RuntimeError("mpv IPC socket did not appear. Is mpv installed and runnable?")

    def _send(self, command_obj, expect_reply=True, timeout=1.0):
        """
        Send a JSON command to mpv IPC and optionally wait for a reply.
        """
        payload = (json.dumps(command_obj) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(MPV_SOCKET)
            s.sendall(payload)
            if not expect_reply:
                return None
            data = b""
            # mpv replies with one JSON line per command
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

    def _get_property(self, prop):
        resp = self._send({"command": ["get_property", prop]})
        if resp and resp.get("error") == "success":
            return resp.get("data")
        return None

    def _set_property(self, prop, value):
        return self._send({"command": ["set_property", prop, value]}, expect_reply=True)

    def _command(self, *args):
        return self._send({"command": list(args)}, expect_reply=True)

    def _resolve_target_to_play_arg(self, target_path: str):
        """
        Returns a playable argument for mpv:
        - If folder: create a temp m3u playlist of all audio files
        - If .m3u/.m3u8: use as-is
        - If file: use as-is
        """
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
            # Load new file/playlist
            self._command("loadfile", play_arg, "replace")

            # Resume if same target and we have cached time
            if target == self.playback_cache.get("target"):
                resume_time = float(self.playback_cache.get("time_pos") or 0.0)
                cached_file = self.playback_cache.get("file")

                # If it’s a playlist, try to restore to the same file first
                if cached_file:
                    # mpv playlist entries are file paths; try to jump to it if it exists
                    # Note: "playlist-play-index" approach is more complex; we do a simple search by cycling.
                    # For small playlists this is fine.
                    self._try_restore_playlist_entry(cached_file)

                if resume_time > 0.5:
                    print(f"Resuming at {resume_time:.1f}s")
                    self._command("seek", resume_time, "absolute", "exact")

            # Ensure unpaused
            self._set_property("pause", False)

            # Update cache target
            self.playback_cache["target"] = target

    def _try_restore_playlist_entry(self, wanted_path: str, max_steps=200):
        """
        Best-effort: move through playlist until current path matches wanted_path.
        (Only used for resume; harmless if it fails.)
        """
        wanted_path = os.path.expanduser(wanted_path)
        cur = self._get_property("path")
        if cur == wanted_path:
            return True

        # Try a limited number of steps forward
        for _ in range(max_steps):
            self._command("playlist-next", "force")
            cur = self._get_property("path")
            if cur == wanted_path:
                return True
        return False

    def pause(self):
        with self._lock:
            self._set_property("pause", True)
            self.store_playback()

    def resume(self):
        with self._lock:
            self._set_property("pause", False)

    def toggle_pause(self):
        with self._lock:
            paused = self._get_property("pause")
            self._set_property("pause", not bool(paused))
            if bool(paused) is False:
                # we were playing, now paused -> store
                self.store_playback()

    def stop(self):
        with self._lock:
            # Stop playback and go idle
            self.store_playback()
            self._command("stop")

    def next_track(self):
        with self._lock:
            self._command("playlist-next", "force")

    def prev_track(self):
        with self._lock:
            self._command("playlist-prev", "force")

    def store_playback(self):
        """
        Cache current playback state so we can resume after pausing (magnet lost).
        """
        try:
            time_pos = self._get_property("time-pos")
            path = self._get_property("path")
            if time_pos is None:
                time_pos = 0.0

            self.playback_cache["time_pos"] = float(time_pos or 0.0)
            self.playback_cache["file"] = path
            # target is kept as-is (the mapping key) by play()
            print(f"Stored playback: target={self.playback_cache.get('target')}, file={path}, time={self.playback_cache['time_pos']:.1f}s")
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
    def __init__(self, player, motor, rfid, hall_sensor):
        self.player = player
        self.motor = motor
        self.rfid = rfid
        self.hall_sensor = hall_sensor

        self.current_rfid = None
        self.spinning = False

    def update(self):
        magnet_detected = self.hall_sensor.value

        if magnet_detected and not self.spinning:
            print("Magnet detected → start")
            self.spinning = True
            self.motor.start()

        elif not magnet_detected and self.spinning:
            print("Magnet lost → stop (pause)")
            self.spinning = False
            self.current_rfid = None
            self.motor.stop()
            self.player.pause()

        if self.spinning:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and rfid_id != self.current_rfid:
                print(f"RFID changed: {rfid_id}")
                self.current_rfid = rfid_id
                self.player.play(str(rfid_id))


def main():
    print("Starting Record Player (LOCAL FILES via mpv)")
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
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        motor.stop()
        GPIO.cleanup()


if __name__ == "__main__":
    main()