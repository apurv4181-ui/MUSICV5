# anony/core/youtube.py
import os
import re
import asyncio
import aiohttp
import threading
import shutil
from time import sleep
from pathlib import Path
from typing import Optional

import yt_dlp
from py_yt import Playlist, VideosSearch

from anony import logger, app, config, userbot
from anony.helpers import Track, utils
from anony.helpers._api import NexGenApi, YukiApi
from backend_prefs import get_primary

# Config / defaults
JIOSAVAN_API_URL = os.getenv("JIOSAVAN_API_URL", "").rstrip("/")
NEXGEN_API_URL = getattr(config, "API_URL", os.getenv("API_URL", "https://pvtz.nexgenbots.xyz")).rstrip("/")
NEXGEN_VIDEO_API_URL = getattr(config, "VIDEO_API_URL", os.getenv("VIDEO_API_URL", "https://api.video.nexgenbots.xyz")).rstrip("/")
NEXGEN_API_KEY = getattr(config, "API_KEY", os.getenv("API_KEY", ""))

# --- Yuki Music API (replaces Shruti) --------------------------------------------
YUKI_API_KEY = getattr(config, "YUKI_API_KEY", os.getenv("YUKI_API_KEY", "yuki_9c29d8e669f4a0ad9577d1f99818e420"))
YUKI_PRIMARY_URL = getattr(config, "YUKI_PRIMARY_URL", os.getenv("YUKI_PRIMARY_URL", "https://music.yukiapi.site")).rstrip("/")
YUKI_BACKUP_URL = getattr(config, "YUKI_BACKUP_URL", os.getenv("YUKI_BACKUP_URL", "https://play.yukiapi.site")).rstrip("/")

# New provider: NubCoders (stream resolver)
NUBCODERS_API_URL = getattr(config, "NUBCODERS_API_URL", os.getenv("NUBCODERS_API_URL", "https://api.nubcoders.com")).rstrip("/")
NUBCODERS_TOKEN = getattr(config, "NUBCODERS_TOKEN", os.getenv("NUBCODERS_TOKEN", ""))

# Channel used as DB for pre-uploaded tracks (adjust via config)
ARC_DB_CHANNEL_ID = int(getattr(config, "ARC_DATABASE_ID", -1001677848376))

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True, parents=True)


# --- Helper to Extract Video ID ---
def extract_video_id(url_or_id: str) -> str:
    """Extract YouTube video ID from URL or return as-is if already an ID"""
    url_or_id = (url_or_id or "").strip()
    if len(url_or_id) == 11 and "http" not in url_or_id:
        return url_or_id
    if "v=" in url_or_id:
        return url_or_id.split("v=")[-1].split("&")[0].split("#")[0]
    if "youtu.be/" in url_or_id:
        return url_or_id.split("youtu.be/")[-1].split("?")[0]
    if "shorts/" in url_or_id:
        return url_or_id.split("shorts/")[-1].split("?")[0]
    return url_or_id


# ----------------- JioSaavn helpers ------------------------------------------------

async def _jiosaavn_search(query: str) -> Optional[dict]:
    if not JIOSAVAN_API_URL:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{JIOSAVAN_API_URL}/search",
                params={"q": query},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    logger.warning("JioSaavn search HTTP %s", r.status)
                    return None
                data = await r.json()
                tracks = data.get("tracks") or []
                return tracks[0] if tracks else None
    except Exception as e:
        logger.warning("JioSaavn search error: %s", e)
        return None


async def _jiosaavn_download(song_id: str) -> Optional[str]:
    if not JIOSAVAN_API_URL:
        return None
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{JIOSAVAN_API_URL}/download",
                params={"id": song_id},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    logger.warning("JioSaavn download HTTP %s id=%s", r.status, song_id)
                    return None
                data = await r.json()
                return data.get("url") or None
    except Exception as e:
        logger.warning("JioSaavn download error: %s", e)
        return None


# ----------------- NexGenBots integration -----------------------------------------

_nexgen_client: Optional[NexGenApi] = None


async def _get_nexgen_client() -> Optional[NexGenApi]:
    global _nexgen_client
    if _nexgen_client:
        return _nexgen_client
    if not NEXGEN_API_URL or not NEXGEN_API_KEY:
        logger.warning("NexGen config missing: API_URL or API_KEY not set")
        return None
    _nexgen_client = NexGenApi(api_url=NEXGEN_API_URL, api_key=NEXGEN_API_KEY, video_api_url=NEXGEN_VIDEO_API_URL)
    try:
        await _nexgen_client.get_session()
    except Exception as e:
        logger.warning("Failed to create NexGen session: %s", e)
        _nexgen_client = None
    return _nexgen_client


