#!/usr/bin/env python3
"""Portable readiness helper; never provisions credentials implicitly."""
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yandex_speech.readiness import main

if __name__ == "__main__":
    raise SystemExit(main())
