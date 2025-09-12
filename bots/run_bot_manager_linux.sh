#!/bin/zsh
# or use #!/bin/bash if your default shell is bash

# go to your repo root
cd "/home/$(whoami)/PythonProjects/Omers_stocks" || exit 1

python -u -m bots.bot_meneger
