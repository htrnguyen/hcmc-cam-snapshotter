import argparse
import base64
import sqlite3
from pathlib import Path
from datetime import datetime
from io import BytesIO

from PIL import Image

# -------------------- helpers --------------------


def ensure_ext(ext: str) -> str:
    if not ext:
        return ".jpg"
    ext = ext.strip().lower()
    if not ext.startswith("."):
        ext = "." + ext
    return ".jpg" if ext == ".jpeg" else ext


def guess_ext_from_ct(ct: str | None) -> str:
    if not ct:
        return ".jpg"
    ct = ct.lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    return ".jpg"


def parse_ts(ts_ms, ts_iso) -> datetime:
    if ts_ms is not None:
        try:
            return datetime.fromtimestamp(int(ts_ms) / 1000.0)
        except Exception:
            pass
    if ts_iso:
        try:
            return datetime.fromisoformat(str(ts_iso))
        except Exception:
            pass
    raise ValueError(f"Không parse được timestamp: ts_ms={ts_ms!r}, ts_iso={ts_iso!r}")


def safe_slug(s: str | None) -> str:
    s = (s or "nocode").strip()
    return s[:80] or "nocode"


def pil_save_or_bytes(img_bytes: bytes, out_path: Path):
    try:
        with Image.open(BytesIO(img_bytes)) as im:
            im.save(out_path)
    except Exception:
        out_path.write_bytes(img_bytes)


# -------------------- main export --------------------


