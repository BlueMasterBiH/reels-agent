"""Reels agent: objavljuje trial reelove iz dva Google Drive foldera i javlja koji su prošli prag.

Pokreće se svaki sat (GitHub Actions). Jedan prolaz radi tri stvari:
  1. provjerava trial reelove kojima je prošlo CHECK_AFTER_HOURS sati i šalje email
     ako imaju >= VIEWS_THRESHOLD pregleda (tada ih ručno prebaciš na feed u aplikaciji),
  2. na dan objave odredi najbolji sat za objavu (aktivnost publike),
  3. kad dođe taj sat, uzme sljedeći video, napiše caption s hookom i objavi ga kao trial reel.

Stanje se čuva u state.json (workflow ga commita nazad u repo).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import smtplib
import subprocess
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------- konfiguracija


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        if default is not None:
            return default
        raise SystemExit(f"Nedostaje env varijabla {name}")
    return value


STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))
LOCAL_TZ = ZoneInfo(env("LOCAL_TZ", "Europe/Sarajevo"))
POST_EVERY_DAYS = int(env("POST_EVERY_DAYS", "2"))
VIEWS_THRESHOLD = int(env("VIEWS_THRESHOLD", "1000"))
CHECK_AFTER_HOURS = float(env("CHECK_AFTER_HOURS", "2"))
EARLIEST_HOUR = int(env("EARLIEST_HOUR", "8"))
LATEST_HOUR = int(env("LATEST_HOUR", "22"))
DEFAULT_HOUR = int(env("DEFAULT_HOUR", "19"))
MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "280"))
GRAPH_VERSION = env("GRAPH_VERSION", "v25.0")
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
# Sati u online_followers metrici su po pacifičkom vremenu.
ONLINE_FOLLOWERS_TZ = ZoneInfo(env("ONLINE_FOLLOWERS_TZ", "America/Los_Angeles"))
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-opus-5-5")
DRY_RUN = env("DRY_RUN", "0") == "1"
FORCE_POST = env("FORCE_POST", "0") == "1" or DRY_RUN  # probni rad ne čeka raspored

DRIVE_FOLDERS = [f.strip() for f in env("DRIVE_FOLDER_IDS", "").split(",") if f.strip()]


def log(*args) -> None:
    print(datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"), *args, flush=True)


# ---------------------------------------------------------------- stanje


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"posts": [], "used_files": [], "next_folder_index": 0, "plan": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- email


def send_email(subject: str, body: str) -> None:
    log("EMAIL:", subject)
    if DRY_RUN:
        print(body)
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env("SMTP_FROM", os.environ.get("SMTP_USER", ""))
    msg["To"] = env("EMAIL_TO")
    msg.set_content(body)
    host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=60) as s:
        s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
        s.send_message(msg)


# ---------------------------------------------------------------- Instagram Graph API


def ig_token() -> str:
    return env("IG_ACCESS_TOKEN")


_IG_USER_ID: str | None = None


def ig_user() -> str:
    """IG_USER_ID iz secreta, ili automatski iz tokena (Facebook stranica -> povezani Instagram)."""
    global _IG_USER_ID
    if _IG_USER_ID:
        return _IG_USER_ID
    configured = os.environ.get("IG_USER_ID", "").strip()
    if configured:
        _IG_USER_ID = configured
        return configured
    pages = graph("GET", "me/accounts", fields="name,instagram_business_account{id,username}")["data"]
    linked = [p for p in pages if p.get("instagram_business_account")]
    if not linked:
        raise RuntimeError(
            f"Token ne vidi nijednu Facebook stranicu s povezanim Instagramom (stranice: "
            f"{[p.get('name') for p in pages]}). Dodijeli stranicu system useru (Assign assets -> Pages)."
        )
    ig = linked[0]["instagram_business_account"]
    log(f"Pronađen Instagram nalog @{ig.get('username')} (IG_USER_ID={ig['id']}, stranica: {linked[0]['name']})")
    _IG_USER_ID = ig["id"]
    return _IG_USER_ID


def graph(method: str, path: str, **params) -> dict:
    params["access_token"] = ig_token()
    url = f"{GRAPH}/{path.lstrip('/')}"
    if method == "GET":
        r = requests.get(url, params=params, timeout=60)
    else:
        r = requests.post(url, data=params, timeout=120)
    if r.status_code >= 400:
        raise RuntimeError(f"Graph API {method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def insight_value(item: dict) -> int:
    if "total_value" in item:
        return int(item["total_value"].get("value", 0))
    values = item.get("values") or [{}]
    return int(values[-1].get("value", 0) or 0)


def get_views(media_id: str) -> int:
    data = graph("GET", f"{media_id}/insights", metric="views")["data"]
    return insight_value(data[0]) if data else 0


def publish_trial_reel(video: Path, caption: str) -> dict:
    """Resumable upload lokalnog fajla kao trial reel (graduation: MANUAL)."""
    container = graph(
        "POST",
        f"{ig_user()}/media",
        media_type="REELS",
        upload_type="resumable",
        caption=caption,
        trial_params=json.dumps({"graduation_strategy": "MANUAL"}),
    )
    container_id = container["id"]
    upload_uri = container.get("uri") or (
        f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}/{container_id}"
    )
    size = video.stat().st_size
    log(f"Upload {video.name} ({size / 1e6:.1f} MB) -> container {container_id}")
    with video.open("rb") as fh:
        r = requests.post(
            upload_uri,
            headers={
                "Authorization": f"OAuth {ig_token()}",
                "offset": "0",
                "file_size": str(size),
            },
            data=fh,
            timeout=1800,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Upload nije uspio: {r.status_code} {r.text}")

    deadline = time.time() + 30 * 60
    while True:
        status = graph("GET", container_id, fields="status_code,status")
        code = status.get("status_code")
        log("Container status:", code)
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram obrada nije uspjela: {status}")
        if time.time() > deadline:
            raise RuntimeError("Instagram obrada traje predugo (30 min)")
        time.sleep(20)

    media_id = graph("POST", f"{ig_user()}/media_publish", creation_id=container_id)["id"]
    info = graph("GET", media_id, fields="permalink,timestamp")
    return {"media_id": media_id, "permalink": info.get("permalink"), "timestamp": info.get("timestamp")}


# ---------------------------------------------------------------- najbolje vrijeme


def best_hours_from_online_followers() -> dict[int, dict[int, float]] | None:
    """weekday -> {lokalni sat: prosječno online pratilaca} iz zadnjih 30 dana."""
    until = int(time.time())
    since = until - 29 * 86400
    try:
        data = graph(
            "GET",
            f"{ig_user()}/insights",
            metric="online_followers",
            period="lifetime",
            since=since,
            until=until,
        )["data"]
    except Exception as e:  # metrika nije dostupna (npr. < 100 pratilaca)
        log("online_followers nije dostupan:", e)
        return None
    sums: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for item in data:
        for v in item.get("values", []):
            hours = v.get("value") or {}
            if not isinstance(hours, dict) or not hours:
                continue
            end = datetime.fromisoformat(v["end_time"].replace("+0000", "+00:00"))
            day = (end.astimezone(ONLINE_FOLLOWERS_TZ) - timedelta(days=1)).date()
            for h, count in hours.items():
                src = datetime(day.year, day.month, day.day, int(h), tzinfo=ONLINE_FOLLOWERS_TZ)
                local = src.astimezone(LOCAL_TZ)
                sums[local.weekday()][local.hour].append(float(count))
    if not sums:
        return None
    return {wd: {h: sum(c) / len(c) for h, c in hrs.items()} for wd, hrs in sums.items()}


def best_hours_from_history() -> dict[int, dict[int, float]] | None:
    """weekday -> {lokalni sat: prosječni pregledi} iz tvojih zadnjih reelova."""
    try:
        media = graph(
            "GET", f"{ig_user()}/media", fields="id,timestamp,media_product_type", limit=50
        )["data"]
    except Exception as e:
        log("Historija objava nije dostupna:", e)
        return None
    buckets: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for m in media:
        if m.get("media_product_type") != "REELS":
            continue
        try:
            views = get_views(m["id"])
        except Exception:
            continue
        ts = datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")).astimezone(LOCAL_TZ)
        buckets[ts.weekday()][ts.hour].append(views)
    if not buckets:
        return None
    return {wd: {h: sum(v) / len(v) for h, v in hrs.items()} for wd, hrs in buckets.items()}


def pick_hour(scores: dict[int, float]) -> int | None:
    allowed = {h: s for h, s in scores.items() if EARLIEST_HOUR <= h <= LATEST_HOUR}
    if not allowed:
        return None
    # najjači prozor od 2 sata; objava na početku prozora
    def window(h: int) -> float:
        return allowed.get(h, 0) + allowed.get(h + 1, 0)

    return max(allowed, key=window)


def best_post_hour(day: date) -> tuple[int, str]:
    wd = day.weekday()
    online = best_hours_from_online_followers()
    if online:
        hour = pick_hour(online.get(wd) or {})
        if hour is None:  # nema podataka za taj dan u sedmici -> prosjek svih dana
            merged: dict[int, list[float]] = defaultdict(list)
            for hrs in online.values():
                for h, s in hrs.items():
                    merged[h].append(s)
            hour = pick_hour({h: sum(v) / len(v) for h, v in merged.items()})
        if hour is not None:
            return hour, "aktivnost pratilaca (online_followers, zadnjih 30 dana)"
    history = best_hours_from_history()
    if history:
        hour = pick_hour(history.get(wd) or {})
        if hour is not None:
            return hour, "pregledi tvojih ranijih reelova po satu objave"
    return DEFAULT_HOUR, "zadano vrijeme (nema dovoljno podataka)"


# ---------------------------------------------------------------- Google Drive


def drive_session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    info = json.loads(env("GDRIVE_SA_JSON"))
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return AuthorizedSession(creds)


def list_videos(session, folder_id: str) -> list[dict]:
    files, token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and mimeType contains 'video/' and trashed = false",
            "fields": "nextPageToken, files(id,name,size,createdTime)",
            "orderBy": "createdTime",
            "pageSize": 200,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        r = session.get("https://www.googleapis.com/drive/v3/files", params=params, timeout=60)
        r.raise_for_status()
        body = r.json()
        files += body.get("files", [])
        token = body.get("nextPageToken")
        if not token:
            return files


def pick_next_video(session, state: dict) -> tuple[dict, int] | None:
    used = set(state["used_files"])
    n = len(DRIVE_FOLDERS)
    for offset in range(n):
        idx = (state["next_folder_index"] + offset) % n
        for f in list_videos(session, DRIVE_FOLDERS[idx]):
            if f["id"] not in used:
                return f, idx
    return None


def download(session, file_id: str, dest: Path) -> None:
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}"
    with session.get(url, params={"alt": "media", "supportsAllDrives": "true"}, stream=True, timeout=600) as r:
        r.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                fh.write(chunk)


# ---------------------------------------------------------------- video obrada


def duration_seconds(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def shrink_if_needed(video: Path, workdir: Path) -> Path:
    limit = MAX_UPLOAD_MB * 1_000_000
    if video.stat().st_size <= limit:
        return video
    dur = duration_seconds(video)
    # ciljani bitrate tako da fajl stane u limit (uz rezervu za audio)
    video_kbps = max(1500, int(limit * 8 / dur / 1000 * 0.9) - 128)
    out = workdir / "upload.mp4"
    log(f"Video je {video.stat().st_size / 1e6:.0f} MB, kompresujem na ~{video_kbps} kbps")
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
            "-vf", "scale='min(1080,iw)':-2", "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
        ],
        check=True,
    )
    return out


def extract_frames(video: Path, workdir: Path, max_frames: int = 24) -> list[Path]:
    dur = duration_seconds(video)
    count = int(min(max_frames, max(6, dur / 2)))
    frames_dir = workdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
            "-vf", f"fps={count / dur:.5f},scale=720:-2", "-q:v", "4",
            str(frames_dir / "f%03d.jpg"),
        ],
        check=True,
    )
    return sorted(frames_dir.glob("f*.jpg"))[:max_frames]


# ---------------------------------------------------------------- caption (Claude)

CAPTION_PROMPT = """Ovo su kadrovi iz jednog Instagram reela (redom, od početka do kraja).
Na videu su titlovi - pročitaj ih pažljivo i shvati o čemu se priča.