async def _nexgen_download(video_id: str, is_video: bool = False) -> Optional[str]:
    client = await _get_nexgen_client()
    if not client:
        return None
    try:
        vid = extract_video_id(video_id)
        result = await client.download(vid, video=is_video)
        return result
    except Exception as e:
        logger.warning("NexGen download error for %s: %s", video_id, e)
        return None


# ----------------- Yuki Music API (replaces Shruti) -------------------------------

_yuki_client: Optional[YukiApi] = None


async def _get_yuki_client() -> Optional[YukiApi]:
    global _yuki_client
    if _yuki_client:
        return _yuki_client
    if not YUKI_API_KEY:
        logger.warning("Yuki config missing: YUKI_API_KEY not set")
        return None
    _yuki_client = YukiApi(
        api_key=YUKI_API_KEY,
        primary_url=YUKI_PRIMARY_URL,
        backup_url=YUKI_BACKUP_URL,
    )
    try:
        await _yuki_client.get_session()
    except Exception as e:
        logger.warning("Failed to create Yuki session: %s", e)
        _yuki_client = None
    return _yuki_client


async def _yuki_download(video_id_or_url: str, is_video: bool = False) -> Optional[str]:
    client = await _get_yuki_client()
    if not client:
        return None
    try:
        vid = extract_video_id(video_id_or_url)
        return await client.download(vid, video=is_video)
    except Exception as e:
        logger.warning("Yuki download error for %s: %s", video_id_or_url, e)
        return None


async def _yuki_get_stream_link(video_id_or_url: str, is_video: bool = False) -> Optional[str]:
    client = await _get_yuki_client()
    if not client:
        return None
    try:
        vid = extract_video_id(video_id_or_url)
        return await client.get_stream_link(vid, video=is_video)
    except Exception as e:
        logger.warning("Yuki stream link error for %s: %s", video_id_or_url, e)
        return None


# ----------------- NubCoders provider ---------------------------------------------

async def _nubcoders_get_stream_link(video_id_or_url: str, is_video: bool = False) -> Optional[str]:
    """Return a direct stream URL from NubCoders /info without downloading."""
    if not NUBCODERS_API_URL or not NUBCODERS_TOKEN:
        logger.warning("NubCoders config missing: NUBCODERS_API_URL or NUBCODERS_TOKEN not set")
        return None
    try:
        vid = extract_video_id(video_id_or_url)
        watch_url = f"https://www.youtube.com/watch?v={vid}"
        endpoint = f"{NUBCODERS_API_URL.rstrip('/')}/info"
        params = {"token": NUBCODERS_TOKEN, "q": watch_url, "type": "video" if is_video else "audio"}
        async with aiohttp.ClientSession() as session:
            async with session.get(endpoint, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.warning("NubCoders info HTTP %s for %s: %s", resp.status, vid, text)
                    return None
                try:
                    data = await resp.json()
                except Exception:
                    logger.warning("NubCoders returned non-JSON for %s", vid)
                    return None
                stream = data.get("stream_url") or data.get("streamUrl") or data.get("url") or data.get("stream")
                if not stream:
                    logger.warning("NubCoders did not return stream_url for %s: %s", vid, data)
                    return None
                if stream.startswith("/"):
                    from urllib.parse import urljoin
                    stream = urljoin(NUBCODERS_API_URL, stream)
                logger.info("NubCoders returned stream for %s", vid)
                return stream
    except Exception as e:
        logger.warning("NubCoders error for %s: %s", video_id_or_url, e)
        return None


async def _nubcoders_download(video_id_or_url: str, is_video: bool = False) -> Optional[str]:
    """Optional fallback: query stream and download to local file."""
    try:
        stream = await _nubcoders_get_stream_link(video_id_or_url, is_video=is_video)
        if not stream:
            return None
        vid = extract_video_id(video_id_or_url)
        ext = "mp4" if is_video else "m4a"
        filename = str(DOWNLOAD_DIR / f"{vid}.{ext}")
        out = await _download_via_url_to_file(stream, filename)
        return out
    except Exception:
        return None


# ----------------- Telegram DB (CDN) helpers -------------------------------------

def _schedule_delete(filepath: str, delay: int = 300) -> None:
    def _worker():
        sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info("Cache cleanup: removed %s", filepath)
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()


async def _ffmpeg_extract_audio(input_path: str, out_path: str) -> bool:
    """Extract audio (m4a) from a video file using ffmpeg asynchronously."""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found in PATH; cannot extract audio from video.")
        return False
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", input_path, "-vn", "-c:a", "aac", "-b:a", "128k", out_path
    ]
    proc = await asyncio.create_subprocess_exec(*cmd)
    rc = await proc.wait()
    return rc == 0


