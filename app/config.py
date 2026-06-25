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
    yandex_api_key: str
    yandex_cloud_folder_id: str
    ocr_min_delay_seconds: float
    max_ocr_retries: int


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


def _parse_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    return float(value)


def _parse_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


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
        yandex_api_key=_require_env("YANDEX_API_KEY"),
        yandex_cloud_folder_id=_require_env("YANDEX_CLOUD_FOLDER_ID"),
        ocr_min_delay_seconds=_parse_float_env("OCR_MIN_DELAY_SECONDS", 1.5),
        max_ocr_retries=_parse_int_env("MAX_OCR_RETRIES", 5),
    )
