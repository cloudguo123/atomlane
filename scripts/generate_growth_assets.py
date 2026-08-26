#!/usr/bin/env python3
"""Generate deterministic launch and benchmark-sharing assets."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCHMARK_PATH = ROOT / "docs" / "benchmark-results.json"


def seconds(value: float) -> str:
    minutes, remainder = divmod(round(value), 60)
    return f"{minutes}m {remainder:02d}s"


def load_benchmark() -> dict[str, Any]:
    data = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    latest = data.get("latest")
    cumulative = data.get("cumulative")
    if not isinstance(latest, dict) or not isinstance(cumulative, dict):
        raise TypeError("benchmark-results.json does not contain latest and cumulative objects")
    return data


def social_svg(data: dict[str, Any]) -> str:
    latest = data["latest"]
    serial = seconds(latest["serial_equivalent"]["seconds"])
    parallel = seconds(latest["parallel"]["wall_time_seconds"])
    saved = seconds(latest["savings"]["seconds"])
    speedup = f"{latest['savings']['speedup_multiplier']:.2f}×"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#07100e"/><stop offset="1" stop-color="#10261f"/></linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0"><stop stop-color="#65e6b4"/><stop offset="1" stop-color="#80b7ff"/></linearGradient>
    <radialGradient id="glow"><stop stop-color="#65e6b4" stop-opacity=".24"/><stop offset="1" stop-color="#65e6b4" stop-opacity="0"/></radialGradient>
  </defs>
  <rect width="1280" height="640" fill="url(#bg)"/>
  <circle cx="1090" cy="65" r="360" fill="url(#glow)"/>
  <path d="M80 91h42l15 15h92" fill="none" stroke="#65e6b4" stroke-width="8" stroke-linecap="round"/>
  <text x="80" y="154" fill="#65e6b4" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="24" font-weight="700" letter-spacing="4">CODEX PLUGIN · MACOS</text>
  <text x="80" y="235" fill="#ecf8f3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="60" font-weight="800" letter-spacing="-2">Finish local work faster.</text>
  <text x="80" y="304" fill="#ecf8f3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="60" font-weight="800" letter-spacing="-2">Keep task semantics intact.</text>
  <text x="82" y="360" fill="#9bb4ab" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="25">Atomic planning · safe concurrency · live progress · measured savings</text>
  <rect x="80" y="408" width="1120" height="142" rx="22" fill="#0b1714" stroke="#27433a"/>
  <g font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif">
    <text x="112" y="451" fill="#809a90" font-size="18">CONTROLLED 5+ MIN BENCHMARK</text>
    <text x="112" y="510" fill="#ecf8f3" font-size="42" font-weight="800">{html.escape(serial)}</text><text x="112" y="536" fill="#809a90" font-size="16">serial equivalent</text>
    <text x="398" y="510" fill="#65e6b4" font-size="42" font-weight="800">{html.escape(parallel)}</text><text x="398" y="536" fill="#809a90" font-size="16">parallel wall</text>
    <text x="675" y="510" fill="#80b7ff" font-size="42" font-weight="800">{html.escape(saved)}</text><text x="675" y="536" fill="#809a90" font-size="16">time saved</text>
    <text x="965" y="510" fill="#d7a6ff" font-size="42" font-weight="800">{speedup}</text><text x="965" y="536" fill="#809a90" font-size="16">observed speedup</text>
  </g>
  <text x="80" y="596" fill="#718b81" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="18">github.com/cloudguo123/mac-parallel-accelerator</text>
</svg>
"""


def listing_logo_svg() -> str:
    """Return the square marketplace logo used by plugin directories."""
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#07100e"/><stop offset="1" stop-color="#10261f"/></linearGradient>
    <linearGradient id="accent" gradientUnits="userSpaceOnUse" x1="208" y1="286" x2="816" y2="626"><stop stop-color="#65e6b4"/><stop offset="1" stop-color="#80b7ff"/></linearGradient>
    <radialGradient id="glow"><stop stop-color="#65e6b4" stop-opacity=".30"/><stop offset="1" stop-color="#65e6b4" stop-opacity="0"/></radialGradient>
  </defs>
  <rect width="1024" height="1024" rx="192" fill="url(#bg)"/>
  <circle cx="790" cy="180" r="360" fill="url(#glow)"/>
  <g fill="none" stroke="url(#accent)" stroke-width="34" stroke-linecap="round" stroke-linejoin="round">
    <path d="M208 286h174c54 0 80 46 130 46h304"/>
    <path d="M208 456h174c54 0 80-46 130-46h304"/>
    <path d="M208 626h608"/>
  </g>
  <g fill="#07100e" stroke="#65e6b4" stroke-width="20">
    <circle cx="208" cy="286" r="32"/><circle cx="208" cy="456" r="32"/><circle cx="208" cy="626" r="32"/>
    <circle cx="816" cy="332" r="32"/><circle cx="816" cy="410" r="32"/><circle cx="816" cy="626" r="32"/>
  </g>
  <text x="512" y="790" text-anchor="middle" fill="#ecf8f3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="150" font-weight="850" letter-spacing="16">MPA</text>
  <text x="512" y="875" text-anchor="middle" fill="#8ea79e" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="34" font-weight="700" letter-spacing="9">SAFE PARALLELISM</text>