Napiši caption za ovaj reel. Reel ide kao TRIAL reel - prvo ga vide ljudi koji NE prate profil,
zato caption mora odmah zaintrigirati nekoga ko ne zna ko smo.

Pravila:
- Prvi red je HOOK: najviše ~110 znakova, izaziva radoznalost ili otvara "loop"
  (pitanje, kontra-tvrdnja, konkretan broj/rezultat, "većina ljudi griješi u ..."). Ne otkrivaj poentu odmah.
- Zatim 2-4 kratka reda koja pojačavaju interes i navode da se pogleda do kraja.
- Jedan jasan poziv na akciju (npr. sačuvaj, podijeli, komentariši ključnu riječ).
- Na kraju 3-6 relevantnih hashtagova.
- Piši na istom jeziku kao titlovi u videu (ako su na bosanskom/hrvatskom/srpskom, piši ijekavicom na bosanskom).
- Bez izmišljanja činjenica koje nisu u videu. Bez clickbaita koji video ne ispunjava.
- Emojiji umjereno (0-3).
{extra}
Vrati SAMO tekst captiona između <caption> i </caption>, bez ikakvog drugog teksta."""


def extract_caption(text: str) -> str:
    import re

    blocks = [b.strip() for b in re.findall(r"<caption>(.*?)</caption>", text, flags=re.S)]
    blocks = [b for b in blocks if len(b) >= 20]
    if not blocks:
        raise RuntimeError(f"Claude nije vratio caption. Odgovor:\n{text[-3000:]}")
    return blocks[-1][:2200]  # Instagram limit


def write_caption(frames: list[Path], filename: str) -> str:
    """Caption preko Claude pretplate (Claude Code token) ako postoji, inače preko API ključa."""
    if subscription_token():
        return write_caption_claude_code(frames, filename)
    return write_caption_api(frames, filename)


def subscription_token() -> str:
    """Token pretplate (sk-ant-oat...) iz CLAUDE_CODE_OAUTH_TOKEN, ili ako je greškom stavljen u ANTHROPIC_API_KEY."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if token:
        return token
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return key if key.startswith("sk-ant-oat") else ""


