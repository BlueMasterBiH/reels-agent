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


def upload_chunked(uri: str, video: Path, chunk_mb: int = 4) -> dict:
    data = video.read_bytes()
    size, offset, step, last = len(data), 0, chunk_mb * 1024 * 1024, None
    while offset < size:
        chunk = data[offset:offset + step]
        r = requests.post(
            uri,
            headers={
                "Authorization": f"OAuth {agent.ig_token()}",
                "offset": str(offset),
                "file_size": str(size),
            },
            data=chunk,
            timeout=600,
        )
        last = {"status": r.status_code, "body": r.text[:300], "offset": offset}
        print("    chunk", last)
        if r.status_code >= 400:
            return last
        offset += len(chunk)
    return last


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


def try_variant(name: str, video: Path, chunked: bool) -> None:
    print(f"\n=== {name} ({video.stat().st_size / 1e6:.1f} MB, chunked={chunked})")
    c = create_container("v25.0", True)
    body = c["body"]
    if "id" not in body:
        print("  kontejner:", c)
        return
    uri = body.get("uri")
    u = upload_chunked(uri, video) if chunked else upload(uri, video, None)
    print("  upload:", u)
    if u and u["status"] < 400:
        print("  status kontejnera:", container_status("v25.0", body["id"]))


def main() -> None:
    work = Path(tempfile.mkdtemp())
    big = work / "big.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "58", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "2500k",
         "-c:a", "aac", "-ac", "2", "-movflags", "+faststart", str(big)],
        check=True,
    )
    try_variant("sintetički 58s", big, chunked=False)
    try_variant("sintetički 58s", big, chunked=True)

    session = agent.drive_session()
    f = agent.list_videos(session, agent.DRIVE_FOLDERS[0])[0]
    src = work / "src.mp4"
    agent.download(session, f["id"], src)
    real = agent.prepare_for_instagram(src, work)
    try_variant(f"pravi video {f['name']}", real, chunked=False)
    try_variant(f"pravi video {f['name']}", real, chunked=True)


if __name__ == "__main__":
    import os
    if os.environ.get("DIAG_UPLOAD") == "1":
        main()
