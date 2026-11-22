#!/usr/bin/env python3
"""Build and inspect a source plugin ZIP without local state or credentials."""
import argparse
import json
from pathlib import Path
import sys
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    manifest = json.loads((root / "plugin.json").read_text())
    compat = json.loads((root / ".codex-plugin/plugin.json").read_text())
    if (manifest["name"], manifest["version"]) != (compat["name"], compat["version"]):
        raise SystemExit("Manifest identity/version mismatch")
    sys.path.insert(0, str(root / "server"))
    from workflow import VERSION
    if VERSION != manifest["version"]:
        raise SystemExit("Runtime and manifest version mismatch")
    base = ["plugin.json", "mcp.json", ".codex-plugin/plugin.json", "README.md",
            ".gitignore", "THIRD_PARTY_NOTICES.md"]
    for directory in ("skills", "server", "scripts", "tests", "licenses", "docs"):
        base += [str(p.relative_to(root)) for p in (root / directory).rglob("*")
                 if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    for name in base:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SystemExit("Unexpected symbolic link or outside file: " + name)
    output = args.out or root.parent / f"{manifest['name']}-{VERSION}.zip"
    output = output.resolve()
    if output.is_relative_to(root):
        raise SystemExit("Write archives outside the plugin directory")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(set(base)):
            archive.write(root / name, manifest["name"] + "/" + name)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise SystemExit("Archive CRC verification failed")
        names = archive.namelist()
        for required in ("THIRD_PARTY_NOTICES.md", "licenses/session-handoff-MIT.txt",
                         "licenses/codex-app-server-mcp-MIT.txt", "server/vendor/session_handoff_state.py"):
            if manifest["name"] + "/" + required not in names:
                raise SystemExit("Missing required upstream component: " + required)
        if any("/.git/" in n or "/work/" in n or "__pycache__" in n for n in names):
            raise SystemExit("Local working data leaked into package")
    print(json.dumps({"archive": str(output), "version": VERSION, "files": len(names)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
