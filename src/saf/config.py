from pathlib import Path
from pprint import pprint

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | Path = "configs/project.yaml") -> dict:
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path

    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError("The configuration must be a YAML mapping.")

    for section in ("data", "paths", "session"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"Missing or invalid section: {section}")

    for name, value in config["paths"].items():
        directory = Path(value)
        if not directory.is_absolute():
            directory = PROJECT_ROOT / directory
        config["paths"][name] = directory.resolve()

    manifest = config["data"].get("date_manifest")
    if manifest is not None:
        manifest_path = Path(manifest)
        if not manifest_path.is_absolute():
            manifest_path = PROJECT_ROOT / manifest_path
        config["data"]["date_manifest"] = manifest_path.resolve()

    return config


if __name__ == "__main__":
    pprint(load_config())