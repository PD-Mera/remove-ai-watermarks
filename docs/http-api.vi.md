# API xóa watermark Gemini / Nano Banana và Veo

Bộ API dùng trực tiếp mã nguồn trong repository này, không gọi dịch vụ bên ngoài.
Mỗi request nhận một file qua multipart và trả về một file để tải xuống. Docker
chạy Linux, Python 3.12, FFmpeg và CPU; cấu hình mặc định không cần GPU, model hay
Google API key. Các endpoint không yêu cầu xác thực hay header API key.

## Phạm vi xử lý

| Input | Xử lý | Output |
| --- | --- | --- |
| PNG, JPEG, WebP từ Gemini / Nano Banana | Mặc định chỉ dò dấu Gemini với thông tin nguồn do người gọi xác nhận, inpaint vùng dấu, xóa metadata AI | PNG, giữ kích thước và alpha theo thư viện |
| MP4, MOV, M4V từ Veo | Chỉ dò dấu Veo (`mark="veo"`), kiểm tra vị trí ổn định qua các frame, inpaint có temporal consistency, xóa metadata AI | Video cùng container; video encode lại, audio copy theo khả năng thư viện |

Đây là xóa **watermark nhìn thấy và metadata AI**, không phải cam kết loại bỏ
SynthID ẩn trong pixel hoặc watermark âm thanh. Header `X-SynthID-Processed` luôn
là `false`. Nhánh SynthID của project dùng tái tạo ảnh/video, có thể làm thay đổi
nội dung và cần bộ phụ thuộc/model khác; API này không bật nhánh đó.

Ảnh mặc định dùng `detection_mode=gemini`: việc chọn chế độ này xác nhận ảnh đến
từ Gemini, giúp dùng ngưỡng dành cho nguồn đã biết ngay cả khi ảnh mất metadata.
Đây là thông tin do người gọi cung cấp, không phải chứng thực metadata. Chế độ
`detection_mode=auto` giữ hành vi cũ: registry nhiều nhà cung cấp, chỉ nới ngưỡng
khi có bằng chứng nguồn tương ứng. Ảnh động bị từ chối. Video chỉ nhận một video stream, không có
cover art; audio không được xác minh watermark. Dấu bị crop, biến dạng hoặc chèn
theo mẫu khác có thể không được nhận diện. Video được inpaint bằng nội suy nên
vùng xóa có thể có vết; không có bộ xác minh thị giác độc lập cho video trong luồng này.

## Triển khai trên máy khác

Mang **toàn bộ thư mục repository**, bao gồm `src/` và các assets đi kèm, lên máy
có Docker Engine và Docker Compose v2. Build cần Internet để tải base image,
gói hệ thống và thư viện Python. Không cần cài Python/FFmpeg trên host.

```bash
cp .env.api.example .env.api
```

Sau đó:

```bash
docker compose --env-file .env.api up -d --build
docker compose --env-file .env.api logs -f watermark-api
```

Mặc định dịch vụ chỉ bind `127.0.0.1:8000`. Để truy cập trực tiếp từ máy khác,
đổi `API_BIND_HOST=0.0.0.0`; dùng firewall và HTTPS qua reverse proxy cho môi
trường triển khai có truy cập từ Internet.

- Swagger: `http://localhost:8000/docs`, có thể gọi trực tiếp không cần xác thực.
- OpenAPI: `http://localhost:8000/openapi.json`.
- Health: `GET http://localhost:8000/healthz`, không cần API key.

`/healthz` chỉ kiểm tra HTTP host còn hoạt động; không chạy xử lý media hay tải model.
Lưu ý: chưa build image, chạy dịch vụ hoặc chạy test trên máy viết code, theo yêu
cầu. Cần xác nhận chất lượng với ảnh/video thực tế trên máy triển khai.

## Gọi API

Ví dụ bên dưới dành cho shell Linux/macOS. Trên PowerShell dùng `curl.exe` và
viết lệnh trên một dòng, hoặc thay dấu xuống dòng `\` bằng backtick.

Ảnh Gemini / Nano Banana:

```bash
curl --fail --show-error \
  -F 'file=@gemini.png' \
  -F 'detection_mode=gemini' \
  -D image-headers.txt \
  -o clean.png \
  http://localhost:8000/v1/remove/image
