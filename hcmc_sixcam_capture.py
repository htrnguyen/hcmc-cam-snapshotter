"""
HCMC 6-CAM CAPTURE (PRO)

- Mỗi lần chạy xử lý đúng 6 camera cố định (đọc từ 1 file chunk).
- Chu kỳ 15 giây: chụp 6 cam theo mô hình 2+2+2 (subslots) để tránh bị chặn.
- BẮT BUỘC truy cập expand_url bằng Playwright; lấy ảnh đang hiển thị (img lớn nhất).
- Chỉ lưu ảnh tĩnh: nếu nguồn là GIF → ép sang JPEG (frame đầu).
- Tên file chuyên nghiệp theo giờ Việt Nam:
    images/<cam_id>__<code_slug>/<YYYYMMDD>/<cam_id>__<code_slug>__<YYYYMMDD>__<HHMMSS>__<hash8>.<ext>

Usage:
    python hcmc_sixcam_capture.py --chunk-file camera_catalog_chunks/cams_chunk_000.json
Optional:
    --interval 15 --offset 0 --headful --page-timeout 20
"""

import asyncio
import argparse
import json
import hashlib
from socket import timeout
import time
import unicodedata
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse
from io import BytesIO

from playwright.async_api import async_playwright
from PIL import Image

# ---------- Timezone: Việt Nam ----------
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
    out = []
    for ch in text:
        out.append(ch if (ch.isalnum() or ch in "._- ") else "_")
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
    t = when_vn.strftime("%H%M%S")  # HHMMSS theo giờ VN
    h8 = sha256_bytes(img_bytes)[:8]
    folder = save_dir / f"{cam_id}__{code_slug}" / d
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{cam_id}__{code_slug}__{d}__{t}__{h8}{ext}"


async def get_displayed_img_url(page) -> Optional[str]:
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
    page, img_url: str, referer: str, timeout_s: int
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


async def capture_one(
    context, cam: Dict[str, Any], page_timeout: int, save_dir: Path, gif_retry: int
) -> Dict[str, Any]:
    cam_id = cam.get("cam_id") or "unknown"
    code = cam.get("code") or cam.get("title") or "nocode"
    title = cam.get("title") or "untitled"
    expand = cam.get("expand_url")
    code_slug = slugify(code)

    result = {
        "cam_id": cam_id,
        "code": code,
        "title": title,
        "ok": False,
        "file_path": None,
        "error": None,
    }
    if not expand:
        result["error"] = "Missing expand_url"
        return result

    page = await context.new_page()
    try:
        await page.goto(expand, wait_until="domcontentloaded")
        # Tránh GIF loading ban đầu
        await asyncio.sleep(2.5)

        img_url = await get_displayed_img_url(page)
        if not img_url:
            await asyncio.sleep(1.5)
            img_url = await get_displayed_img_url(page)
            if not img_url:
                result["error"] = "No displayed image"
                return result

        b, ct = await fetch_img_bytes(page, img_url, expand, page_timeout)
        if (not b) and gif_retry:
            await asyncio.sleep(2.0)
            img_url = await get_displayed_img_url(page) or img_url
            b, ct = await fetch_img_bytes(page, img_url, expand, page_timeout)

        if not b:
            result["error"] = "Fetch image failed"
            return result

        b2, ext, _ = force_jpeg_if_gif(b, ct, img_url)
        ts_vn = now_vn()
        fpath = build_filepath(save_dir, cam_id, code_slug, ts_vn, b2, ext)
        fpath.write_bytes(b2)
        result["ok"] = True
        result["file_path"] = str(fpath)
        print(f"[OK] {title} -> {result['file_path']}")
        return result
    except Exception as e:
        result["error"] = str(e)
        print(f"[ERR] {title} -> {result['error']}")
        return result
    finally:
        await page.close()


# ---------- Runner (2+2+2 per 15s) ----------
async def run_loop(
    cams: List[Dict[str, Any]],
    interval: float,
    offset: float,
    page_timeout: int,
    headless: bool,
    save_dir: Path,
):
    # Giữ đúng 6 cam
    cams = cams[:6]

    subslots = [(0.0, 2), (5.0, 2), (10.0, 2)]  # (delay, count)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        # Dùng 1 context chung để giữ cookies & giảm overhead
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

        # Pre-warm cookie
        try:
            p = await context.new_page()
            await p.goto(
                "https://giaothong.hochiminhcity.gov.vn/Map.aspx",
                wait_until="domcontentloaded",
            )
            await p.close()
        except Exception:
            pass

        if offset > 0:
            await asyncio.sleep(offset)

        try:
            while True:
                t0 = time.perf_counter()
                idx = 0

                for delay, cnt in subslots:
                    # chờ tới mốc subslot
                    remaining = delay - (time.perf_counter() - t0)
                    if remaining > 0:
                        await asyncio.sleep(remaining)

                    batch = cams[idx : idx + cnt]
                    idx += cnt

                    tasks = [
                        asyncio.create_task(
                            capture_one(context, c, page_timeout, save_dir, gif_retry=1)
                        )
                        for c in batch
                    ]
                    await asyncio.gather(*tasks)

                # ngủ đến hết chu kỳ
                elapsed = time.perf_counter() - t0
                sleep_left = max(0.0, interval - elapsed)
                await asyncio.sleep(sleep_left)
        except KeyboardInterrupt:
            pass
        finally:
            await context.close()
            await browser.close()


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(
        description="HCMC 6-cam capture (expand_url → static image)"
    )
    ap.add_argument(
        "--chunk-file", required=True, help="JSON chứa đúng 6 camera (tạo từ split)"
    )
    ap.add_argument("--interval", type=float, default=15.0, help="Chu kỳ chụp (giây)")
    ap.add_argument(
        "--offset",
        type=float,
        default=0.0,
        help="Trễ khởi động (giây) để so-le giữa các chunk",
    )
    ap.add_argument(
        "--page-timeout", type=int, default=20, help="Timeout tải ảnh (giây)"
    )
    ap.add_argument(
        "--headful", action="store_true", help="Mở trình duyệt có giao diện (debug)"
    )
    ap.add_argument("--save-dir", default="images", help="Thư mục lưu ảnh")
    args = ap.parse_args()

    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    cams = json.loads(Path(args.chunk_file).read_text(encoding="utf-8"))
    if len(cams) < 1:
        raise SystemExit("Chunk rỗng.")
    if len(cams) != 6:
        print(
            f"[WARN] File {args.chunk_file} có {len(cams)} cam (không phải 6). Vẫn chạy với số hiện có."
        )

    asyncio.run(
        run_loop(
            cams=cams,
            interval=args.interval,
            offset=args.offset,
            page_timeout=(
                args.page - timeout
                if hasattr(args, "page-timeout")
                else args.page_timeout
            ),  # safeguard
            headless=not args.headful,
            save_dir=save_dir,
        )
    )


if __name__ == "__main__":
    main()
