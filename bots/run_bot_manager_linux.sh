#!/bin/zsh
# or use #!/bin/bash if your default shell is bash

# go to your repo root
cd "/home/$(whoami)/PythonProjects/Omers_stocks" || exit 1

# Try python, if fails then try python3
if command -v python &>/dev/null; then
    python -u -m bots.bot_meneger || python3 -u -m bots.bot_meneger
elif command -v python3 &>/dev/null; then
    python3 -u -m bots.bot_meneger
else
    echo "Error: Neither python nor python3 found on this system."
    exit 1
fi