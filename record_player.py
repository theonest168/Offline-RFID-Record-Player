import json
import os
import sys
import threading
import time
import RPi.GPIO as GPIO

from gpiozero import DigitalInputDevice, DigitalOutputDevice
from gpiozero.pins.lgpio import LGPIOFactory
from mfrc522 import SimpleMFRC522

from mpd import MPDClient

HALL_SENSOR_PIN = 17
STEPPER_PINS = [14, 15, 18, 23]
RFID_FILE = "rfid.json"
PLAYBACK_CACHE_FILE = "playback_cache.json"


class MPDController:
    """
    Controls local audio playback via MPD.
    Supports play/pause/resume + per-RFID resume (stores track + elapsed seconds).
    """
    def __init__(self, host="localhost", port=6600):
        self.host = host
        self.port = port
        self.client = MPDClient()
        self.client.timeout = 10
        self.client.idletimeout = None

        self.init_mpd_client()
        self.init_rfid_map()
        self.playback_cache = self._load_cache()  # per RFID

    def init_mpd_client(self):
        try:
            self.client.connect(self.host, self.port)
        except Exception as e:
            print(f"Error: could not connect to MPD at {self.host}:{self.port} -> {e}")
            print("Make sure MPD is installed/running: sudo systemctl status mpd")
            sys.exit(1)

    def init_rfid_map(self):
        rfid_map = {}
        try:
            with open(RFID_FILE, "r") as f:
                rfid_map = json.load(f)
            if not rfid_map:
                print("Warning: RFID map is empty.")
        except FileNotFoundError:
            print(f"Warning: RFID map file {RFID_FILE} not found.")
        except json.JSONDecodeError as e:
            print(f"Error decoding JSON: {e}")
        self.rfid_map = rfid_map

    def _load_cache(self):
        try:
            with open(PLAYBACK_CACHE_FILE, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            return {}

    def _save_cache(self):
        try:
            with open(PLAYBACK_CACHE_FILE, "w") as f:
                json.dump(self.playback_cache, f, indent=2)
        except Exception as e:
            print(f"Warning: failed to save cache: {e}")

    def _status(self):
        try:
            return self.client.status()
        except Exception:
            # reconnect once
            self._reconnect()
            return self.client.status()

    def _current_song(self):
        try:
            return self.client.currentsong()
        except Exception:
            self._reconnect()
            return self.client.currentsong()

    def _reconnect(self):
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.client.connect(self.host, self.port)

    def _clear_and_add(self, target_path: str):
        """
        target_path is relative to MPD music_directory, e.g. 'audiobooks/Book1' or 'music/song.mp3'
        MPD can 'add' both folders and files.
        """
        self.client.clear()
        self.client.add(target_path)

    def play(self, rfid_id: int):
        print("Starting playback (MPD)")
        target = self.rfid_map.get(str(rfid_id))
        if not target:
            print(f"No audio path mapped to RFID {rfid_id}")
            return

        # Load resume info for this RFID, if any
        resume = self.playback_cache.get(str(rfid_id), {})
        resume_song = resume.get("file")          # MPD 'file' field (relative path)
        resume_elapsed = resume.get("elapsed", 0) # seconds
        resume_queue_seed = resume.get("seed")    # the folder/file we originally queued

        try:
            # Always (re)build queue for the selected RFID.
            # This keeps behavior consistent when switching tags.
            self._clear_and_add(target)
            self.client.play()

            # If we previously played this same target, try to seek to previous position.
            # Works reliably for single-file targets; for folders, we try to seek to saved track if it still exists in queue.
            if resume_queue_seed == target and resume_song:
                # Find song position in current playlist by matching 'file'
                playlist = self.client.playlistinfo()
                match_pos = None
                for item in playlist:
                    if item.get("file") == resume_song:
                        match_pos = item.get("pos")
                        break

                if match_pos is not None:
                    print(f"Resuming RFID {rfid_id} at {resume_elapsed:.1f}s in {resume_song}")
                    self.client.play(int(match_pos))
                    # seekcur exists on newer MPD; fallback to seek(pos, seconds)
                    try:
                        self.client.seekcur(float(resume_elapsed))
                    except Exception:
                        self.client.seek(int(match_pos), float(resume_elapsed))
                else:
                    # If song isn't found, at least try seeking current track
                    if resume_elapsed and resume_elapsed > 0:
                        try:
                            self.client.seekcur(float(resume_elapsed))
                        except Exception:
                            pass

        except Exception as e:
            print(f"Failed to start playback: {e}")

    def pause(self, current_rfid: int | None):
        # Pause playback and store position for the current RFID (if provided)
        try:
            self.client.pause(1)
        except Exception as e:
            print(f"Failed to pause: {e}")
        if current_rfid is not None:
            self.store_playback(current_rfid)

    def resume(self):
        try:
            self.client.pause(0)
        except Exception as e:
            print(f"Failed to resume: {e}")

    def store_playback(self, current_rfid: int):
        """
        Store the current track file + elapsed seconds so we can resume next time.
        """
        try:
            st = self._status()
            song = self._current_song()
            elapsed = float(st.get("elapsed", 0.0))
            file_rel = song.get("file")

            seed = self.rfid_map.get(str(current_rfid))  # what was queued for this RFID

            self.playback_cache[str(current_rfid)] = {
                "seed": seed,
                "file": file_rel,
                "elapsed": elapsed,
            }
            self._save_cache()
            print(f"Stored playback for RFID {current_rfid}: {self.playback_cache[str(current_rfid)]}")
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
    def __init__(self, audio, motor, rfid, hall_sensor):
        self.audio = audio
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
            print("Magnet lost → stop")
            self.spinning = False
            # pause and store progress for the current RFID
            self.audio.pause(self.current_rfid)
            self.current_rfid = None
            self.motor.stop()

        if self.spinning:
            rfid_id = self.rfid.read_id_no_block()
            if rfid_id and rfid_id != self.current_rfid:
                print(f"RFID changed: {rfid_id}")
                self.current_rfid = rfid_id
                self.audio.play(rfid_id)


def main():
    print("Starting Record Player (local MP3 via MPD)")
    audio = MPDController()
    motor = StepperMotor()
    rfid = SimpleMFRC522()
    hall_sensor = DigitalInputDevice(HALL_SENSOR_PIN, pull_up=True, pin_factory=LGPIOFactory())

    player = RecordPlayer(
        audio=audio,
        motor=motor,
        rfid=rfid,
        hall_sensor=hall_sensor,
    )

    try:
        while True:
            player.update()
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Shutting down...")
    finally:
        motor.stop()
        GPIO.cleanup()


if __name__ == "__main__":
    main()