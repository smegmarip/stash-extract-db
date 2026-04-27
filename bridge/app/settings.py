from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    stash_url: str = "http://host.docker.internal:9999"
    stash_api_key: str = ""
    stash_session_cookie: str = ""
    extractor_url: str = "http://extractor-gateway:12000"
    data_dir: str = "/data"
    log_level: str = "INFO"

    class Config:
        env_prefix = ""
        case_sensitive = False


settings = Settings()
