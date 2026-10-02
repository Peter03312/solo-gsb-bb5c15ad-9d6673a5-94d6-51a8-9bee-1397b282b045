"""应用配置：全部通过环境变量注入，方便容器化与离线部署。"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "caltrace"
    db_user: str = "caltrace"
    db_password: str = "caltrace"
    # 连接池大小
    db_min_size: int = 2
    db_max_size: int = 10
    # 启动时等待数据库就绪的最长秒数（compose 已配 healthcheck，这里再兜底）
    db_connect_timeout: int = 60

    app_host: str = "0.0.0.0"
    app_port: int = 8080


settings = Settings()
