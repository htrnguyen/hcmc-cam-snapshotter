# hcmc_sixcam_capture.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HCMC 6-CAM CAPTURE — headful debug & mở 6 tab cùng lúc

- Đọc đúng 6 camera từ 1 file chunk (JSON).
- Mở CÙNG LÚC 6 TAB (headful khi dùng --headful) và GIỮ NGUYÊN các tab qua nhiều vòng.
- Mỗi 15 giây (mặc định) chụp 6 cam song song (không chia 2/2/2).
- Chỉ lưu ảnh tĩnh: nếu nguồn là GIF → ép sang JPEG (frame đầu).
- Tên file theo giờ Việt Nam:
    images/<cam_id>__<code_slug>/<YYYYMMDD>/<cam_id>__<code_slug>__<YYYYMMDD>__<HHMMSS>__<hash8>.<ext>

Log:
- Mặc định: mỗi vòng chỉ in 1 dòng: "HH:MM:SS 6/6"
- Thêm --debug để in chi tiết từng cam: "HH:MM:SS 5/6  [✓ TTH_33.9] [× cam_xx] ..."

Chạy:
    python hcmc_sixcam_capture.py --chunk-file camera_catalog_chunks/cams_chunk_000.json

Chạy (headful + debug):
    python hcmc_sixcam_capture.py --chunk-file camera_catalog_chunks/cams_chunk_000.json --headful --debug
