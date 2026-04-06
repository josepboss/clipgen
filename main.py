#!/usr/bin/env python3
"""
ClipGen — YouTube short clip generator.

Web UI mode (default):
    python main.py
    → opens http://localhost:5000

CLI mode:
    python main.py "<youtube_url>"
    python main.py "<youtube_url>" --top 3
"""

import sys
import argparse
from dotenv import load_dotenv


def run_cli(url: str, top: int, output: str) -> None:
    from utils import logger, validate_youtube_url
    from processor import run_pipeline

    if not validate_youtube_url(url):
        logger.error(f"Invalid YouTube URL: {url}")
        sys.exit(1)

    logger.info(f"ClipGen CLI starting for: {url}")
    logger.info(f"Will generate up to {top} clips -> '{output}/'")

    try:
        clips = run_pipeline(url, output_dir=output, top_n=top)
        logger.info(f"\nSuccess! {len(clips)} clip(s) ready in '{output}/'")
        for c in clips:
            print(c)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def run_web() -> None:
    from app import app
    import os

    port = int(os.environ.get("PORT", 5000))
    print(f"ClipGen UI → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "ClipGen — generate vertical short clips from YouTube.\n"
            "No arguments: start web UI.  Pass a URL: run CLI pipeline."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", nargs="?", default=None, help="YouTube video URL (omit to start web UI)")
    parser.add_argument("--top", type=int, default=5, help="Number of clips to generate (default: 5)")
    parser.add_argument("--output", type=str, default="output", help="Output directory (default: output)")
    args = parser.parse_args()

    if args.url:
        run_cli(args.url, args.top, args.output)
    else:
        run_web()


if __name__ == "__main__":
    main()
