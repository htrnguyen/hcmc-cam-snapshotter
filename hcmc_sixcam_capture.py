import argparse
import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import sys
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import Page, async_playwright

# -------- Cấu hình --------
INTERVAL_SEC = 15.0
PAGE_TIMEOUT_S = 20
HEADFUL = False
DB_ROOT = Path("sqlite_dataset")
STORE_BASE64 = False
DEBUG = True  
# -----------------------------------

try:
    from zoneinfo import ZoneInfo

    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


def slugify(text: Optional[str]) -> str:
    if not text:
        return "nocode"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.strip().lower()
    s = "".join((ch if (ch.isalnum() or ch in "._- ") else "_") for ch in text).replace(
        " ", "_"
    )
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("._-") or "nocode"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def short_lab(lab: str) -> str:
    return lab if len(lab) <= 12 else (lab[:10] + "_" + sha256_bytes(lab.encode())[:4])


def force_jpeg_if_gif(
    img_bytes: bytes, content_type: Optional[str], url_hint: Optional[str]
) -> Tuple[bytes, str, bool, Optional[int], Optional[int]]:
    is_gif = False
    if content_type and "gif" in content_type.lower():
        is_gif = True
    elif img_bytes[:4] in (b"GIF8",):
        is_gif = True
    elif url_hint and urlparse(url_hint).path.lower().endswith(".gif"):
        is_gif = True
    if not is_gif:
        w = h = None
        try:
            im = Image.open(BytesIO(img_bytes))
            w, h = im.size
        except Exception:
            pass
        ext = ".jpg"
        if content_type:
            ct = content_type.lower()
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
        elif url_hint:
            p = urlparse(url_hint).path.lower()
            for e in (".jpg", ".jpeg", ".png", ".webp"):
                if p.endswith(e):
                    ext = ".jpg" if e == ".jpeg" else e
        return img_bytes, ext, False, w, h
    im = Image.open(BytesIO(img_bytes))
    try:
        im.seek(0)
    except Exception:
        pass
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        if im.mode == "P":
            im = im.convert("RGBA")
        bg.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
        im = bg
    else:
        im = im.convert("RGB")
    out = BytesIO()
    im.save(out, format="JPEG", quality=90, optimize=True)
    b2 = out.getvalue()
    w, h = im.size
    return b2, ".jpg", True, w, h


async def get_displayed_img_url(page: Page) -> Optional[str]:
    try:
        return await page.evaluate(
            """() => {
              const imgs = Array.from(document.images || []);
              if (!imgs.length) return null;
              const big = imgs.filter(i => (i.naturalWidth||0)>=80 && (i.naturalHeight||0)>=80)
                              .sort((a,b)=>(b.naturalWidth*b.naturalHeight)-(a.naturalWidth*a.naturalHeight))[0];
              const pick = big || imgs[0];
              return pick.currentSrc || pick.src || null;
            }"""
        )
    except Exception:
        return None


async def fetch_img_bytes(
    page: Page, img_url: str, referer: str
) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        resp = await page.context.request.get(
            img_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
                "Origin": "https://giaothong.hochiminhcity.gov.vn",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
            timeout=PAGE_TIMEOUT_S * 1000,
        )
        if resp.ok:
            return await resp.body(), resp.headers.get("content-type")
    except Exception:
        pass
    return None, None


async def init_six_pages(
    context, cams: List[Dict[str, Any]]
) -> List[Tuple[Dict[str, Any], Page]]:
    pages: List[Page] = [await context.new_page() for _ in range(len(cams))]
    await asyncio.gather(
        *[
            p.goto(c["expand_url"], wait_until="domcontentloaded")
            for p, c in zip(pages, cams)
        ]
    )
    await asyncio.sleep(2.0)
    return list(zip(cams, pages))


