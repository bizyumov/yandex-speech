"""Operation-owned files and atomic publication without clobbering caller data."""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import shutil
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

def _discard_staging(base, record_path, record):
    directory = base / record_path.stem
    try:
        if directory.is_symlink():
            raise SpeechError("STATE", "staging path was replaced")
        if directory.exists():
            identity = directory.stat()
            if (identity.st_dev, identity.st_ino) != (record["device"], record["inode"]):
                raise SpeechError("STATE", "staging ownership changed")
            shutil.rmtree(directory)
        record_path.unlink(missing_ok=True)
        record_path.with_suffix(".lock").unlink(missing_ok=True)
        return True
    except Exception as error:
        record.update(status="cleanup_pending", reason=type(error).__name__)
        try:
            atomic_write(record_path, json.dumps(record).encode(), overwrite=True)
        except OSError:
            pass
        logging.getLogger(__name__).warning(
            "TTS staging cleanup pending; retry receipt %s (%s)", record_path, type(error).__name__)
        return False


def cleanup_staging(base):
    """Retry only registered, inactive TTS stages; never sweep cache prefixes."""
    base = Path(base)
    pending = []
    for path in sorted(base.glob("*.json")):
        fd = None
        try:
            if path.is_symlink() or not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                continue
            record = json.loads(path.read_text())
            if record.get("kind") != "yandex-speech-tts-stage" or record.get("retain_on_error"):
                continue
            fd = os.open(path.with_suffix(".lock"), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not _discard_staging(base, path, record):
                pending.append(str(path))
        except (BlockingIOError, FileNotFoundError):
            continue
        except (OSError, ValueError, KeyError, TypeError):
            pending.append(str(path))
        finally:
            if fd is not None:
                os.close(fd)
    return pending


@contextlib.contextmanager
def staging_directory(base, *, retain_on_error=False):
    """Register before producing audio; a process crash releases its cleanup lease."""
    base = Path(base)
    cleanup_staging(base)
    directory = private_job_dir(base)
    path = base / (directory.name + ".json")
    lock = path.with_suffix(".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    identity = directory.stat()
    record = {"kind": "yandex-speech-tts-stage", "status": "active",
              "device": identity.st_dev, "inode": identity.st_ino,
              "retain_on_error": retain_on_error}
    failed = False
    try:
        atomic_write(path, json.dumps(record).encode())
        try:
            yield directory
        except BaseException:
            failed = True
            raise
    finally:
        try:
            if failed and retain_on_error:
                record["status"] = "retained"
                atomic_write(path, json.dumps(record).encode(), overwrite=True)
            else:
                _discard_staging(base, path, record)
        finally:
            os.close(fd)


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
