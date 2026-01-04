"""
调制解调器管理器 - 修复版本
"""
import asyncio
import time
import random
import re
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger

import gsmmodem
from gsmmodem.modem import GsmModem
from gsmmodem.exceptions import (
    PinRequiredError, IncorrectPinError, CommandError,
    TimeoutException, GsmModemException
)

from .config import Config


@dataclass
class ModemInfo:
    """调制解调器信息"""
    port: str
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    imei: str = "Unknown"
    imsi: str = "Unknown"
    signal_strength: int = -1
    network_name: str = "Unknown"
    smsc_number: str = ""
    sms_text_mode: bool = True
    sms_encoding: str = "GSM"
    is_available: bool = False
    in_use: bool = False
    last_used: float = 0.0
    error_count: int = 0
    max_retries: int = 3
    retry_delay: float = 1.0


class ManagedModem:
    """
    托管调制解调器

    封装 GsmModem 实例，提供连接池、错误处理和监控功能
    """

    def __init__(self, modem: GsmModem, info: ModemInfo):
        self.modem = modem
        self.info = info
        self._lock = asyncio.Lock()
        self._last_health_check = time.time()

    async def send_sms(self, phone_number: str, message: str) -> Tuple[bool, str]:
        """
        发送短信

        Args:
            phone_number: 手机号码
            message: 短信内容

        Returns:
            (成功状态, 消息/错误)
        """
        try:
            async with self._lock:
                self.info.in_use = True
                self.info.last_used = time.time()

                logger.debug(f"📤 通过 {self.info.port} 发送短信到 {phone_number}")
                logger.debug(f"📄 内容长度: {len(message)} 字符")
                logger.debug(f"📱 调制解调器编码: {self.info.sms_encoding}")

                # 发送短信 - 注意：移除 unicode 参数！
                try:
                    # 基本调用，让 gsmmodem 自动处理编码
                    self.modem.sendSms(
                        destination=phone_number,
                        text=message,
                        waitForDeliveryReport=False,
                        deliveryTimeout=30,
                        sendFlash=False  # 不是闪信
                    )

                    logger.info(f"✅ 短信发送成功: {phone_number} via {self.info.port}")
                    self.info.is_available = True
                    self.info.error_count = 0

                    return True, "短信发送成功"

                except CommandError as e:
                    error_msg = f"命令错误: {str(e)}"
                    logger.error(f"❌ 发送失败: {error_msg}")
                    self.info.error_count += 1

                    # 如果是编码错误，尝试不同的处理方式
                    if "encoding" in str(e).lower() or "character" in str(e).lower():
                        logger.warning(f"⚠️ 可能编码问题，尝试特殊处理...")
                        # 这里可以添加编码特殊处理逻辑

                    return False, error_msg

                except TimeoutException as e:
                    error_msg = f"超时: {str(e)}"
                    logger.error(f"⏰ 发送超时: {error_msg}")
                    self.info.error_count += 1
                    return False, error_msg

                except Exception as e:
                    error_msg = f"未知错误: {str(e)}"
                    logger.error(f"💥 发送异常: {error_msg}")
                    self.info.error_count += 1
                    return False, error_msg

        except Exception as e:
            logger.error(f"💥 发送过程异常: {e}")
            return False, f"发送过程异常: {str(e)}"

        finally:
            self.info.in_use = False

    async def health_check(self) -> bool:
        """健康检查"""
        try:
            async with self._lock:
                # 检查信号强度
                signal = self.modem.signalStrength
                self.info.signal_strength = signal

                # 检查网络连接
                network = self.modem.networkName
                if network:
                    self.info.network_name = network

                self.info.is_available = signal > 0
                self._last_health_check = time.time()

                if self.info.is_available:
                    logger.debug(f"✅ 调制解调器健康: {self.info.port}, 信号: {signal}")
                else:
                    logger.warning(f"⚠️ 调制解调器信号弱: {self.info.port}, 信号: {signal}")

                return self.info.is_available

        except Exception as e:
            logger.error(f"❌ 调制解调器健康检查失败: {self.info.port} - {e}")
            self.info.is_available = False
            self.info.error_count += 1
            return False

    async def close(self):
        """关闭调制解调器连接"""
        try:
            if self.modem:
                self.modem.close()
                logger.debug(f"🔌 关闭调制解调器连接: {self.info.port}")
        except Exception as e:
            logger.error(f"❌ 关闭调制解调器失败: {self.info.port} - {e}")


