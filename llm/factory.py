import os
from pathlib import Path

from .codex import CodexProvider
from .openrouter import OpenRouterProvider


def describe_provider(provider: any) -> str:
    name = provider.__class__.__name__
    parts = [name]

    model = getattr(provider, "model", None)
    if model:
        parts.append(f"model={model}")

    agent = getattr(provider, "agent", None)
    if agent:
        parts.append(f"agent={agent}")

    mode_fn = getattr(provider, "_mode", None)
    if callable(mode_fn):
        parts.append(f"mode={mode_fn()}")

    server_url = getattr(provider, "_server_url", None)
    if server_url:
        parts.append(f"url={server_url}")

    root_dir = getattr(provider, "root_dir", None)
    if root_dir:
        parts.append(f"root={root_dir}")

    return " ".join(parts)


class ProviderFactory:
    """
    Factory to instantiate the correct LLM provider.
    """

    @staticmethod
    def _load_env_file(env_path: Path) -> None:
        if not env_path.is_file():
            return
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ[key] = value

    @staticmethod
    def get_provider(root_dir: str = ".") -> any:
        # 1. Load Global Config (Matted Home)
        matted_home = Path(__file__).parent.parent
        ProviderFactory._load_env_file(matted_home / ".env")

        # 2. Load Project Config (Root Dir) - Project wins over Global
        ProviderFactory._load_env_file(Path(root_dir) / ".env")

        active = os.environ.get("ACTIVE_LLM_PROVIDER", "codex").strip().lower()

        # Aliases keep provider switching ergonomic from `.env`.
        aliases = {
            "codex": "codex",
            "openrouter": "openrouter",
            "openclaude": "openrouter",
        }
        resolved = aliases.get(active, active)

        providers = {
            "codex": lambda: CodexProvider(root_dir=root_dir),
            "openrouter": lambda: OpenRouterProvider(),
        }
        builder = providers.get(resolved)
        if builder:
            return builder()

        raise ValueError(
            f"ACTIVE_LLM_PROVIDER invalido: '{active}'. "
            "Use 'codex', 'openrouter' ou 'openclaude'."
        )