def write_caption_claude_code(frames: list[Path], filename: str) -> str:
    """Claude Code CLI u headless modu (-p), koristi pretplatu preko CLAUDE_CODE_OAUTH_TOKEN."""
    extra = os.environ.get("CAPTION_CONTEXT", "").strip()
    extra = f"\nKontekst o profilu/brendu: {extra}\n" if extra else ""
    frame_list = "\n".join(f"- {f.name}" for f in frames)
    prompt = (
        f"U ovom folderu su kadrovi iz videa \"{filename}\". Pročitaj SVE ove slike alatom Read, redom:\n"
        f"{frame_list}\n\n" + CAPTION_PROMPT.format(extra=extra)
    )
    child_env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    child_env["CLAUDE_CODE_OAUTH_TOKEN"] = subscription_token()
    result = subprocess.run(
        ["claude", "-p", prompt, "--allowedTools", "Read", "--output-format", "text"],
        cwd=frames[0].parent,
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude Code greška ({result.returncode}): {result.stderr[-2000:] or result.stdout[-2000:]}")
    return extract_caption(result.stdout)


def write_caption_api(frames: list[Path], filename: str) -> str:
    import anthropic

    content: list[dict] = []
    for f in frames:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(f.read_bytes()).decode("utf-8"),
            },
        })
    extra = os.environ.get("CAPTION_CONTEXT", "").strip()
    extra = f"\nKontekst o profilu/brendu: {extra}\n" if extra else ""
    content.append({"type": "text", "text": f"Naziv fajla: {filename}\n\n" + CAPTION_PROMPT.format(extra=extra)})

    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude je odbio zahtjev: {response.stop_details}")
    return extract_caption("".join(b.text for b in response.content if b.type == "text"))


