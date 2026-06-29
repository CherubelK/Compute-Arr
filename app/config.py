from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # API
    api_key: str = "changeme"
    debug: bool = False

    # Database
    database_url: str = "postgresql://postgres:postgres@localhost:5432/compute_arr"

    # Poller
    poll_interval_seconds: int = 90
    gpu_classes: list[str] = ["H100", "A100", "A40", "RTX4090", "RTX3090", "RTX3080"]

    # Provider API keys
    runpod_api_key: str = ""
    vast_api_key: str = ""
    lambda_api_key: str = ""
    lambda_ssh_key_names: list[str] = []

    # Routing baseline (used for savings calculation in GET /usage)
    baseline_provider: str = "lambda"


settings = Settings()
