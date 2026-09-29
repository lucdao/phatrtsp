# VLC Streaming Architecture - Chi tiết Codec, VOUT và Buffer

## Mục lục
1. [VLC Decoder Pipeline](#1-vlc-decoder-pipeline-avcodec-wrapper)
2. [Hardware Acceleration](#2-hardware-acceleration-setup)
3. [VOUT Buffer Management](#3-vout-buffer-management)
4. [Frame Dropping Logic](#4-frame-dropping-logic---quan-trọng)
5. [RTSP Jitter Buffer](#5-rtsp-jitter-buffer)
6. [Buffer Size Summary](#6-buffer-sizes-summary)
7. [Cấu hình Buffer cho Multi-Camera](#7-cấu-hình-buffer-cho-multi-camera-mượt)

---

## 1. VLC Decoder Pipeline (avcodec wrapper)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          DECODER LAYER                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   INPUT QUEUE                    DECODER                    OUTPUT QUEUE   │
│   ┌───────────┐               ┌───────────┐               ┌───────────┐   │
│   │ RTP Pkt 1 │──────────────►│           │               │ Frame 1   │   │
│   │ (H264)    │               │  FFmpeg   │───────────────►│           │   │
│   └───────────┘               │ libavcodec│               └───────────┘   │
│   ┌───────────┐               │           │               ┌───────────┐   │
│   │ RTP Pkt 2 │──────────────►│  H264     │───────────────►│ Frame 2   │   │
│   │ (H264)    │               │  Decoder  │               │           │   │
│   └───────────┘               │           │               └───────────┘   │
│   ┌───────────┐               │           │               ┌───────────┐   │
│   │ RTP Pkt 3 │──────────────►│           │───────────────►│ Frame 3   │   │
│   │ (H264)    │               └───────────┘               │ (dropped) │   │
│   └───────────┘                                           └───────────┘   │
│                                                                             │
│   FIFO Queue                 Single decoder          Frames to render       │
│   decoder_QueueIncoming()   instance                vout_RenderPicture()   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Decoder Core Code (modules/codec/avcodec/video.c)

```c
// VLC decoder wrapper around FFmpeg libavcodec

struct decoder_sys_t {
    /* FFmpeg structures */
    AVCodecContext  *p_context;      // FFmpeg codec context
    AVCodec         *p_codec;         // FFmpeg codec (h264/hevc...)
    AVFrame         *p_frame;         // Decoded frame buffer

    /* Hardware acceleration */
    enum AVHWDeviceType hw_device;   // VAAPI, VDPAU, CUDA...
    AVBufferRef     *hw_frames_ctx;   // GPU frame context

    /* Buffer management */
    bool        b_flush;             // Flush flag
    mtime_t     i_pts;               // Last valid timestamp
    date_t      last_pts;            // PTS date tracking
};

// Main decode function
static int DecodeBlock(decoder_t *p_dec, block_t *p_block)
{
    decoder_sys_t *p_sys = p_dec->p_sys;

    // 1. Send packet to FFmpeg decoder
    if (p_block->i_buffer > 0) {
        AVPacket pkt;
        av_init_packet(&pkt);
        pkt.data = p_block->p_buffer;
        pkt.size = p_block->i_buffer;

        // Track timestamps
        p_sys->i_pts = p_block->i_pts;

        int ret = avcodec_send_packet(p_sys->p_context, &pkt);
        if (ret < 0) {
            // Handle decoder error
        }
    }

    // 2. Receive decoded frames
    while (avcodec_receive_frame(p_sys->p_context, p_sys->p_frame) == 0) {

        // 3. Check if frame is valid (not a duplicate)
        if (p_sys->p_frame->pts != AV_NOPTS_VALUE) {
            p_sys->i_pts = p_sys->p_frame->pts;
        }

        // 4. Allocate output picture
        picture_t *p_pic = decoder_NewPicture(p_dec);
        if (!p_pic) {
            av_frame_unref(p_sys->p_frame);
            continue;
        }

        // 5. Copy frame data (with possible format conversion)
        if (p_sys->p_frame->format == p_sys->p_context->pix_fmt) {
            // Direct copy - same format
            for (int i = 0; i < p_pic->i_planes; i++) {
                p_pic->p[i].p_pixels = p_sys->p_frame->data[i];
            }
        } else {
            // Format conversion needed
            SwsContext *sws = sws_getContext(
                p_sys->p_context->width, p_sys->p_context->height, p_sys->p_context->pix_fmt,
                p_sys->p_context->width, p_sys->p_context->height, AV_PIX_FMT_YUV420P,
                SWS_FAST_BILINEAR, NULL, NULL, NULL
            );
            sws_scale(sws, p_sys->p_frame->data, p_sys->p_frame->linesize,
                      0, p_sys->p_context->height,
                      p_pic->planes, p_pic->pitches);
            sws_freeContext(sws);
        }

        // 6. Queue frame for display
        p_pic->date = p_sys->i_pts;
        decoder_QueueVideo(p_dec, p_pic);

        av_frame_unref(p_sys->p_frame);
    }

    block_Release(p_block);
    return VLCDEC_SUCCESS;
}
```

---

## 2. Hardware Acceleration Setup

```c
// Setup hardware decoder in order of preference
static int SetupHardwareDecoder(vlc_va_t *va, decoder_t *dec)
{
    // Priority order for hardware decode:
    const enum AVHWDeviceType hw_devices[] = {
        AV_HWDEVICE_TYPE_VAAPI,      // Linux Intel/AMD
        AV_HWDEVICE_TYPE_D3D11VA,   // Windows
        AV_HWDEVICE_TYPE_VIDEOTOOLBOX, // macOS
        AV_HWDEVICE_TYPE_CUDA,      // NVIDIA GPU
        AV_HWDEVICE_TYPE_VDPAU,    // Old NVIDIA Linux
    };

    for (int i = 0; i < ARRAY_SIZE(hw_devices); i++) {
        if (vlc_va_Initialize(va, hw_devices[i]) == VLC_SUCCESS) {
            msg_Dbg(va, "Using hardware decoder: %s",
                    av_hwdevice_get_type_name(hw_devices[i]));
            return VLC_SUCCESS;
        }
    }

    // Fallback to software decode
    msg_Dbg(va, "Using software decoder");
    return VLC_EGENERIC;
}
```

### VLC sử dụng FFmpeg như thế nào

```c
// VLC không dùng FFmpeg CLI, mà dùng FFmpeg libraries
// Cụ thể là libavcodec + libavformat

// Trong configure.ac hoặc meson.build:
dependency('libavcodec', version: '>= 58.18')
dependency('libavformat', version: '>= 58.12')
dependency('libavutil', version: '>= 56.14')

// avcodec decoder wrapper trong VLC:
struct AVCodecContext {
    void *codec;                // FFmpeg codec (H264, H265...)
    void *hwaccel_context;      // GPU acceleration context
    enum AVPixelFormat hwfmt;   // Hardware pixel format (NV12, P010...)
};
```

---

## 3. VOUT Buffer Management

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          VIDEO OUTPUT LAYER                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   DECODER                                    VOUT THREADS                    │
│   QUEUE                  CIRCULAR               ┌──────────────┐           │
│   ┌─────┐               BUFFER                  │ VOUT DISPLAY │           │
│   │ F1  │──┐           ┌─────────┐              │ THREAD       │           │
│   │ F2  │──┼──────────►│         │──────────────►│              │           │
│   │ F3  │──┤           │   N     │              │ - Manage     │           │
│   │ F4  │──┤           │ frames  │              │   display    │           │
│   │ F5  │──┘           │         │              │ - Timing     │           │
│   └─────┘              │ (config)│              │ - OSD        │           │
│                       └─────────┘              └──────┬───────┘           │
│                                                       │                    │
│                       Pipeline:                       │                    │
│   decoder_QueueVideo() → picture_Render() → vout_Render() → display()    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### VOUT Buffer Code (src/video_output/vout.c)

```c
// VOUT internal structure - defines buffer size
struct vout_thread_sys_t {
    // Circular buffer of decoded frames
    picture_t *displayed[MAX_PICTURES];    // Pictures being displayed
    picture_t *filtered[MAX_PICTURES];     // Pictures after filter
    int i_picture;                         // Current index

    // Timing control
    vlc_tick_t render_delay;              // Frame rendering delay
    vlc_tick_t drift;                      // Clock drift compensation

    // Frame rate control
    float fps;                             // Source frame rate
    vlc_tick_t frame_interval;            // Expected time between frames

    // Configuration
    int display.width;                     // Output resolution
    int display.height;
    bool vsync;                            // Vertical sync enabled
};

// Default buffer sizes
#define MAX_PICTURES 20          // Max frames in decoder queue
#define VOUT_MAX_PICTURES 20    // Max frames in vout queue
#define VOUT_DISPLAY_PICTURES 3 // Number of buffers from display
```

---

## 4. Frame Dropping Logic - Quan trọng!

```c
// src/video_output/video_output.c - Frame timing and dropping

static int ThreadDisplayPicture(vout_thread_t *vout)
{
    vout_thread_sys_t *sys = vout->p;

    // 1. Get next frame from queue
    picture_t *next = vout_GetNextFrame(vout);
    if (!next)
        return VLC_SUCCESS;

    // 2. Calculate expected display time
    vlc_tick_t date = vlc_tick_now();
    vlc_tick_t render_date = next->date;

    // 3. Check if we're too late (drop frame)
    if (render_date + sys->frame_interval < date) {
        // Frame is too old, drop it
        msg_Dbg(vout, "dropping frame (late)");
        picture_Release(next);
        vout->p->cucul++;
        return VLC_SUCCESS;
    }

    // 4. Check if we're too early (wait)
    if (render_date > date + sys->frame_interval) {
        // Frame is too early, wait
        vlc_tick_t wait = render_date - date - sys->frame_interval;
        msleep(wait);
    }

    // 5. Render and display
    vout_Render(vout, next);
    vout_Display(vout, next);

    return VLC_SUCCESS;
}

// Anti-jitter: Don't accumulate too much delay
static void vout_FilterAlign(vout_thread_t *vout)
{
    vout_thread_sys_t *sys = vout->p;

    // If decoder queue is too full, signal decoder to drop
    if (sys->dpb_size > 4) {  // DPB = Decoded Picture Buffer
        vout_control_PushVoid(&sys->control, VOUT_CONTROL_FLUSH);
    }
}
```

---

## 5. RTSP Jitter Buffer

```c
// modules/demux/rtp.c - RTP jitter buffer - critical for smooth playback!

struct rtp_track_t {
    /* Jitter buffer configuration */
    int32_t  i_jitter_max;       // Max jitter (usec) - DEFAULT: 100000 (100ms)
    int32_t  i_jitter_pts;       // Current estimated jitter
    uint16_t w_ifps_jitter;      // Inter-frame packet spacing jitter

    /* Buffer queue */
    vlc_queue_t queue;           // Incoming packets waiting to be ordered

    /* Sequence tracking */
    uint16_t i_seq_min;          // Minimum sequence received
    uint16_t i_seq_max;          // Maximum sequence received
    uint16_t i_seq_last;         // Last sequence number

    /* Timing */
    uint64_t i_ts_offset;        // RTP timestamp offset
    uint64_t i_ts_last;          // Last RTP timestamp
    int64_t  i_roll_offset;     // For sequence number rollover

    /* Statistics */
    uint32_t i_cum_drop;         // Cumulative dropped packets
    uint32_t i_trust;            // Packet trust level
};

// Key jitter buffer algorithm
static block_t *RTPProcessJitter(rtp_track_t *track, block_t *pkt)
{
    uint16_t seq = pkt->i_seq;
    uint32_t ts = pkt->i_ts;

    // 1. Initialize jitter buffer if empty
    if (vlc_queue_IsEmpty(&track->queue)) {
        track->i_seq_min = track->i_seq_max = seq;
        track->i_ts_offset = ts - date_Get(&track->last_pts);
    }

    // 2. Update sequence range
    if (seq_diff(seq, track->i_seq_max) > 0)
        track->i_seq_max = seq;

    // 3. Check for gap (missing packets)
    int gap = seq_diff(seq, track->i_seq_last);
    if (gap > 1) {
        // Missing packet(s) - wait for retransmit or timeout
        // Timeout is handled by RTP session timeout
        track->i_cum_drop += gap - 1;
    }
    track->i_seq_last = seq;

    // 4. Calculate jitter
    int32_t drift = ts - track->i_ts_last;
    track->i_jitter_pts += (abs(drift) - track->i_jitter_pts) >> 4;

    // 5. Enqueue packet
    vlc_queue_Push(&track->queue, pkt);

    // 6. Dequeue packets ready to display
    block_t *out = NULL;
    while (!vlc_queue_IsEmpty(&track->queue)) {
        block_t *head = vlc_queue_Peek(&track->queue);

        // Packet is "on time" if within jitter window
        if (seq_diff(head->i_seq, track->i_seq_min) <= 0 ||
            track->i_jitter_pts <= track->i_jitter_max) {
            vlc_queue_Take(&track->queue);
            out = head;
            track->i_seq_min++;
        } else {
            // Wait for more packets to fill jitter window
            break;
        }
    }

    return out;  // NULL means wait, valid block means ready to decode
}
```

---

## 6. Buffer Sizes Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         BUFFER SIZE CHAIN                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   NETWORK ──► RTP Jitter ──► Decoder ──► VOUT ──► DISPLAY                 │
│              Buffer        Buffer    Buffer   Buffer                       │
│                                                                             │
│   ════════════════════════════════════════════════════════════════════     │
│   Default sizes:                                                           │
│                                                                             │
│   Layer           Default     Configurable    Purpose                      │
│   ─────────────────────────────────────────────────────────────            │
│   RTP Jitter      100ms       Yes             Reorder packets               │
│   Decoder Queue   20 frames   Yes (VOUT_MAX_PICTURES)  Buffer decoded      │
│   VOUT Queue      20 frames   Yes             Filter/processing buffer      │
│   Display Buff    3 frames   Partially       Triple buffer for display    │
│                                                                             │
│   ════════════════════════════════════════════════════════════════════     │
│                                                                             │
│   How to adjust in VLC:                                                    │
│   Tools → Preferences → Input/Codecs → Hardware-accelerated decoding      │
│   Advanced → "File caching (ms)", "Network caching (ms)"                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### So sánh Buffer Strategies

| Strategy | Pros | Cons |
|----------|------|------|
| **Large Buffer (500ms)** | Smooth, handles jitter well | High latency |
| **Small Buffer (50ms)** | Low latency | May stutter on jitter |
| **Adaptive Buffer** | Balances both | Complex implementation |
| **Recommended (100-300ms)** | Good balance | Suitable for most cases |

---

## 7. Cấu hình Buffer cho Multi-Camera Mượt

### VLC command line options cho RTSP buffering

```bash
vlc rtsp://camera --rtsp-tcp \
    --network-caching=300 \         # Network cache 300ms
    --live-caching=300 \           # Live stream cache
    --clock-jitter=100 \           # Max clock jitter (ms)
    --clock-synchro=0              # Disable clock sync (for local streams)
```

### libvlc options trong code

```c
// Or in code (libvlc):
libvlc_media_add_option(media, "network-caching=300");
libvlc_media_add_option(media, "rtsp-tcp");
libvlc_media_add_option(media, "clock-jitter=100");
```

### Python với VLC engine

```python
# Python - dùng VLC SDK trực tiếp với buffer settings
import vlc

# Tạo player với các options
options = [
    '--network-caching=300',
    '--rtsp-tcp',
    '--clock-jitter=100',
    '--live-caching=300'
]

instance = vlc.Instance(options)
player = instance.media_player_new()
player.set_mrl('rtsp://camera-ip:8553/stream')

# Hoặc thêm options trực tiếp vào media
media = instance.media_new('rtsp://camera-ip:8553/stream')
media.add_option('network-caching=300')
media.add_option('rtsp-tcp')
player.set_media(media)
player.play()
```

### FFmpeg-based client với buffer settings

```python
import cv2

# OpenCV với FFmpeg backend - cấu hình buffer
cap = cv2.VideoCapture(rtsp_url)

# Buffer size nhỏ = low latency
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# Hoặc dùng FFmpeg trực tiếp với options
stream = ffmpeg.input(
    rtsp_url,
    rtsp_transport='tcp',           # TCP ổn định hơn UDP
    fflags='nobuffer+genpts+flush_packets',  # Low latency flags
    max_delay='100000',             # 100ms max delay
    framerate='30'                  # Frame rate
)
```

---

## Key Takeaways cho ứng dụng Multi-Camera

1. **Decode**: Dùng FFmpeg/libavcodec (như VLC) - cùng engine
2. **Buffer**:
   - RTP Jitter Buffer: 100ms (có thể tăng lên 200-300ms nếu mạng không ổn định)
   - Decoder Queue: 20 frames
   - VOUT Queue: 20 frames
3. **Drop**: Tự động drop frames nếu render không kịp (anti-jitter logic)
4. **Sync**: Dùng PTS (Presentation Time Stamp) để sync video
5. **Hardware Acceleration**: Ưu tiên VAAPI (Linux), D3D11VA (Windows), VideoToolbox (macOS)

---

## Tài liệu tham khảo

- VLC Source Code: https://code.videolan.org/videolan/vlc
- FFmpeg/libavcodec Documentation
- RTP Specification: RFC 3550
- RTSP Specification: RFC 7826
