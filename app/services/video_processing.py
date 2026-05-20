from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def is_mp4_video_media(media) -> bool:
    filename = str(getattr(media, "original_filename", "") or "").strip().lower()
    content_type = str(getattr(media, "content_type", "") or "").strip().lower()
    return filename.endswith(".mp4") or content_type == "video/mp4"


def is_mov_video_media(media) -> bool:
    filename = str(getattr(media, "original_filename", "") or "").strip().lower()
    content_type = str(getattr(media, "content_type", "") or "").strip().lower()
    return filename.endswith(".mov") or content_type in {"video/quicktime", "video/mov"}


def is_ebay_video_upload_candidate(media) -> bool:
    return is_mp4_video_media(media) or is_mov_video_media(media)


def mp4_filename_for_media(media) -> str:
    filename = str(getattr(media, "original_filename", "") or "listing-video.mp4").strip()
    if not filename:
        return "listing-video.mp4"
    if "." in filename:
        stem = filename.rsplit(".", 1)[0].strip() or "listing-video"
        return f"{stem}.mp4"
    return f"{filename}.mp4"


def transcode_mov_to_mp4(file_bytes: bytes, *, filename: str = "listing-video.mov") -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("MOV video requires ffmpeg to convert to eBay-required MP4 format.")
    if not file_bytes:
        raise RuntimeError("MOV video bytes are empty.")

    suffix = ".mov"
    raw_name = str(filename or "").strip().lower()
    if raw_name.endswith(".qt"):
        suffix = ".qt"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        input_path = tmp_path / f"input{suffix}"
        output_path = tmp_path / "output.mp4"
        input_path.write_bytes(file_bytes)
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(input_path),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0 or not output_path.exists():
            stderr = str(result.stderr or "").strip()
            raise RuntimeError(
                "MOV to MP4 conversion failed."
                + (f" ffmpeg: {stderr[-500:]}" if stderr else "")
            )
        return output_path.read_bytes()
