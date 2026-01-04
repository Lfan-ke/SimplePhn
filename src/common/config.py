"""
公共配置管理模块
"""
import os
from pathlib import Path
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator
import yaml
from loguru import logger


class LogMode(str, Enum):
    """日志模式"""
    CONSOLE = "console"
    FILE = "file"
    BOTH = "both"


class ModemConfig(BaseModel):
    """调制解调器配置"""
    port_patterns: list[str] = Field(
        default_factory=lambda: [
            "/dev/ttyUSB*",
            "/dev/ttyACM*",
            "/dev/ttyAMA*",
            "/dev/ttyS*",
            "COM*"
        ]
    )
    baudrate: int = Field(default=115200, ge=9600, le=115200)
    timeout: float = Field(default=10.0, ge=1.0, le=60.0)
    pin: Optional[str] = Field(default=None)

    # 管理配置
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_delay: float = Field(default=1.0, ge=0.5, le=5.0)
    connection_timeout: float = Field(default=30.0, ge=5.0, le=120.0)
    lock_timeout: float = Field(default=30.0, ge=5.0, le=120.0)

    @field_validator('port_patterns')
    @classmethod
    def validate_port_patterns(cls, v: list[str]) -> list[str]:
        """根据操作系统调整端口模式"""
        if os.name == 'nt':
            return ["COM*"] if not v else v
        return v


class ConsulConfig(BaseModel):
    """Consul配置"""
    host: str = Field(default="localhost:8500")
    token: str = Field(default="")
    scheme: str = Field(default="http")
    health_check_interval: str = Field(default="10s")
    health_check_timeout: str = Field(default="5s")
    deregister_after: str = Field(default="30s")

    @field_validator('host')
    @classmethod
    def validate_host(cls, v: str) -> str:
        if not v:
            return "localhost:8500"
        return v


class LogConfig(BaseModel):
    """日志配置"""
    mode: LogMode = Field(default=LogMode.CONSOLE)
    level: str = Field(default="INFO")
    encoding: str = Field(default="plain")
    stat: bool = Field(default=False)
    file_path: Optional[str] = Field(default=None)


class ServerConfig(BaseModel):
    """服务器配置"""
    name: str = Field(default="sms.rpc")
    listen_on: str = Field(default="0.0.0.0:50052")
    mode: str = Field(default="dev")
    max_workers: int = Field(default=10, ge=1, le=50)


class AppConfig(BaseModel):
    """应用配置"""
    server: ServerConfig = Field(default_factory=ServerConfig)
    consul: ConsulConfig = Field(default_factory=ConsulConfig)
    modem: ModemConfig = Field(default_factory=ModemConfig)
    log: LogConfig = Field(default_factory=LogConfig)


class ConfigManager:
    """
    配置管理器（单例模式）
    """
    _instance = None
    _config: Optional[AppConfig] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def load(self, config_path: Path) -> bool:
        """
        加载配置文件

        Args:
            config_path: 配置文件路径

        Returns:
            是否加载成功
        """
        try:
            if not config_path.exists():
                logger.error(f"配置文件不存在: {config_path}")
                return False

            with open(config_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)

            self._config = AppConfig(**data)
            logger.info(f"配置加载成功: {config_path}")
            return True

        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return False

    def get(self) -> AppConfig:
        """
        获取配置对象

        Returns:
            配置对象
        """
        if self._config is None:
            raise RuntimeError("配置未加载，请先调用load()方法")
        return self._config

    @property
    def server(self) -> ServerConfig:
        """获取服务器配置"""
        return self.get().server

    @property
    def consul(self) -> ConsulConfig:
        """获取Consul配置"""
        return self.get().consul

    @property
    def modem(self) -> ModemConfig:
        """获取调制解调器配置"""
        return self.get().modem

    @property
    def log(self) -> LogConfig:
        """获取日志配置"""
        return self.get().log
