"""VidGrab — Simple video downloader for Instagram, Threads, Twitter/X, Facebook, and TikTok."""

import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import yt_dlp  # type: ignore

app = FastAPI(title="VidGrab")

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)
STATIC_DIR = Path(__file__).parent / "static"
COOKIES_DIR = Path(__file__).parent / ".cookies"
COOKIES_DIR.mkdir(exist_ok=True)

FFMPEG_PATH = os.path.expanduser("~/bin/ffmpeg")

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Platforms that need cookies (Chrome DB lock makes this unreliable — skip for now)
COOKIE_DOMAINS = ("instagram.com", "facebook.com", "fb.watch", "threads.net")


class URLRequest(BaseModel):
    url: str


def _needs_cookies(url: str) -> bool:
    """Check if URL is from a Meta platform that requires auth."""
    url_lower = url.lower()
    return any(d in url_lower for d in COOKIE_DOMAINS)


def _get_chrome_cookies_path() -> str | None:
    """Copy Chrome's cookie DB to avoid lock contention, then convert to
    Netscape format for yt-dlp."""
    import subprocess
    import tempfile

    chrome_db = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default/Cookies"
    )
    if not os.path.exists(chrome_db):
        return None

    # Copy to temp to avoid lock contention
    tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp_db.close()
    try:
        shutil.copy2(chrome_db, tmp_db.name)
    except (OSError, PermissionError):
        os.unlink(tmp_db.name)
        return None

    # Convert SQLite cookies to Netscape format using sqlite3
    netscape_file = str(COOKIES_DIR / "cookies.txt")
    try:
        subprocess.run([
            "sqlite3", tmp_db.name,
            "SELECT host_key, CASE WHEN host_key LIKE '.%' THEN 'TRUE' ELSE 'FALSE' END,"
            " path, CASE WHEN is_secure THEN 'TRUE' ELSE 'FALSE' END,"
            " expires_utc, name, encrypted_value FROM cookies"
        ], capture_output=True, text=True, timeout=5)
        # sqlite3 can't decrypt Chrome's encrypted_value, so this won't fully work
        # Fall back to cookiesfrombrowser approach
    except Exception:
        pass
    finally:
        os.unlink(tmp_db.name)
        if os.path.exists(netscape_file):
            return netscape_file
    return None


def _try_cookiesfrombrowser(url: str, opts: dict) -> None:
    """Try adding cookies-from-browser. Returns silently on failure."""
    try:
        from yt_dlp.utils import YoutubeDLError
        # We can't actually test this — just add it and let yt-dlp handle fail
        opts["cookiesfrombrowser"] = ("chrome",)
    except Exception:
        pass


def build_ydl_opts(extra: dict | None = None, url: str = "") -> dict:
    """Build yt-dlp options. Tries Chrome cookies for platforms that need them."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
        "http_headers": {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if _needs_cookies(url):
        opts["cookiesfrombrowser"] = ("chrome",)

    if extra:
        opts.update(extra)
    return opts


def cleanup_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@app.post("/api/info")
async def get_info(req: URLRequest):
    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(url=req.url)) as ydl:
            info = ydl.extract_info(req.url, download=False)
            return {
                "title": info.get("title", "Untitled"),
                "duration": info.get("duration"),
                "uploader": info.get("uploader", info.get("channel", "")),
                "thumbnail": info.get("thumbnail", ""),
                "platform": info.get("extractor_key", ""),
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/download")
async def download_video(req: URLRequest, bg: BackgroundTasks):
    video_id = uuid.uuid4().hex[:8]
    output_template = str(DOWNLOADS_DIR / f"{video_id}.%(ext)s")

    try:
        opts = build_ydl_opts({
            "outtmpl": output_template,
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
        }, url=req.url)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=True)
            filename = ydl.prepare_filename(info)
            final_path = Path(filename).with_suffix(".mp4")
            if not final_path.exists():
                final_path = Path(filename)
            if not final_path.exists():
                candidates = list(DOWNLOADS_DIR.glob(f"{video_id}.*"))
                final_path = candidates[0] if candidates else None
            if not final_path or not final_path.exists():
                raise HTTPException(status_code=500, detail="Download failed — no output file")

            title = info.get("title", "video") or "video"
            safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip() or "video"

            bg.add_task(cleanup_file, str(final_path))
            return FileResponse(
                path=str(final_path),
                filename=f"{safe_title}.mp4",
                media_type="video/mp4",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>VidGrab</h1><p>Frontend not found.</p>"


@app.post("/api/tiktok/resolve")
async def resolve_tiktok(req: URLRequest):
    """Resolve TikTok video info. Returns video ID + metadata so the
    frontend can download client-side using the user's own IP."""
    url = req.url.strip()

    # Try yt-dlp first for metadata
    try:
        with yt_dlp.YoutubeDL(build_ydl_opts(url="")) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "duration": info.get("duration"),
                "uploader": info.get("uploader", ""),
                "thumbnail": info.get("thumbnail", ""),
                "video_url": info.get("url", ""),  # direct CDN URL if available
                "extractor": "yt-dlp",
            }
    except Exception:
        pass

    # Fallback: extract video ID from URL for client-side resolution
    patterns = [
        r"/video/(\d+)",
        r"/v/(\d+)",
        r"vm\.tiktok\.com/(\w+)",
        r"vt\.tiktok\.com/(\w+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return {
                "video_id": m.group(1),
                "is_shortlink": "vm.tiktok.com" in url or "vt.tiktok.com" in url,
                "extractor": "fallback",
            }

    raise HTTPException(status_code=400, detail="Could not parse TikTok URL")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)