# hcmc_sixcam_capture.py (tối giản + debug console)
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import asyncio
import hashlib
import json
import unicodedata
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from PIL import Image
from playwright.async_api import Page, async_playwright

# ========================== CẤU HÌNH ==========================
INTERVAL_SEC = 15.0
OFFSET_SEC = 0.0
PAGE_TIMEOUT_S = 20
SAVE_DIR = Path("images")
HEADFUL = True  # mở browser GUI
DEBUG = True  
# ===============================================================

# Timezone VN
try:
    from zoneinfo import ZoneInfo

    VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
except Exception:
    VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


def now_vn() -> datetime:
    return datetime.now(VN_TZ)


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


def force_jpeg_if_gif(
    img_bytes: bytes, content_type: Optional[str], url_hint: Optional[str]
) -> Tuple[bytes, str]:
    is_gif = False
    if content_type and "gif" in content_type.lower():
        is_gif = True
    elif img_bytes[:4] in (b"GIF8",):
        is_gif = True
    elif url_hint and urlparse(url_hint).path.lower().endswith(".gif"):
        is_gif = True
    if not is_gif:
        return img_bytes, ".jpg"
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
    return out.getvalue(), ".jpg"


def build_filepath(
    save_dir: Path,
    cam_id: str,
    code_slug: str,
    when_vn: datetime,
    img_bytes: bytes,
    ext: str,
) -> Path:
    d = when_vn.strftime("%Y%m%d")
    t = when_vn.strftime("%H%M%S")
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
                .filter(i => (i.naturalWidth||0)>=80 && (i.naturalHeight||0)>=80)
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
            headers={"Referer": referer},
            timeout=PAGE_TIMEOUT_S * 1000,
        )
        if resp.ok:
            b = await resp.body()
            return b, resp.headers.get("content-type")
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
    await asyncio.sleep(2.5)
    return list(zip(cams, pages))


async def capture_on_open_page(pair: Tuple[Dict[str, Any], Page]) -> Tuple[bool, str]:
    cam, page = pair
    cam_id = cam.get("cam_id") or "unknown"
    code_slug = slugify(cam.get("code") or cam.get("title") or "nocode")
    referer = cam.get("expand_url") or ""
    img_url = await get_displayed_img_url(page)
    if not img_url:
        return False, code_slug
    b, ct = await fetch_img_bytes(page, img_url, referer)
    if not b:
        return False, code_slug
    b2, ext = force_jpeg_if_gif(b, ct, img_url)
    fpath = build_filepath(SAVE_DIR, cam_id, code_slug, now_vn(), b2, ext)
    fpath.write_bytes(b2)
    return True, code_slug


async def run_loop(cams: List[Dict[str, Any]]):
    cams = cams[:6]
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not HEADFUL)
        context = await browser.new_context()
        cam_pages = await init_six_pages(context, cams)
        if OFFSET_SEC > 0:
            await asyncio.sleep(OFFSET_SEC)
        try:
            while True:
                start = asyncio.get_event_loop().time()
                results = await asyncio.gather(
                    *[capture_on_open_page(cp) for cp in cam_pages]
                )
                ts_label = now_vn().strftime("%H:%M:%S")
                ok_total = sum(1 for ok, _ in results if ok)
                den = len(results)
                if DEBUG:
                    parts = []
                    for ok, lab in results:
                        lab = (
                            lab
                            if len(lab) <= 12
                            else lab[:10] + "_" + sha256_bytes(lab.encode())[:4]
                        )
                        parts.append(f"[{'✓' if ok else '×'} {lab}]")
                    print(f"{ts_label} {ok_total}/{den}", *parts)
                else:
                    print(f"{ts_label} {ok_total}/{den}")
                elapsed = asyncio.get_event_loop().time() - start
                await asyncio.sleep(max(0.0, INTERVAL_SEC - elapsed))
        except KeyboardInterrupt:
            pass
        finally:
            await context.close()
            await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-file", required=True)
    args = ap.parse_args()
    cams = json.loads(Path(args.chunk_file).read_text(encoding="utf-8"))
    asyncio.run(run_loop(cams))


if __name__ == "__main__":
    main()