```

Nếu ảnh bị resize, crop hoặc detector không nhận ra dấu, có thể chỉ định vùng
cần xóa bằng `region=x,y,width,height` theo pixel của ảnh đã decode. Vùng này phải
nằm hoàn toàn trong ảnh. Tham số này có ưu tiên cao hơn `detection_mode`, không
cần detector nhận diện trước và chỉ inpaint đúng hình chữ nhật bạn chỉ định.
Hãy chọn vùng bao hết dấu, có thêm ít viền nền. Ví dụ sau chỉ minh họa một vùng
80×80 tại (920,920) trên ảnh đủ lớn; thay tọa độ theo ảnh thực tế:

```bash
curl --fail --show-error \
  -F 'file=@gemini.png' \
  -F 'region=920,920,80,80' \
  -o clean.png \
  http://localhost:8000/v1/remove/image
```

API không tự xóa góc phải nếu dò không ra dấu. `region` là lệnh xóa vùng do người
gọi chọn; kết quả báo `processed`, không khẳng định đã nhận diện đúng dấu Gemini.
Nếu còn dấu được detector chấp nhận chồng lên vùng vừa xóa, API vẫn trả 422.

Video Veo:

```bash
curl --fail --show-error --max-time 1900 \
  -F 'file=@veo.mp4' \
  -D video-headers.txt \
  -o clean.mp4 \
  http://localhost:8000/v1/remove/video
