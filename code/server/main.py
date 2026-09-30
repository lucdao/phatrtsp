#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstRtspServer, GstApp, GLib
import os, glob
import json
import shlex
import signal
import subprocess
import logging
import threading
import time
from urllib.parse import quote
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

LOW_LATENCY_QUEUE = "queue max-size-buffers=120 max-size-time=1000000000 max-size-bytes=0 leaky=downstream"
PARSER_CAPS = "video/x-h264,stream-format=byte-stream,alignment=au"


class ContinuousPtsLoopRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    Loop an H.264 MP4 while keeping RTSP timestamps monotonic.
    H.264 access units pass through unchanged: there is no decode/re-encode.
    """
    def __init__(self, filepath: str):
        super().__init__()
        self.filepath = filepath
        self._lock = threading.Lock()
        self._reader = None
        self._appsink = None
        self._frame_index = 0
        self._frame_duration = Gst.SECOND // 30
        self._push_thread = None
        self.connect("media-configure", self._on_media_configure)

    def do_create_element(self, _url):
        pipeline = (
            '( appsrc name=src is-live=true block=true format=time do-timestamp=false '
            '! queue max-size-buffers=60 max-size-time=2000000000 max-size-bytes=0 leaky=downstream '
            '! h264parse config-interval=-1 '
            f'! {PARSER_CAPS} '
            '! rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
        )
        logging.info(f"H.264 passthrough loop pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)

    def _make_reader(self):
        pipeline = Gst.parse_launch(
            f'filesrc location="{os.path.abspath(self.filepath)}" ! qtdemux name=d '
            'd.video_0 ! queue max-size-buffers=60 max-size-time=2000000000 max-size-bytes=0 '
            '! h264parse config-interval=-1 '
            f'! {PARSER_CAPS} '
            # appsink follows the source clock, so each encoded access unit arrives
            # at the video's native rate.  The push loop must not add another sleep.
            '! appsink name=sink emit-signals=false sync=true max-buffers=2 drop=true'
        )
        sink = pipeline.get_by_name("sink")
        pipeline.set_state(Gst.State.PLAYING)
        return pipeline, sink

    def _restart_reader_locked(self):
        if self._reader is not None:
            # A flushing seek clears EOS and preserves the running clock, making
            # the black-gap-to-video transition much quicker than rebuilding MP4.
            if self._reader.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0,
            ):
                self._reader.set_state(Gst.State.PLAYING)
                return
            self._reader.set_state(Gst.State.NULL)
        self._reader, self._appsink = self._make_reader()

    def _pull_sample_locked(self):
        if self._appsink is None:
            self._restart_reader_locked()

        # Allow enough time for cold MP4 parsing and initial preroll.
        sample = self._appsink.try_pull_sample(10 * Gst.SECOND)
        if sample is not None:
            return sample

        logging.info(f"Looping {self.filepath}: seeking H.264 reader to start with continuous PTS")
        self._restart_reader_locked()
        return self._appsink.try_pull_sample(10 * Gst.SECOND)

    def _on_need_data(self, appsrc, _length):
        with self._lock:
            sample = self._pull_sample_locked()
            if sample is None:
                logging.warning(f"No decoded sample available for {self.filepath}")
                return

            caps = sample.get_caps()
            if caps:
                appsrc.set_property("caps", caps)
                struct = caps.get_structure(0)
                ok, fps_num, fps_den = struct.get_fraction("framerate")
                if ok and fps_num > 0:
                    self._frame_duration = Gst.SECOND * fps_den // fps_num

            src_buffer = sample.get_buffer()
            if src_buffer is None:
                return
            buffer = src_buffer.copy()
            pts = self._frame_index * self._frame_duration
            buffer.pts = pts
            buffer.dts = pts
            buffer.duration = self._frame_duration
            self._frame_index += 1

        ret = appsrc.emit("push-buffer", buffer)
        if ret != Gst.FlowReturn.OK:
            logging.warning(f"appsrc push-buffer returned {ret.value_nick}")

    def _push_loop(self, appsrc):
        logging.info(f"Starting continuous PTS push loop for {self.filepath}")
        # Each newly prepared RTSP media pipeline has its own running clock.
        # Start its timestamps at zero so reconnecting clients receive frames
        # immediately instead of waiting for a previous session's elapsed PTS.
        frame_index = 0
        while True:
            with self._lock:
                sample = self._pull_sample_locked()
                if sample is None:
                    logging.warning(f"No decoded sample available for {self.filepath}")
                    time.sleep(0.1)
                    continue

                caps = sample.get_caps()
                if caps:
                    appsrc.set_property("caps", caps)
                    struct = caps.get_structure(0)
                    ok, fps_num, fps_den = struct.get_fraction("framerate")
                    if ok and fps_num > 0:
                        self._frame_duration = Gst.SECOND * fps_den // fps_num

                src_buffer = sample.get_buffer()
                if src_buffer is None:
                    continue
                buffer = src_buffer.copy()
                pts = frame_index * self._frame_duration
                buffer.pts = pts
                buffer.dts = pts
                buffer.duration = self._frame_duration
                frame_index += 1

            ret = appsrc.emit("push-buffer", buffer)
            if ret != Gst.FlowReturn.OK:
                logging.info(f"Stopping continuous PTS push loop: {ret.value_nick}")
                break

    def _on_media_configure(self, _factory, media):
        elem = media.get_element()
        appsrc = elem.get_by_name("src")
        appsrc.set_property("stream-type", GstApp.AppStreamType.STREAM)
        # A shared factory can still prepare a fresh dynamic pipeline after the
        # last client disconnects. Always bind a pusher to that new appsrc; the
        # prior pusher exits with GST_FLOW_FLUSHING on its old appsrc.
        self._push_thread = threading.Thread(
            target=self._push_loop,
            args=(appsrc,),
            daemon=True,
        )
        self._push_thread.start()


class SimpleLoopRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory cho từng file MP4 phát lặp liên tục.
    Dùng filesrc để pipeline seek được về đầu khi nhận EOS.
    """
    def __init__(self, filepath: str, with_audio=False):
        super().__init__()
        self.filepath = filepath
        self.with_audio = with_audio
        # Connect media-constructed callback to add EOS handler
        self.connect("media-constructed", self._on_media_constructed)

    def do_create_element(self, _url):
        if self.with_audio:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! '
                'qtdemux name=d '
                f'd.video_0 ! identity sync=true ! {LOW_LATENCY_QUEUE} ! '
                'decodebin ! videoconvert ! '
                'x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max=30 ! '
                'h264parse config-interval=-1 ! '
                f'{PARSER_CAPS} ! '
                'identity sync=true single-segment=true ! '
                'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 '
                f'd.audio_0 ! {LOW_LATENCY_QUEUE} ! '
                'aacparse ! rtpmp4apay name=pay1 pt=97 )'
            )
        else:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! '
                'qtdemux name=d '
                f'd.video_0 ! identity sync=true ! {LOW_LATENCY_QUEUE} ! '
                'decodebin ! videoconvert ! '
                'x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max=30 ! '
                'h264parse config-interval=-1 ! '
                f'{PARSER_CAPS} ! '
                'identity sync=true single-segment=true ! '
                'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
            )
        logging.info(f"Loop pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)

    def _on_media_constructed(self, factory, media):
        """Callback để xử lý EOS - seek về đầu thay vì dừng."""
        elem = media.get_element()
        bus = elem.get_bus()
        bus.add_signal_watch()
        seek_pending = {"value": False}

        def _seek_to_start():
            logging.info(f"Looping {self.filepath}: seeking to start")
            ok = elem.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0
            )
            if not ok:
                logging.error(f"Seek-to-start failed for {self.filepath}")
            seek_pending["value"] = False
            return False

        def _drop_eos_and_loop(_pad, info):
            event = info.get_event()
            if event and event.type == Gst.EventType.EOS:
                if not seek_pending["value"]:
                    seek_pending["value"] = True
                    GLib.idle_add(_seek_to_start)
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        def _add_eos_probe(pad):
            pad_name = pad.get_name()
            if not pad_name.startswith("video"):
                return
            pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, _drop_eos_and_loop)

        demux = elem.get_by_name("d")
        if demux:
            for pad in demux.pads:
                if pad.get_direction() == Gst.PadDirection.SRC:
                    _add_eos_probe(pad)

            def _on_demux_pad_added(_demux, pad):
                if pad.get_direction() == Gst.PadDirection.SRC:
                    _add_eos_probe(pad)

            demux.connect("pad-added", _on_demux_pad_added)

        def _on_msg(_bus, msg):
            if msg.type == Gst.MessageType.EOS:
                logging.info(f"EOS for {self.filepath}, seeking to start...")
                ok = elem.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0
                )
                if not ok:
                    logging.error(f"Seek-to-start failed for {self.filepath}")
            elif msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logging.error(f"Pipeline error: {err} - {debug}")

        bus.connect("message", _on_msg)

    def on_media_configure(self, _factory, media):
        """Đảm bảo media không bị suspend khi không có client."""
        media.set_automatic_direction(False)


