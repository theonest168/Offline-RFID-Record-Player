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
# Gesture timing (seconds)
# ==========================================================
SHORT_LIFT_MAX = 0.6
DOUBLE_LIFT_WINDOW = 0.8
LONG_LIFT_MIN = 1.5

# Previous behavior window
PREV_RESTART_THRESHOLD = 5.0
# ==========================================================


def _list_audio_files(folder: str):
    files = []
    for root, _, names in os.walk(folder):
        for name in names:
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                files.append(os.path.join(root, name))
    files.sort(key=lambda p: p.lower())
    return files


def _write_m3u(paths, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(p + "\n")


# ==========================================================
# MPV CONTROLLER
# ==========================================================

class MPVController:
    def __init__(self):
        self.rfid_map = self._load_rfid_map()
        self.playback_cache = {"target": None, "file": None, "time_pos": 0.0}
        self._lock = threading.Lock()
        self._ensure_mpv()

    def _load_rfid_map(self):
        try:
            with open(RFID_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _ensure_mpv(self):
        if os.path.exists(MPV_SOCKET):
            os.remove(MPV_SOCKET)

        subprocess.Popen(
            [
                "mpv",
                "--no-video",
                "--idle=yes",
                f"--input-ipc-server={MPV_SOCKET}",
                "--terminal=no",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(50):
            if os.path.exists(MPV_SOCKET):
                return
            time.sleep(0.05)

        raise RuntimeError("mpv IPC socket not created")

    def _send(self, cmd):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(MPV_SOCKET)
            s.sendall((json.dumps(cmd) + "\n").encode())
            data = s.recv(4096)
            return json.loads(data.decode()) if data else None

    def _cmd(self, *args):
        return self._send({"command": list(args)})

    def _get(self, prop):
        r = self._send({"command": ["get_property", prop]})
        return r.get("data") if r and r.get("error") == "success" else None

    def play(self, rfid_id):
        target = self.rfid_map.get(str(rfid_id))
        if not target:
            return

        if os.path.isdir(target):
            files = _list_audio_files(target)
            if not files:
                return
            _write_m3u(files, MPV_PLAYLIST_TMP)
            target = MPV_PLAYLIST_TMP

        with self._lock:
            self._cmd("loadfile", target, "replace")
            self._cmd("set_property", "pause", False)
            self.playback_cache["target"] = target

    def pause(self):
        with self._lock:
            self.store_playback()
            self._cmd("set_property", "pause", True)

    def resume(self):
        with self._lock:
            self._cmd("set_property", "pause", False)

    def next_track(self):
        count = self._get("playlist-count") or 1
        pos = self._get("playlist-pos") or 0

        if count <= 1 or pos >= count - 1:
            self._cmd("seek", 0, "absolute", "exact")
        else:
            self._cmd("playlist-next", "force")

    def restart_or_prev(self):
        pos = float(self._get("time-pos") or 0)
        if pos > PREV_RESTART_THRESHOLD:
            self._cmd("seek", 0, "absolute", "exact")
        else:
            self._cmd("playlist-prev", "force")

    def store_playback(self):
        self.playback_cache["time_pos"] = float(self._get("time-pos") or 0)
        self.playback_cache["file"] = self._get("path")


# ==========================================================
# STEPPER MOTOR
# ==========================================================

class StepperMotor:
    STEP_SEQUENCE = [
        [1,0,0,1],[1,0,0,0],[1,1,0,0],[0,1,0,0],
        [0,1,1,0],[0,0,1,0],[0,0,1,1],[0,0,0,1]
    ]
    STEP_DELAY = 0.002

    def __init__(self):
        self.pins = [DigitalOutputDevice(p) for p in STEPPER_PINS]
        self.running = False

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self.running = False

    def _run(self):
        while self.running:
            for step in self.STEP_SEQUENCE:
                for pin, v in zip(self.pins, step):
                    pin.value = v
                time.sleep(self.STEP_DELAY)
        for p in self.pins:
            p.off()


# ==========================================================
# RECORD PLAYER (GESTURES)
# ==========================================================

class RecordPlayer:
    def __init__(self, player, motor, rfid, hall):
        self.player = player
        self.motor = motor
        self.rfid = rfid
        self.hall = hall

        self.current_rfid = None
        self.magnet_present = None
        self.lift_start = None

        self.short_lifts = 0
        self.pending_single_deadline = None

    def reset_gesture(self):
        self.short_lifts = 0
        self.pending_single_deadline = None

    def update(self):
        now = time.time()
        magnet = bool(self.hall.value)

        # Fire delayed NEXT (single lift)
        if self.pending_single_deadline and now >= self.pending_single_deadline:
            if self.short_lifts == 1:
                self.player.next_track()
            self.reset_gesture()

        if self.magnet_present is None:
            self.magnet_present = magnet
            return

        # Magnet returned
        if magnet and not self.magnet_present:
            lift_time = now - self.lift_start if self.lift_start else 0
            self.motor.start()
            self.player.resume()

            if lift_time <= SHORT_LIFT_MAX:
                self.short_lifts += 1
                if self.short_lifts == 1:
                    self.pending_single_deadline = now + DOUBLE_LIFT_WINDOW
                else:
                    self.player.restart_or_prev()
                    self.reset_gesture()
            else:
                self.reset_gesture()

        # Magnet removed
        elif not magnet and self.magnet_present:
            self.motor.stop()
            self.player.pause()
            self.lift_start = now

        self.magnet_present = magnet

        # RFID change while spinning
        if magnet:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and str(rfid_id) != str(self.current_rfid):
                self.current_rfid = rfid_id
                self.reset_gesture()
                self.player.play(str(rfid_id))


# ==========================================================
# MAIN
# ==========================================================

def main():
    print("Starting RFID Record Player (gesture-stable)")
    rp = RecordPlayer(
        MPVController(),
        StepperMotor(),
        SimpleMFRC522(),
        DigitalInputDevice(HALL_SENSOR_PIN, pull_up=True, pin_factory=LGPIOFactory()),
    )

    try:
        while True:
            rp.update()
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()