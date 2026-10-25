"""Operation-owned files and atomic publication without clobbering caller data."""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

from .errors import SpeechError


def private_job_dir(base: Path) -> Path:
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / uuid.uuid4().hex
    path.mkdir(mode=0o700)
    return path


def atomic_write(path: Path, content: bytes, *, overwrite: bool = False) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise SpeechError("CONFIG", "refusing a symlink output")
    if path.exists() and (not overwrite or not path.is_file()):
        raise SpeechError("CONFIG", "output exists; choose another path or explicit overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None

    def sync_parent():
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    try:
        fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            if path.is_symlink():
                raise SpeechError("CONFIG", "refusing a symlink output")
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        sync_parent()
        return path
    except FileExistsError:
        raise SpeechError("CONFIG", "output already exists") from None
    except OSError:
        raise SpeechError("PREREQUISITE", "cannot publish a durable output in the selected directory") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
                sync_parent()
            except OSError:
                raise SpeechError("PREREQUISITE", "owned output cleanup or directory durability failed") from None

def publish_prepared_file(temporary,target,*,overwrite=False):
    """Publish an owned, fully written sibling file without buffering it in memory."""
    temporary,target=Path(temporary),Path(target)
    if temporary.parent.resolve()!=target.parent.resolve() or not temporary.is_file():
        raise SpeechError("CONFIG","prepared output must be an owned sibling file")
    if target.is_symlink() or target.exists() and (not overwrite or not target.is_file()):
        raise SpeechError("CONFIG","output exists or is a symlink")
    try:
        if overwrite:os.replace(temporary,target)
        else:os.link(temporary,target);temporary.unlink()
        descriptor=os.open(target.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
        try:os.fsync(descriptor)
        finally:os.close(descriptor)
    except OSError:
        raise SpeechError("PREREQUISITE","cannot durably publish the completed output") from None
    return target
