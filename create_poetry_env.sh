#!/bin/bash

# Remove existing python env
# Uncomment this if you want to rebuild the python env from scratch
# poetry env remove python3

# List env path
# poetry env info --path

# Example of how to send data to systemd log
# <python start cmd> | systemd-cat -t app-name

python3 -m poetry lock
python3 -m poetry install
