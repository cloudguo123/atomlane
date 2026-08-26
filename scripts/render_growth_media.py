#!/usr/bin/env python3
"""Render checked-in SVG growth assets to PNG and animated GIF on macOS."""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CHROME = pathlib.Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
GIF_FILTER = (
    "fps=2,scale=960:-1:flags=lanczos,split[s0][s1];"
    "[s0]palettegen=max_colors=64:stats_mode=diff[p];"
    "[s1][p]paletteuse=dither=none:diff_mode=rectangle"
)


def run(argv: list[str]) -> None:
    completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"command failed: {argv[0]}")


def render_social(chrome: pathlib.Path, source: pathlib.Path, output: pathlib.Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            str(chrome),
            "--headless",
            "--disable-gpu",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=1280,640",
            f"--screenshot={output}",
            source.resolve().as_uri(),
        ]
    )


def render_demo(source_dir: pathlib.Path, output: pathlib.Path, frame_rate: int) -> None:
    source_frames = sorted(source_dir.glob("frame-*.svg"))
    if not source_frames:
        raise FileNotFoundError(f"no SVG frames found in {source_dir}")
    with tempfile.TemporaryDirectory(prefix="mpa-demo-") as temporary:
        temporary_path = pathlib.Path(temporary)
        rendered = temporary_path / "rendered"
        rendered.mkdir()
        for index, source in enumerate(source_frames):
            run(["qlmanage", "-t", "-s", "1200", "-o", str(temporary_path), str(source)])
            preview = temporary_path / f"{source.name}.png"
            target = rendered / f"frame-{index:03d}.png"
            run(["sips", "-c", "675", "1200", str(preview), "--out", str(target)])
        output.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-framerate",
                str(frame_rate),
                "-i",
                str(rendered / "frame-%03d.png"),
                "-filter_complex",
                GIF_FILTER,
                "-loop",
                "0",
                str(output),
            ]
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", type=pathlib.Path, default=DEFAULT_CHROME)
    parser.add_argument("--assets-dir", type=pathlib.Path, default=ROOT / "assets" / "growth")
    parser.add_argument("--share-dir", type=pathlib.Path, default=ROOT / "docs" / "share")
    parser.add_argument("--frame-rate", type=int, default=2)
    args = parser.parse_args()
    if not args.chrome.is_file():
        raise FileNotFoundError(f"Chrome executable not found: {args.chrome}")
    for executable in ("qlmanage", "sips", "ffmpeg"):
        if shutil.which(executable) is None:
            raise FileNotFoundError(f"required renderer not found: {executable}")
    social_png = args.assets_dir / "social-preview.png"
    demo_gif = args.assets_dir / "demo.gif"
    render_social(args.chrome, args.assets_dir / "social-preview.svg", social_png)
    render_demo(args.assets_dir / "demo-frames", demo_gif, args.frame_rate)
    args.share_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(social_png, args.share_dir / social_png.name)
    shutil.copy2(demo_gif, args.share_dir / demo_gif.name)
    print(f"rendered {social_png} and {demo_gif}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