class DayDb:
    def __init__(self, db_root: Path, cam_id: str, chunk_file: str):
        self.db_root = db_root
        self.cam_id = cam_id
        self.chunk_file = chunk_file
        self.conn: Optional[sqlite3.Connection] = None
        self.current_date_str: Optional[str] = None
        (self.db_root / self.cam_id).mkdir(parents=True, exist_ok=True)

    def _db_path_for_date(self, date_str: str) -> Path:
        return self.db_root / self.cam_id / f"{date_str}.sqlite"

    def _open_conn_for_date(self, date_str: str):
        if self.conn:
            self.conn.close()
        db_path = self._db_path_for_date(date_str)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA temp_store=MEMORY;")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS frames (
                cam_id       TEXT NOT NULL,
                ts_vn_ms     INTEGER NOT NULL,
                ts_vn_iso    TEXT NOT NULL,
                chunk_file   TEXT,
                code_slug    TEXT,
                expand_url   TEXT,
                img_url      TEXT,
                content_type TEXT,
                ext          TEXT,
                w            INTEGER,
                h            INTEGER,
                sha256       TEXT,
                was_gif      INTEGER,
                ok           INTEGER,
                err          TEXT,
                img_bytes    BLOB,
                img_b64      TEXT
            );
            """
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_frames_cam_ts ON frames(cam_id, ts_vn_ms);"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_frames_ts ON frames(ts_vn_ms);"
        )
        self.conn.commit()
        self.current_date_str = date_str

    def _ensure_open_for_today(self, ts_vn: datetime):
        date_str = ts_vn.strftime("%Y-%m-%d")
        if self.conn and self.current_date_str == date_str:
            return
        self._open_conn_for_date(date_str)

    def upsert_one(self, row: Dict[str, Any]):
        ts: datetime = row["ts_vn"]
        self._ensure_open_for_today(ts)
        ts_ms = int(ts.timestamp() * 1000)
        payload = {
            "cam_id": row["cam_id"],
            "ts_vn_ms": ts_ms,
            "ts_vn_iso": ts.isoformat(),
            "chunk_file": row.get("chunk_file", ""),
            "code_slug": row.get("code_slug", ""),
            "expand_url": row.get("expand_url", ""),
            "img_url": row.get("img_url", ""),
            "content_type": row.get("content_type", ""),
            "ext": row.get("ext", ""),
            "w": row.get("w"),
            "h": row.get("h"),
            "sha256": row.get("sha256", ""),
            "was_gif": 1 if row.get("was_gif") else 0,
            "ok": 1 if row.get("ok") else 0,
            "err": row.get("err", ""),
        }
        if STORE_BASE64:
            payload["img_b64"] = row.get("img_b64", "")
            self.conn.execute(
                """
                INSERT INTO frames (
                    cam_id, ts_vn_ms, ts_vn_iso, chunk_file, code_slug, expand_url,
                    img_url, content_type, ext, w, h, sha256, was_gif, ok, err, img_b64
                ) VALUES (
                    :cam_id, :ts_vn_ms, :ts_vn_iso, :chunk_file, :code_slug, :expand_url,
                    :img_url, :content_type, :ext, :w, :h, :sha256, :was_gif, :ok, :err, :img_b64
                )
                ON CONFLICT(cam_id, ts_vn_ms) DO UPDATE SET
                    ts_vn_iso=excluded.ts_vn_iso,
                    chunk_file=excluded.chunk_file,
                    code_slug=excluded.code_slug,
                    expand_url=excluded.expand_url,
                    img_url=excluded.img_url,
                    content_type=excluded.content_type,
                    ext=excluded.ext,
                    w=excluded.w,
                    h=excluded.h,
                    sha256=excluded.sha256,
                    was_gif=excluded.was_gif,
                    ok=excluded.ok,
                    err=excluded.err,
                    img_b64=excluded.img_b64
                """,
                payload,
            )
        else:
            img_bytes = row.get("img_bytes", b"")
            self.conn.execute(
                """
                INSERT INTO frames (
                    cam_id, ts_vn_ms, ts_vn_iso, chunk_file, code_slug, expand_url,
                    img_url, content_type, ext, w, h, sha256, was_gif, ok, err, img_bytes
                ) VALUES (
                    :cam_id, :ts_vn_ms, :ts_vn_iso, :chunk_file, :code_slug, :expand_url,
                    :img_url, :content_type, :ext, :w, :h, :sha256, :was_gif, :ok, :err, :img_bytes
                )
                ON CONFLICT(cam_id, ts_vn_ms) DO UPDATE SET
                    ts_vn_iso=excluded.ts_vn_iso,
                    chunk_file=excluded.chunk_file,
                    code_slug=excluded.code_slug,
                    expand_url=excluded.expand_url,
                    img_url=excluded.img_url,
                    content_type=excluded.content_type,
                    ext=excluded.ext,
                    w=excluded.w,
                    h=excluded.h,
                    sha256=excluded.sha256,
                    was_gif=excluded.was_gif,
                    ok=excluded.ok,
                    err=excluded.err,
                    img_bytes=excluded.img_bytes
                """,
                {**payload, "img_bytes": sqlite3.Binary(img_bytes)},
            )
        self.conn.commit()

    def close(self):
        if self.conn:
            try:
                self.conn.commit()
            finally:
                self.conn.close()
        self.conn = None
        self.current_date_str = None


async def capture_and_record(
    pair: Tuple[Dict[str, Any], Page], sink: DayDb
) -> Tuple[bool, str]:
    cam, page = pair
    cam_id = cam.get("cam_id") or "unknown"
    code_slug = slugify(cam.get("code") or cam.get("title") or "nocode")
    expand = cam.get("expand_url") or ""
    ts = now_vn()

    ok = False
    err = None
    img_url = await get_displayed_img_url(page)
    out_bytes = None
    content_type = None
    ext = None
    w = h = None
    was_gif = False

    if not img_url:
        await asyncio.sleep(1.0)
        img_url = await get_displayed_img_url(page)

    if img_url:
        b, ct = await fetch_img_bytes(page, img_url, expand)
        if not b:
            await asyncio.sleep(1.0)
            b, ct = await fetch_img_bytes(page, img_url, expand)
        if b:
            b2, ext, was_gif, w, h = force_jpeg_if_gif(b, ct, img_url)
            out_bytes = b2
            ok = True
            content_type = "image/jpeg" if was_gif else (ct or "image/jpeg")
        else:
            err = "fetch_failed"
    else:
        err = "no_img_found"

    sha_hex = sha256_bytes(out_bytes) if (ok and out_bytes) else ""
    row: Dict[str, Any] = {
        "ts_vn": ts,
        "chunk_file": sink.chunk_file,
        "cam_id": cam_id,
        "code_slug": code_slug,
        "expand_url": expand,
        "img_url": img_url or "",
        "content_type": content_type or "",
        "ext": ext or "",
        "w": w if isinstance(w, int) else None,
        "h": h if isinstance(h, int) else None,
        "sha256": sha_hex,
        "was_gif": was_gif,
        "ok": ok,
        "err": err or "",
    }
    if STORE_BASE64:
        row["img_b64"] = (
            base64.b64encode(out_bytes).decode("ascii") if (ok and out_bytes) else ""
        )
    else:
        row["img_bytes"] = out_bytes if (ok and out_bytes) else b""

    try:
        sink.upsert_one(row)
    except Exception:
        return False, code_slug
    return ok, code_slug


# ----------- ESC listener (Windows & Unix) -----------
def start_esc_listener(stop_event: threading.Event):
    if os.name == "nt":
        import msvcrt

        def worker():
            while not stop_event.is_set():
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch in (b"\x1b",):  # ESC
                        stop_event.set()
                        break

        threading.Thread(target=worker, daemon=True).start()
    else:
        import termios, tty, select

        def worker():
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setcbreak(fd)
                while not stop_event.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch == "\x1b":  # ESC
                            stop_event.set()
                            break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)

        threading.Thread(target=worker, daemon=True).start()


# ------------------------------ RUN LOOP ------------------------------
async def run_loop(cams: List[Dict[str, Any]], chunk_file_name: str):
    cams = cams[:6]
    DB_ROOT.mkdir(parents=True, exist_ok=True)
    sinks: Dict[str, DayDb] = {
        (c.get("cam_id") or "unknown"): DayDb(
            DB_ROOT, c.get("cam_id") or "unknown", chunk_file_name
        )
        for c in cams
    }

    stop_event = threading.Event()
    start_esc_listener(stop_event)

    browser = None
    context = None
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not HEADFUL)
            context = await browser.new_context()
            cam_pages = await init_six_pages(context, cams)

            while not stop_event.is_set():
                start = asyncio.get_event_loop().time()
                ts_label = now_vn().strftime("%H:%M:%S")

                results = await asyncio.gather(
                    *[
                        asyncio.create_task(
                            capture_and_record(
                                (cam, page), sinks[cam.get("cam_id") or "unknown"]
                            )
                        )
                        for cam, page in cam_pages
                    ],
                    return_exceptions=True,
                )

                # tổng hợp & in debug
                oks: List[bool] = []
                labs: List[str] = []
                for r, (cam, _p) in zip(results, cam_pages):
                    lab = slugify(cam.get("code") or cam.get("title") or "nocode")
                    if isinstance(r, Exception):
                        oks.append(False)
                        labs.append(lab)
                    else:
                        ok, lab2 = r
                        oks.append(bool(ok))
                        labs.append(lab2 or lab)

                ok_total = sum(1 for x in oks if x)
                den = len(oks)
                if DEBUG:
                    parts = [
                        f"[{'✓' if o else '×'} {short_lab(l)}]"
                        for o, l in zip(oks, labs)
                    ]
                    print(f"{ts_label} {ok_total}/{den}", *parts)
                else:
                    print(f"{ts_label} {ok_total}/{den}")

                elapsed = asyncio.get_event_loop().time() - start
                await asyncio.sleep(max(0.0, INTERVAL_SEC - elapsed))
    finally:
        try:
            if context:
                await context.close()
        except Exception:
            pass
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        for s in sinks.values():
            try:
                s.close()
            except Exception:
                pass


# --------------------------------- CLI ---------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-file", required=True)
    args = ap.parse_args()
    p = Path(args.chunk_file)
    cams = json.loads(p.read_text(encoding="utf-8"))
    asyncio.run(run_loop(cams, chunk_file_name=p.name))


if __name__ == "__main__":
    main()
