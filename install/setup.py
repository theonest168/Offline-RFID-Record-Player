import json
import os
from pathlib import Path

from mfrc522 import SimpleMFRC522

RFID_FILE = "rfid.json"

# IMPORTANT:
# These paths MUST be relative to MPD's "music_directory"
# Usually that's /home/pi/Music
DEFAULT_MUSIC_DIR = "/home/pi/Music"


def read_rfid_file():
    rfid_map = {}
    try:
        with open(RFID_FILE, "r") as file:
            rfid_map = json.load(file)
    except FileNotFoundError:
        print("RFID map file not found, will create a new one when you save.")
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON: {e}")
    return rfid_map


def write_rfid_file(rfid_map):
    with open(RFID_FILE, "w") as json_file:
        json.dump(rfid_map, json_file, indent=4)


def _music_dir():
    # Allow override via env var if you want:
    # export MUSIC_DIR=/somewhere
    return os.getenv("MUSIC_DIR", DEFAULT_MUSIC_DIR)


def validate_target_path(target: str) -> bool:
    """
    Validate that the target exists under the MPD music directory.
    target must be RELATIVE (no leading slash).
    It can be either a file (.mp3, .wav, etc.) or a folder.
    """
    target = target.strip()
    if not target:
        return False
    if target.startswith("/"):
        print("Please enter a RELATIVE path (no leading /). Example: audiobooks/Book1")
        return False
    if ".." in Path(target).parts:
        print("Path may not contain '..'")
        return False

    abs_path = Path(_music_dir()) / target
    if not abs_path.exists():
        print(f"Not found: {abs_path}")
        return False
    return True


def list_music_library(limit=200):
    """
    Lists a sample of files/folders under MUSIC_DIR to help users pick paths.
    """
    base = Path(_music_dir())
    if not base.exists():
        print(f"Music directory does not exist: {base}")
        print("Set MUSIC_DIR env var or edit DEFAULT_MUSIC_DIR in this script.")
        return

    print(f"\nListing up to {limit} items under: {base}\n")
    count = 0
    for p in base.rglob("*"):
        if p.is_dir():
            continue
        rel = p.relative_to(base)
        print(rel.as_posix())
        count += 1
        if count >= limit:
            break
    if count == 0:
        print("(No files found)")


def write_rfid_tags():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    print("\nRFID WRITE MODE")
    print(f"MPD music directory is assumed to be: {_music_dir()}")
    print("You will map each RFID tag to a RELATIVE path under that folder.")
    print("Examples:")
    print("  audiobooks/Book1            (folder)")
    print("  music/AlbumX/track01.mp3    (file)\n")

    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())
        print(f"RFID ID {rfid_id} detected.")

        print("\nOptions:")
        print("1) Enter path manually")
        print("2) List library sample")
        choice = input("Choose (1/2): ").strip()

        if choice == "2":
            list_music_library()
            print()

        target = input("Enter folder or file path (relative): ").strip()

        if not validate_target_path(target):
            print("Invalid path. Nothing saved.\n")
        else:
            rfid_map[rfid_id] = target
            write_rfid_file(rfid_map)
            print(f"Stored mapping: {rfid_id} -> {target}\n")

        print("Please choose an option:")
        print("1. Add another RFID tag")
        print("2. Exit")
        if input("Enter your choice: ").strip() == "2":
            break


def read_rfid_tags():
    rfid = SimpleMFRC522()
    rfid_map = read_rfid_file()

    print("\nRFID READ MODE\n")
    while True:
        print("Please scan RFID tag:")
        rfid_id = str(rfid.read_id())

        if rfid_id in rfid_map:
            print(f"RFID ID {rfid_id} is mapped to: {rfid_map.get(rfid_id)}\n")
        else:
            print(f"RFID ID {rfid_id} is not configured.\n")

        print("Please choose an option:")
        print("1. Read another RFID tag")
        print("2. Exit")
        if input("Enter your choice: ").strip() == "2":
            break


def show_config():
    print("\nCONFIG")
    print(f"MUSIC_DIR (MPD music_directory): {_music_dir()}")
    print(f"RFID mapping file: {Path.cwd() / RFID_FILE}")
    print("Tip: You can override MUSIC_DIR like this:")
    print("  export MUSIC_DIR=/home/pi/Music\n")


def get_user_choice():
    actions = {
        "1": ("Show config", show_config),
        "2": ("List music library sample", list_music_library),
        "3": ("Write RFID tags", write_rfid_tags),
        "4": ("Read RFID tags", read_rfid_tags),
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