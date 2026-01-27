import json
import os
from datetime import datetime

from mfrc522 import SimpleMFRC522

RFID_FILE = "rfid.json"
AUDIOBOOK_MARKER = "audiobook.json"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}


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


def read_rfid_file():
    try:
        with open(RFID_FILE, "r") as file:
            return json.load(file)
    except FileNotFoundError:
        print("RFID map not found, creating a new one.")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    return {}


def write_rfid_file(rfid_map):
    with open(RFID_FILE, "w") as json_file:
        json.dump(rfid_map, json_file, indent=4)


def _ensure_audiobook_marker(folder: str):
    marker = os.path.join(folder, AUDIOBOOK_MARKER)
    if os.path.exists(marker):
        print(f"'{AUDIOBOOK_MARKER}' already exists (not overwriting).")
        return

    files = _list_audio_files(folder)
    if not files:
        print("No audio files found in this folder; not creating audiobook marker.")
        return

    rel_first = os.path.relpath(files[0], folder)
    data = {
        "type": "audiobook",
        "version": 1,
        "current_file": rel_first,
        "time_pos": 0.0,
        "updated_at": _now_iso(),
    }
    tmp = marker + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, marker)
    print(f"Created '{AUDIOBOOK_MARKER}' in folder (initialized to first track).")


def write_rfid_tags_local():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())

        target = input("Enter local path (file, playlist, or folder): ").strip()
        if not target:
            print("Empty path, try again.")
            continue

        target_exp = os.path.expanduser(target)
        if not os.path.exists(target_exp):
            print("Path does not exist.")
            continue

        rfid_map[rfid_id] = target
        write_rfid_file(rfid_map)
        print(f"Stored local path for RFID ID {rfid_id}")

        print("1. Add another RFID tag")
        print("2. Exit")
        choice = input("Enter your choice: ").strip()
        if choice == "2":
            break


def write_rfid_tags_audiobooks():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())

        folder = input("Enter audiobook folder path (must contain audio files): ").strip()
        if not folder:
            print("Empty path, try again.")
            continue

        folder_exp = os.path.expanduser(folder)
        if not os.path.isdir(folder_exp):
            print("That is not a folder.")
            continue

        files = _list_audio_files(folder_exp)
        if not files:
            print("No supported audio files found in this folder.")
            continue

        # map RFID -> folder
        rfid_map[rfid_id] = folder
        write_rfid_file(rfid_map)
        print(f"Stored audiobook folder for RFID ID {rfid_id}")

        # create marker if missing
        _ensure_audiobook_marker(folder_exp)

        print("1. Add another audiobook RFID tag")
        print("2. Exit")
        choice = input("Enter your choice: ").strip()
        if choice == "2":
            break


def read_rfid_tags():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())

        if rfid_id in rfid_map:
            print(f"RFID ID {rfid_id} is mapped to {rfid_map.get(rfid_id)}.")
        else:
            print(f"RFID ID {rfid_id} is not configured.")

        print("1. Read another RFID tag")
        print("2. Exit")
        choice = input("Enter your choice: ").strip()
        if choice == "2":
            break


def get_user_choice():
    actions = {
        "1": ("Write RFID tags (local paths)", write_rfid_tags_local),
        "2": ("Write RFID tags for audiobooks (local paths)", write_rfid_tags_audiobooks),
        "3": ("Read RFID tags", read_rfid_tags),
    }
    exit_key = "4"

    while True:
        print("\nPlease choose an option:")
        for key, (label, _) in actions.items():
            print(f"{key}. {label}")
        print(f"{exit_key}. Exit")

        choice = input("Enter your choice: ").strip()
        if choice == exit_key:
            print("Exiting the program.")
            break

        action = actions.get(choice)
        if not action:
            print("Invalid choice. Please try again.")
            continue

        _, func = action
        func()


if __name__ == "__main__":
    get_user_choice()