"""

import asyncio
import argparse
import json
import hashlib
import unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse
from io import BytesIO

from playwright.async_api import async_playwright, Page
from PIL import Image

# ---------- Timezone VN ----------
try:
    from zoneinfo import ZoneInfo  # Python 3.9+

    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


# ---------- Utils ----------
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def slugify(text: Optional[str]) -> str:
    if not text:
        return "nocode"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.strip().lower()
    out = [(ch if (ch.isalnum() or ch in "._- ") else "_") for ch in text]
    s = "".join(out).replace(" ", "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("._-") or "nocode"


def pick_ext_by_content_type(ct: Optional[str], url_hint: Optional[str]) -> str:
    if ct:
        ct = ct.lower()
        if "jpeg" in ct or "jpg" in ct:
            return ".jpg"
        if "png" in ct:
            return ".png"
        if "webp" in ct:
            return ".webp"
        if "gif" in ct:
            return ".gif"
    if url_hint:
        p = urlparse(url_hint).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            if p.endswith(ext):
                return ext
    return ".jpg"


def force_jpeg_if_gif(
    img_bytes: bytes, content_type: Optional[str], url_hint: Optional[str]
) -> Tuple[bytes, str, bool]:
    is_gif = False
    if content_type and "gif" in content_type.lower():
        is_gif = True
    elif img_bytes[:4] in (b"GIF8",):
        is_gif = True
    elif url_hint and urlparse(url_hint).path.lower().endswith(".gif"):
        is_gif = True
    if not is_gif:
        return img_bytes, pick_ext_by_content_type(content_type, url_hint), False

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
    return out.getvalue(), ".jpg", True


def build_filepath(
    save_dir: Path,
    cam_id: str,
    code_slug: str,
    when_vn: datetime,
    img_bytes: bytes,
    ext: str,
) -> Path:
    d = when_vn.strftime("%Y%m%d")
    t = when_vn.strftime("%H%M%S")  # HHMMSS (VN)
    h8 = sha256_bytes(img_bytes)[:8]
    folder = save_dir / f"{cam_id}__{code_slug}" / d
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{cam_id}__{code_slug}__{d}__{t}__{h8}{ext}"


async def get_displayed_img_url(page: Page) -> Optional[str]:
    try:
        return await page.evaluate(
            """() => {
              const imgs = Array.from(document.images || []);
              if (!imgs.length) return null;
              const big = imgs
                .filter(i => (i.naturalWidth||0) >= 80 && (i.naturalHeight||0) >= 80)
                .sort((a,b) => (b.naturalWidth*b.naturalHeight) - (a.naturalWidth*a.naturalHeight))[0];
              const pick = big || imgs[0];
              return pick.currentSrc || pick.src || null;
            }"""
        )
    except Exception:
        return None


async def fetch_img_bytes(
    page: Page, img_url: str, referer: str, timeout_s: int
) -> Tuple[Optional[bytes], Optional[str]]:
    try:
        resp = await page.context.request.get(
            img_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
                "Referer": referer,
                "Origin": "https://giaothong.hochiminhcity.gov.vn",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
            timeout=timeout_s * 1000,
        )
        if resp.ok:
            b = await resp.body()
            if b:
                ct = resp.headers.get("content-type")
                return b, ct
    except Exception:
        pass
    return None, None


# ---------- Init 6 tabs & keep them ----------
async def init_six_pages(
    context, cams: List[Dict[str, Any]], page_timeout: int
) -> List[Tuple[Dict[str, Any], Page]]:
    pages: List[Page] = []
    for _ in range(len(cams)):
        p = await context.new_page()
        pages.append(p)
    # goto all at once
    await asyncio.gather(
        *[
            p.goto(c["expand_url"], wait_until="domcontentloaded")
            for p, c in zip(pages, cams)
        ]
    )
    # small settle to avoid GIF loading at first view
    await asyncio.sleep(2.5)
    return list(zip(cams, pages))


async def capture_on_open_page(
    pair: Tuple[Dict[str, Any], Page], save_dir: Path, page_timeout: int
) -> Tuple[bool, str]:
    """
    Chụp ảnh ngay trên tab đã mở. Trả (ok, label_for_debug)
    """
    cam, page = pair
    cam_id = cam.get("cam_id") or "unknown"
    code = cam.get("code") or cam.get("title") or "nocode"
    code_slug = slugify(code)
    expand = cam.get("expand_url") or ""

    # lấy URL ảnh đang hiển thị; nếu rỗng → thử thêm 1 lần
    img_url = await get_displayed_img_url(page)
    if not img_url:
        await asyncio.sleep(1.5)
        img_url = await get_displayed_img_url(page)
        if not img_url:
            return False, code_slug

    # fetch bytes (1 retry nếu lỗi)
    b, ct = await fetch_img_bytes(page, img_url, expand, page_timeout)
    if not b:
        await asyncio.sleep(1.5)
        img_url = await get_displayed_img_url(page) or img_url
        b, ct = await fetch_img_bytes(page, img_url, expand, page_timeout)
        if not b:
            return False, code_slug

    # ép GIF → JPEG nếu cần rồi lưu
    b2, ext, _ = force_jpeg_if_gif(b, ct, img_url)
    ts_vn = now_vn()
    fpath = build_filepath(save_dir, cam_id, code_slug, ts_vn, b2, ext)
    fpath.write_bytes(b2)
    return True, code_slug


# ---------- Runner: 6 tabs parallel each cycle ----------
async def run_loop(
    cams: List[Dict[str, Any]],
    interval: float,
    offset: float,
    page_timeout: int,
    headless: bool,
    save_dir: Path,
    debug: bool,
):
    cams = cams[:6]  # ensure exactly 6 (or less if file smaller)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headless is False and not headless
        )  # not used; set via param below
        # Actually respect headful flag:
        await browser.close()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=not headless
        )  # headful if --headful
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
            extra_http_headers={
                "Referer": "https://giaothong.hochiminhcity.gov.vn/Map.aspx",
                "Origin": "https://giaothong.hochiminhcity.gov.vn",
                "Accept-Language": "vi,en;q=0.9",
            },
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
        )
        context.set_default_timeout(page_timeout * 1000)

        # Prewarm cookies
        try:
            p0 = await context.new_page()
            await p0.goto(
                "https://giaothong.hochiminhcity.gov.vn/Map.aspx",
                wait_until="domcontentloaded",
            )
            await p0.close()
        except Exception:
            pass

        # Open 6 tabs once and keep them
        cam_pages = await init_six_pages(context, cams, page_timeout)

        if offset > 0:
            await asyncio.sleep(offset)

        try:
            while True:
                start = asyncio.get_event_loop().time()
                ts_label = now_vn().strftime("%H:%M:%S")

                # capture all 6 in parallel
                tasks = [
                    asyncio.create_task(
                        capture_on_open_page(cp, save_dir, page_timeout)
                    )
                    for cp in cam_pages
                ]
                results = await asyncio.gather(*tasks)
                ok_total = sum(1 for ok, _ in results if ok)

                if debug:
                    # compact per-cam marks
                    parts = []
                    for ok, label in results:
                        lab = label[:12] if label else "cam"  # shorten
                        parts.append(f"[{'✓' if ok else '×'} {lab}]")
                    print(f"{ts_label} {ok_total}/6", *parts)
                else:
                    print(f"{ts_label} {ok_total}/6")

                # sleep to complete the interval
                elapsed = asyncio.get_event_loop().time() - start
                to_sleep = max(0.0, interval - elapsed)
                await asyncio.sleep(to_sleep)
        except KeyboardInterrupt:
            pass
        finally:
            await context.close()
            await browser.close()


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(
        description="HCMC 6-cam capture (6 tabs parallel, headful debug)"
    )
    ap.add_argument("--chunk-file", required=True, help="JSON chứa đúng 6 camera")
    ap.add_argument(
        "--interval", type=float, default=15.0, help="Chu kỳ chụp (giây), mặc định 15"
    )
    ap.add_argument(
        "--offset",
        type=float,
        default=0.0,
        help="Trễ khởi động (giây) để so-le giữa các runner",
    )
    ap.add_argument(
        "--page-timeout", type=int, default=20, help="Timeout tải ảnh (giây)"
    )
    ap.add_argument(
        "--headful",
        action="store_true",
        help="Mở trình duyệt có giao diện (debug trực quan)",
    )
    ap.add_argument(
        "--save-dir", default="images", help="Thư mục lưu ảnh (mặc định ./images)"
    )
    ap.add_argument(
        "--debug", action="store_true", help="In log chi tiết từng cam trong mỗi vòng"
    )
    args = ap.parse_args()

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    cams = json.loads(Path(args.chunk_file).read_text(encoding="utf-8"))
    if len(cams) < 1:
        raise SystemExit("Chunk rỗng.")
    if len(cams) != 6:
        print(
            f"[WARN] {args.chunk_file} có {len(cams)} cam (không phải 6). Vẫn chạy với số hiện có."
        )

    asyncio.run(
        run_loop(
            cams=cams,
            interval=args.interval,
            offset=args.offset,
            page_timeout=args.page_timeout,
            headless=args.headful,  # True => headful
            save_dir=save_dir,
            debug=args.debug,
        )
    )


if __name__ == "__main__":
    main()
