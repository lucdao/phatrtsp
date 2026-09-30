#!/usr/bin/env python3
"""Convert one video to H.264 or H.265 without starting the RTSP server."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_MEDIA_DIR = Path(__file__).resolve().parent.parent.parent / "media"


def probe_video_codec(ffprobe: str, filepath: Path) -> str:
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "json", str(filepath),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(result.stdout).get("streams", [])
    if not streams or not streams[0].get("codec_name"):
        raise ValueError(f"No video stream found in {filepath}")
    return streams[0]["codec_name"].lower()


def output_filename(input_path: Path, target_codec: str) -> str:
    stem = input_path.stem
    lowered_stem = stem.lower()
    for suffix in ("_h264", "_h265"):
        if lowered_stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    return f"{stem}_{target_codec}.mp4"


def transcode(input_path: Path, target_codec: str, output_dir: Path,
              overwrite: bool = False) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("ffmpeg and ffprobe must be installed")

    input_path = input_path.expanduser().resolve(strict=True)
    if not input_path.is_file():
        raise ValueError(f"Input is not a file: {input_path}")

    source_codec = probe_video_codec(ffprobe, input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_filename(input_path, target_codec)
    if output_path.resolve() == input_path:
        raise ValueError("Input and output paths are the same; choose another output folder")
    if output_path.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}. Pass --overwrite to replace it."
        )

    temporary_path = output_dir / f".{output_path.stem}.partial-{os.getpid()}.mp4"
    encoder = "libx264" if target_codec == "h264" else "libx265"
    codec_options = (
        ["-x264-params", "keyint=30:min-keyint=30:scenecut=0", "-tag:v", "avc1"]
        if target_codec == "h264"
        else ["-x265-params", "keyint=30:min-keyint=30:scenecut=0", "-tag:v", "hvc1"]
    )
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "warning", "-stats",
        "-stats_period", "5", "-y", "-i", str(input_path),
        "-map", "0:v:0", "-an", "-c:v", encoder,
        "-preset", "ultrafast", "-tune", "zerolatency",
        "-crf", "23" if target_codec == "h264" else "28",
        "-pix_fmt", "yuv420p", *codec_options,
        "-movflags", "+faststart", str(temporary_path),
    ]

    print(f"Input codec: {source_codec}")
    print(f"Output codec: {target_codec}")
    print(f"Output file: {output_path}")
    try:
        subprocess.run(command, check=True)
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg did not create a usable output file")
        if output_path.exists() and not overwrite:
            raise FileExistsError(f"Output already exists: {output_path}")
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a video to H.264 or H.265 and save it separately."
    )
    parser.add_argument("input", type=Path, help="Input video file")
    parser.add_argument("codec", choices=("h264", "h265"), help="Target codec")
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_MEDIA_DIR,
        help=f"Output folder (default: {PROJECT_MEDIA_DIR})",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing output file",
    )
    args = parser.parse_args()

    try:
        output_path = transcode(
            args.input, args.codec, args.output_dir, overwrite=args.overwrite
        )
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Transcode failed: {error}", file=sys.stderr)
        return 1

    print(f"Done: {output_path}")
    print("The RTSP watcher will pick up the output file from media/ automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