async def _tg_channel_download_by_id(video_id: str, is_video: bool = False) -> Optional[str]:
    """Search Telegram DB channel for exact ID and download matching audio/video."""
    vid = extract_video_id(video_id)
    if not vid:
        return None
    client = userbot.clients[0] if userbot.clients else app
    try:
        ub_client = getattr(client, "app", client)
        async for msg in ub_client.search_messages(chat_id=ARC_DB_CHANNEL_ID, query=vid, limit=10):
            caption_text = (msg.caption or msg.text or "")
            if vid in caption_text and (msg.audio or msg.document or msg.voice or msg.video):
                logger.info("Found EXACT Track ID match '%s' in DB channel. Downloading...", vid)
                if not is_video and (msg.audio or msg.document or msg.voice):
                    media = msg.audio or msg.document or msg.voice
                    ext = "m4a" if getattr(media, "mime_type", "").startswith("audio") or getattr(media, "file_name", "").endswith(".m4a") else "mp3"
                    out_name = DOWNLOAD_DIR / f"{vid}.{ext}"
                    file_path = await ub_client.download_media(msg, file_name=str(out_name))
                    if file_path and os.path.exists(file_path):
                        return file_path
                if is_video and msg.video:
                    out_name = DOWNLOAD_DIR / f"{vid}.mp4"
                    file_path = await ub_client.download_media(msg, file_name=str(out_name))
                    if file_path and os.path.exists(file_path):
                        return file_path
                if not is_video and msg.video:
                    temp_video = DOWNLOAD_DIR / f"{vid}.temp"
                    file_path = await ub_client.download_media(msg, file_name=str(temp_video))
                    if file_path and os.path.exists(file_path):
                        audio_out = DOWNLOAD_DIR / f"{vid}.m4a"
                        ok = await _ffmpeg_extract_audio(str(temp_video), str(audio_out))
                        try:
                            os.remove(temp_video)
                        except Exception:
                            pass
                        if ok and os.path.exists(audio_out):
                            return str(audio_out)
    except Exception as e:
        logger.warning("TG DB exact search error for ID '%s': %s", vid, e)
    return None


async def _tg_channel_download_by_title(title: str, is_video: bool = False) -> Optional[str]:
    if not title:
        return None
    client = userbot.clients[0] if userbot.clients else app
    try:
        ub_client = getattr(client, "app", client)
        async for msg in ub_client.search_messages(chat_id=ARC_DB_CHANNEL_ID, query=title, limit=15):
            caption_text = (msg.caption or msg.text or "").lower()
            if any(x in caption_text for x in ["cover", "speed up", "slowed", "lyrics", "remix"]):
                continue
            if not is_video and (msg.audio or msg.document or msg.voice):
                media = msg.audio or msg.document or msg.voice
                ext = "m4a" if getattr(media, "mime_type", "").startswith("audio") or getattr(media, "file_name", "").endswith(".m4a") else "mp3"
                out_name = DOWNLOAD_DIR / f"{msg.id}.{ext}"
                file_path = await ub_client.download_media(msg, file_name=str(out_name))
                if file_path and os.path.exists(file_path):
                    return file_path
            if is_video and msg.video:
                out_name = DOWNLOAD_DIR / f"{msg.id}.mp4"
                file_path = await ub_client.download_media(msg, file_name=str(out_name))
                if file_path and os.path.exists(file_path):
                    return file_path
            if not is_video and msg.video:
                temp_video = DOWNLOAD_DIR / f"{msg.id}.temp"
                file_path = await ub_client.download_media(msg, file_name=str(temp_video))
                if file_path and os.path.exists(file_path):
                    audio_out = DOWNLOAD_DIR / f"{msg.id}.m4a"
                    ok = await _ffmpeg_extract_audio(str(temp_video), str(audio_out))
                    try:
                        os.remove(temp_video)
                    except Exception:
                        pass
                    if ok and os.path.exists(audio_out):
                        return str(audio_out)
    except Exception as e:
        logger.warning("TG DB title search error for '%s': %s", title, e)
    return None


# ----------------- yt-dlp fallback with cookies ---------------------------------

def _sanitize_for_filename(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '_', s)


