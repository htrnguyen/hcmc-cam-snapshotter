# HCMC Camera Snapshotter

Một công cụ Python tự động để chụp ảnh từ các camera giao thông công cộng tại Thành phố Hồ Chí Minh. Dự án này cho phép thu thập dữ liệu hình ảnh từ hệ thống camera giao thông của thành phố một cách có tổ chức và tự động.

## 🚀 Tính năng chính

-   **Thu thập tự động**: Chụp ảnh từ tối đa 6 camera cùng lúc với khoảng cách thời gian có thể tùy chỉnh
-   **Xử lý hình ảnh thông minh**: Tự động chuyển đổi GIF sang JPEG để tối ưu dung lượng
-   **Tổ chức dữ liệu**: Lưu trữ hình ảnh theo cấu trúc thư mục có tổ chức (camera/ngày)
-   **Xử lý bất đồng bộ**: Sử dụng Playwright để xử lý nhiều camera đồng thời
-   **Quản lý catalog**: Chia nhỏ danh sách camera thành các chunk để xử lý hiệu quả
-   **Debug console**: Hiển thị trạng thái real-time của quá trình chụp ảnh

## 📂 Cấu trúc dự án

```
├── hcmc_sixcam_capture.py      # Script chính để chụp ảnh
├── requirements.txt            # Dependencies Python
├── camera_catalog/             # Danh sách camera
│   ├── camera_catalog_full.json
│   └── camera_catalog_light.json
├── camera_catalog_chunks/      # Camera chunks (6 camera/file)
│   ├── cams_chunk_000.json
│   ├── cams_chunk_001.json
│   └── ...
├── scripts/                    # Utility scripts
│   ├── parse_folder_ajax_response_full.py
│   └── split_light_into_chunks.py
└── images/                     # Thư mục lưu trữ ảnh (được tạo tự động)
```

## 🛠️ Cài đặt

### Yêu cầu hệ thống

-   Python 3.7+
-   Windows/Linux/macOS

### Cài đặt dependencies

```bash
# Clone repository
git clone https://github.com/htrnguyen/hcmc-cam-snapshotter.git
cd hcmc-cam-snapshotter

# Cài đặt packages
pip install -r requirements.txt

# Cài đặt Playwright browsers
playwright install chromium
```

## 📖 Hướng dẫn sử dụng

### 1. Chụp ảnh từ một chunk camera

```bash
python hcmc_sixcam_capture.py --chunk-file camera_catalog_chunks/cams_chunk_000.json
```

### 2. Tùy chỉnh cấu hình

Chỉnh sửa các biến trong file `hcmc_sixcam_capture.py`:

```python
INTERVAL_SEC = 15.0     # Khoảng cách giữa các lần chụp (giây)
OFFSET_SEC = 0.0        # Delay ban đầu
PAGE_TIMEOUT_S = 20     # Timeout cho page load
SAVE_DIR = Path("images")  # Thư mục lưu ảnh
HEADFUL = True          # Hiển thị browser GUI
DEBUG = True            # Bật debug console
```

### 3. Tạo camera chunks mới

```bash
cd scripts
python split_light_into_chunks.py
```

## 📁 Cấu trúc lưu trữ hình ảnh

Hình ảnh được lưu theo cấu trúc:

```
images/
└── {cam_id}__{code_slug}/
    └── {YYYYMMDD}/
        └── {cam_id}__{code_slug}__{YYYYMMDD}__{HHMMSS}__{hash8}.jpg
```

Ví dụ:

```
images/59d3524f02eb490011a0a61b__tth_33_9/20250821/59d3524f02eb490011a0a61b__tth_33_9__20250821__143052__a1b2c3d4.jpg
```

## 🔧 Scripts tiện ích

### `scripts/split_light_into_chunks.py`

Chia file `camera_catalog_light.json` thành các chunk nhỏ (6 camera/chunk) để xử lý song song.

**Cấu hình:**

-   `CHUNK_SIZE = 6`: Số camera mỗi chunk
-   `PRESERVE_ORDER = True`: Giữ nguyên thứ tự camera

### `scripts/parse_folder_ajax_response_full.py`

Script để xử lý response từ `https://giaothong.hochiminhcity.gov.vn/Map.aspx`.

## 🎛️ Tùy chọn command line

```bash
python hcmc_sixcam_capture.py --help
```

**Tùy chọn:**

-   `--chunk-file`: Đường dẫn đến file JSON chứa danh sách camera (bắt buộc)

## 📊 Output console

Khi chạy với `DEBUG = True`:

```
14:30:52 6/6 [✓ tth_33_9] [✓ ben_thanh_market] [✓ nguyen_hue] [✓ dong_khoi] [✓ le_loi] [✓ ham_nghi]
14:31:07 5/6 [✓ tth_33_9] [× ben_thanh_market] [✓ nguyen_hue] [✓ dong_khoi] [✓ le_loi] [✓ ham_nghi]
```

Format: `[timestamp] [success_count/total] [status per camera]`

-   ✓: Chụp thành công
-   ×: Chụp thất bại

## 🔍 Troubleshooting

### Camera không load được

-   Kiểm tra kết nối internet
-   Tăng `PAGE_TIMEOUT_S` nếu mạng chậm
-   Kiểm tra URL trong file catalog

### Browser crash

-   Đảm bảo đã cài đặt Playwright browsers: `playwright install chromium`
-   Thử giảm số camera đồng thời (chỉnh CHUNK_SIZE)

### Lỗi permission

-   Đảm bảo có quyền ghi vào thư mục `images/`
-   Chạy với quyền administrator nếu cần

## 📄 Cấu trúc dữ liệu Camera

Mỗi camera trong catalog có cấu trúc:

```json
{
    "cam_id": "59d3524f02eb490011a0a61b",
    "code": "TTH 33.9",
    "title": "CAMERA",
    "district": null,
    "cam_type": "tth",
    "ptz": false,
    "angle": 330,
    "expand_url": "https://giaothong.hochiminhcity.gov.vn/expandcameraplayer/?camId=...",
    "expand_loc_raw": "CAMERA",
    "has_snapshot_url": false,
    "has_hls": false,
    "has_rtsp": false
}
```

## 📝 License

Dự án này được phát triển cho mục đích nghiên cứu và giáo dục. Vui lòng tuân thủ các điều khoản sử dụng của website nguồn.

## ⚠️ Lưu ý quan trọng

-   Sử dụng có trách nhiệm, không spam request
-   Tôn trọng bandwidth của server nguồn
-   Dữ liệu thu được chỉ dùng cho mục đích nghiên cứu/giáo dục
-   Kiểm tra tính hợp pháp trước khi sử dụng trong môi trường production

## 👥 Tác giả

-   **htrnguyen** - [GitHub](https://github.com/htrnguyen)

## 🔗 Links hữu ích

-   [Playwright Documentation](https://playwright.dev/python/)
-   [HCMC Traffic Management](https://giaothong.hochiminhcity.gov.vn/)
-   [PIL/Pillow Documentation](https://pillow.readthedocs.io/)
