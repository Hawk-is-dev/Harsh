import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("api")

API_KEY = os.getenv("API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("API_KEY variable is required. Set any long secret text.")

CACHE_DIR = Path(os.getenv("CACHE_DIR", "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_HOURS = float(os.getenv("CACHE_HOURS", "12"))
MAX_DURATION_MIN = int(os.getenv("MAX_DURATION_MIN", "180"))
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL", "3"))

app = FastAPI()
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
meta_cache = {}   # key -> (time, info dict)
locks = {}        # (id, kind) -> asyncio.Lock
tasks = set()
sem = asyncio.Semaphore(MAX_PARALLEL)
_cookie_path = None


def cookie_file():
    """Write COOKIES_TEXT / COOKIES_B64 to a file once and return its path."""
    global _cookie_path
    if _cookie_path and os.path.exists(_cookie_path):
        return _cookie_path
    raw = (os.getenv("COOKIES_TEXT") or "").strip()
    if not raw and os.getenv("COOKIES_B64"):
        try:
            raw = base64.b64decode(os.getenv("COOKIES_B64")).decode("utf-8", "ignore").strip()
        except Exception:
            raw = ""
    if not raw:
        return None
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n").replace("\\t", "\t")
    path = "/tmp/yt_cookies.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw + "\n")
    _cookie_path = path
    return path


def base_opts():
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "retries": 3,
        "js_runtimes": {"node": {}},
    }
    c = cookie_file()
    if c:
        o["cookiefile"] = c
    return o


def sign(vid, kind):
    return hmac.new(API_KEY.encode(), f"{vid}:{kind}".encode(), hashlib.sha256).hexdigest()[:24]


def fmt_duration(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def find_file(vid, kind):
    for p in CACHE_DIR.glob(f"{vid}.{kind}.*"):
        if p.suffix not in (".part", ".ytdl", ".temp") and p.stat().st_size > 0:
            return p
    return None


def resolve_sync(q):
    """Return {id, title, duration (sec)} for an id, a URL or a search text."""
    if ID_RE.match(q):
        target = f"https://www.youtube.com/watch?v={q}"
        flat = False
    elif q.startswith("http"):
        target = q
        flat = False
    else:
        target = f"ytsearch1:{q}"
        flat = True
    opts = base_opts()
    opts["skip_download"] = True
    opts["ignore_no_formats_error"] = True
    if flat:
        opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)
    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
    if not info or not info.get("id"):
        raise LookupError("not found")
    return {
        "id": info["id"],
        "title": info.get("title") or "Unknown Title",
        "duration": info.get("duration") or 0,
    }


def download_sync(vid, kind):
    opts = base_opts()
    opts["outtmpl"] = str(CACHE_DIR / f"{vid}.{kind}.%(ext)s")
    if kind == "video":
        opts["format"] = "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720]/b"
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={vid}"])


async def ensure_file(vid, kind):
    existing = find_file(vid, kind)
    if existing:
        return existing
    lock = locks.setdefault((vid, kind), asyncio.Lock())
    async with lock:
        existing = find_file(vid, kind)
        if existing:
            return existing
        async with sem:
            t0 = time.time()
            await asyncio.to_thread(download_sync, vid, kind)
            log.info("downloaded %s (%s) in %.1fs", vid, kind, time.time() - t0)
        path = find_file(vid, kind)
        if not path:
            raise RuntimeError("download produced no file")
        return path


def kick(vid, kind):
    async def runner():
        try:
            await ensure_file(vid, kind)
        except Exception as e:
            log.warning("background download failed for %s: %s", vid, e)

    t = asyncio.create_task(runner())
    tasks.add(t)
    t.add_done_callback(tasks.discard)


def check_key(api_key):
    if not hmac.compare_digest(api_key or "", API_KEY):
        raise HTTPException(status_code=401, detail="bad api_key")


@app.get("/")
async def root():
    return {"status": "ok"}


@app.get("/search")
async def search(q: str = Query(...), type: str = "audio", api_key: str = ""):
    check_key(api_key)
    kind = "video" if type == "video" else "audio"
    q = q.strip()
    key = q.lower()

    hit = meta_cache.get(key)
    if hit and time.time() - hit[0] < CACHE_HOURS * 3600:
        info = hit[1]
    else:
        try:
            info = await asyncio.to_thread(resolve_sync, q)
        except Exception as e:
            log.warning("resolve failed for %r: %s", q, e)
            raise HTTPException(status_code=404, detail="not found")
        meta_cache[key] = (time.time(), info)
        meta_cache[info["id"].lower()] = (time.time(), info)

    if info["duration"] and info["duration"] > MAX_DURATION_MIN * 60:
        raise HTTPException(status_code=413, detail="too long")

    vid = info["id"]
    kick(vid, kind)  # start downloading right away, bot fetches it a moment later
    return {
        "id": vid,
        "title": info["title"],
        "duration": fmt_duration(info["duration"]),
        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "stream_url": f"/stream/{vid}/{kind}?sig={sign(vid, kind)}",
    }


@app.get("/stream/{vid}/{kind}")
async def stream(vid: str, kind: str, sig: str = ""):
    if not ID_RE.match(vid) or kind not in ("audio", "video"):
        raise HTTPException(status_code=400, detail="bad request")
    if not hmac.compare_digest(sig, sign(vid, kind)):
        raise HTTPException(status_code=401, detail="bad signature")
    try:
        path = await ensure_file(vid, kind)
    except Exception as e:
        log.warning("stream failed for %s: %s", vid, e)
        raise HTTPException(status_code=502, detail="download failed")
    path.touch()
    media = "video/mp4" if path.suffix == ".mp4" else ("audio/mp4" if path.suffix == ".m4a" else "audio/webm")
    return FileResponse(path, media_type=media)


async def cleaner():
    while True:
        await asyncio.sleep(1800)
        try:
            limit = time.time() - CACHE_HOURS * 3600
            for p in CACHE_DIR.iterdir():
                if p.is_file() and p.stat().st_mtime < limit:
                    p.unlink(missing_ok=True)
            for k in [k for k, v in meta_cache.items() if time.time() - v[0] > CACHE_HOURS * 3600]:
                meta_cache.pop(k, None)
        except Exception as e:
            log.warning("cleanup error: %s", e)


@app.on_event("startup")
async def on_start():
    asyncio.create_task(cleaner())
    log.info("API ready. cookies=%s", bool(cookie_file()))