async def _yt_dlp_download(video_id_or_url: str, is_video: bool = False) -> Optional[str]:
    cookiefile = None
    local_cookie_dir = Path("anony/cookies")
    if local_cookie_dir.exists() and local_cookie_dir.is_dir():
        for f in local_cookie_dir.iterdir():
            if f.is_file() and f.suffix == ".txt":
                cookiefile = str(f)
                break

    if not cookiefile and getattr(config, "COOKIES_URL", None):
        for url in config.COOKIES_URL:
            if not url:
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            cookies_path = DOWNLOAD_DIR / "cookies.txt"
                            cookies_path.write_bytes(await resp.read())
                            cookiefile = str(cookies_path)
                            logger.info("Saved cookiefile from URL to %s", cookiefile)
                            break
            except Exception as e:
                logger.debug("Failed to download cookie URL %s: %s", url, e)
                continue

    loop = asyncio.get_event_loop()
    vid = extract_video_id(video_id_or_url)
    safe_name = _sanitize_for_filename(vid)
    return await loop.run_in_executor(None, _yt_dlp_sync, vid, is_video, cookiefile, safe_name)


def _yt_dlp_sync(video_id_or_url: str, is_video: bool, cookiefile: Optional[str], safe_name: str) -> Optional[str]:
    try:
        out_ext = "mp4" if is_video else "m4a"
        out_template = str(DOWNLOAD_DIR / f"{safe_name}.%(ext)s")
        ydl_opts = {
            "format": "bestvideo+bestaudio/best" if is_video else "bestaudio/best",
            "outtmpl": out_template,
            "noplaylist": True,
            "quiet": True,
            "continuedl": True,
            "nocheckcertificate": True,
            "ignoreerrors": True,
            "no_warnings": True,
            "retries": 3,
        }
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile
        if not is_video:
            ydl_opts.update({
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                }]
            })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_id_or_url, download=True)
            if not info:
                return None
            ext = info.get("ext") or out_ext
            fname = str(DOWNLOAD_DIR / f"{safe_name}.{ext}")
            if os.path.exists(fname):
                return fname
            for f in os.listdir(DOWNLOAD_DIR):
                if safe_name in f or (info.get("id") and info.get("id") in f):
                    if f.endswith((".mp4", ".m4a", ".webm", ".mp3")):
                        return str(DOWNLOAD_DIR / f)
            return None
    except Exception as e:
        logger.warning("yt-dlp download error for %s: %s", video_id_or_url, e)
        return None


# ----------------- YouTube metadata search --------------------------------------

async def _yt_search_meta(query: str, m_id: int, video: bool) -> Optional[Track]:
    try:
        _search = VideosSearch(query, limit=1, with_live=False)
        results = await _search.next()
    except Exception:
        return None
    if results and results.get("result"):
        data = results["result"][0]
        return Track(
            id=data.get("id"),
            channel_name=data.get("channel", {}).get("name"),
            duration=data.get("duration"),
            duration_sec=utils.to_seconds(data.get("duration")),
            message_id=m_id,
            title=data.get("title")[:30],
            thumbnail=data.get("thumbnails", [{}])[-1].get("url", "").split("?")[0],
            url=data.get("link"),
            view_count=data.get("viewCount", {}).get("short"),
            video=video,
        )
    return None


# ----------------- YouTube class (public API) -----------------------------------

