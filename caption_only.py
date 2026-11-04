"""Napiše caption za jedan video po imenu (iz Drive foldera). NIŠTA NE OBJAVLJUJE."""

import os
import shutil
import tempfile
from pathlib import Path

import agent

wanted = os.environ["CAPTION_FOR"].strip().lower()
session = agent.drive_session()
match = None
for idx, folder in enumerate(agent.DRIVE_FOLDERS):
    for f in agent.list_videos(session, folder):
        print(f"folder {idx + 1}: {f['name']}")
        if match is None and wanted in f["name"].lower():
            match = f
if not match:
    raise SystemExit(f"Nema videa čije ime sadrži '{wanted}'")

work = Path(tempfile.mkdtemp())
try:
    src = work / ("src" + Path(match["name"]).suffix.lower())
    agent.download(session, match["id"], src)
    caption = agent.write_caption(agent.extract_frames(src, work), match["name"])
    print(f"\n===== CAPTION ZA {match['name']} =====\n{caption}\n===== KRAJ =====")
finally:
    shutil.rmtree(work, ignore_errors=True)
