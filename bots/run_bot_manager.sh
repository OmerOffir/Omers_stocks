#!/bin/zsh
# or use #!/bin/bash if your default shell is bash

# go to your repo root
cd "/Users/$(whoami)/PythonProjects/Omers_stocks" || exit 1

export PYTHONPATH="$PWD"
"./.venv/bin/python" -u -m bots.bot_meneger