class NativeLoopRtspMediaFactory(SimpleLoopRtspMediaFactory):
    """Pass through an H.264 or H.265 file and loop it at EOS."""
    def __init__(self, filepath: str, codec: str):
        self.codec = codec
        super().__init__(filepath, with_audio=False)

    def do_create_element(self, _url):
        filepath = GLib.filename_to_uri(os.path.abspath(self.filepath))
        if self.codec == "h264":
            caps = "video/x-h264"
            parser = "h264parse config-interval=-1"
            payloader = "rtph264pay"
            config_interval = "1"
        else:
            caps = "video/x-h265"
            parser = "h265parse config-interval=-1"
            payloader = "rtph265pay"
            config_interval = "-1"

        pipeline = (
            f'( uridecodebin uri="{filepath}" caps="{caps}" '
            f'! queue max-size-buffers=120 max-size-time=2000000000 max-size-bytes=0 '
            f'! {parser} '
            f'! {caps},stream-format=byte-stream,alignment=au '
            '! identity sync=true single-segment=true '
            f'! {payloader} name=pay0 pt=96 config-interval={config_interval} mtu=1400 )'
        )
        logging.info(f"{self.codec.upper()} passthrough loop pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)


class UdpRelayRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory relay một RTP/H264 UDP stream live.
    UDP source được phát lặp nền, nên RTSP client không nhận EOS khi file gốc hết.
    """
    def __init__(self, udp_port: int):
        super().__init__()
        self.udp_port = udp_port

    def do_create_element(self, _url):
        caps = (
            "application/x-rtp,media=video,clock-rate=90000,"
            "encoding-name=H264,payload=96"
        )
        pipeline = (
            f'( udpsrc port={self.udp_port} buffer-size=1048576 caps="{caps}" ! '
            'queue max-size-buffers=120 max-size-time=1000000000 max-size-bytes=0 leaky=downstream ! '
            'rtph264depay ! h264parse config-interval=-1 ! '
            'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
        )
        logging.info(f"UDP relay pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)


class AutoLoopRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory với seek-to-start loop.
    Khi video kết thúc, seek về 0 để phát lại từ đầu một cách mượt mà.
    """
    def __init__(self, filepath: str, with_audio=True):
        super().__init__()
        self.filepath = filepath
        self.with_audio = with_audio

    def do_create_element(self, _url):
        # Pipeline đơn giản
        if self.with_audio:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                'd. ! queue max-size-buffers=10 ! h264parse ! rtph264pay name=pay0 pt=96 '
                'd. ! queue max-size-buffers=10 ! aacparse ! rtpmp4apay name=pay1 pt=97 )'
            )
        else:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                'd. ! queue max-size-buffers=10 ! h264parse ! rtph264pay name=pay0 pt=96 )'
            )
        return Gst.parse_launch(pipeline)

    def on_media_constructed(self, _factory, media):
        """Callback để xử lý EOS - seek về đầu thay vì dừng."""
        elem = media.get_element()
        bus = elem.get_bus()
        bus.add_signal_watch()

        def _on_msg(_bus, msg):
            if msg.type == Gst.MessageType.EOS:
                logging.info(f"EOS for {self.filepath}, seeking to start...")
                elem.seek_simple(
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                    0
                )
            elif msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logging.error(f"Pipeline error: {err} - {debug}")
            elif msg.type == Gst.MessageType.WARNING:
                warn, debug = msg.parse_warning()
                logging.warning(f"Pipeline warning: {warn}")

        bus.connect("message", _on_msg)


class LoopRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory cho phát video tuần hoàn (loop) liên tục.
    Không restart khi hết video - phát lại từ đầu một cách mượt mà.
    """
    def __init__(self, filepath: str, with_audio=True, loop=True):
        super().__init__()
        self.filepath = filepath
        self.with_audio = with_audio
        self.loop = loop

    def do_create_element(self, _url):
        if self.with_audio:
            if self.loop:
                # Dùng multifilesrc với loop=true để phát lặp liên tục
                pipeline = (
                    f'( multifilesrc location="{self.filepath}" '
                    'caps="video/quicktime" '
                    'loop=true ! '
                    'qtdemux name=d '
                    'd.video_0 ! queue ! '
                    'h264parse config-interval=-1 ! '
                    'video/x-h264,stream-format=byte-stream,alignment=au ! '
                    'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 '
                    'd.audio_0 ! queue ! '
                    'aacparse ! rtpmp4apay name=pay1 pt=97 )'
                )
            else:
                pipeline = (
                    f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                    'd.video_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                    'h264parse config-interval=-1 ! '
                    'video/x-h264,stream-format=byte-stream,alignment=au ! '
                    'rtph264pay name=pay0 pt=96 config-interval=1 '
                    'd.audio_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                    'aacparse ! rtpmp4apay name=pay1 pt=97 )'
                )
        else:
            if self.loop:
                pipeline = (
                    f'( multifilesrc location="{self.filepath}" '
                    'caps="video/quicktime" '
                    'loop=true ! '
                    'qtdemux name=d '
                    'd.video_0 ! queue ! '
                    'h264parse config-interval=-1 ! '
                    'video/x-h264,stream-format=byte-stream,alignment=au ! '
                    'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
                )
            else:
                pipeline = (
                    f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                    'd.video_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                    'h264parse config-interval=-1 ! '
                    'video/x-h264,stream-format=byte-stream,alignment=au ! '
                    'rtph264pay name=pay0 pt=96 config-interval=1 )'
                )

        logging.debug(f"Loop pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)


class LoopSequenceRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory cho phát chuỗi video theo thứ tự và LẶP LẠI từ đầu.
    Ví dụ: video1.mp4 -> video2.mp4 -> video3.mp4 -> video1.mp4 -> ...
    """
    def __init__(self, filepaths: list[str], with_audio=False):
        super().__init__()
        self.filepaths = filepaths
        self.with_audio = with_audio
        self._current_index = 0

    def do_create_element(self, _url):
        pipeline = self._build_loop_pipeline()
        logging.debug(f"Loop sequence pipeline: {pipeline}")
        return Gst.parse_launch(pipeline)

    def _build_loop_pipeline(self, codec="h264"):
        if codec == 'h265':
            parser_pre = "h265parse config-interval=-1"
            caps_pre = "video/x-h265,stream-format=byte-stream"
            payloader = "rtph265pay"
        else:
            parser_pre = "h264parse config-interval=-1"
            caps_pre = "video/x-h264,stream-format=byte-stream"
            payloader = "rtph264pay"

        videos = self.filepaths

        # Xây dựng pipeline với tất cả filesrc nối tiếp
        # Mỗi filesrc sẽ nối vào concat thông qua valve để switch
        pipeline_parts = []

        # Tạo một mảng queue cho mỗi video
        for i, video in enumerate(videos):
            # multifilesrc với loop=true sẽ phát lại từ đầu khi hết file
            pipeline_parts.append(
                f'multifilesrc location="{video}" caps="video/quicktime" loop=true '
                f'! qtdemux name=d{i} '
                f'd{i}.video_0 ! {parser_pre} ! {caps_pre} ! queue name=q{i} ! '
            )

        # Nối tất cả queue vào concat
        concat_sources = " ".join([f"q{i}.sink" for i in range(len(videos))])

        # Concat với adjust-base-time=true cho seamless transition
        pipeline_parts.append(
            f'concat name=c adjust-base-time=true n={len(videos)} ! '
            f'queue ! {parser_pre} ! {payloader} name=pay0 pt=96 config-interval=1 mtu=1400'
        )

        # Kết nối: q0.src ! c.sink_0, q1.src ! c.sink_1, ...
        for i in range(len(videos)):
            pipeline_parts.append(f'q{i}.src ! c.sink_{i}')

        return "(" + " ".join(pipeline_parts) + ")"


class SeqLoopRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    RTSP Factory đơn giản hơn: phát từng file một, tự động chuyển sang file tiếp theo,
    và lặp lại từ đầu khi hết tất cả.
    """
    def __init__(self, filepaths: list[str], with_audio=False):
        super().__init__()
        self.filepaths = filepaths

    def do_create_element(self, _url):
        pipeline_str = self._build_pipeline()
        logging.info(f"SeqLoop pipeline: {pipeline_str}")
        return Gst.parse_launch(pipeline_str)

    def _build_pipeline(self):
        """
        Pipeline sử dụng concat nối tiếp các video và loop lại từ đầu.
        """
        videos = self.filepaths
        parts = []

        # Tạo source và demux cho mỗi video
        for i, video in enumerate(videos):
            parts.append(
                f'multifilesrc location="{video}" caps="video/quicktime" loop=true '
                f'! qtdemux name=d{i} '
                f'd{i}.video_0 ! h264parse ! video/x-h264,stream-format=byte-stream ! queue name=qsrc{i} '
            )

        # Tạo các link queue -> concat
        # concat name=c sẽ tự động chuyển sang sink tiếp theo khi một nguồn EOS
        concat_link = " ".join([f"qsrc{i}.src" for i in range(len(videos))])

        # Tạo concat với đủ sink pads
        for i in range(len(videos)):
            pass  # sinks will be requested dynamically

        # Xây dựng pipeline hoàn chỉnh
        # multifilesrc loop=true sẽ phát lại file khi hết
        # concat sẽ chờ EOS từ tất cả sources trước khi dừng
        # Nhưng vấn đề: concat không loop được!

        # GIẢI PHÁP: Dùng script-based loop thay vì pipeline thuần
        # Tạo một pipeline đơn giản với multifilesrc loop

        if len(videos) == 1:
            # Một file đơn lẻ - loop đơn giản
            return (
                f'( multifilesrc location="{videos[0]}" caps="video/quicktime" loop=true '
                '! qtdemux name=d '
                'd.video_0 ! queue ! h264parse ! video/x-h264,stream-format=byte-stream ! '
                'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
            )

        # Nhiều files - dùng approach khác
        # Tạo pipeline với bin chứa tất cả sources
        pipeline_str = '( '
        for i, video in enumerate(videos):
            pipeline_str += (
                f'multifilesrc location="{video}" caps="video/quicktime" loop=true '
                f'! qtdemux name=d{i} '
                f'd{i}.video_0 ! h264parse ! video/x-h264,stream-format=byte-stream ! '
                f'queue name=q{i} '
            )
        # Tất cả queues nối vào concat
        # concat sẽ đợi tất cả EOS trước khi kết thúc
        # Nhưng multifilesrc với loop=true sẽ không bao giờ EOS!
        for i in range(len(videos)):
            pipeline_str += f'q{i}.src ! '
        pipeline_str += (
            'concat name=c adjust-base-time=true ! '
            'queue ! h264parse ! video/x-h264,stream-format=byte-stream ! '
            'rtph264pay name=pay0 pt=96 config-interval=1 mtu=1400 )'
        )

        return pipeline_str


class ConcatRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    Factory cũ - giữ lại để tương thích ngược.
    Phát chuỗi video nhưng KHÔNG loop.
    """
    def __init__(self, filepaths: list[str], with_audio=False):
        super().__init__()
        self.filepaths = filepaths
        self.with_audio = with_audio

    def _get_video_source(self, filepath: str):
        return f'filesrc location="{filepath}"'

    def do_create_element(self, _url):
        pipeline_str = f"( {self._build_passthrough_pipeline()} )"
        logging.debug(f"Launching concat pipeline: {pipeline_str}")
        return Gst.parse_launch(pipeline_str)

    def _build_passthrough_pipeline(self, codec="h264"):
        if codec == 'h265':
            parser_pre = "h265parse config-interval=-1"
            caps_pre = "video/x-h265,stream-format=byte-stream"
            payloader = "rtph265pay"
        else:
            parser_pre = "h264parse config-interval=-1"
            caps_pre = "video/x-h264,stream-format=byte-stream"
            payloader = "rtph264pay"

        videos = self.filepaths
        pipeline_parts = []

        # 1. Setup the concat element first with its global properties
        # adjust-base-time=true is CRITICAL for sequential playback
        pipeline_parts.append(f"concat name=c adjust-base-time=true")

        # 2. Add source branches
        for i, video in enumerate(videos):
            source = self._get_video_source(video)
            # We link each filesrc branch to a numbered sink pad on concat
            # Use distinct name for qtdemux to explicitly link video pad
            pipeline_parts.append(f"{source} ! qtdemux name=d{i} d{i}.video_0 ! {parser_pre} ! {caps_pre} ! c.sink_{i}")

        # 3. Add the output branch (after the concat)
        # We put a parser AFTER concat to re-align the joined streams
        # Large queue buffer to prevent stuttering
        pipeline_parts.append(
            f"c. ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! "
            f"{parser_pre} ! {payloader} name=pay0 pt=96 config-interval=1 aggregate-mode=0 mtu=1400"
        )

        return " ".join(pipeline_parts)


