"""Dijagnostika Instagram uploada. NIŠTA NE OBJAVLJUJE (nikad ne zove media_publish).

Napravi kratki testni video i proba više varijanti kreiranja kontejnera + uploada,
pa ispiše odgovor Instagrama za svaku. Kontejneri koji se ne objave sami isteknu za 24h.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import requests

import agent


def make_test_video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "6", "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", str(path),
        ],
        check=True,
    )


def create_container(version: str, trial: bool) -> dict:
    params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": "diagnostika - ne objavljuje se",
        "access_token": agent.ig_token(),
    }
    if trial:
        params["trial_params"] = json.dumps({"graduation_strategy": "MANUAL"})
    r = requests.post(f"https://graph.facebook.com/{version}/{agent.ig_user()}/media", data=params, timeout=60)
    return {"status": r.status_code, "body": r.json()}


def upload(uri: str, video: Path, content_type: str | None) -> dict:
    headers = {
        "Authorization": f"OAuth {agent.ig_token()}",
        "offset": "0",
        "file_size": str(video.stat().st_size),
    }
    if content_type:
        headers["Content-Type"] = content_type
    r = requests.post(uri, headers=headers, data=video.read_bytes(), timeout=600)
    return {"status": r.status_code, "body": r.text[:500]}


def container_status(version: str, cid: str) -> dict:
    for _ in range(12):
        r = requests.get(
            f"https://graph.facebook.com/{version}/{cid}",
            params={"fields": "status_code,status", "access_token": agent.ig_token()},
            timeout=60,
        ).json()
        if r.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            return r
        time.sleep(10)
    return r


def main() -> None:
    video = Path(tempfile.mkdtemp()) / "test.mp4"
    make_test_video(video)
    print("Testni video:", agent.probe(video))

    variants = [
        ("v25.0", True, None),
        ("v25.0", False, None),
        ("v25.0", True, "application/x-www-form-urlencoded"),
        ("v23.0", True, None),
        ("v23.0", False, None),
    ]
    for version, trial, ctype in variants:
        name = f"{version} trial={trial} content-type={ctype}"
        print(f"\n=== {name}")
        c = create_container(version, trial)
        print("  kontejner:", c)
        body = c["body"]
        if "id" not in body:
            continue
        uri = body.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{version}/{body['id']}"
        print("  upload uri:", uri)
        u = upload(uri, video, ctype)
        print("  upload:", u)
        if u["status"] < 400:
            print("  status kontejnera:", container_status(version, body["id"]))


if __name__ == "__main__":
    main()
