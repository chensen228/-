from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_BASE_URL = "http://127.0.0.1:5050"
DEFAULT_OUTPUT_DIR = BASE_DIR / "screenshots"


PAGE_SPECS = [
    {
        "path": "/",
        "filename": "home_latest.png",
        "ready_selector": "#chart-courses",
        "extra_wait_ms": 1600,
    },
    {
        "path": "/library",
        "filename": "library_latest.png",
        "ready_selector": ".workspace-shell",
        "extra_wait_ms": 600,
    },
    {
        "path": "/academic",
        "filename": "academic_latest.png",
        "ready_selector": ".workspace-shell",
        "extra_wait_ms": 600,
    },
    {
        "path": "/practice",
        "filename": "practice_latest.png",
        "ready_selector": ".workspace-shell",
        "extra_wait_ms": 600,
    },
    {
        "path": "/data-center",
        "filename": "data_center_latest.png",
        "ready_selector": ".workspace-shell",
        "extra_wait_ms": 700,
    },
    {
        "path": "/governance-lab",
        "filename": "governance_latest.png",
        "ready_selector": ".workspace-shell",
        "extra_wait_ms": 700,
    },
    {
        "path": "/graph-lab",
        "filename": "graph_latest.png",
        "ready_selector": "#mynetwork canvas",
        "extra_wait_ms": 2200,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture full-page screenshots for the smart campus demo.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--scale", type=float, default=1.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.scale,
            locale="zh-CN",
            color_scheme="dark",
        )
        page = context.new_page()

        for spec in PAGE_SPECS:
            url = args.base_url.rstrip("/") + spec["path"]
            target = output_dir / spec["filename"]
            print(f"[capture] {url} -> {target.name}")

            page.goto(url, wait_until="networkidle", timeout=30000)
            try:
                page.wait_for_selector(spec["ready_selector"], state="visible", timeout=15000)
            except PlaywrightTimeoutError:
                print(f"  warning: selector not ready: {spec['ready_selector']}")
            page.wait_for_timeout(spec["extra_wait_ms"])
            page.screenshot(path=str(target), full_page=True)

        context.close()
        browser.close()

    print(output_dir)


if __name__ == "__main__":
    main()
