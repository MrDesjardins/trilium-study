from __future__ import annotations

import argparse
from pathlib import Path

from app.config import get_settings
from app.pipeline import configure_tts_runtime_warnings


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Generate WAV narration with Kokoro.")
    parser.add_argument("--input", required=True, help="Path to a UTF-8 text file.")
    parser.add_argument("--output", required=True, help="Path to the WAV output file.")
    parser.add_argument("--voice", default=settings.kokoro_voice)
    parser.add_argument("--lang-code", default=settings.kokoro_lang_code)
    parser.add_argument("--speed", type=float, default=settings.kokoro_speed)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    configure_tts_runtime_warnings(settings.hf_token)
    try:
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline
    except ImportError as exc:  # pragma: no cover
        raise SystemExit("Missing Kokoro dependencies. Run: uv sync --extra tts") from exc

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    text = input_path.read_text(encoding="utf-8")

    pipeline = KPipeline(lang_code=args.lang_code)
    parts = [
        audio
        for _, _, audio in pipeline(
            text,
            voice=args.voice,
            speed=args.speed,
            split_pattern=r"\n+",
        )
    ]
    if not parts:
        raise SystemExit("Kokoro produced no audio segments.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, np.concatenate(parts), 24000)
    print(output_path)


if __name__ == "__main__":
    main()
