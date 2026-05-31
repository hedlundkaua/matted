from __future__ import annotations

import hashlib
import os
import re


RESET = "\033[0m"
COLORS = ("31", "32", "33", "34", "35", "36", "91", "92", "93", "94", "95", "96")


def colors_enabled() -> bool:
    raw = os.environ.get("MATTED_COLOR", "1").strip().lower()
    if raw in {"0", "false", "no", "nao", "não", "off"}:
        return False
    return os.environ.get("TERM", "") != "dumb"


def color_for_name(name: str) -> str:
    digest = hashlib.sha256(name.encode("utf-8", errors="replace")).digest()
    return COLORS[digest[0] % len(COLORS)]


def color_text(name: str, text: str) -> str:
    if not colors_enabled():
        return text
    return f"\033[{color_for_name(name)}m{text}{RESET}"


def colorize_bracketed_names(text: str) -> str:
    if not colors_enabled():
        return text

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return color_text(name, f"[{name}]")

    return re.sub(r"\[([A-Za-z0-9_-]+)\]", repl, text)


def strip_leading_bracket_name(text: str) -> str:
    return re.sub(r"^\[([A-Za-z0-9_-]+)\]\s*", "", text)
