"""Scratch: contact sheets for frame-checking intro answers (read-only on media; sheets go to ./sheets).

sheet(path, answers, out): one row per boundary to check. For each answer (label, start, end): a row around its start
(start-8 .. start+4) and a row around its end (end-4 .. end+8), 5 frames each, 256 px wide, timestamps on the frames."""

from __future__ import annotations

import io
import os
import subprocess
import sys

from PIL import Image, ImageDraw

W = 256
OFFS_START = (-8, -4, 0, 2, 4)
OFFS_END = (-4, -2, 0, 4, 8)


def frame(path: str, t: float) -> Image.Image:
    t = max(0.0, t)
    cmd = ["nice", "-n", "19", "/usr/bin/ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={W}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]  # fmt: skip
    out = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    if not out:
        return Image.new("RGB", (W, W * 9 // 16), (80, 0, 0))
    return Image.open(io.BytesIO(out)).convert("RGB")


def sheet(path: str, answers: list[tuple[str, float, float]], out: str) -> None:
    rows = []
    for label, start, end in answers:
        rows.append((f"{label} start {start:.1f}", [start + d for d in OFFS_START]))
        rows.append((f"{label} inside", [start + (end - start) * k / 6 for k in range(1, 6)]))
        rows.append((f"{label} end {end:.1f}", [end + d for d in OFFS_END]))
    tiles = [[frame(path, t) for t in times] for _, times in rows]
    h = max(im.height for row in tiles for im in row)
    img = Image.new("RGB", (W * 5, (h + 18) * len(rows)), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for r, ((title, times), row) in enumerate(zip(rows, tiles, strict=True)):
        y = r * (h + 18)
        draw.text((4, y + 2), title, fill=(0, 0, 0))
        for c, (t, im) in enumerate(zip(times, row, strict=True)):
            img.paste(im, (c * W, y + 18))
            draw.rectangle((c * W, y + 18, c * W + 58, y + 30), fill=(0, 0, 0))
            draw.text((c * W + 2, y + 19), f"{t:.1f}", fill=(255, 255, 0))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    img.save(out, quality=70)


if __name__ == "__main__":
    sheet(sys.argv[1], [("a", float(sys.argv[2]), float(sys.argv[3]))], sys.argv[4])
