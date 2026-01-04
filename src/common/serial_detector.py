"""
通用串口检测器 - 更新版
"""
import asyncio
import glob
import re
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ConfigManager
from src.sms_service.sms_sender import SMSSender


@dataclass
class ModemInfo:
    """调制解调器信息"""
    port: str
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    imei: str = ""
    signal_strength: str = "0"
    is_connected: bool = False


class SerialDetector:
    """串口检测器"""

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager
        self.detected_modems: list[ModemInfo] = []

    async def detect_modems(self, force_refresh: bool = False) -> list[ModemInfo]:
        """
        检测可用的调制解调器

        Args:
            force_refresh: 是否强制重新检测

        Returns:
            检测到的调制解调器列表
        """
        if not force_refresh and self.detected_modems:
            return self.detected_modems

        self.detected_modems.clear()

        # 获取串口配置
        try:
            serial_config = self.config_manager.serial_config
            port_patterns = serial_config.port_patterns
            baudrate = serial_config.baudrate
        except RuntimeError:
            logger.warning("配置未加载，使用默认配置")
            port_patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "COM*"] if os.name == 'nt' else ["/dev/ttyUSB*", "/dev/ttyACM*"]
            baudrate = 115200

        # 展开所有glob模式
        all_ports = []
        for pattern in port_patterns:
            try:
                matched_ports = glob.glob(pattern)
                all_ports.extend(matched_ports)
            except Exception as e:
                logger.warning(f"Glob模式 {pattern} 错误: {e}")

        # 去重并排序
        all_ports = sorted(set(all_ports))

        if not all_ports:
            logger.warning(f"未找到匹配的串口: {port_patterns}")
            return []

        logger.debug(f"找到串口: {all_ports}")

        # 并发检测所有端口
        tasks = [self._test_port(port, baudrate) for port in all_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效的调制解调器
        for result in results:
            if isinstance(result, ModemInfo) and result.is_connected:
                self.detected_modems.append(result)

        logger.info(f"检测到 {len(self.detected_modems)} 个调制解调器")
        for modem in self.detected_modems:
            logger.info(f"  - {modem.port}: {modem.manufacturer} {modem.model} (信号: {modem.signal_strength})")

        return self.detected_modems

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=0.5, max=2)
    )
    async def _test_port(self, port: str, baudrate: int) -> Optional[ModemInfo]:
        """测试指定端口是否连接了GSM调制解调器"""
        try:
            # 检查端口是否存在
            if not Path(port).exists():
                return None

            # 创建SMSSender实例
            sender = SMSSender(port=port, baudrate=baudrate)

            # 尝试连接
            connected = await sender.connect()

            if not connected:
                sender.disconnect()
                return None

            # 获取调制解调器信息
            info_dict = await sender.get_modem_info()

            # 创建ModemInfo对象
            modem_info = ModemInfo(
                port=port,
                manufacturer=info_dict.get("manufacturer", "Unknown"),
                model=info_dict.get("model", "Unknown"),
                imei=info_dict.get("imei", ""),
                signal_strength=info_dict.get("signal_strength", "0"),
                is_connected=info_dict.get("is_connected", False)
            )

            # 断开连接
            await sender.disconnect()

            if modem_info.is_connected:
                return modem_info

            return None

        except Exception as e:
            logger.debug(f"检测端口 {port} 失败: {e}")
            return None

    def get_best_modem(self) -> Optional[ModemInfo]:
        """获取最佳的调制解调器（信号最强）"""
        if not self.detected_modems:
            return None

        # 按信号强度排序
        sorted_modems = sorted(
            self.detected_modems,
            key=lambda m: int(m.signal_strength) if m.signal_strength.isdigit() else 0,
            reverse=True
        )

        return sorted_modems[0]

    def get_modem_by_port(self, port: str) -> Optional[ModemInfo]:
        """根据端口获取调制解调器"""
        for modem in self.detected_modems:
            if modem.port == port:
                return modem
        return None
