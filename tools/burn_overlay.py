"""Burn the cursor + trail + click ripples into a video, deterministically from
the recorded events (so the master stays clean and this is reproducible).

    python tools/burn_overlay.py --dataset data/demo --out data/demo/video_cursor.mp4

Frames are decoded to rawvideo, composited with PIL, and re-encoded in a single
streaming pipeline (no temp PNG sequence). Expect ~2-5x realtime at 1080p.

When the recorder already drew the OS cursor into the video
(``metadata.cursor_in_video``), the arrow is skipped by default so it is not
drawn twice; trail and ripples are still added (``--force-cursor`` overrides).
Coordinates go through ``view/geometry.json``, so monitor/app captures whose
origin is negative (monitor left of the primary) line up correctly.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from recorder.i18n import L, safe_stdio  # noqa: E402
from recorder.paths import ffmpeg_path, ffprobe_path  # noqa: E402

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - reported by main()
    Image = ImageDraw = None

ARROW = [(0, 0), (0, 18.6), (4.7, 14.2), (7.5, 20.6), (10.3, 19.3), (7.6, 13.1), (13.3, 13.1)]
BUTTON_COLORS = {"left": (77, 163, 255), "right": (255, 107, 107), "middle": (255, 200, 87)}


def probe(path: str, ffprobe: str = "ffprobe") -> Tuple[int, int, float]:
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", path]
    st = (json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)["streams"])[0]
    num, _, den = (st.get("avg_frame_rate") or "30/1").partition("/")
    fps = float(num) / max(1.0, float(den or 1))
    return int(st["width"]), int(st["height"]), fps if fps > 0 else 30.0


def load_frames(path: str, total: int) -> Tuple[List[int], List[int], List[int]]:
    """Per-frame cursor (x, y, button bits); frames missing from the index keep
    the previous position ((0, 0) is a real position on multi-monitor setups)."""
    xs, ys, bs = [0] * total, [0] * total, [0] * total
    seen = [False] * total
    if not os.path.exists(path):
        return xs, ys, bs
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if len(line) < 5:
                continue
            o = json.loads(line)
            f = o.get("f", -1)
            if isinstance(f, int) and 0 <= f < total:
                xs[f], ys[f], bs[f] = o.get("x", 0), o.get("y", 0), o.get("b", 0)
                seen[f] = True
    for f in range(1, total):
        if not seen[f]:
            xs[f], ys[f], bs[f] = xs[f - 1], ys[f - 1], bs[f - 1]
    return xs, ys, bs


def cursor_already_in_video(dataset: str, meta: Dict) -> bool:
    if "cursor_in_video" in meta:
        return meta.get("cursor_in_video") is True
    try:
        with open(os.path.join(dataset, "capture.json"), "r", encoding="utf-8") as fh:
            return json.load(fh).get("cursor_in_video") is True
    except (OSError, ValueError, AttributeError):
        return False


def load_events(path: str) -> List[Dict]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if len(line) > 5]


def _axis_scale(video_px: int, screen_px) -> float:
    """video px per screen px; a 1 px difference is even-size padding, not scaling."""
    try:
        screen = float(screen_px)
    except (TypeError, ValueError):
        return 1.0
    if screen <= 0 or abs(video_px - screen) <= 1:
        return 1.0
    return video_px / screen


def draw_frame(
    rgb: Image.Image,
    f: int,
    xs: List[int],
    ys: List[int],
    bs: List[int],
    events: List[Dict],
    fps: float,
    geom: Dict,
    scale_hint: float,
    trail_frames: int,
    cursor: bool,
    trail: bool,
    ripple: bool,
) -> Image.Image:
    W, H = rgb.size
    ox, oy = geom.get("originX", 0), geom.get("originY", 0)
    sx = _axis_scale(W, geom.get("width"))
    sy = _axis_scale(H, geom.get("height"))
    s = max(1.0, H / 900.0) * 1.05

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    to_px = lambda x, y: ((x - ox) * sx, (y - oy) * sy)  # noqa: E731

    if trail and f > 0:
        pts = []
        for k in range(max(1, f - trail_frames + 1), f + 1):
            px, py = to_px(xs[k], ys[k])
            if not pts or abs(px - pts[-1][0]) + abs(py - pts[-1][1]) > 1.5:
                pts.append((px, py))
        n = len(pts)
        for i in range(1, n):
            a = i / n
            alpha = int(115 * a * a)
            width = max(1, int(round(s * (0.6 + 3.4 * a))))
            d.line([pts[i - 1], pts[i]], fill=(120, 190, 255, alpha), width=width)

    if ripple:
        now = f / fps
        for e in events:
            if e.get("type") != "click" or not e.get("isDown"):
                continue
            et = e.get("t", 0) / 1000.0
            if et > now or now - et > 0.4:
                continue
            p = (now - et) / 0.4
            px, py = to_px(e.get("x", 0), e.get("y", 0))
            r = s * (4 + 22 * p)
            color = BUTTON_COLORS.get(e.get("button", "left"), BUTTON_COLORS["left"])
            d.ellipse([px - r, py - r, px + r, py + r],
                      outline=color + (int(190 * (1 - p)),), width=max(1, int(s * 2 * (1 - p)) + 1))

    if cursor:
        px, py = to_px(xs[f], ys[f])
        k = s * (0.94 if bs[f] & 1 else 1.0)
        poly = [(px + ax * k, py + ay * k) for ax, ay in ARROW]
        d.polygon(poly, fill=(255, 255, 255, 255) if not bs[f] & 1 else (207, 230, 255, 255))
        d.line(poly + [poly[0]], fill=(0, 0, 0, 220), width=max(1, int(s * 1.2)))
        if bs[f] & 1:
            r = 9 * s
            d.ellipse([px - r, py - r, px + r, py + r], outline=(77, 163, 255, 220),
                      width=max(1, int(s * 1.5)))

    return Image.alpha_composite(rgb.convert("RGBA"), layer).convert("RGB")


def main(argv=None) -> int:
    safe_stdio()   # bilingual messages on a cp1252 pipe (installed app runs python -E)
    ap = argparse.ArgumentParser(description=L("把光标、拖尾和点击涟漪烧录进视频",
                                               "Burn the cursor, trail and click ripples into the video"))
    ap.add_argument("--dataset", required=True, help=L("数据包目录", "dataset folder"))
    ap.add_argument("--out", default=None, help=L("输出视频（默认 <dataset>/video_cursor.mp4）",
                                                  "output video (default <dataset>/video_cursor.mp4)"))
    ap.add_argument("--trail-ms", type=float, default=600)
    ap.add_argument("--no-cursor", action="store_true", help=L("不画光标箭头", "do not draw the cursor arrow"))
    ap.add_argument("--force-cursor", action="store_true",
                    help=L("视频已带光标时仍画箭头", "draw the arrow even if the video already shows the cursor"))
    ap.add_argument("--no-trail", action="store_true")
    ap.add_argument("--no-ripple", action="store_true")
    ap.add_argument("--crf", default="18")
    ap.add_argument("--ffmpeg", default=None, help=L("FFmpeg 路径（默认自带或 PATH 中的）",
                                                     "FFmpeg path (default: bundled or on PATH)"))
    args = ap.parse_args(argv)
    if Image is None:
        print(L("需要 Pillow：pip install pillow", "Pillow is required: pip install pillow"))
        return 2
    ffmpeg = args.ffmpeg or ffmpeg_path()
    sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    ffprobe = sibling if args.ffmpeg and os.path.isfile(sibling) else ffprobe_path()

    d = os.path.abspath(args.dataset)
    with open(os.path.join(d, "metadata.json"), "r", encoding="utf-8") as fh:
        meta = json.load(fh)
    video = os.path.join(d, meta.get("video_file") or ("clip.mp4" if os.path.exists(os.path.join(d, "clip.mp4"))
                                                      else "video.mp4"))
    if not os.path.exists(video):
        print(L("[burn] 找不到视频 {path}", "[burn] missing {path}", path=video))
        return 2
    out = args.out or os.path.join(d, "video_cursor.mp4")
    try:
        W, H, fps = probe(video, ffprobe)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        print(L("[burn] 无法读取视频信息（{err}）", "[burn] cannot probe the video ({err})", err=exc))
        return 2
    draw_cursor = not args.no_cursor
    if draw_cursor and not args.force_cursor and cursor_already_in_video(d, meta):
        print(L("[burn] 视频已带系统光标，只叠加拖尾和涟漪（--force-cursor 可强制画箭头）",
                "[burn] the video already shows the OS cursor; adding trail and ripples only (--force-cursor to draw it)"))
        draw_cursor = False
    total = int(meta["total_frames"])
    geom = {"originX": 0, "originY": 0, "width": W, "height": H}
    geo_path = os.path.join(d, "view", "geometry.json")
    if os.path.exists(geo_path):
        with open(geo_path, "r", encoding="utf-8") as fh:
            geom.update(json.load(fh))
    xs, ys, bs = load_frames(os.path.join(d, "view", "mouse.jsonl"), total)
    events = load_events(os.path.join(d, "view", "events.jsonl"))
    trail_frames = max(1, int(args.trail_ms / 1000.0 * fps))

    dec = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", video,
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    # yuv420p needs even dimensions; pad odd monitor/window sizes by one pixel
    enc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                            "-r", f"{fps}", "-i", "-",
                            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf),
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
                           stdin=subprocess.PIPE)
    assert dec.stdout and enc.stdin
    fsize = W * H * 3
    f = 0
    try:
        while True:
            buf = dec.stdout.read(fsize)
            if not buf or len(buf) < fsize:
                break
            img = Image.frombytes("RGB", (W, H), buf)
            if f < len(xs):
                img = draw_frame(img, f, xs, ys, bs, events, fps, geom, H / 900.0,
                                 trail_frames,
                                 cursor=draw_cursor, trail=not args.no_trail,
                                 ripple=not args.no_ripple)
            enc.stdin.write(img.tobytes())
            f += 1
            if f % 150 == 0:
                print(f"\r[burn] {f}/{total} frames", end="", flush=True)
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        enc.wait()
        dec.wait()
    print(f"\r[burn] wrote {out} ({f} frames)" + " " * 20)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