class TestRtspMediaFactory(GstRtspServer.RTSPMediaFactory):
    """
    Factory cũ - giữ lại để tương thích ngược.
    Phát một file đơn lẻ nhưng KHÔNG loop.
    """
    def __init__(self, filepath: str, with_audio=True):
        super().__init__()
        self.filepath = filepath
        self.with_audio = with_audio

    def do_create_element(self, _url):
        if self.with_audio:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                'd.video_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                'h264parse config-interval=-1 ! '
                'video/x-h264,stream-format=byte-stream,alignment=au ! '
                'rtph264pay name=pay0 pt=96 config-interval=1 '
                'd.audio_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                'aacparse ! rtpmp4apay name=pay1 pt=97 )'
            )
        else:
            pipeline = (
                f'( filesrc location="{self.filepath}" ! qtdemux name=d '
                'd.video_0 ! queue max-size-buffers=0 max-size-time=3000000000 max-size-bytes=0 ! '
                'h264parse config-interval=-1 ! '
                'video/x-h264,stream-format=byte-stream,alignment=au ! '
                'rtph264pay name=pay0 pt=96 config-interval=1 )'
            )
        return Gst.parse_launch(pipeline)


class FileBoardcaster:
    def __init__(self, rtspServer):
        self.rtspServer = rtspServer
        self.rtspServer.set_service("8553")
        self._factories = []  # keep refs so factories aren't GC'ed
        self._dynamic_factories = {}
        self._processes: list[subprocess.Popen] = []

        self.rtspServer.attach(None)
        logging.info("RTSP Server started at rtsp://127.0.0.1:8553/")

    def _start_udp_loop_sender(self, filepath: str, udp_port: int) -> None:
        quoted_path = shlex.quote(filepath)
        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".mp4", ".mov", ".m4v"):
            source_pipeline = (
                f"filesrc location={quoted_path} ! "
                f"qtdemux name=d d.video_0 ! {LOW_LATENCY_QUEUE} ! "
                "h264parse config-interval=-1 ! "
                f"{PARSER_CAPS} ! "
                "identity sync=true single-segment=true ! "
                "rtph264pay pt=96 config-interval=1 mtu=1400"
            )
        else:
            source_pipeline = (
                f"filesrc location={quoted_path} ! "
                "decodebin ! videoconvert ! "
                "x264enc tune=zerolatency speed-preset=veryfast bitrate=2500 key-int-max=30 ! "
                "h264parse config-interval=-1 ! "
                f"{PARSER_CAPS} ! "
                "identity sync=true single-segment=true ! "
                "rtph264pay pt=96 config-interval=1 mtu=1400"
            )
        command = (
            "while true; do "
            f"gst-launch-1.0 -q {source_pipeline} ! "
            f"udpsink host=127.0.0.1 port={udp_port} sync=false async=false; "
            "sleep 0.05; "
            "done"
        )
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        self._processes.append(proc)
        logging.info(f"Started UDP loop sender for {filepath} on 127.0.0.1:{udp_port}")

    def stop(self) -> None:
        for proc in self._processes:
            if proc.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def on_media_constructed(self, _factory, media):
        elem = media.get_element()
        bus = elem.get_bus()
        bus.add_signal_watch()

        def _on_msg(_bus, msg):
            if msg.type == Gst.MessageType.EOS:
                # Với multifilesrc loop=true, EOS sẽ không xảy ra
                # Nhưng nếu xảy ra, seek về 0
                logging.debug("EOS received, seeking to start")
                elem.seek_simple(Gst.Format.TIME,
                                 Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                                 0)
            elif msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logging.error(f"Pipeline error: {err} - {debug}")

        bus.connect("message", _on_msg)

    def on_media_constructed_loop(self, _factory, media):
        """
        Callback cho loop media factory - không cần xử lý EOS
        vì multifilesrc với loop=true sẽ không bao giờ gửi EOS.
        """
        elem = media.get_element()
        bus = elem.get_bus()
        bus.add_signal_watch()

        def _on_msg(_bus, msg):
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                logging.error(f"Pipeline error: {err}")

        bus.connect("message", _on_msg)

    def broadcast_auto_loop_folder(self, folder: str, with_audio: bool = True,
                                   patterns: tuple[str, ...] = ("*.mp4", "*.mov", "*.m4v", "*.avi")) -> list[tuple[str, str]]:
        """
        Mount mỗi file video trong folder thành một RTSP stream với LOOP.
        Mỗi file được phát lặp nền qua UDP nội bộ đã được pace theo clock;
        RTSP chỉ relay luồng live để tránh EOS trực tiếp từ file source.
        """
        mnts = self.rtspServer.get_mount_points()
        added: list[tuple[str, str]] = []

        files: list[str] = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(folder, pat)))
        files = sorted(f for f in files if os.path.isfile(f))

        base_udp_port = 15000
        for index, fpath in enumerate(files):
            fname = os.path.basename(fpath)
            name_no_ext, _ = os.path.splitext(fname)
            mount_name = name_no_ext.replace(" ", "_")
            mount_path = f"/{mount_name}_loop"
            udp_port = base_udp_port + index

            factory = ContinuousPtsLoopRtspMediaFactory(fpath)
            logging.info(f"Using continuous-PTS loop RTSP factory for {fpath}")

            factory.set_shared(True)
            factory.set_eos_shutdown(False)
            factory.set_suspend_mode(GstRtspServer.RTSPSuspendMode.NONE)
            factory.set_stop_on_disconnect(False)

            mnts.add_factory(mount_path, factory)
            self._factories.append(factory)

            logging.info(f"Mounted AUTO-LOOP {fpath} at rtsp://127.0.0.1:8553{mount_path}")
            added.append((mount_path, os.path.abspath(fpath)))
        return added

    def broadcast_auto_loop_file(self, filepath: str,
                                 mount_path: str = "/stream") -> list[tuple[str, str]]:
        """Publish exactly one local video as one continuously looping stream."""
        filepath = os.path.abspath(filepath)
        if not os.path.isfile(filepath):
            logging.error(f"Video file not found: {filepath}")
            return []

        if not mount_path.startswith("/"):
            mount_path = f"/{mount_path}"

        factory = ContinuousPtsLoopRtspMediaFactory(filepath)
        factory.set_shared(True)
        factory.set_eos_shutdown(False)
        factory.set_suspend_mode(GstRtspServer.RTSPSuspendMode.NONE)
        factory.set_stop_on_disconnect(False)

        self.rtspServer.get_mount_points().add_factory(mount_path, factory)
        self._factories.append(factory)
        logging.info(f"Mounted single AUTO-LOOP video {filepath} at "
                     f"rtsp://127.0.0.1:8553{mount_path}")
        return [(mount_path, filepath)]

    def broadcast_native_loop_file(self, filepath: str, mount_path: str,
                                   codec: str) -> list[tuple[str, str]]:
        """Publish a native H.264/H.265 file as a looping RTSP stream."""
        filepath = os.path.abspath(filepath)
        if not os.path.isfile(filepath):
            logging.error(f"Video file not found: {filepath}")
            return []

        if not mount_path.startswith("/"):
            mount_path = f"/{mount_path}"

        self.remove_dynamic_stream(mount_path)
        factory = NativeLoopRtspMediaFactory(filepath, codec)
        factory.set_shared(True)
        factory.set_eos_shutdown(False)
        factory.set_suspend_mode(GstRtspServer.RTSPSuspendMode.NONE)
        factory.set_stop_on_disconnect(False)
        self.rtspServer.get_mount_points().add_factory(mount_path, factory)
        self._factories.append(factory)
        self._dynamic_factories[mount_path] = factory

        logging.info(f"Mounted {codec.upper()} loop {filepath} at "
                     f"rtsp://127.0.0.1:8553{mount_path}")
        return [(mount_path, filepath)]

    def remove_dynamic_stream(self, mount_path: str) -> None:
        factory = self._dynamic_factories.pop(mount_path, None)
        if factory is not None:
            self.rtspServer.get_mount_points().remove_factory(mount_path)
            if factory in self._factories:
                self._factories.remove(factory)
            logging.info(f"Removed RTSP stream {mount_path}")

    def broadcast_loop_folder(self, folder: str, with_audio: bool = True,
                              patterns: tuple[str, ...] = ("*.mp4", "*.mov", "*.m4v")) -> list[tuple[str, str]]:
        """
        Mount mỗi file video trong folder thành một RTSP stream LOOP.
        Mỗi video sẽ phát liên tục, lặp lại từ đầu khi hết.
        """
        mnts = self.rtspServer.get_mount_points()
        added: list[tuple[str, str]] = []

        files: list[str] = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(folder, pat)))
        files = sorted(f for f in files if os.path.isfile(f))

        for fpath in files:
            fname = os.path.basename(fpath)
            name_no_ext, _ = os.path.splitext(fname)
            mount_name = name_no_ext.replace(" ", "_")
            mount_path = f"/{mount_name}_loop"

            # Dùng LoopRtspMediaFactory thay vì TestRtspMediaFactory
            factory = LoopRtspMediaFactory(fpath, with_audio=with_audio, loop=True)
            factory.set_shared(False)
            factory.set_eos_shutdown(False)
            factory.connect("media-constructed", self.on_media_constructed_loop)

            mnts.add_factory(mount_path, factory)
            self._factories.append(factory)

            logging.info(f"Mounted LOOP {fpath} at rtsp://127.0.0.1:8553{mount_path}")
            added.append((mount_path, os.path.abspath(fpath)))
        return added

    def broadcast_sequence_loop(self, mount_path: str, folder: str,
                                patterns: tuple[str, ...] = ("*.mp4",)) -> list[tuple[str, str]]:
        """
        Mount một path phát chuỗi video theo thứ tự và LẶP LẠI từ đầu.
        """
        files: list[str] = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(folder, pat)))
        files.sort()

        if not files:
            logging.error("No files found!")
            return []

        logging.info(f"Creating loop sequence with {len(files)} videos")

        # Dùng SeqLoopRtspMediaFactory
        factory = SeqLoopRtspMediaFactory(files, with_audio=False)
        factory.set_shared(False)
        factory.set_eos_shutdown(False)
        factory.connect("media-constructed", self.on_media_constructed_loop)

        mnts = self.rtspServer.get_mount_points()
        mnts.add_factory(mount_path, factory)
        self._factories.append(factory)

        logging.info(f"Mounted LOOP sequence at rtsp://127.0.0.1:8553{mount_path}")
        return [(mount_path, f) for f in files]

    def broadcast_folder(self, folder: str, with_audio: bool = True,
                         patterns: tuple[str, ...] = ("*.mp4", "*.mov", "*.m4v")) -> list[tuple[str, str]]:
        """
        Mount mỗi file video trong folder (KHÔNG loop - factory cũ).
        """
        mnts = self.rtspServer.get_mount_points()
        added: list[tuple[str, str]] = []

        files: list[str] = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(folder, pat)))
        files = sorted(f for f in files if os.path.isfile(f))

        for fpath in files:
            fname = os.path.basename(fpath)
            name_no_ext, _ = os.path.splitext(fname)
            mount_name = name_no_ext.replace(" ", "_")
            mount_path = f"/{mount_name}"

            factory = TestRtspMediaFactory(fpath, with_audio=with_audio)
            factory.set_shared(False)
            factory.set_eos_shutdown(False)
            factory.connect("media-constructed", self.on_media_constructed)

            mnts.add_factory(mount_path, factory)
            self._factories.append(factory)

            logging.info(f"Mounted {fpath} at rtsp://127.0.0.1:8553{mount_path}")
            added.append((mount_path, os.path.abspath(fpath)))
        return added

    def broadcast_sequence(self, mount_path: str, folder: str, patterns=("*.mp4",)):
        """
        Mount path phát chuỗi video theo thứ tự (KHÔNG loop - factory cũ).
        """
        files = []
        for pat in patterns:
            files.extend(glob.glob(os.path.join(folder, pat)))
        files.sort()

        if not files:
            logging.error("No files found to concatenate!")
            return

        factory = ConcatRtspMediaFactory(files, with_audio=False)
        factory.set_shared(True)
        factory.connect("media-constructed", self.on_media_constructed)

        mnts = self.rtspServer.get_mount_points()
        mnts.add_factory(mount_path, factory)
        self._factories.append(factory)

        logging.info(f"Mounted sequence at rtsp://127.0.0.1:8553{mount_path}")


