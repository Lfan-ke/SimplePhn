"""
通用配置管理
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


class SerialConfig(BaseModel):
    """串口配置"""
    port_patterns: list[str] = Field(
        default_factory=lambda: [
            "/dev/ttyUSB*",
            "/dev/ttyACM*",
            "/dev/ttyAMA*",
            "/dev/ttyS*",
            "COM*"  # Windows串口
        ]
    )
    baudrate: int = Field(default=9600, ge=9600, le=115200)
    timeout: float = Field(default=2.0, ge=0.1, le=10.0)
    write_timeout: float = Field(default=2.0, ge=0.1, le=10.0)
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_delay: float = Field(default=1.0, ge=0.1, le=5.0)


class ConsulConfig(BaseModel):
    """Consul配置"""
    host: str = Field(default="127.0.0.1:8500")
    token: str = Field(default="")
    scheme: str = Field(default="http")
    health_check_interval: str = Field(default="10s")
    health_check_timeout: str = Field(default="5s")
    deregister_after: str = Field(default="30s")

    @field_validator('host')
    @classmethod
    def validate_host(cls, v: str) -> str:
        """验证Consul主机地址"""
        if not v:
            return "127.0.0.1:8500"
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


class Config(BaseModel):
    """主配置"""
    server: ServerConfig = ServerConfig()
    consul: ConsulConfig = ConsulConfig()
    serial: SerialConfig = SerialConfig()
    log: LogConfig = LogConfig()


class ConfigManager:
    """
    配置管理器 - 单例模式
    """
    _instance = None

    def __new__(cls):
        """实现单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config: Optional[Config] = None
        return cls._instance

    def __init__(self):
        """初始化配置管理器"""
        pass

    async def load_config(self, config_path: Path) -> bool:
        """
        从YAML文件加载配置

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

            # 根据系统平台调整串口配置
            if os.name == 'nt':  # Windows
                if 'serial' in data:
                    if 'port_patterns' not in data['serial']:
                        data['serial']['port_patterns'] = ["COM*"]

            self._config = Config(**data)
            logger.info(f"配置加载成功: {config_path}")
            return True

        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return False

    def get_config(self) -> Config:
        """
        获取配置

        Returns:
            配置对象
        """
        if self._config is None:
            raise RuntimeError("配置未加载，请先调用load_config()")
        return self._config

    @property
    def server_config(self) -> ServerConfig:
        """获取服务器配置"""
        return self.get_config().server

    @property
    def consul_config(self) -> ConsulConfig:
        """获取Consul配置"""
        return self.get_config().consul

    @property
    def serial_config(self) -> SerialConfig:
        """获取串口配置"""
        return self.get_config().serial

    @property
    def log_config(self) -> LogConfig:
        """获取日志配置"""
        return self.get_config().log
