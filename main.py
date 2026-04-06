#!/usr/bin/env python3
"""
ClipGen — YouTube short clip generator.

Usage:
    python main.py "<youtube_url>"
    python main.py "<youtube_url>" --top 3
"""

import sys
import argparse
from dotenv import load_dotenv
from utils import logger, validate_youtube_url
from processor import run_pipeline


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate vertical short clips from a YouTube video URL."
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Number of top clips to generate (default: 5)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output",
        help="Output directory (default: output)",
    )
    args = parser.parse_args()

    if not validate_youtube_url(args.url):
        logger.error(f"Invalid YouTube URL: {args.url}")
        sys.exit(1)

    logger.info(f"ClipGen starting for: {args.url}")
    logger.info(f"Will generate up to {args.top} clips -> '{args.output}/'")

    try:
        clips = run_pipeline(args.url, output_dir=args.output, top_n=args.top)
        logger.info(f"\nSuccess! {len(clips)} clip(s) ready in '{args.output}/'")
        for c in clips:
            print(c)
    except Exception as e:
        logger.error(f"Pipeline failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