# ---------------------------------------------------------------- koraci


def check_trials(state: dict, now: datetime) -> None:
    for p in state["posts"]:
        if p.get("checked") or not p.get("media_id"):
            continue
        posted = datetime.fromisoformat(p["posted_at"])
        if now - posted < timedelta(hours=CHECK_AFTER_HOURS):
            continue
        hours = (now - posted).total_seconds() / 3600
        try:
            views = get_views(p["media_id"]) if not DRY_RUN else 0
        except Exception as e:
            log("Ne mogu pročitati preglede za", p["media_id"], e)
            continue
        p.update(checked=True, views_at_check=views, checked_at=now.isoformat())
        passed = views >= VIEWS_THRESHOLD
        p["passed"] = passed
        if passed:
            send_email(
                f"🚀 Trial reel prošao: {views} pregleda za {hours:.1f}h - prebaci na feed",
                f"Trial reel \"{p['file_name']}\" ima {views} pregleda nakon {hours:.1f} sati "
                f"(prag je {VIEWS_THRESHOLD}).\n\n"
                f"Otvori ga u Instagram aplikaciji i prebaci na feed (Share to followers / Graduate):\n"
                f"{p.get('permalink')}\n\nCaption:\n{p['caption']}\n",
            )
        else:
            send_email(
                f"Trial reel ispod praga: {views} pregleda za {hours:.1f}h",
                f"Trial reel \"{p['file_name']}\" ima {views} pregleda nakon {hours:.1f} sati "
                f"(prag je {VIEWS_THRESHOLD}). Ostaje kao trial.\n{p.get('permalink')}\n",
            )


