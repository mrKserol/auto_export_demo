from dataclasses import dataclass, field
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
    mini_app_base_url: str
    mini_app_token_secret: str
    web_host: str
    web_port: int
    mini_app_token_ttl_seconds: int
    telegram_init_data_max_age_seconds: int
    customer_upload_max_file_bytes: int = 20 * 1024 * 1024
    telegram_bot_username: str | None = None
    miniapp_test_mode: bool = False
    miniapp_allowed_telegram_user_ids: frozenset[int] = field(default_factory=frozenset)
    app_commit_sha: str | None = None


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


def parse_miniapp_allowed_telegram_user_ids(raw: str | None) -> frozenset[int]:
    """Parse comma-separated Telegram user IDs. Empty → empty set (deny all)."""
    if raw is None or not str(raw).strip():
        return frozenset()

    allowed: set[int] = set()
    for part in str(raw).split(","):
        token = part.strip()
        if not token:
            continue
        try:
            user_id = int(token)
        except ValueError as error:
            raise RuntimeError(
                "Environment variable MINIAPP_ALLOWED_TELEGRAM_USER_IDS "
                f"contains invalid telegram user id: {token!r}"
            ) from error
        if user_id <= 0:
            raise RuntimeError(
                "Environment variable MINIAPP_ALLOWED_TELEGRAM_USER_IDS "
                f"contains invalid telegram user id: {token!r}"
            )
        allowed.add(user_id)
    return frozenset(allowed)


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _resolve_web_port() -> int:
    # Railway and many platforms provide PORT; prefer that first, then WEB_PORT for local overrides.
    for name in ("PORT", "WEB_PORT"):
        value = os.getenv(name)
        if value is not None and value.strip():
            return int(value)
    return 8000


def resolve_app_commit_sha() -> str:
    for name in ("RAILWAY_GIT_COMMIT_SHA", "GIT_COMMIT_SHA"):
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return "unknown"


def load_settings() -> Settings:
    enable_processing = _parse_bool_env("ENABLE_PROCESSING", default=False)
    yandex_function_url = os.getenv("YANDEX_FUNCTION_URL")
    if enable_processing and not yandex_function_url:
        raise RuntimeError("YANDEX_FUNCTION_URL is required when ENABLE_PROCESSING=true")

    telegram_bot_token = _require_env("TELEGRAM_BOT_TOKEN")
    mini_app_token_secret = _require_env("MINI_APP_TOKEN_SECRET")
    if mini_app_token_secret == telegram_bot_token:
        raise RuntimeError(
            "MINI_APP_TOKEN_SECRET must not be the same as TELEGRAM_BOT_TOKEN"
        )

    return Settings(
        telegram_bot_token=telegram_bot_token,
        database_url=_require_env("DATABASE_URL"),
        yandex_disk_token=_require_env("YANDEX_DISK_TOKEN"),
        yandex_disk_base_path=os.getenv("YANDEX_DISK_BASE_PATH", "/auto_export_demo"),
        yandex_function_url=yandex_function_url,
        enable_processing=enable_processing,
        yandex_api_key=_require_env("YANDEX_API_KEY"),
        yandex_cloud_folder_id=_require_env("YANDEX_CLOUD_FOLDER_ID"),
        ocr_min_delay_seconds=_parse_float_env("OCR_MIN_DELAY_SECONDS", 1.5),
        max_ocr_retries=_parse_int_env("MAX_OCR_RETRIES", 5),
        mini_app_base_url=_normalize_base_url(_require_env("MINI_APP_BASE_URL")),
        mini_app_token_secret=mini_app_token_secret,
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=_resolve_web_port(),
        mini_app_token_ttl_seconds=_parse_int_env("MINI_APP_TOKEN_TTL_SECONDS", 900),
        telegram_init_data_max_age_seconds=_parse_int_env(
            "TELEGRAM_INIT_DATA_MAX_AGE_SECONDS",
            900,
        ),
        customer_upload_max_file_bytes=_parse_int_env(
            "CUSTOMER_UPLOAD_MAX_FILE_BYTES",
            20 * 1024 * 1024,
        ),
        telegram_bot_username=(
            (os.getenv("TELEGRAM_BOT_USERNAME") or "").strip().lstrip("@") or None
        ),
        miniapp_test_mode=_parse_bool_env("MINIAPP_TEST_MODE", default=False),
        miniapp_allowed_telegram_user_ids=parse_miniapp_allowed_telegram_user_ids(
            os.getenv("MINIAPP_ALLOWED_TELEGRAM_USER_IDS")
        ),
        app_commit_sha=resolve_app_commit_sha(),
    )
