import json
from pathlib import Path
from datetime import datetime

INPUT_PATH = "../camera_catalog/camera_catalog_light.json"
OUTPUT_DIR = Path("../camera_catalog_chunks")
CHUNK_SIZE = 6  # mỗi file 6 camera
PRESERVE_ORDER = True  # True: giữ nguyên thứ tự trong file gốc


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(Path(INPUT_PATH).read_text(encoding="utf-8"))
    cams = [c for c in data if c.get("expand_url")]

    if not PRESERVE_ORDER:
        # nếu muốn random:
        import random

        random.shuffle(cams)

    total = len(cams)
    print(f"Tổng camera: {total}. Chia mỗi {CHUNK_SIZE} cái / file.")

    count = 0
    for idx, block in enumerate(chunks(cams, CHUNK_SIZE)):
        out_path = OUTPUT_DIR / f"cams_chunk_{idx:03d}.json"
        out_path.write_text(
            json.dumps(block, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[OK] {out_path}  ({len(block)} cams)")
        count += 1

    index = {
        "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": INPUT_PATH,
        "chunk_size": CHUNK_SIZE,
        "total_cameras": total,
        "total_files": count,
    }
    (OUTPUT_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[OK] Đã tạo {count} file trong thư mục {OUTPUT_DIR}/")
    print(f"[OK] Ghi chỉ mục: {OUTPUT_DIR/'index.json'}")


if __name__ == "__main__":
    main()
