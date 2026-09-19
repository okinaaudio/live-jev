#!/usr/bin/env python3.13
"""List installed Mac plug-ins (VST3, AU, and VST) in plugins.json without modifying them."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
ROOTS = {
    "vst3": [Path("/Library/Audio/Plug-Ins/VST3"), HOME / "Library/Audio/Plug-Ins/VST3"],
    "au": [Path("/Library/Audio/Plug-Ins/Components"), HOME / "Library/Audio/Plug-Ins/Components"],
    "vst": [Path("/Library/Audio/Plug-Ins/VST"), HOME / "Library/Audio/Plug-Ins/VST"],
}
SUFFIX = {"vst3": ".vst3", "au": ".component", "vst": ".vst"}
OUTPUT = Path(__file__).resolve().parent.parent / "plugins.json"
NOISE = re.compile(r"\s*(AU64|AUHook|VST3|VST|x64|64)$", re.IGNORECASE)


def _bundles(root: Path, suffix: str) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.suffix.lower() == suffix:
            found.append(entry)
        elif entry.is_dir():
            found.extend(child for child in sorted(entry.iterdir()) if child.suffix.lower() == suffix)
    return found


def clean_name(stem: str) -> str:
    return NOISE.sub("", stem).strip()


def scan() -> dict[str, object]:
    catalog: dict[str, dict[str, object]] = {}
    for kind, roots in ROOTS.items():
        for root in roots:
            for bundle in _bundles(root, SUFFIX[kind]):
                name = clean_name(bundle.stem)
                if not name:
                    continue
                entry = catalog.setdefault(name, {"name": name, "formats": [], "paths": []})
                if kind not in entry["formats"]:
                    entry["formats"].append(kind)
                entry["paths"].append(str(bundle))
    plugins = sorted(catalog.values(), key=lambda item: str(item["name"]).casefold())
    return {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(plugins), "plugins": plugins}


def main() -> int:
    data = scan()
    OUTPUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{data['count']} 件を {OUTPUT} に書きました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
