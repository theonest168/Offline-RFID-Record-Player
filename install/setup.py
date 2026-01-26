import json
import os
from mfrc522 import SimpleMFRC522

RFID_FILE = "rfid.json"

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
PLAYLIST_EXTS = {".m3u", ".m3u8"}


def read_rfid_file():
    rfid_map = {}
    try:
        with open(RFID_FILE, "r") as f:
            rfid_map = json.load(f)
    except FileNotFoundError:
        print("rfid.json not found, a new one will be created.")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    return rfid_map


def write_rfid_file(rfid_map):
    with open(RFID_FILE, "w") as json_file:
        json.dump(rfid_map, json_file, indent=4)


def _looks_like_audio_target(path: str) -> bool:
    p = os.path.expanduser(path.strip())
    if not p:
        return False
    if os.path.isdir(p):
        return True
    if os.path.isfile(p):
        ext = os.path.splitext(p)[1].lower()
        return ext in AUDIO_EXTS or ext in PLAYLIST_EXTS
    return False


def write_rfid_tags():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    print("\nRFID mapping mode (LOCAL FILES)")
    print("Map an RFID tag to ONE of:")
    print(" - a folder containing audio files")
    print(" - a single audio file (.mp3/.flac/...)")
    print(" - a playlist file (.m3u/.m3u8)\n")

    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())

        print(f"RFID ID {rfid_id} detected.")
        target = input("Enter local path (folder/file/.m3u): ").strip()

        expanded = os.path.expanduser(target)
        if not _looks_like_audio_target(expanded):
            print("Invalid path.")
            print("It must be an existing folder, an existing audio file, or an existing .m3u/.m3u8 playlist.\n")
        else:
            rfid_map[rfid_id] = expanded
            write_rfid_file(rfid_map)
            print(f"Stored mapping: {rfid_id} -> {expanded}\n")

        print("Please choose an option:")
        print("1. Add another RFID tag")
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
            print(f"RFID ID {rfid_id} is mapped to: {rfid_map.get(rfid_id)}")
        else:
            print(f"RFID ID {rfid_id} is not configured.")

        print("Please choose an option:")
        print("1. Read another RFID tag")
        print("2. Exit")
        choice = input("Enter your choice: ").strip()
        if choice == "2":
            break


def get_user_choice():
    actions = {
        "1": ("Write RFID tags (local paths)", write_rfid_tags),
        "2": ("Read RFID tags", read_rfid_tags),
    }
    exit_key = str(len(actions) + 1)

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

        label, func = action
        print(f"You selected: {label}")
        func()


if __name__ == "__main__":
    get_user_choice()