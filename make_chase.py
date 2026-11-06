#!/usr/bin/env python3
"""Replaces the cue list with a looping blue and green chase.

Fixtures fade to green one after another, then fade back to blue in the same
order. Art-Net settings and fixture names are kept. The previous show is saved
as show.backup.json.

Stop the controller before running this, and start it again afterwards:
  python3 make_chase.py          (10 seconds per fixture)
  python3 make_chase.py 1.67     (about 10 seconds per sweep of six fixtures)
  python3 make_chase.py 10 /path/to/show.json
"""

import json
import os
import shutil
import sys

BLUE = [0, 0, 255]
GREEN = [0, 255, 0]
MAX_FADE = 100


def fade_time():
    if len(sys.argv) == 1:
        return 10
    try:
        fade = float(sys.argv[1])
    except ValueError:
        fade = -1
    if not 0 <= fade <= MAX_FADE:
        sys.exit(f"The fade time must be a number from 0 to {MAX_FADE}.")
    return fade


def build_cues(fixtures, fade):
    cues = []
    colours = [BLUE] * len(fixtures)
    for colour, label in ((GREEN, "green"), (BLUE, "blue")):
        for index, fixture in enumerate(fixtures):
            colours = list(colours)
            colours[index] = colour
            cues.append({
                "id": len(cues) + 1,
                "name": f"{fixture['name'][:28]} to {label}",
                "fade": fade,
                "next": 0,
                "levels": [list(level) for level in colours],
            })
    return cues


def main():
    fade = fade_time()
    folder = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(folder, "show.json")
    if not os.path.exists(path):
        sys.exit(f"Could not find {path}. Start the controller once to create it.")
    with open(path, encoding="utf-8") as handle:
        show = json.load(handle)

    backup = os.path.join(os.path.dirname(path), "show.backup.json")
    shutil.copy2(path, backup)

    show["cues"] = build_cues(show["fixtures"], fade)
    show["loop"] = True
    if "effect" in show:
        show["effect"]["running"] = False  # Otherwise the effect would resume at boot instead.
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(show, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    details = os.stat(path)
    os.chown(temporary, details.st_uid, details.st_gid)
    os.replace(temporary, path)
    print(f"Loaded {len(show['cues'])} cues with {fade:g} second fades. Previous show saved as {backup}.")


if __name__ == "__main__":
    main()