def export_sqlite(
    db_path: Path, out_root: Path, batch: int = 2000
) -> tuple[int, int, int]:
    """
    Đọc 1 file sqlite_dataset/<CAM_ID>/<YYYY-MM-DD>.sqlite và xuất ảnh ra:
      images_export/<CAM_ID>/<YYYYMMDD>/<CAM_ID>__<code_slug>__<YYYYMMDD>__<HHMMSS>__<sha8>.<ext>

    - Nếu file ảnh đã tồn tại → bỏ qua (đã xử lý).
    - Nếu chưa có → giải nén và ghi ra (xử lý tiếp).
    - Tự nhận diện bảng ('frames' hoặc 'captures') và các cột có sẵn.
    """
    if not db_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file: {db_path}")

    cam_id = db_path.parent.name
    date_str = db_path.stem  # YYYY-MM-DD
    date_compact = date_str.replace("-", "")
    out_dir = out_root / cam_id / date_compact
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # chọn bảng
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    table = (
        "frames"
        if "frames" in tables
        else ("captures" if "captures" in tables else None)
    )
    if not table:
        raise ValueError(f"Không tìm thấy bảng dữ liệu. Các bảng có: {sorted(tables)}")

    # cột có trong bảng
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    # map tên cột linh hoạt cho 2 schema
    col_cam = "cam_id" if "cam_id" in cols else None
    col_ts_ms = "ts_vn_ms" if "ts_vn_ms" in cols else None
    col_ts_iso = (
        "ts_vn_iso" if "ts_vn_iso" in cols else ("ts_vn" if "ts_vn" in cols else None)
    )
    col_slug = (
        "code_slug"
        if "code_slug" in cols
        else ("code" if "code" in cols else ("title" if "title" in cols else None))
    )
    col_sha = "sha256" if "sha256" in cols else None
    col_ok = "ok" if "ok" in cols else None
    col_ext = "ext" if "ext" in cols else None
    col_ct = "content_type" if "content_type" in cols else None
    col_w = "w" if "w" in cols else None
    col_h = "h" if "h" in cols else None
    col_img_bytes = "img_bytes" if "img_bytes" in cols else None
    col_img_b64 = "img_b64" if "img_b64" in cols else None

    # kiểm tra tối thiểu
    need_min = [col_cam, col_slug]
    if not all(need_min):
        raise ValueError(f"Thiếu cột cơ bản trong bảng {table}. Có cột: {sorted(cols)}")

    if not (col_img_bytes or col_img_b64):
        raise ValueError(f"Thiếu cột ảnh (img_bytes/img_b64) trong bảng {table}")

    if not (col_ts_ms or col_ts_iso):
        raise ValueError(
            f"Thiếu cột thời gian (ts_vn_ms/ts_vn_iso/ts_vn) trong bảng {table}"
        )

    # điều kiện WHERE
    conds = []
    if col_ok:
        conds.append(f"{col_ok}=1")
    has_b64_cond = (
        f"{col_img_b64} IS NOT NULL AND {col_img_b64} <> ''" if col_img_b64 else None
    )
    has_bytes_cond = f"{col_img_bytes} IS NOT NULL" if col_img_bytes else None
    if has_b64_cond and has_bytes_cond:
        conds.append(f"(({has_bytes_cond}) OR ({has_b64_cond}))")
    elif has_bytes_cond:
        conds.append(has_bytes_cond)
    elif has_b64_cond:
        conds.append(has_b64_cond)
    where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""

    # chọn cột để SELECT
    select_cols = [col_cam, col_slug]
    if col_ts_ms:
        select_cols.append(f"{col_ts_ms} AS ts_ms")
    else:
        select_cols.append(f"NULL AS ts_ms")
    if col_ts_iso:
        select_cols.append(f"{col_ts_iso} AS ts_iso")
    else:
        select_cols.append(f"NULL AS ts_iso")
    if col_sha:
        select_cols.append(f"{col_sha} AS sha256")
    else:
        select_cols.append("NULL AS sha256")
    if col_ext:
        select_cols.append(f"{col_ext} AS ext")
    else:
        select_cols.append("NULL AS ext")
    if col_ct:
        select_cols.append(f"{col_ct} AS content_type")
    else:
        select_cols.append("NULL AS content_type")
    if col_w:
        select_cols.append(f"{col_w} AS w")
    else:
        select_cols.append("NULL AS w")
    if col_h:
        select_cols.append(f"{col_h} AS h")
    else:
        select_cols.append("NULL AS h")
    if col_img_bytes:
        select_cols.append(f"{col_img_bytes} AS img_bytes")
    else:
        select_cols.append("NULL AS img_bytes")
    if col_img_b64:
        select_cols.append(f"{col_img_b64} AS img_b64")
    else:
        select_cols.append("NULL AS img_b64")

    order_sql = "ORDER BY " + (
        col_ts_ms if col_ts_ms else (col_ts_iso if col_ts_iso else col_cam)
    )
    base_sql = f"SELECT {', '.join(select_cols)} FROM {table} {where_sql} {order_sql} LIMIT ? OFFSET ?"

    exported = 0
    skipped = 0
    total_ok = 0
    offset = 0

    while True:
        rows = conn.execute(base_sql, (batch, offset)).fetchall()
        if not rows:
            break
        for row in rows:
            total_ok += 1

            ts = parse_ts(row["ts_ms"], row["ts_iso"])
            slug = safe_slug(row[col_slug])
            sha_hex = (row["sha256"] or "").strip()
            sha8 = sha_hex[:8] if sha_hex else "nohash"

            ext = ensure_ext(row["ext"] or guess_ext_from_ct(row["content_type"]))
            hhmmss = ts.strftime("%H%M%S")
            filename = f"{cam_id}__{slug}__{date_compact}__{hhmmss}__{sha8}{ext}"
            out_path = out_dir / filename

            if out_path.exists():
                skipped += 1
                continue

            img_bytes = row["img_bytes"]
            if not img_bytes and row["img_b64"]:
                try:
                    img_bytes = base64.b64decode(row["img_b64"])
                except Exception:
                    img_bytes = b""

            if not img_bytes:
                skipped += 1
                continue

            pil_save_or_bytes(img_bytes, out_path)
            exported += 1

        offset += batch

    conn.close()
    return exported, skipped, total_ok


# -------------------- CLI --------------------


def main():
    ap = argparse.ArgumentParser(
        description="Xuất ảnh từ 1 file SQLite camera/ngày (bỏ qua file đã có)"
    )
    ap.add_argument(
        "--sqlite-file",
        required=True,
        help="Đường dẫn sqlite_dataset/<CAM_ID>/<YYYY-MM-DD>.sqlite",
    )
    ap.add_argument("--out-root", default="images_export", help="Thư mục gốc xuất ảnh")
    ap.add_argument("--batch", type=int, default=2000, help="Batch size đọc DB")
    args = ap.parse_args()

    exported, skipped, total_ok = export_sqlite(
        Path(args.sqlite_file), Path(args.out_root), args.batch
    )
    print(
        f"[DONE] total_ok={total_ok}  exported={exported}  skipped={skipped}  -> {args.out_root}"
    )

if __name__ == "__main__":
    main()
