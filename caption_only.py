"""Napiše caption za video(e) iz Drive foldera. NIŠTA NE OBJAVLJUJE.

CAPTION_FOR="AnelVideo8"                      -> jedan video čije ime sadrži taj tekst
CAPTION_FOR="all -Anel2Final -AnelVideo8"     -> svi videi osim onih čije ime sadrži -tekst
"""

import os
import shutil
import tempfile
import traceback
from pathlib import Path

import agent

spec = os.environ["CAPTION_FOR"].strip()
words = spec.split()
batch = words[0].lower() == "all"
skip = [w[1:].lower() for w in words[1:] if w.startswith("-")] if batch else []
wanted = spec.lower()

session = agent.drive_session()
targets = []
for idx, folder in enumerate(agent.DRIVE_FOLDERS):
    for f in agent.list_videos(session, folder):
        name = f["name"].lower()
        print(f"folder {idx + 1}: {f['name']}")
        if batch:
            if not any(s in name for s in skip):
                targets.append(f)
        elif wanted in name and not targets:
            targets.append(f)
if not targets:
    raise SystemExit(f"Nema videa za '{spec}'")

for f in targets:
    work = Path(tempfile.mkdtemp())
    try:
        src = work / ("src" + Path(f["name"]).suffix.lower())
        agent.download(session, f["id"], src)
        caption = agent.write_caption(agent.extract_frames(src, work), f["name"])
        print(f"\n===== CAPTION ZA {f['name']} =====\n{caption}\n===== KRAJ =====", flush=True)
    except Exception:
        print(f"\n===== GREŠKA ZA {f['name']} =====\n{traceback.format_exc()}\n===== KRAJ =====", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
