import asyncio
import re
import aiofiles
import aiohttp


def extract_video_id(url_or_id: str) -> str:
    """Extracts 11-character YouTube video ID from raw input or full URLs."""
    if not url_or_id:
        return ""
    regex = r"(?:v=|\/([0-9A-Za-z_-]{11}).*|youtu\.be\/([0-9A-Za-z_-]{11}))"
    match = re.search(regex, str(url_or_id))
    if match:
        return match.group(1) or match.group(2)
    return str(url_or_id).strip()


class NexGenApi:
    def __init__(
        self,
        api_url: str,
        api_key: str,
        video_api_url: str,
        retries: int = 10,
        timeout: int = 40,
    ):
        self.api_url = api_url
        self.video_api_url = video_api_url
        self.api_key = api_key
        self.chunk_limit = 128 * 1024
        self.dl_cache = {}
        self.v_cache = {}
        self.retries = retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept": "application/json"}

    async def get_session(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def save_file(self, vid_id: str, url: str, video: bool = False) -> str | None:
        try:
            await self.get_session()
            async with self.session.get(url) as resp:
                if resp.status != 200:
                    return None

                file_name = None
                cd = resp.headers.get("Content-Disposition")
                if cd:
                    match = re.search(r'filename="?(.+?)"?$', cd)
                    if match:
                        file_name = match.group(1)
                if not file_name:
                    file_name = vid_id + (".mp4" if video else ".mp3")

                fname = f"downloads/{file_name}"
                async with aiofiles.open(fname, "wb") as f:
                    async for chunk in resp.content.iter_chunked(self.chunk_limit):
                        if chunk:
                            await f.write(chunk)

                if video:
                    self.v_cache[vid_id] = fname
                else:
                    self.dl_cache[vid_id] = fname

                return fname
        except Exception:
            pass
        return None

    async def download(self, vid_id: str, video: bool = False) -> str | None:
        vid_id = extract_video_id(vid_id)
        if video and vid_id in self.v_cache:
            return self.v_cache[vid_id]
        elif not video and vid_id in self.dl_cache:
            return self.dl_cache[vid_id]

        await self.get_session()
        endp = f"{self.api_url}/song/{vid_id}?api={self.api_key}"
        if video:
            endp = f"{self.video_api_url}/video/{vid_id}?api={self.api_key}"

        for _ in range(self.retries):
            try:
                async with self.session.get(endp, headers=self.headers) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

                    status = data.get("status")
                    dl_link = data.get("link")
                    if not status:
                        return None

                    if status == "done":
                        if not dl_link:
                            return None
                        return await self.save_file(vid_id, dl_link, video)
                    elif status == "downloading":
                        await asyncio.sleep(4)
                        continue
                    else:
                        break
            except Exception:
                break
        return None


class YukiApi:
    """
    Direct-stream API for:
      https://music.yukiapi.site/stream/{VIDEO_ID}?key={API_KEY}&type=audio
      https://play.yukiapi.site/stream/{VIDEO_ID}?key={API_KEY}&type=audio
      https://music.yukiapi.site/stream/{VIDEO_ID}?key={API_KEY}&type=video
    """

    def __init__(
        self,
        api_key: str,
        primary_url: str = "https://music.yukiapi.site",
        backup_url: str = "https://play.yukiapi.site",
        retries: int = 5,
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.primary_url = primary_url.rstrip("/")
        self.backup_url = backup_url.rstrip("/")
        self.chunk_limit = 128 * 1024
        self.dl_cache = {}
        self.v_cache = {}
        self.retries = retries
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None
        self.headers = {"Accept": "application/json"}

    async def get_session(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def download(self, vid_id: str, video: bool = False) -> str | None:
        """
        Return the stream URL directly from Yuki API.
        This is simpler and matches the API docs:
        /stream/{VIDEO_ID}?key={KEY}&type=audio|video
        """
        vid_id = extract_video_id(vid_id)
        if video and vid_id in self.v_cache:
            return self.v_cache[vid_id]
        elif not video and vid_id in self.dl_cache:
            return self.dl_cache[vid_id]

        await self.get_session()
        media_type = "video" if video else "audio"

        urls = [
            f"{self.primary_url}/stream/{vid_id}?key={self.api_key}&type={media_type}",
            f"{self.backup_url}/stream/{vid_id}?key={self.api_key}&type={media_type}",
        ]

        for idx, stream_url in enumerate(urls):
            try:
                async with self.session.get(stream_url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        if not video and vid_id not in self.dl_cache:
                            self.dl_cache[vid_id] = stream_url
                        if video and vid_id not in self.v_cache:
                            self.v_cache[vid_id] = stream_url
                        return stream_url
            except Exception:
                pass

            if idx == len(urls) - 1:
                return None

        return None