def last_post_day(state: dict) -> date | None:
    days = [datetime.fromisoformat(p["posted_at"]).astimezone(LOCAL_TZ).date() for p in state["posts"]]
    return max(days) if days else None


def ensure_plan(state: dict, now_local: datetime) -> None:
    today = now_local.date()
    plan = state.get("plan")
    if plan and plan["date"] == today.isoformat():
        return
    last = last_post_day(state)
    if not FORCE_POST and last is not None and (today - last).days < POST_EVERY_DAYS:
        state["plan"] = None
        return
    hour, reason = best_post_hour(today)
    if FORCE_POST:
        hour, reason = now_local.hour, "ručno pokretanje (FORCE_POST)"
    state["plan"] = {"date": today.isoformat(), "hour": hour, "reason": reason, "done": False, "attempts": 0}
    log(f"Plan za {today}: objava u {hour}:00 ({reason})")


def maybe_post(state: dict, now: datetime, now_local: datetime) -> None:
    plan = state.get("plan")
    if not plan or plan["done"] or plan["date"] != now_local.date().isoformat():
        return
    if now_local.hour < plan["hour"] or plan["attempts"] >= 3:
        return
    plan["attempts"] += 1
    workdir = Path(tempfile.mkdtemp(prefix="reel-"))
    try:
        session = drive_session()
        picked = pick_next_video(session, state)
        if not picked:
            plan["done"] = True
            send_email("Reels agent: nema više novih videa", "Oba Drive foldera su iskorištena. Dodaj nove videe.")
            return
        f, folder_idx = picked
        log(f"Sljedeći video: {f['name']} (folder {folder_idx + 1})")
        src = workdir / ("source" + Path(f["name"]).suffix.lower())
        download(session, f["id"], src)
        frames = extract_frames(src, workdir)
        caption = write_caption(frames, f["name"])
        log("Caption:\n" + caption)
        upload = shrink_if_needed(src, workdir)
        if DRY_RUN:
            result = {"media_id": None, "permalink": None}
            log("DRY_RUN: preskačem objavu")
        else:
            result = publish_trial_reel(upload, caption)
        state["posts"].append({
            "file_id": f["id"],
            "file_name": f["name"],
            "folder_index": folder_idx,
            "posted_at": now.isoformat(),
            "caption": caption,
            "planned_hour": plan["hour"],
            "plan_reason": plan["reason"],
            **result,
            "checked": DRY_RUN,
        })
        state["used_files"].append(f["id"])
        state["next_folder_index"] = (folder_idx + 1) % len(DRIVE_FOLDERS)
        plan["done"] = True
        check_at = (now + timedelta(hours=CHECK_AFTER_HOURS)).astimezone(LOCAL_TZ)
        send_email(
            f"Objavljen trial reel: {f['name']}",
            f"Objavljeno u {now_local:%H:%M} ({plan['reason']}).\n{result.get('permalink')}\n\n"
            f"Provjera pregleda oko {check_at:%H:%M}.\n\nCaption:\n{caption}\n",
        )
    except Exception:
        err = traceback.format_exc()
        log(err)
        send_email(f"Reels agent: greška pri objavi (pokušaj {plan['attempts']}/3)", err)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    if len(DRIVE_FOLDERS) == 0:
        raise SystemExit("DRIVE_FOLDER_IDS nije postavljen")
    log(f"Instagram IG_USER_ID: {ig_user()}")
    state = load_state()
    now = datetime.now(timezone.utc)
    now_local = now.astimezone(LOCAL_TZ)
    check_trials(state, now)
    ensure_plan(state, now_local)
    maybe_post(state, now, now_local)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
