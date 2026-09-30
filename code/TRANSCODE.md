# Chuyển mã video thủ công

RTSP server chỉ quét và phát các file H.264/H.265 có trong `media/`. Chuyển mã
là lệnh riêng, không chạy trong tiến trình RTSP.

Chạy PowerShell tại thư mục `code`:

```powershell
docker compose run --rm transcoder /media/0519_fixed.mp4 h265 --output-dir /media --overwrite
docker compose run --rm transcoder /media/0519_fixed_h265.mp4 h264 --output-dir /media
```

File đầu ra được lưu vào `media/` với hậu tố codec, ví dụ
`0519_fixed_h265.mp4` hoặc `0519_fixed_h264.mp4`. Nếu đầu ra đã tồn tại, thêm
`--overwrite` để thay thế. Khi file đầu ra hoàn tất, watcher RTSP tự phát nó
theo tên file; không cần khởi động lại server.

Có thể chạy script trực tiếp trên máy nếu đã cài FFmpeg và FFprobe:

```powershell
python .\server\transcode.py ..\media\0519_fixed.mp4 h265
```
