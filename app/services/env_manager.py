from pathlib import Path
import os


ENV_EDITABLE_PREFIXES = (
    "APP_",
    "EBAY_",
    "STORAGE_",
    "AWS_",
    "S3_",
    "SPOT_",
    "METALS_",
    "YAHOO_",
    "SYNC_",
    "COMP_",
    "OPENAI_",
    "DB_AUTO_MIGRATE",
)

SENSITIVE_ENV_KEYS = {
    "POSTGRES_PASSWORD",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "EBAY_CLIENT_SECRET",
    "EBAY_USER_ACCESS_TOKEN",
    "OPENAI_API_KEY",
    "METALS_API_KEY",
    "MINIO_ROOT_PASSWORD",
}

NON_EDITABLE_KEYS = {
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
}


def uses_env_file(app_env: str | None = None) -> bool:
    resolved = str(app_env or os.getenv("APP_ENV", "local")).strip().lower()
    return resolved == "local"


def read_process_env_values(
    *, tracked_keys: set[str] | None = None, include_untracked_editable: bool = True
) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in os.environ.items():
        normalized = str(key or "").strip()
        if not normalized:
            continue
        if tracked_keys is not None:
            if normalized in tracked_keys:
                values[normalized] = str(value or "")
                continue
            if include_untracked_editable and (
                is_editable_env_key(normalized)
                or normalized in SENSITIVE_ENV_KEYS
                or normalized in NON_EDITABLE_KEYS
            ):
                values[normalized] = str(value or "")
                continue
        else:
            values[normalized] = str(value or "")
    return values


def _parse_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def read_env_file(path: str = ".env") -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        return {}
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def mask_env_value(key: str, value: str) -> str:
    if key in SENSITIVE_ENV_KEYS:
        if not value:
            return ""
        if len(value) <= 6:
            return "*" * len(value)
        return f"{'*' * (len(value) - 4)}{value[-4:]}"
    return value


def is_editable_env_key(key: str) -> bool:
    if key in NON_EDITABLE_KEYS:
        return False
    return any(key.startswith(prefix) for prefix in ENV_EDITABLE_PREFIXES)


def upsert_env_key(path: str, key: str, value: str) -> None:
    p = Path(path)
    lines = _parse_env_lines(p)
    updated = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        existing_key = line.split("=", 1)[0].strip()
        if existing_key == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        new_lines.append(f"{key}={value}")
    p.write_text("\n".join(new_lines).rstrip("\n") + "\n", encoding="utf-8")


def ensure_env_defaults(path: str, defaults: dict[str, str]) -> list[str]:
    existing = read_env_file(path)
    added: list[str] = []
    for key, default_value in defaults.items():
        if key in existing:
            continue
        upsert_env_key(path, key, default_value)
        added.append(key)
    return added
