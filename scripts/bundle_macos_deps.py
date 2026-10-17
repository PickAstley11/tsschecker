#!/usr/bin/env python3
"""Bundle non-system dylibs beside the macOS tsschecker executable."""
from __future__ import annotations
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(sys.argv[1]).resolve()
EXECUTABLE = ROOT / "tsschecker"
LIB_DIR = ROOT / "lib"
SYSTEM_PREFIXES = ("/System/Library/", "/usr/lib/")

def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)

def dependencies(binary: Path) -> list[str]:
    found = []
    for line in run("otool", "-L", str(binary)).splitlines()[1:]:
        match = re.match(r"\s*(\S+)\s+\(compatibility version", line)
        if match:
            found.append(match.group(1))
    return found

def expand_rpath(value: str, binary: Path) -> Path:
    value = value.replace("@loader_path", str(binary.parent))
    value = value.replace("@executable_path", str(ROOT))
    if value.startswith("@rpath/"):
        raise RuntimeError(f"Nested @rpath entry is unsupported: {value}")
    return Path(value)

def rpaths(binary: Path) -> list[Path]:
    lines = run("otool", "-l", str(binary)).splitlines()
    result = []
    for index, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            for candidate in lines[index + 1:index + 5]:
                match = re.match(r"\s*path (.+?) \(offset \d+\)", candidate)
                if match:
                    result.append(expand_rpath(match.group(1), binary))
                    break
    return result

def resolve(name: str, owner: Path) -> Optional[Path]:
    if name.startswith(SYSTEM_PREFIXES):
        return None
    candidates = []
    if name.startswith("@loader_path/"):
        candidates.append(owner.parent / name.removeprefix("@loader_path/"))
    elif name.startswith("@executable_path/"):
        candidates.append(ROOT / name.removeprefix("@executable_path/"))
    elif name.startswith("@rpath/"):
        suffix = name.removeprefix("@rpath/")
        candidates.extend(path / suffix for path in rpaths(owner))
    elif name.startswith("/"):
        candidates.append(Path(name))
    else:
        raise RuntimeError(f"Unexpected non-system dylib name: {name}")
    for candidate in candidates:
        if candidate.exists():
            resolved = candidate.resolve()
            if str(resolved).startswith(SYSTEM_PREFIXES):
                return None
            return resolved
    raise RuntimeError(f"Cannot resolve {name}, referenced by {owner}")

def bundled_name(source: Path) -> str:
    destination = LIB_DIR / source.name
    if destination.exists() and destination.resolve() != source.resolve():
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:10]
        return f"{source.stem}-{digest}{source.suffix}"
    return source.name

def main() -> None:
    if not EXECUTABLE.is_file():
        raise SystemExit(f"Missing executable: {EXECUTABLE}")
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    queue = [EXECUTABLE]
    visited = set()
    copied = {}
    while queue:
        owner = queue.pop(0)
        if owner in visited:
            continue
        visited.add(owner)
        for name in dependencies(owner):
            source = resolve(name, owner)
            if source is None:
                continue
            destination = copied.get(source)
            if destination is None:
                destination = LIB_DIR / bundled_name(source)
                if not destination.exists():
                    shutil.copy2(source, destination)
                    destination.chmod(destination.stat().st_mode | 0o200)
                    run("install_name_tool", "-id", f"@loader_path/{destination.name}", str(destination))
                copied[source] = destination
                queue.append(destination)
            relative = (f"@loader_path/lib/{destination.name}" if owner == EXECUTABLE
                        else f"@loader_path/{destination.name}")
            if name != relative:
                run("install_name_tool", "-change", name, relative, str(owner))
    files = [EXECUTABLE, *sorted(LIB_DIR.glob("*.dylib"))]
    for binary in files:
        for name in dependencies(binary):
            if name.startswith(("/opt/homebrew/", "/usr/local/Cellar/", "/usr/local/opt/")):
                raise RuntimeError(f"Unbundled Homebrew dependency {name} in {binary}")
    for binary in [*sorted(LIB_DIR.glob("*.dylib")), EXECUTABLE]:
        run("codesign", "--force", "--sign", "-", str(binary))
    run(str(EXECUTABLE), "--help")
    print(f"Bundled {len(copied)} non-system dylibs; --help smoke check passed.")

if __name__ == "__main__":
    main()