class YouTube:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )

    def valid(self, url: str) -> bool:
        return bool(re.match(self.regex, url))

    def invalid(self, url: str) -> bool:
        # FIX: this must be the logical negation of valid(), not a duplicate of it.
        return not bool(re.match(self.regex, url))

    async def search(self, query: str, m_id: int, video: bool = False) -> Optional[Track]:
        mode = get_primary()
        if mode == "jio":
            saavn = await _jiosaavn_search(query)
            if saavn:
                return Track(
                    id=saavn.get("id"),
                    channel_name=saavn.get("artists"),
                    duration=str(saavn.get("duration") or ""),
                    duration_sec=int(saavn.get("duration") or 0),
                    message_id=m_id,
                    title=(saavn.get("title") or query)[:30],
                    thumbnail=saavn.get("thumbnail", ""),
                    url=saavn.get("url", ""),
                    view_count="",
                    video=False,
                )
        return await _yt_search_meta(query, m_id, video)

    async def playlist(self, limit: int, user: str, url: str, video: bool) -> list[Optional[Track]]:
        tracks = []
        try:
            plist = await Playlist.get(url)
            if not plist or "videos" not in plist:
                return tracks
            for data in plist["videos"][:limit]:
                track = Track(
                    id=data.get("id"),
                    channel_name=data.get("channel", {}).get("name", ""),
                    duration=data.get("duration"),
                    duration_sec=utils.to_seconds(data.get("duration")),
                    title=data.get("title")[:25],
                    thumbnail=data.get("thumbnails", [{}])[-1].get("url", "").split("?")[0],
                    url=data.get("link", "").split("&list=")[0],
                    user=user,
                    view_count="",
                    video=video,
                )
                tracks.append(track)
        except Exception:
            pass
        return tracks

    async def download(self, video_id: str, video: bool = False, title: Optional[str] = None) -> Optional[str]:
        vid_for_file = extract_video_id(video_id)
        ext = "mp4" if video else "m4a"
        filename = str(DOWNLOAD_DIR / f"{vid_for_file}.{ext}")
        if Path(filename).exists():
            return filename

        mode = get_primary()

        # 1) JioSaavn (/jio) - Audio Only
        if mode == "jio":
            url = await _jiosaavn_download(vid_for_file)
            if url:
                out = await _download_via_url_to_file(url, filename)
                if out:
                    _schedule_delete(out)
                    return out
            logger.warning("JioSaavn download failed [jio] for: %s", vid_for_file)
            return None

        # 2) NexGenBots (/y)
        if mode in ("yt", "nexgen", "arc", "y"):
            out = await _nexgen_download(vid_for_file, is_video=video)
            if out:
                if os.path.exists(out):
                    _schedule_delete(out)
                return out
            logger.warning("NexGen download failed [yt/nexgen] for: %s", vid_for_file)
            return None

        # 3) Yuki Music API (/yuki or /yk, plus legacy /s, /shruti aliases)
        if mode in ("yuki", "yk", "shruti", "s"):
            out = await _yuki_download(vid_for_file, is_video=video)
            if out:
                _schedule_delete(out)
                return out
            logger.warning("Yuki download failed [yuki] for: %s", vid_for_file)
            return None

        # 4) NubCoders (/n) - Direct Stream Link or Local Download
        if mode in ("n", "nub", "nubcoders"):
            stream = await _nubcoders_get_stream_link(vid_for_file, is_video=video)
            if stream:
                return stream
            out = await _nubcoders_download(vid_for_file, is_video=video)
            if out:
                _schedule_delete(out)
                return out
            logger.warning("NubCoders download failed [nub] for: %s", vid_for_file)
            return None

        # 5) Auto Mode (/auto) - Fallback: Telegram CDN -> Yuki -> NubCoders -> yt-dlp
        if mode == "auto":
            tg_file = await _tg_channel_download_by_id(vid_for_file, is_video=video)
            if tg_file:
                _schedule_delete(tg_file)
                return tg_file
            if title:
                tg_file_t = await _tg_channel_download_by_title(title, is_video=video)
                if tg_file_t:
                    _schedule_delete(tg_file_t)
                    return tg_file_t

            yuki_out = await _yuki_download(vid_for_file, is_video=video)
            if yuki_out:
                _schedule_delete(yuki_out)
                return yuki_out

            nub_stream = await _nubcoders_get_stream_link(vid_for_file, is_video=video)
            if nub_stream:
                return nub_stream

            ytd = await _yt_dlp_download(vid_for_file, is_video=video)
            if ytd:
                _schedule_delete(ytd)
                return ytd

            logger.warning("All auto sources failed for: %s", vid_for_file)
            return None

        # 6) Default Fallback: NexGen -> Yuki -> TG CDN -> yt-dlp
        out = await _nexgen_download(vid_for_file, is_video=video)
        if out:
            if os.path.exists(out):
                _schedule_delete(out)
            return out

        yuki_out = await _yuki_download(vid_for_file, is_video=video)
        if yuki_out:
            _schedule_delete(yuki_out)
            return yuki_out

        tg_file = await _tg_channel_download_by_id(vid_for_file, is_video=video)
        if tg_file:
            _schedule_delete(tg_file)
            return tg_file

        ytd = await _yt_dlp_download(vid_for_file, is_video=video)
        if ytd:
            _schedule_delete(ytd)
            return ytd

        logger.warning("All sources failed [default] for: %s", vid_for_file)
        return None


# ----------------- helpers -------------------------------------------------------

def self_like(s: str) -> bool:
    if not s:
        return False
    return s.startswith("http://") or s.startswith("https://") or "youtube.com" in s or "youtu.be" in s


async def _download_via_url_to_file(url: str, filepath: str) -> Optional[str]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status != 200:
                    logger.warning("Failed to fetch URL %s HTTP %s", url, r.status)
                    return None
                import aiofiles
                async with aiofiles.open(filepath, "wb") as f:
                    async for chunk in r.content.iter_chunked(65536):
                        if chunk:
                            await f.write(chunk)
        return filepath
    except Exception as e:
        logger.warning("File write error: %s", e)
        return None
