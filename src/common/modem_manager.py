import asyncio
import time
import glob
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

from .config import AppConfig


@dataclass
class ModemInfo:
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
    def __init__(self, modem: GsmModem, info: ModemInfo):
        self.modem = modem
        self.info = info
        self._lock = asyncio.Lock()
        self._last_health_check = time.time()

    async def send_sms(self, phone_number: str, message: str) -> Tuple[bool, str]:
        try:
            async with self._lock:
                self.info.in_use = True
                self.info.last_used = time.time()

                logger.debug(f"📤 通过 {self.info.port} 发送短信到 {phone_number}")
                logger.debug(f"📄 内容长度: {len(message)} 字符")
                logger.debug(f"📱 调制解调器编码: {self.info.sms_encoding}")

                try:
                    self.modem.sendSms(
                        destination=phone_number,
                        text=message,
                        waitForDeliveryReport=False,
                        deliveryTimeout=30,
                        sendFlash=False
                    )

                    logger.info(f"✅ 短信发送成功: {phone_number} via {self.info.port}")
                    self.info.is_available = True
                    self.info.error_count = 0

                    return True, "短信发送成功"

                except CommandError as e:
                    error_msg = f"命令错误: {str(e)}"
                    logger.error(f"❌ 发送失败: {error_msg}")
                    self.info.error_count += 1

                    if "encoding" in str(e).lower() or "character" in str(e).lower():
                        logger.warning(f"⚠️ 可能编码问题，尝试特殊处理...")

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
        try:
            async with self._lock:
                signal = self.modem.signalStrength
                self.info.signal_strength = signal

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
        try:
            if self.modem:
                self.modem.close()
                logger.debug(f"🔌 关闭调制解调器连接: {self.info.port}")
        except Exception as e:
            logger.error(f"❌ 关闭调制解调器失败: {self.info.port} - {e}")


class ModemManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.modems: Dict[str, ManagedModem] = {}
        self._initialized = False
        self._lock = asyncio.Lock()
        self._last_status_check = 0
        self._status_cache = None
        self._status_cache_ttl = 5

    async def initialize(self) -> bool:
        try:
            logger.info("🚀 初始化调制解调器管理器...")

            modem_ports = await self._discover_modem_ports()
            if not modem_ports:
                logger.warning("⚠️ 未找到调制解调器端口")
                return False

            logger.info(f"🔍 发现 {len(modem_ports)} 个调制解调器端口: {modem_ports}")

            await self._initialize_modems_async(modem_ports)

            if self._initialized:
                logger.info(f"✅ 调制解调器管理器初始化完成: {len(self.modems)}/{len(modem_ports)} 个调制解调器可用")
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
        modem_config = self.config.modem

        discovered_ports = []
        for pattern in modem_config.port_patterns:
            matched_ports = glob.glob(pattern)
            discovered_ports.extend(matched_ports)

        discovered_ports = sorted(set(discovered_ports))

        existing_ports = []
        for port in discovered_ports:
            port_path = Path(port)
            if port_path.exists():
                existing_ports.append(port)
                logger.debug(f"  发现端口: {port}")
            else:
                logger.warning(f"⚠️ 端口不存在: {port}")

        if existing_ports:
            logger.info(f"✅ 发现 {len(existing_ports)} 个调制解调器端口")
            return existing_ports
        else:
            logger.warning("⚠️ 未发现任何调制解调器端口")
            return []

    async def _initialize_modems_async(self, modem_ports: List[str]):
        tasks = []
        for port in modem_ports:
            task = asyncio.create_task(self._initialize_modem_with_timeout(port))
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        successful_modems = 0
        for i, result in enumerate(results):
            port = modem_ports[i]
            if isinstance(result, Exception):
                logger.debug(f"🔄 调制解调器初始化失败 {port}: {result}")
            elif result:
                successful_modems += 1

        self._initialized = successful_modems > 0

    async def _initialize_modem_with_timeout(self, port: str) -> bool:
        try:
            return await asyncio.wait_for(
                self._initialize_modem(port),
                timeout=self.config.modem.connection_timeout
            )
        except asyncio.TimeoutError:
            logger.debug(f"⏰ 连接调制解调器超时: {port}")
            return False
        except Exception as e:
            logger.debug(f"🔄 调制解调器初始化异常 {port}: {e}")
            return False

    async def _initialize_modem(self, port: str) -> bool:
        try:
            logger.debug(f"🔄 初始化调制解调器: {port}")

            modem = GsmModem(
                port=port,
                baudrate=self.config.modem.baudrate,
                incomingCallCallbackFunc=None,
                smsReceivedCallbackFunc=None,
                smsStatusReportCallback=None,
                requestDelivery=False,
                AT_CNMI=''
            )

            logger.debug(f"  连接调制解调器: {port}")
            modem.connect(pin=self.config.modem.pin)

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

            managed_modem = ManagedModem(modem, info)

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
            logger.debug(f"🔒 调制解调器需要 PIN 码: {port}")
            return False
        except IncorrectPinError as e:
            logger.debug(f"❌ PIN 码错误: {port}")
            return False
        except TimeoutException as e:
            logger.debug(f"⏰ 连接调制解调器超时: {port}")
            return False
        except Exception as e:
            logger.debug(f"🔄 调制解调器初始化异常 {port}: {e}")
            return False

    async def send_sms(self, phone_number: str, message: str) -> Tuple[bool, str, str]:
        if not self._initialized:
            return False, "调制解调器管理器未初始化", ""

        selected_modem = await self._select_modem_for_sending()
        if not selected_modem:
            return False, "没有可用的调制解调器", ""

        success, message_result = await selected_modem.send_sms(phone_number, message)

        return success, message_result, selected_modem.info.port

    async def _select_modem_for_sending(self) -> Optional[ManagedModem]:
        available_modems = []
        for modem in self.modems.values():
            if modem.info.is_available and not modem.info.in_use:
                available_modems.append(modem)

        if not available_modems:
            logger.warning("⚠️ 没有可用的调制解调器")
            return None

        def modem_score(modem: ManagedModem) -> float:
            signal_score = modem.info.signal_strength / 99.0 if modem.info.signal_strength > 0 else 0
            error_penalty = modem.info.error_count * 0.1
            time_since_last_use = time.time() - modem.info.last_used
            freshness_bonus = min(time_since_last_use / 3600.0, 1.0)
            return signal_score + freshness_bonus - error_penalty

        selected_modem = max(available_modems, key=modem_score)

        logger.debug(f"📱 选择调制解调器: {selected_modem.info.port}, "
                    f"信号: {selected_modem.info.signal_strength}, "
                    f"分数: {modem_score(selected_modem):.2f}")

        return selected_modem

    async def health_check(self) -> bool:
        if not self.modems:
            logger.warning("⚠️ 没有调制解调器可检查")
            return False

        tasks = []
        for modem in self.modems.values():
            tasks.append(asyncio.create_task(modem.health_check()))

        results = await asyncio.gather(*tasks, return_exceptions=True)

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
        current_time = time.time()
        if (self._status_cache and
            current_time - self._last_status_check < self._status_cache_ttl):
            return self._status_cache

        await self.health_check()

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

        self._status_cache = status
        self._last_status_check = current_time

        return status

    async def _log_modem_details(self):
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
        logger.info("🧹 清理调制解调器管理器...")

        tasks = []
        for modem in self.modems.values():
            tasks.append(asyncio.create_task(modem.close()))

        await asyncio.gather(*tasks, return_exceptions=True)

        self.modems.clear()
        self._initialized = False

        logger.info("✅ 调制解调器管理器清理完成")
