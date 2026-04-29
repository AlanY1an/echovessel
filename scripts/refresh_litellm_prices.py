"""Refresh the vendored LiteLLM model-price JSON + LICENSE.

Run: uv run python scripts/refresh_litellm_prices.py

Pulls the latest from LiteLLM's `main` branch on GitHub. LiteLLM is
MIT-licensed; we keep their LICENSE alongside the JSON for attribution.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

REPO_RAW = "https://raw.githubusercontent.com/BerriAI/litellm/main"
DATA_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "echovessel" / "core" / "llm" / "data"
)

ASSETS: tuple[tuple[str, str], ...] = (
    ("model_prices_and_context_window.json", "litellm_prices.json"),
    ("LICENSE", "LITELLM-LICENSE"),
)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for upstream, local in ASSETS:
        url = f"{REPO_RAW}/{upstream}"
        dest = DATA_DIR / local
        print(f"Fetching {url}")
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
        dest.write_bytes(data)
        print(f"  -> {dest} ({len(data) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
