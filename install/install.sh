#!/bin/bash
set -e

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 is not installed. Please install it first."
    exit 1
fi

echo "Installing system dependencies"
sudo apt-get update -qq > /dev/null

# GPIO + RFID dependencies (existing)
sudo apt-get install -y swig liblgpio-dev > /dev/null

# Local audio player (mpv) + bluetooth audio stack
# Notes:
# - mpv is the playback engine
# - bluez is bluetooth
# - pulseaudio + module-bluetooth enables A2DP sink on many Raspberry Pi OS images
sudo apt-get install -y mpv bluez pulseaudio pulseaudio-module-bluetooth > /dev/null

echo "Creating python virtual environment"
python3 -m venv venv
source venv/bin/activate

echo "Installing python dependencies"
pip install -r install/requirements.txt -qq > /dev/null

echo "Enabling SPI Interface"
sudo sed -i 's/^dtparam=spi=.*/dtparam=spi=on/' /boot/config.txt
sudo sed -i 's/^#dtparam=spi=.*/dtparam=spi=on/' /boot/config.txt
sudo raspi-config nonint do_spi 0

read -p "Would you like to restart your Raspberry Pi now? [Y/N] " userInput
userInput="${userInput^^}"

if [[ "${userInput,,}" == "y" ]]; then
    echo "You entered 'Y', rebooting now..."
    sleep 2
    sudo reboot now
elif [[ "${userInput,,}" == "n" ]]; then
    echo "Please restart your Raspberry Pi later to apply changes by running 'sudo reboot now'."
    exit
else
    echo "Unknown input, please restart your Raspberry Pi later to apply changes by running 'sudo reboot now'."
    sleep 1
fi