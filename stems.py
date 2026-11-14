"""Splits a song into stems with audio-separator.

Run as a script so the heavy ML libraries stay out of the bot's own process:

    python stems.py <input audio> <output dir> <mode> [fast|best]

Modes: vocals, stems4, stems6, drumkit (see MODES).

Prints the finished files as a final line starting with "RESULT ", a JSON object
mapping stem name -> file path. Everything else printed is progress/log output.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MODELS_DIR = Path(os.getenv("STEM_MODELS_DIR", Path(__file__).parent / "models"))

# Model choices, overridable from the environment. Quality notes come from the
# SDR scores audio-separator publishes (higher is better):
#   Mel-Band RoFormer  12.6 dB vocals - best available, but ~2x slower than real time on a fast CPU
#   htdemucs_ft        10.8 dB vocals - a bag of four models, about four times the work of htdemucs
#   htdemucs           the fast default, roughly half real time on a fast CPU
VOCAL_MODEL_BEST = os.getenv("STEM_VOCAL_MODEL", "vocals_mel_band_roformer.ckpt")
DEMUCS_FAST = os.getenv("STEM_DEMUCS_MODEL", "htdemucs.yaml")
DEMUCS_BEST = os.getenv("STEM_DEMUCS_BEST_MODEL", "htdemucs_ft.yaml")
DEMUCS_6S = "htdemucs_6s.yaml"
DRUM_MODEL = "MDX23C-DrumSep-aufr33-jarredou.ckpt"

# mode -> what it produces
MODES = {
    "vocals": "Vocals + instrumental",
    "stems4": "4 stems: vocals, drums, bass, other",
    "stems6": "6 stems: vocals, drums, bass, guitar, piano, other",
    "drumkit": "Vocals, bass, other, and the drums split into kick, snare, toms, hi-hat, ride, crash",
}
QUALITIES = ("fast", "best")

STEM_NAME = re.compile(r"\(([^)]+)\)")


def log(message: str) -> None:
    print(message, flush=True)


def run_model(model: str, source: Path, out_dir: Path, rename: dict[str, str] | None = None) -> dict[str, Path]:
    """Run one model and return {stem name: file}, with stems renamed to plain lowercase names."""
    from audio_separator.separator import Separator

    out_dir.mkdir(parents=True, exist_ok=True)
    separator = Separator(
        output_dir=str(out_dir),
        model_file_dir=str(MODELS_DIR),
        output_format="FLAC",
        # one pass instead of two: half the time, a small quality cost
        demucs_params={"segment_size": "Default", "shifts": 1, "overlap": 0.25, "segments_enabled": True},
        log_level=30,
    )
    log(f"Loading {model}")
    separator.load_model(model_filename=model)
    log(f"Separating with {model}")
    started = time.monotonic()
    produced = separator.separate(str(source))
    log(f"Separated in {time.monotonic() - started:.1f}s with {model}")

    stems = {}
    for produced_file in produced:
        path = Path(produced_file)
        if not path.is_absolute():
            path = out_dir / path.name
        match = STEM_NAME.search(path.name)
        stem = match.group(1).lower() if match else path.stem
        stem = (rename or {}).get(stem, stem)
        target = path.with_name(f"{stem}{path.suffix}")
        if target != path:
            if target.exists():
                target.unlink()
            path.rename(target)
        stems[stem] = target
    return stems


def mix(parts: list[Path], destination: Path) -> Path:
    """Sum several stems into one file (used to rebuild an instrumental from drums + bass + other)."""
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    for part in parts:
        command += ["-i", str(part)]
    command += ["-filter_complex", f"amix=inputs={len(parts)}:normalize=0:duration=longest", str(destination)]
    subprocess.run(command, check=True)
    return destination


def split(source: Path, out_dir: Path, mode: str, quality: str) -> dict[str, Path]:
    work = out_dir / "work"
    demucs = DEMUCS_BEST if quality == "best" else DEMUCS_FAST

    if mode == "vocals":
        if quality == "best":
            return run_model(VOCAL_MODEL_BEST, source, work, {"other": "instrumental"})
        # fast: separate with Demucs, then put everything that isn't vocals back together
        stems = run_model(demucs, source, work)
        instrumental = mix([stems.pop(name) for name in ("drums", "bass", "other")], work / "instrumental.flac")
        return {"vocals": stems["vocals"], "instrumental": instrumental}

    if mode == "stems4":
        return run_model(demucs, source, work)

    if mode == "stems6":
        return run_model(DEMUCS_6S, source, work)

    if mode == "drumkit":
        stems = run_model(demucs, source, work / "main")
        drums = stems.pop("drums")
        pieces = run_model(DRUM_MODEL, drums, work / "drums")
        drums.unlink()
        stems.update(pieces)
        return stems

    raise ValueError(f"unknown mode {mode!r}")


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        return 2

    source, out_dir, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    quality = sys.argv[4] if len(sys.argv) == 5 else "fast"
    if quality not in QUALITIES:
        print(f"quality must be one of {QUALITIES}")
        return 2

    stems = split(source, out_dir, mode, quality)

    final_dir = out_dir / "stems"
    final_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, path in stems.items():
        destination = final_dir / f"{name}{path.suffix}"
        shutil.move(str(path), destination)
        result[name] = str(destination)
    log("RESULT " + json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
