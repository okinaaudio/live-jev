#!/opt/homebrew/bin/python3.13
"""Provide one-shot and interactive modes for Live Jev."""

from __future__ import annotations

import argparse
import json

from daemon import LiveJevService


VERSION = "0.08"


def _print(response: dict[str, object]) -> None:
    line = str(response.get("line", ""))
    timing = response.get("ms")
    if isinstance(timing, dict) and "total" in timing:
        line += f"  {timing['total']}ms"
    print(line)
    decision = response.get("decision")
    if response.get("kind") == "result" and isinstance(decision, dict):
        labels = [decision.get("action_label"), decision.get("track"), decision.get("param"), decision.get("step_label")]
        detail = " / ".join(str(item) for item in labels if item)
        conf = decision.get("conf")
        if isinstance(conf, dict):
            scores = [value for value in (conf.get("action"), conf.get("track"), conf.get("param")) if isinstance(value, (int, float))]
            if scores:
                detail += "（" + "・".join(f"{value:.2f}" for value in scores) + "）"
        rewritten = decision.get("rewritten")
        if isinstance(rewritten, list) and rewritten:
            detail += "  言い換え: " + " / ".join(str(item) for item in rewritten)
        if isinstance(rewritten, list) and rewritten and isinstance(timing, dict) and isinstance(timing.get("llm"), (int, float)):
            detail += f"  +LLM {timing['llm'] / 1000:.1f}s"
        print("Jev: " + detail)
    if response.get("kind") == "ask" and isinstance(response.get("options"), list):
        print(" / ".join(str(item) for item in response["options"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="日本語の一言で Ableton Live を操作します")
    parser.add_argument("--version", action="version", version=f"Live Jev {VERSION}")
    parser.add_argument("text", nargs="*")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    service = LiveJevService(verbose=args.verbose)
    startup = service.start()
    if args.text:
        text = " ".join(args.text)
        if text in {"status", "refresh"}:
            response = service.process({"cmd": text})
        else:
            response = service.process({"text": text})
        _print(response)
        return 0 if response.get("kind") != "error" else 1
    _print(startup)
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "quit":
            return 0
        message = {"cmd": text} if text in {"status", "refresh"} else {"text": text}
        _print(service.process(message))


if __name__ == "__main__":
    raise SystemExit(main())
