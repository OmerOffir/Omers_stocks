#!/bin/zsh
# or use #!/bin/bash if your default shell is bash

# go to your repo root
cd "/Users/$(whoami)/PycharmProjects/Omers_stocks" || exit 1

# ensure this folder is on PYTHONPATH so 'bots' imports work
export PYTHONPATH="$PWD"

# pick the Python you use in VS Code (adjust if needed)
PYTHON_EXE="$(which python3)"

# run as module from the repo root
"$PYTHON_EXE" -u -m bots.bot_meneger
