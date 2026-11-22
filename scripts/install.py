#!/usr/bin/env python3
"""Install the skill and register its local MCP server using the supported CLI."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", help="Path to the intended host's Codex executable")
    parser.add_argument("--update", action="store_true", help="Update an existing skill installed under the same name")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "server"))
    from orchestrator import codex_binary
    binary = args.codex or codex_binary()
    server = root / "server/orchestrator.py"
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    target = home / "skills/project-orchestrator"
    if target.exists() and not args.update:
        parser.error("Skill already exists; review it and use --update to replace this skill only")
    existing = subprocess.run([binary, "mcp", "get", "project-orchestrator", "--json"],
                              capture_output=True, text=True)
    if existing.returncode == 0:
        config = json.loads(existing.stdout)
        transport = config.get("transport", {})
        if transport.get("args") != [str(server)] or transport.get("command") != sys.executable:
            parser.error("A different project-orchestrator MCP entry exists; preserve it and resolve manually")
    else:
        # Never treat arbitrary CLI failures as proof that a server entry is absent.
        listing = subprocess.run([binary, "mcp", "list", "--json"], check=True,
                                 capture_output=True, text=True)
        if any(x.get("name") == "project-orchestrator" for x in json.loads(listing.stdout)):
            parser.error("MCP entry exists but could not be inspected; no configuration changed")
        subprocess.run([binary, "mcp", "add", "project-orchestrator", "--env",
                        "CODEX_ORCHESTRATOR_CODEX=" + binary, "--", sys.executable, str(server)], check=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "skills/project-orchestrator", target, dirs_exist_ok=True)
    print("Installed skill:", target)
    print("Registered local MCP server. Keep this source directory in place.")
    print("Start a new chat and call orchestrator_health; registration alone does not prove host tool discovery.")


if __name__ == "__main__":
    main()
