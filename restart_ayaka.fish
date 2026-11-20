#!/usr/bin/fish
set -l root (dirname (status filename))
exec "$root/venv/bin/python" -B "$root/ayakactl.py" restart $argv
