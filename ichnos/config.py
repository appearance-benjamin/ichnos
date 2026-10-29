"""Settings from environment variables, or from a .env file in the project root."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    openalex_api_key: str | None
    s2_api_key: str | None
    contact_email: str | None
    db_path: Path
    ncbi_api_key: str | None = None
    grobid_url: str | None = None
    reviewer: str | None = None
    library_proxy: str | None = None     # e.g. https://ezproxy.example.edu/login?url= ; a link through the library
    downloads_dir: Path | None = None    # where the browser saves PDFs; add-pdfs looks there too
    core_api_key: str | None = None      # CORE (repository copies); without it CORE allows about 10 requests a minute
    llm_endpoint: str | None = None      # agent-run: an OpenAI-compatible /v1 address (Ollama, LM Studio, vLLM)
    llm_model: str | None = None         # agent-run: the model's name on that server
    llm_api_key: str | None = None       # agent-run: only for a server started with a key (vLLM --api-key)


def load_settings(root: Path = ROOT) -> Settings:
    env = _read_dotenv(root / ".env")
    get = lambda key: os.environ.get(key) or env.get(key) or None  # noqa: E731
    downloads = get("DOWNLOADS_DIR")
    if downloads is None:
        home = Path.home() / "Downloads"
        downloads_dir = home if home.is_dir() else None
    else:
        downloads_dir = None if downloads.strip().lower() in ("off", "none", "no", "") else Path(downloads)
    return Settings(
        openalex_api_key=get("OPENALEX_API_KEY"),
        s2_api_key=get("S2_API_KEY"),
        contact_email=get("CONTACT_EMAIL"),
        db_path=Path(get("EVIDENCE_DB") or root / "data" / "evidence.sqlite"),
        ncbi_api_key=get("NCBI_API_KEY"),
        grobid_url=get("GROBID_URL"),
        reviewer=get("REVIEWER"),
        library_proxy=get("LIBRARY_PROXY"),
        downloads_dir=downloads_dir,
        core_api_key=get("CORE_API_KEY"),
        llm_endpoint=get("LLM_ENDPOINT"),
        llm_model=get("LLM_MODEL"),
        llm_api_key=get("LLM_API_KEY"),
    )


def load_protocol(corpus: str, root: Path = ROOT) -> tuple[Path, str, dict]:
    path = root / "protocol" / f"corpus_{corpus.lower()}.toml"
    text = path.read_text(encoding="utf-8")
    return path, text, tomllib.loads(text)


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values
