from functools import lru_cache
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


def find_config_path(start: Path | None = None) -> Path:
    """v45 (3): Config-Pfad aufloesen.

    Wird eine config.yaml im Arbeitsverzeichnis (start, Default: cwd) gefunden,
    hat sie Vorrang vor der config.yaml neben config.py (Modul-Pfad).
    """
    cwd_cfg = (start or Path.cwd()) / "config.yaml"
    if cwd_cfg.exists():
        return cwd_cfg.resolve()
    return CONFIG_PATH


@lru_cache(maxsize=1)
def load_config(path: str | Path | None = None) -> dict:
    # v45 (3): Default-Pfad ueber find_config_path (cwd hat Vorrang)
    config_path = Path(path) if path is not None else find_config_path()
    with config_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def config_path_info() -> str:
    """v45 (3): geladener Config-Pfad (absolut) als Text fuers Logging/CLI."""
    return str(find_config_path())
