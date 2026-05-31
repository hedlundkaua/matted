#!/bin/bash

# Load variables from .env file if it exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
    echo "[start_squad] Loaded configuration from .env"
else
    echo "[start_squad] No .env file found. Using system environment variables."
fi

# Run the master orchestrator
# Adjust arguments as needed or pass them through the script
python3 -u master.py "$@"