</svg>
"""


def demo_frame(data: dict[str, Any], index: int, total: int) -> str:
    latest = data["latest"]
    progress = index / max(total - 1, 1)
    duration = latest["parallel"]["wall_time_seconds"]
    elapsed = duration * progress
    running = 0 if index == total - 1 else min(4, 1 + index // 2)
    completed = 4 if index == total - 1 else 0
    ready = max(0, 4 - running - completed)
    saved = latest["savings"]["seconds"] * progress
    bars = []
    for lane, label in enumerate(("artifact-hash", "planner-json", "isolated-io", "scheduler-sim")):
        active = progress > lane * 0.025
        width = max(0, min(760, (progress - lane * 0.025) * 800)) if active else 0
        fill = "#65e6b4" if index == total - 1 else "url(#accent)"
        bars.append(
            f'<text x="100" y="{300 + lane * 62}" fill="#a9bcb5" font-size="19">{label}</text>'
            f'<rect x="315" y="{280 + lane * 62}" width="780" height="28" rx="7" fill="#152720"/>'
            f'<rect x="315" y="{280 + lane * 62}" width="{width:.1f}" height="28" rx="7" fill="{fill}"/>'
        )
    state = "VERIFIED" if index == total - 1 else "RUNNING LIVE"
    state_color = "#65e6b4"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="675" viewBox="0 0 1200 675">
  <defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#07100e"/><stop offset="1" stop-color="#10261f"/></linearGradient><linearGradient id="accent"><stop stop-color="#65e6b4"/><stop offset="1" stop-color="#80b7ff"/></linearGradient></defs>
  <rect width="1200" height="675" fill="url(#bg)"/>
  <rect x="55" y="55" width="1090" height="565" rx="22" fill="#0b1714" stroke="#27433a"/>
  <circle cx="91" cy="92" r="7" fill="#ff756d"/><circle cx="116" cy="92" r="7" fill="#f5c451"/><circle cx="141" cy="92" r="7" fill="#65e6b4"/>
  <text x="180" y="101" fill="#769087" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="16">MAC PARALLEL ACCELERATOR · LIVE EXECUTION</text>
  <text x="95" y="176" fill="{state_color}" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="21" font-weight="700">● {state}</text>
  <text x="95" y="228" fill="#ecf8f3" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif" font-size="32" font-weight="750">elapsed {seconds(elapsed)}   running {running}   ready {ready}   completed {completed}   failed 0</text>
  {''.join(bars)}
  <rect x="95" y="545" width="1010" height="1" fill="#27433a"/>
  <text x="95" y="587" fill="#80b7ff" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="23">estimated saved this run  {seconds(saved)}</text>
  <text x="820" y="587" fill="#718b81" font-family="ui-monospace,SFMono-Regular,Menlo,monospace" font-size="18">peak 4-way · {latest['savings']['speedup_multiplier']:.2f}×</text>
</svg>
"""


def write_share_outputs(data: dict[str, Any], output_dir: pathlib.Path) -> None:
    latest = data["latest"]
    cumulative = data["cumulative"]
    share = {
        "schema_version": "1.0",
        "project": "Mac Parallel Accelerator",
        "source": "controlled-duration low-load benchmark",
        "run_id": latest["run_id"],
        "generated_at": latest["generated_at"],
        "parallel_wall_seconds": latest["parallel"]["wall_time_seconds"],
        "serial_equivalent_seconds": latest["serial_equivalent"]["seconds"],
        "time_saved_seconds": latest["savings"]["seconds"],
        "speedup_multiplier": latest["savings"]["speedup_multiplier"],
        "cumulative_saved_seconds": cumulative["saved_seconds"],
        "method_note": latest["serial_equivalent"]["method"],
        "report_url": "https://cloudguo123.github.io/mac-parallel-accelerator/",
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "social-preview.svg").write_text(social_svg(data), encoding="utf-8")
    (output_dir / "latest.json").write_text(
        json.dumps(share, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown = f"""# Mac Parallel Accelerator benchmark

⚡ **{seconds(share['parallel_wall_seconds'])} parallel wall time** vs **{seconds(share['serial_equivalent_seconds'])} serial equivalent** — **{seconds(share['time_saved_seconds'])} saved ({share['speedup_multiplier']:.2f}×)**.

This is a controlled low-load run with four independent workloads. The serial equivalent is the sum of observed task runtimes; the tasks were not rerun serially. [Inspect the live report]({share['report_url']}) and [raw evidence]({share['report_url']}benchmark-results.json).

```markdown
[![Mac Parallel Accelerator benchmark](https://img.shields.io/badge/MPA-{share['speedup_multiplier']:.2f}x%20observed-65e6b4)]({share['report_url']})
```
"""
    (output_dir / "latest.md").write_text(markdown, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets-dir", type=pathlib.Path, default=ROOT / "assets" / "growth")
    parser.add_argument("--share-dir", type=pathlib.Path, default=ROOT / "docs" / "share")
    parser.add_argument("--frames", type=int, default=40)
    args = parser.parse_args()
    data = load_benchmark()
    args.assets_dir.mkdir(parents=True, exist_ok=True)
    (args.assets_dir / "social-preview.svg").write_text(social_svg(data), encoding="utf-8")
    (args.assets_dir / "listing-logo.svg").write_text(listing_logo_svg(), encoding="utf-8")
    frames_dir = args.assets_dir / "demo-frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for index in range(args.frames):
        (frames_dir / f"frame-{index:03d}.svg").write_text(
            demo_frame(data, index, args.frames), encoding="utf-8"
        )
    write_share_outputs(data, args.share_dir)
    print(json.dumps({"social_preview": str(args.assets_dir / "social-preview.svg"), "listing_logo": str(args.assets_dir / "listing-logo.svg"), "frames": args.frames, "share_dir": str(args.share_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