class ModemManager:
    """
    调制解调器管理器

    管理多个调制解调器，提供负载均衡、故障转移和连接池功能
    """

    def __init__(self, config: Config):
        self.config = config
        self.modems: Dict[str, ManagedModem] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._last_status_check = 0
        self._status_cache = None
        self._status_cache_ttl = 5  # 秒

    async def initialize(self) -> bool:
        """初始化调制解调器管理器"""
        try:
            logger.info("🚀 初始化调制解调器管理器...")

            # 1. 获取调制解调器端口列表
            modem_ports = await self._discover_modem_ports()
            if not modem_ports:
                logger.warning("⚠️ 未找到调制解调器端口")
                return False

            logger.info(f"🔍 发现 {len(modem_ports)} 个调制解调器端口: {modem_ports}")

            # 2. 连接和初始化每个调制解调器
            tasks = []
            for port in modem_ports:
                task = asyncio.create_task(self._initialize_modem(port))
                tasks.append(task)

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 3. 统计成功初始化的调制解调器
            successful_modems = 0
            for i, result in enumerate(results):
                port = modem_ports[i]
                if isinstance(result, Exception):
                    logger.error(f"❌ 初始化调制解调器失败 {port}: {result}")
                elif result:
                    successful_modems += 1

            self._initialized = successful_modems > 0

            if self._initialized:
                logger.info(f"✅ 调制解调器管理器初始化完成: {successful_modems}/{len(modem_ports)} 个调制解调器可用")

                # 4. 打印调制解调器详情
                await self._log_modem_details()
            else:
                logger.error("❌ 调制解调器管理器初始化失败: 没有可用的调制解调器")

            return self._initialized

        except Exception as e:
            logger.error(f"💥 调制解调器管理器初始化异常: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def _discover_modem_ports(self) -> List[str]:
        """发现可用的调制解调器端口"""
        # 从配置中获取端口列表
        config_ports = self.config.modem.ports

        if config_ports:
            # 过滤存在的端口
            existing_ports = []
            for port in config_ports:
                port_path = Path(port)
                if port_path.exists():
                    existing_ports.append(port)
                else:
                    logger.warning(f"⚠️ 配置的端口不存在: {port}")
            return existing_ports

        # 如果没有配置端口，尝试自动发现
        logger.info("🔍 自动发现调制解调器端口...")

        # 常见的调制解调器端口
        common_ports = [
            "/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2", "/dev/ttyUSB3",
            "/dev/ttyACM0", "/dev/ttyACM1",
            "/dev/ttyS0", "/dev/ttyS1", "/dev/ttyS2", "/dev/ttyS3",
            "COM1", "COM2", "COM3", "COM4", "COM5", "COM6"
        ]

        # 检查哪些端口存在
        available_ports = []
        for port in common_ports:
            port_path = Path(port)
            if port_path.exists():
                available_ports.append(port)
                logger.debug(f"  发现端口: {port}")

        if available_ports:
            logger.info(f"✅ 自动发现 {len(available_ports)} 个端口")
        else:
            logger.warning("⚠️ 未发现任何调制解调器端口")

        return available_ports

    async def _initialize_modem(self, port: str) -> bool:
        """初始化单个调制解调器"""
        try:
            logger.info(f"🔄 初始化调制解调器: {port}")

            # 1. 创建 GsmModem 实例
            modem = GsmModem(
                port=port,
                baudrate=self.config.modem.baudrate,
                incomingCallCallbackFunc=None,
                smsReceivedCallbackFunc=None,
                smsStatusReportCallback=None,
                requestDelivery=True,
                AT_CNMI=''
            )

            # 2. 连接到调制解调器
            logger.debug(f"  连接调制解调器: {port}")
            modem.connect(pin=self.config.modem.pin)

            # 3. 收集调制解调器信息
            info = ModemInfo(
                port=port,
                manufacturer=modem.manufacturer,
                model=modem.model,
                imei=modem.imei,
                imsi=modem.imsi,
                signal_strength=modem.signalStrength,
                network_name=modem.networkName,
                smsc_number=modem.smsc,
                sms_text_mode=modem.smsTextMode,
                sms_encoding=modem.smsEncoding,
                is_available=True,
                in_use=False,
                last_used=0.0,
                error_count=0,
                max_retries=self.config.modem.max_retries,
                retry_delay=self.config.modem.retry_delay
            )

            # 4. 创建托管调制解调器
            managed_modem = ManagedModem(modem, info)

            # 5. 添加到管理器
            self.modems[port] = managed_modem

            logger.info(f"✅ 调制解调器初始化成功: {port}")
            logger.info(f"   制造商: {info.manufacturer}")
            logger.info(f"   型号: {info.model}")
            logger.info(f"   IMEI: {info.imei}")
            logger.info(f"   信号强度: {info.signal_strength}")
            logger.info(f"   网络: {info.network_name}")
            logger.info(f"   编码: {info.sms_encoding}")
            logger.info(f"   文本模式: {info.sms_text_mode}")

            return True

        except PinRequiredError as e:
            logger.error(f"❌ 调制解调器需要 PIN 码: {port}")
            return False
        except IncorrectPinError as e:
            logger.error(f"❌ PIN 码错误: {port}")
            return False
        except TimeoutException as e:
            logger.error(f"⏰ 连接调制解调器超时: {port}")
            return False
        except Exception as e:
            logger.error(f"💥 初始化调制解调器异常 {port}: {e}")
            return False

    async def send_sms(self, phone_number: str, message: str) -> Tuple[bool, str, str]:
        """
        发送短信（带负载均衡）

        Args:
            phone_number: 手机号码
            message: 短信内容

        Returns:
            (成功状态, 消息/错误, 使用的调制解调器端口)
        """
        if not self._initialized:
            return False, "调制解调器管理器未初始化", ""

        # 1. 选择最优的调制解调器
        selected_modem = await self._select_modem_for_sending()
        if not selected_modem:
            return False, "没有可用的调制解调器", ""

        # 2. 发送短信
        success, message_result = await selected_modem.send_sms(phone_number, message)

        return success, message_result, selected_modem.info.port

    async def _select_modem_for_sending(self) -> Optional[ManagedModem]:
        """选择用于发送短信的调制解调器（负载均衡）"""
        # 获取可用的调制解调器
        available_modems = []
        for modem in self.modems.values():
            if modem.info.is_available and not modem.info.in_use:
                available_modems.append(modem)

        if not available_modems:
            logger.warning("⚠️ 没有可用的调制解调器")
            return None

        # 选择策略：基于信号强度和最近使用时间
        def modem_score(modem: ManagedModem) -> float:
            # 基础分数：信号强度（0-99）
            signal_score = modem.info.signal_strength / 99.0 if modem.info.signal_strength > 0 else 0

            # 错误惩罚：错误越多，分数越低
            error_penalty = modem.info.error_count * 0.1

            # 最近使用惩罚：鼓励使用最近未使用的调制解调器
            time_since_last_use = time.time() - modem.info.last_used
            freshness_bonus = min(time_since_last_use / 3600.0, 1.0)  # 最大1小时

            return signal_score + freshness_bonus - error_penalty

        # 选择分数最高的调制解调器
        selected_modem = max(available_modems, key=modem_score)

        logger.debug(f"📱 选择调制解调器: {selected_modem.info.port}, "
                    f"信号: {selected_modem.info.signal_strength}, "
                    f"分数: {modem_score(selected_modem):.2f}")

        return selected_modem

    async def health_check(self) -> bool:
        """健康检查所有调制解调器"""
        if not self.modems:
            logger.warning("⚠️ 没有调制解调器可检查")
            return False

        tasks = []
        for modem in self.modems.values():
            tasks.append(asyncio.create_task(modem.health_check()))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 统计健康的调制解调器
        healthy_count = 0
        for i, result in enumerate(list(self.modems.values())):
            if isinstance(result, Exception):
                logger.error(f"健康检查异常: {list(self.modems.values())[i].info.port} - {result}")
            elif result:
                healthy_count += 1

        is_healthy = healthy_count > 0

        logger.debug(f"📊 调制解调器健康检查: {healthy_count}/{len(self.modems)} 个健康")

        return is_healthy

    async def get_status(self) -> Dict[str, Any]:
        """获取调制解调器状态"""
        # 使用缓存（如果可用）
        current_time = time.time()
        if (self._status_cache and
            current_time - self._last_status_check < self._status_cache_ttl):
            return self._status_cache

        # 执行健康检查
        await self.health_check()

        # 构建状态信息
        status = {
            "initialized": self._initialized,
            "total_modems": len(self.modems),
            "available_modems": 0,
            "in_use_modems": 0,
            "modems": []
        }

        for modem in self.modems.values():
            modem_status = {
                "port": modem.info.port,
                "manufacturer": modem.info.manufacturer,
                "model": modem.info.model,
                "imei": modem.info.imei,
                "signal_strength": modem.info.signal_strength,
                "network_name": modem.info.network_name,
                "sms_encoding": modem.info.sms_encoding,
                "sms_text_mode": modem.info.sms_text_mode,
                "is_available": modem.info.is_available,
                "in_use": modem.info.in_use,
                "error_count": modem.info.error_count,
                "last_used": modem.info.last_used
            }

            status["modems"].append(modem_status)

            if modem.info.is_available:
                status["available_modems"] += 1

            if modem.info.in_use:
                status["in_use_modems"] += 1

        # 更新缓存
        self._status_cache = status
        self._last_status_check = current_time

        return status

    async def _log_modem_details(self):
        """记录调制解调器详情"""
        logger.info("=" * 50)
        logger.info("📱 调制解调器详情:")

        status = await self.get_status()

        for i, modem in enumerate(status["modems"], 1):
            status_symbol = "✅" if modem["is_available"] else "❌"
            in_use_symbol = "🔒" if modem["in_use"] else "🆓"

            logger.info(f"  {i}. {modem['port']}:")
            logger.info(f"     制造商: {modem['manufacturer']}")
            logger.info(f"     型号: {modem['model']}")
            logger.info(f"     IMEI: {modem['imei']}")
            logger.info(f"     信号: {modem['signal_strength']}")
            logger.info(f"     网络: {modem['network_name']}")
            logger.info(f"     编码: {modem['sms_encoding']}")
            logger.info(f"     状态: {status_symbol} {in_use_symbol}")

        logger.info(f"📊 总结: {status['available_modems']}/{status['total_modems']} 个可用")
        logger.info("=" * 50)

    async def cleanup(self):
        """清理资源"""
        logger.info("🧹 清理调制解调器管理器...")

        tasks = []
        for modem in self.modems.values():
            tasks.append(asyncio.create_task(modem.close()))

        await asyncio.gather(*tasks, return_exceptions=True)

        self.modems.clear()
        self._initialized = False

        logger.info("✅ 调制解调器管理器清理完成")