```

Response thành công là **binary file**, không phải JSON/base64. Ảnh luôn là PNG;
video giữ đuôi container nguồn. Client nên kiểm tra HTTP status trước khi lưu file.
`curl --fail` sẽ không ghi body JSON lỗi thành ảnh/video. Để xem chi tiết lỗi,
gọi lại với `-i` và bỏ `--fail`, `-o`.

Mặc định `require_watermark=true`: nếu không phát hiện dấu phù hợp, trả HTTP 422
để tránh nhầm một bản sao chưa xóa dấu thành kết quả thành công. Có thể thêm:

```bash
-F 'require_watermark=false'
```

Khi đó nếu không phát hiện dấu, API vẫn trả file đã qua bước xóa metadata;
header sẽ là `X-Watermark-Status: no_watermark`. Giá trị này chỉ có nghĩa detector
không thấy dấu được hỗ trợ, không chứng minh file không có watermark.

Các header kết quả:

| Header | Ý nghĩa |
| --- | --- |
| `X-Watermark-Status` | Ảnh: `cleaned`, `processed` (xóa vùng chỉ định) hoặc `no_watermark`; video: `processed` hoặc `no_watermark` |
| `X-Watermark-Report` | JSON nhỏ: mark, trạng thái; chế độ Gemini có confidence/vùng ứng viên/kích thước ảnh và vùng đã xóa; video có số frame |
| `X-SynthID-Processed` | `false` |
| `Content-Disposition` | Tên file tải xuống |
| `Cache-Control` | `no-store` |

`cleaned` của ảnh dựa vào kiểm tra lại của cùng detector trong thư viện; nếu ảnh
còn dấu theo detector (`partial`) hoặc không xác minh được (`unvalidated`), API
trả 422 và không gửi file. `processed` của video báo đã xử lý các frame phát hiện
được, không xác nhận mọi frame đều sạch.

Khi dò/xóa dấu ảnh thất bại, lỗi JSON có `detail.message` và `detail.report` chứa
thông tin nhận diện (chế độ, confidence, vùng ứng viên...) nếu đã có báo cáo.
Vùng ứng viên không được chấp nhận không có nghĩa đó là vị trí watermark thật.
`require_watermark=false` không ép xóa dấu, không phải cách sửa lỗi nhận diện.

Sau khi cập nhật mã nguồn, build lại trên máy triển khai:

```bash
docker compose --env-file .env.api up -d --build --force-recreate
curl http://localhost:8000/healthz
```

Health cần có `version: "1.1.0"` để xác nhận đang chạy adapter mới. Vẫn cần đối
chiếu ảnh thực tế; thay đổi ngưỡng không bảo đảm nhận diện mọi biến thể dấu.

## Giới hạn và vận hành

| Biến trong `.env.api` | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | 512 | Dung lượng một file, MiB; toàn multipart được thêm 1 MiB envelope |
| `MAX_IMAGE_PIXELS` | 40000000 | Tổng pixel ảnh tối đa |
| `MAX_VIDEO_SECONDS` | 300 | Thời lượng video tối đa |
| `MAX_VIDEO_PIXELS` | 8294400 | Tổng pixel mỗi frame tối đa, tương đương 3840×2160 |
| `MAX_CONCURRENT_JOBS` | 1 | Số request upload/xử lý/download đồng thời |
| `PROCESS_TIMEOUT_SECONDS` | 1800 | Thời gian tối đa cho subprocess xử lý, gồm preflight |
| `UPLOAD_IDLE_TIMEOUT_SECONDS` | 60 | Thời gian chờ tối đa giữa hai lần nhận dữ liệu upload |
| `WATERMARK_BACKEND` | cv2 | Backend inpaint: `cv2`, `lama` hoặc `migan` |
| `MEMORY_LIMIT` | 8g | Giới hạn RAM container, không phải cam kết RAM đủ cho mọi input |
| `CPU_LIMIT` | 4.0 | Hạn mức CPU container |

Giữ **một Uvicorn worker** như Dockerfile. Muốn tăng đồng thời thì sửa
`MAX_CONCURRENT_JOBS`, cân nhắc RAM/disk/CPU và giới hạn phù hợp. Khi hết slot,
API trả 503 và `Retry-After: 10`, không xếp hàng vô hạn. Health vẫn phục vụ trong
khi subprocess đang làm việc.

Request dùng thư mục tạm riêng và tên file cố định, không dùng đường dẫn do client
gửi. File upload, kết quả và log tạm được xóa khi gửi xong hoặc khi request lỗi.
Timeout giết cả nhóm subprocess, gồm FFmpeg. Volume `work` cần đủ dung lượng cho
upload, các file trung gian và output của tất cả job; video có thể cần nhiều lần
dung lượng nguồn. Nếu container/host bị kill đột ngột, volume có thể còn file tạm;
khi dịch vụ đã dừng, có thể xóa riêng volume `work` được liệt kê bởi
`docker volume ls`. Không xóa volume `models` nếu muốn giữ model cache.

Reverse proxy cần cho phép body lớn hơn `MAX_UPLOAD_MB` khoảng 1 MiB và timeout
đọc lớn hơn thời gian upload + `PROCESS_TIMEOUT_SECONDS` + download. Đây là API
đồng bộ, kết nối HTTP giữ mở đến khi xử lý xong; ngắt kết nối client không đảm bảo
dừng job ngay. Job vẫn bị chặn bởi timeout và giới hạn tài nguyên.

Mã lỗi chính: 400 upload rỗng/multipart không hợp lệ, 408 upload
bị treo, 413 quá dung lượng, 415 đuôi không hỗ trợ, 422 input không hợp lệ/không
tìm được dấu/xóa chưa đạt, 500 xử lý lỗi, 503 hết slot, 504 quá thời gian.
Các dependency đang dùng khoảng phiên bản trong requirements/pyproject; build lại
ở thời điểm khác có thể nhận phiên bản mới trong khoảng đó.

## Tùy chọn backend model CPU

`cv2` không tải model và là mặc định nhẹ nhất. Nếu cần thử chất lượng inpaint tốt
hơn trên máy triển khai, chỉnh đồng thời hai giá trị trong `.env.api`:

```dotenv
WATERMARK_EXTRAS=video,lama
WATERMARK_BACKEND=lama
```

Hoặc `WATERMARK_EXTRAS=video,migan` và `WATERMARK_BACKEND=migan`.
Sau đó build lại bằng lệnh triển khai phía trên. Model sẽ tải từ Hugging Face khi
thực sự cần inpaint lần đầu và được giữ trong volume `models`; lần đầu có thể lâu
hơn và cần Internet. LaMa cần nhiều RAM hơn cv2; điều chỉnh giới hạn theo thực tế.
Không cấu hình `lama`/`migan` nếu chưa cài extra tương ứng.
