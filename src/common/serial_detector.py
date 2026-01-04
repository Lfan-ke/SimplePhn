"""
通用串口检测器
"""
import asyncio
import glob
import re
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import serial
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import ConfigManager


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
            timeout = serial_config.timeout
        except RuntimeError:
            logger.warning("配置未加载，使用默认配置")
            port_patterns = ["/dev/ttyUSB*", "/dev/ttyACM*", "COM*"] if os.name == 'nt' else ["/dev/ttyUSB*", "/dev/ttyACM*"]
            baudrate = 9600
            timeout = 2.0

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
        tasks = [self._test_port(port, baudrate, timeout) for port in all_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效的调制解调器
        for result in results:
            if isinstance(result, ModemInfo) and result.is_connected:
                self.detected_modems.append(result)

        logger.info(f"检测到 {len(self.detected_modems)} 个调制解调器")
        for modem in self.detected_modems:
            logger.info(f"  - {modem.port}: {modem.manufacturer} {modem.model} (IMEI: {modem.imei})")

        return self.detected_modems

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=0.5, max=2)
    )
    async def _test_port(self, port: str, baudrate: int, timeout: float) -> Optional[ModemInfo]:
        """测试指定端口是否连接了GSM调制解调器"""
        try:
            # 检查端口是否存在
            if not Path(port).exists():
                return None

            # 尝试打开串口
            ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=timeout,
                write_timeout=timeout
            )

            await asyncio.sleep(0.5)

            # 发送AT命令测试连接
            ser.write(b'AT\r\n')
            await asyncio.sleep(0.2)
            response = ser.read_all().decode('utf-8', errors='ignore')

            if 'OK' not in response:
                ser.close()
                return None

            # 创建调制解调器信息
            modem_info = ModemInfo(port=port, is_connected=True)

            # 获取调制解调器信息
            modem_info = await self._get_modem_info(ser, modem_info)

            ser.close()

            if modem_info.imei:
                return modem_info

            return None

        except (serial.SerialException, OSError) as e:
            logger.debug(f"端口 {port} 不可用: {e}")
            return None
        except Exception as e:
            logger.error(f"检测端口 {port} 时发生错误: {e}")
            return None

    async def _get_modem_info(self, ser: serial.Serial, modem_info: ModemInfo) -> ModemInfo:
        """获取调制解调器详细信息"""
        try:
            # 获取制造商信息
            ser.write(b'ATI\r\n')
            await asyncio.sleep(0.2)
            response = ser.read_all().decode('utf-8', errors='ignore')

            # 简单的制造商识别
            response_upper = response.upper()
            if 'HUAWEI' in response_upper:
                modem_info.manufacturer = 'Huawei'
            elif 'ZTE' in response_upper:
                modem_info.manufacturer = 'ZTE'
            elif 'QUECTEL' in response_upper:
                modem_info.manufacturer = 'Quectel'
            elif 'SIERRA' in response_upper:
                modem_info.manufacturer = 'Sierra'
            elif 'SIMCOM' in response_upper:
                modem_info.manufacturer = 'SIMCom'

            # 获取型号
            ser.write(b'AT+GMM\r\n')
            await asyncio.sleep(0.2)
            response = ser.read_all().decode('utf-8', errors='ignore')
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('AT') and 'OK' not in line:
                        modem_info.model = line
                        break

            # 获取IMEI
            ser.write(b'AT+GSN\r\n')
            await asyncio.sleep(0.2)
            response = ser.read_all().decode('utf-8', errors='ignore')
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line.isdigit() and 15 <= len(line) <= 17:
                        modem_info.imei = line
                        break

            # 获取信号强度
            ser.write(b'AT+CSQ\r\n')
            await asyncio.sleep(0.2)
            response = ser.read_all().decode('utf-8', errors='ignore')
            if '+CSQ:' in response:
                match = re.search(r'\+CSQ:\s*(\d+)', response)
                if match:
                    modem_info.signal_strength = match.group(1)

        except Exception as e:
            logger.warning(f"获取调制解调器信息失败: {e}")

        return modem_info

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
