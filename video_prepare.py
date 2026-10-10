#!/usr/bin/env python3
"""Prepare a session-owned quarantined upload. This is not a public upload API."""
import argparse
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile

MAX_BYTES = 75 * 1024 * 1024


def run(args, timeout):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError('Media processing failed or exceeded its time limit.') from None


def inspect(path):
    raw = run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm', '-show_format', '-show_streams', '-of', 'json', str(path)], 30)
    try:
        data = json.loads(raw)
        duration = float(data.get('format', {}).get('duration', 'nan'))
        video = next(s for s in data['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic'))
    except (ValueError, KeyError, StopIteration, TypeError):
        raise ValueError('A decodable video stream with measured duration is required.') from None
    if not math.isfinite(duration):
        # Browser MediaRecorder WebM often omits its container Duration field.
        # Measure bounded video packet timestamps instead of trusting client timing.
        packets = json.loads(run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-format_whitelist', 'mov,matroska,webm', '-select_streams', 'v:0', '-read_intervals', '%+31', '-show_entries', 'packet=pts_time,duration_time', '-show_packets', '-of', 'json', str(path)], 30)).get('packets', [])
        points = []
        for packet in packets:
            try:
                start = float(packet['pts_time']); length = float(packet.get('duration_time', '0'))
                if math.isfinite(start) and math.isfinite(length): points.append((start, start + max(0, length)))
            except (ValueError, KeyError, TypeError): pass
        if not points: raise ValueError('Measured video timing is required.')
        duration = max(end for _, end in points) - min(start for start, _ in points)
    if not math.isfinite(duration) or duration < 10 or duration > 30.1:
        raise ValueError('Video duration must be between 10 and 30 seconds.')
    width, height = video.get('width', 0), video.get('height', 0)
    if not isinstance(width, int) or not isinstance(height, int) or width < 2 or height < 2 or width * height > 3840 * 2160:
        raise ValueError('Video dimensions are unsupported.')
    return {'duration_seconds': duration, 'width': width, 'height': height, 'codec': video.get('codec_name')}


def prepare(source, destination):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise ValueError('FFmpeg and FFprobe are required in the isolated worker.')
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file() or source.suffix.lower() not in ('.mp4', '.webm', '.mov'):
        raise ValueError('A quarantined MP4, WebM or MOV input is required.')
    if source.stat().st_size == 0 or source.stat().st_size > MAX_BYTES:
        raise ValueError('Input size must be greater than zero and at most 75 MiB.')
    if destination.exists() or destination.suffix.lower() != '.mp4' or source == destination:
        raise ValueError('Output must be a new MP4 path.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    measured = inspect(source)
    with tempfile.TemporaryDirectory(prefix='lit-video-', dir=destination.parent) as temporary:
        output = Path(temporary) / 'prepared.mp4'
        run(['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-threads', '2', '-format_whitelist', 'mov,matroska,webm', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?', '-map_metadata', '-1', '-map_chapters', '-1', '-vf', "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2", '-c:v', 'libx264', '-preset', 'fast', '-crf', '24', '-pix_fmt', 'yuv420p', '-threads', '2', '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', '-t', '31', str(output)], 180)
        result = inspect(output)
        if abs(result['duration_seconds'] - measured['duration_seconds']) > 0.25 or output.stat().st_size > MAX_BYTES:
            raise ValueError('Prepared video failed duration or size validation.')
        created = False
        try:
            with destination.open('xb') as target:
                created = True
                with output.open('rb') as input_file:
                    shutil.copyfileobj(input_file, target)
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
    return {**result, 'bytes': destination.stat().st_size, 'content_type': 'video/mp4'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('input')
    parser.add_argument('output')
    options = parser.parse_args()
    try:
        print(json.dumps(prepare(options.input, options.output)))
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')
