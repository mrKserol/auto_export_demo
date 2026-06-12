from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    database_url: str
    yandex_disk_token: str
    yandex_disk_base_path: str
    yandex_function_url: str | None
    enable_processing: bool


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    raise RuntimeError(f"Environment variable {name} must be true or false")


def load_settings() -> Settings:
    enable_processing = _parse_bool_env("ENABLE_PROCESSING", default=False)
    yandex_function_url = os.getenv("YANDEX_FUNCTION_URL")
    if enable_processing and not yandex_function_url:
        raise RuntimeError("YANDEX_FUNCTION_URL is required when ENABLE_PROCESSING=true")

    return Settings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        database_url=_require_env("DATABASE_URL"),
        yandex_disk_token=_require_env("YANDEX_DISK_TOKEN"),
        yandex_disk_base_path=os.getenv("YANDEX_DISK_BASE_PATH", "/auto_export_demo"),
        yandex_function_url=yandex_function_url,
        enable_processing=enable_processing,
    )