def probe_video_codec(filepath: str) -> str:
    """Return the first video stream's codec name using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "json", filepath,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams or not streams[0].get("codec_name"):
        raise ValueError("No video stream found")
    return streams[0]["codec_name"].lower()


class RecursiveVideoWatcher:
    def __init__(self, broadcaster: FileBoardcaster, watch_dirs: list[str],
                 scan_interval: float = 3.0):
        self.broadcaster = broadcaster
        self.watch_dirs = [os.path.abspath(path) for path in watch_dirs]
        self.scan_interval = max(1.0, scan_interval)
        self._stop_event = threading.Event()
        self._thread = None
        self._observed: dict[str, tuple[tuple[int, int], int]] = {}
        self._processed: dict[str, tuple[int, int]] = {}
        self._registered: dict[str, tuple[list[str], tuple[int, int]]] = {}
        self._route_owners: dict[str, str] = {}
        self._route_conflicts: dict[str, set[str]] = {}

    def start(self) -> None:
        logging.info(f"Watching video folders recursively: {', '.join(self.watch_dirs)}")
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _iter_files(self):
        for root in self.watch_dirs:
            if os.path.isfile(root):
                yield root
                continue
            if not os.path.isdir(root):
                logging.warning(f"Video watch folder does not exist: {root}")
                continue
            for directory, subdirs, filenames in os.walk(root):
                subdirs[:] = [name for name in subdirs if name not in (".git", "__pycache__")]
                for filename in filenames:
                    if not filename.startswith("."):
                        yield os.path.join(directory, filename)

    def _scan_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan_once()
            except Exception:
                logging.exception("Video folder scan failed")
            self._stop_event.wait(self.scan_interval)

    def _scan_once(self) -> None:
        candidates = set(self._iter_files())
        for filepath in sorted(candidates):
            try:
                stat = os.stat(filepath)
            except OSError:
                continue
            signature = (stat.st_size, stat.st_mtime_ns)
            previous = self._observed.get(filepath)
            if previous is None or previous[0] != signature:
                self._observed[filepath] = (signature, 1)
                continue

            stable_scans = previous[1] + 1
            self._observed[filepath] = (signature, stable_scans)
            if stable_scans < 2 or self._processed.get(filepath) == signature:
                continue

            if self._process_file(filepath, signature):
                self._processed[filepath] = signature

        for filepath in list(self._observed):
            if filepath in candidates:
                continue
            self._observed.pop(filepath, None)
            self._processed.pop(filepath, None)
            self._remove_registration(filepath)
            for conflicts in self._route_conflicts.values():
                conflicts.discard(filepath)

    def _remove_registration(self, filepath: str) -> None:
        registration = self._registered.pop(filepath, None)
        if registration is None:
            return

        mount_paths, _ = registration
        for mount_path in mount_paths:
            if self._route_owners.get(mount_path) != filepath:
                continue
            self._route_owners.pop(mount_path, None)
            GLib.idle_add(self.broadcaster.remove_dynamic_stream, mount_path)
            for waiting_path in self._route_conflicts.pop(mount_path, set()):
                self._processed.pop(waiting_path, None)

    def _process_file(self, filepath: str, signature: tuple[int, int]) -> bool:
        try:
            source_codec = probe_video_codec(filepath)
        except Exception as error:
            logging.info(f"Skipping non-video or unsupported file {filepath}: {error}")
            self._remove_registration(filepath)
            return True

        if source_codec == "h264":
            output_codec = "h264"
        elif source_codec in ("hevc", "h265"):
            output_codec = "h265"
        else:
            logging.info(
                f"Skipping {filepath}: RTSP accepts H.264/H.265 files; "
                "run transcode.py to create a compatible file in media/"
            )
            self._remove_registration(filepath)
            return True

        filename = os.path.splitext(os.path.basename(filepath))[0]
        mount_path = "/" + quote(filename, safe="-_.~")

        registration = self._registered.get(filepath)
        if registration is not None and registration[1] != signature:
            self._remove_registration(filepath)
            registration = None

        registered_paths = registration[0] if registration is not None else []
        self._registered[filepath] = (registered_paths, signature)

        current_owner = self._route_owners.get(mount_path)
        if current_owner is not None and current_owner != filepath:
            logging.warning(
                f"Skipping duplicate stream path '{mount_path}': {filepath} "
                f"conflicts with {current_owner}"
            )
            self._route_conflicts.setdefault(mount_path, set()).add(filepath)
            return True
        if current_owner == filepath and mount_path in registered_paths:
            return True

        self._route_owners[mount_path] = filepath
        registered_paths.append(mount_path)
        GLib.idle_add(self._mount_file, filepath, filepath, output_codec, mount_path)
        logging.info(
            f"Detected {source_codec} video {filepath}; RTSP path={mount_path}"
        )
        return True

    def _mount_file(self, source_path: str, playback_path: str,
                    codec: str, mount_path: str) -> bool:
        if os.path.isfile(source_path):
            self.broadcaster.broadcast_native_loop_file(playback_path, mount_path, codec)
        return False


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    watch_dirs = [
        path.strip()
        for path in os.environ.get(
            "VIDEO_WATCH_DIRS", os.path.join(script_dir, "media")
        ).split(",")
        if path.strip()
    ]
    Gst.init(None)
    loop = GLib.MainLoop()
    s = FileBoardcaster(GstRtspServer.RTSPServer())
    watcher = RecursiveVideoWatcher(s, watch_dirs)
    watcher.start()

    try:
        loop.run()
    finally:
        watcher.stop()
        s.stop()
