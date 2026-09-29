#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib
import time

Gst.init(None)

pipeline_str = (
    'rtspsrc location=rtsp://127.0.0.1:8553/stream latency=100 protocols=tcp ! '
    'rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! '
    'video/x-raw,format=I420 ! fakesink'
)

print(f"Pipeline: {pipeline_str}")
pipeline = Gst.parse_launch(pipeline_str)

pipeline.set_state(Gst.State.PLAYING)
print('Pipeline playing for 5 seconds...')

loop = GLib.MainLoop()
start = time.time()
frame_count = 0

def check_bus(bus, message):
    global frame_count
    if message.type == Gst.MessageType.ELEMENT:
        print(f"Element message: {message.src.get_name()}")
    elif message.type == Gst.MessageType.STATE_CHANGED:
        print(f"State changed: {message.src.get_name()}")
    elif message.type == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print(f"ERROR: {err}, {debug}")
    return True

bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect("message", check_bus)

# Wait and count messages
while time.time() - start < 5:
    msg = bus.pop_filtered(Gst.MessageType(0))
    time.sleep(0.1)

print('Done')
pipeline.set_state(Gst.State.NULL)
