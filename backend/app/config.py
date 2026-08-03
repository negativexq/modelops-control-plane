from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODELOPS_")

    app_name: str = "ModelOps Control Plane"
    environment: str = "development"
    database_url: str = "sqlite:///./modelops.db"


settings = Settings()
