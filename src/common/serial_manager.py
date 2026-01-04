"""
串口管理器 - 管理多个调制解调器连接
"""
import asyncio
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from loguru import logger

from .serial_detector import SerialDetector, ModemInfo
from src.sms_service.sms_sender import SMSSender


@dataclass
class ManagedModem:
    """管理的调制解调器"""
    info: ModemInfo
    sender: SMSSender
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    is_available: bool = True
    last_used: float = field(default_factory=time.time)
    success_count: int = 0
    failure_count: int = 0
    in_use: bool = False
    _is_quectel: bool = False

    @property
    def success_rate(self) -> float:
        """计算成功率"""
        total = self.success_count + self.failure_count
        if total == 0:
            return 1.0
        return self.success_count / total

    async def test_connection(self) -> bool:
        """测试连接"""
        try:
            return await self.sender.test_connection()
        except:
            return False


class SerialManager:
    """
    串口管理器
    管理多个调制解调器连接，提供负载均衡和并发控制
    """

    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.serial_detector = SerialDetector(config_manager)
        self.modems: Dict[str, ManagedModem] = {}
        self._lock = asyncio.Lock()
        self._round_robin_index = 0
        self._init_complete = False
        self._stats_lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """初始化串口管理器"""
        try:
            logger.info("🔄 初始化串口管理器...")

            # 检测调制解调器
            modems = await self.serial_detector.detect_modems(force_refresh=True)

            if not modems:
                logger.error("❌ 未检测到可用的调制解调器")
                return False

            logger.info(f"📡 检测到 {len(modems)} 个调制解调器")

            # 创建ManagedModem实例
            async with self._lock:
                for modem in modems:
                    try:
                        # 创建短信发送器
                        serial_config = self.config_manager.serial_config
                        sender = SMSSender(
                            port=modem.port,
                            baudrate=serial_config.baudrate,
                            timeout=serial_config.timeout
                        )

                        # 连接调制解调器
                        connected = await sender.connect()
                        if connected:
                            # 检测是否为Quectel调制解调器
                            is_quectel = await self._detect_quectel(sender)

                            managed_modem = ManagedModem(
                                info=modem,
                                sender=sender,
                                _is_quectel=is_quectel
                            )
                            self.modems[modem.port] = managed_modem
                            logger.info(f"✅ 初始化调制解调器: {modem.port} ({modem.manufacturer} {modem.model})")
                            if is_quectel:
                                logger.info(f"   📍 检测为Quectel调制解调器")
                        else:
                            logger.warning(f"⚠️ 调制解调器连接失败: {modem.port}")
                    except Exception as e:
                        logger.error(f"❌ 初始化调制解调器失败 {modem.port}: {e}")

                if not self.modems:
                    logger.error("❌ 没有可用的调制解调器连接")
                    return False

                self._init_complete = True
                logger.info(f"✅ 串口管理器初始化完成，{len(self.modems)} 个调制解调器可用")
                return True

        except Exception as e:
            logger.error(f"❌ 串口管理器初始化失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def _detect_quectel(self, sender: SMSSender) -> bool:
        """检测是否为Quectel调制解调器"""
        try:
            # 通过ATI命令检测
            response = await sender._send_at_command("ATI")
            return "Quectel" in response or "EC20" in response
        except:
            return False

    async def get_best_modem(self) -> Optional[ManagedModem]:
        """获取最佳的调制解调器（基于成功率和负载）"""
        if not self.modems:
            return None

        async with self._stats_lock:
            available_modems = [
                modem for modem in self.modems.values()
                if modem.is_available and not modem.in_use
            ]

            if not available_modems:
                return None

            # 按成功率和信号强度排序
            sorted_modems = sorted(
                available_modems,
                key=lambda m: (
                    m.success_rate,
                    int(m.info.signal_strength) if m.info.signal_strength.isdigit() else 0,
                    -m.last_used  # 最近使用的时间戳越小越好
                ),
                reverse=True
            )

            return sorted_modems[0]

    async def get_round_robin_modem(self) -> Optional[ManagedModem]:
        """轮询获取调制解调器"""
        if not self.modems:
            return None

        async with self._stats_lock:
            available_modems = [
                modem for modem in self.modems.values()
                if modem.is_available and not modem.in_use
            ]

            if not available_modems:
                return None

            # 轮询选择
            self._round_robin_index = (self._round_robin_index + 1) % len(available_modems)
            return available_modems[self._round_robin_index]

    async def acquire_modem(self, modem: ManagedModem) -> bool:
        """获取调制解调器锁"""
        if modem.in_use:
            return False

        try:
            # 尝试获取锁，设置超时避免死锁
            acquired = await asyncio.wait_for(modem.lock.acquire(), timeout=2.0)
            if acquired:
                modem.in_use = True
                modem.last_used = time.time()
                return True
            return False
        except asyncio.TimeoutError:
            logger.warning(f"⏰ 获取调制解调器锁超时: {modem.info.port}")
            return False
        except Exception as e:
            logger.error(f"获取调制解调器锁失败 {modem.info.port}: {e}")
            return False

    async def release_modem(self, modem: ManagedModem, success: bool = True):
        """释放调制解调器锁"""
        try:
            async with self._stats_lock:
                if success:
                    modem.success_count += 1
                else:
                    modem.failure_count += 1

            modem.in_use = False
            if modem.lock.locked():
                modem.lock.release()
        except Exception as e:
            logger.error(f"释放调制解调器锁失败 {modem.info.port}: {e}")

    async def send_sms(self, phone_number: str, content: str) -> Tuple[bool, str, Optional[str]]:
        """
        发送短信（自动选择调制解调器）

        Returns:
            (success, message, modem_port)
        """
        if not self._init_complete:
            return False, "串口管理器未初始化", None

        # 首先尝试获取最佳调制解调器
        modem = await self.get_best_modem()
        if not modem:
            # 如果没有最佳调制解调器，尝试轮询
            modem = await self.get_round_robin_modem()

        if not modem:
            # 如果还没有，尝试任何可用的调制解调器
            async with self._stats_lock:
                for m in self.modems.values():
                    if m.is_available and not m.in_use:
                        modem = m
                        break

        if not modem:
            return False, "没有可用的调制解调器", None

        # 获取调制解调器锁
        if not await self.acquire_modem(modem):
            return False, "调制解调器繁忙", modem.info.port

        try:
            logger.info(f"📱 使用调制解调器 {modem.info.port} 发送短信到: {phone_number}")
            logger.info(f"📄 内容长度: {len(content)} 字符")

            # 发送短信
            result = await modem.sender.send_sms(phone_number, content)

            success = result.success
            message = result.status_message

            return success, message, modem.info.port

        except Exception as e:
            logger.error(f"发送短信失败 {modem.info.port}: {e}")
            return False, f"发送失败: {str(e)}", modem.info.port

        finally:
            # 释放锁
            await self.release_modem(modem, success)

    async def get_health_status(self) -> Dict:
        """获取健康状态"""
        async with self._stats_lock:
            status = {
                "total_modems": len(self.modems),
                "available_modems": sum(1 for m in self.modems.values() if m.is_available),
                "in_use_modems": sum(1 for m in self.modems.values() if m.in_use),
                "modems": []
            }

            for modem in self.modems.values():
                status["modems"].append({
                    "port": modem.info.port,
                    "manufacturer": modem.info.manufacturer,
                    "model": modem.info.model,
                    "signal_strength": modem.info.signal_strength,
                    "imei": modem.info.imei[:8] + "****" if modem.info.imei else "",
                    "is_available": modem.is_available,
                    "in_use": modem.in_use,
                    "success_count": modem.success_count,
                    "failure_count": modem.failure_count,
                    "success_rate": round(modem.success_rate, 3),
                    "last_used": round(time.time() - modem.last_used, 1),
                    "is_quectel": modem._is_quectel
                })

            return status

    async def test_all_connections(self) -> bool:
        """测试所有连接"""
        if not self.modems:
            return False

        results = []
        for modem in self.modems.values():
            try:
                if await self.acquire_modem(modem):
                    connected = await modem.test_connection()
                    await self.release_modem(modem, connected)
                    results.append((modem.info.port, connected))
                else:
                    results.append((modem.info.port, False))
            except Exception as e:
                logger.error(f"测试连接失败 {modem.info.port}: {e}")
                results.append((modem.info.port, False))

        success_count = sum(1 for _, connected in results if connected)
        if success_count < len(results):
            logger.warning(f"⚠️ 连接测试: {success_count}/{len(results)} 个调制解调器正常")
        else:
            logger.info(f"✅ 连接测试: {success_count}/{len(results)} 个调制解调器正常")

        return success_count > 0

    async def disable_modem(self, port: str, reason: str = "未知原因"):
        """禁用调制解调器"""
        if port in self.modems:
            self.modems[port].is_available = False
            logger.warning(f"⚠️ 禁用调制解调器 {port}: {reason}")

    async def enable_modem(self, port: str):
        """启用调制解调器"""
        if port in self.modems:
            self.modems[port].is_available = True
            logger.info(f"✅ 启用调制解调器 {port}")

    async def reset_modem_stats(self, port: str = None):
        """重置调制解调器统计"""
        async with self._stats_lock:
            if port:
                if port in self.modems:
                    self.modems[port].success_count = 0
                    self.modems[port].failure_count = 0
                    logger.info(f"✅ 重置调制解调器统计: {port}")
            else:
                for modem in self.modems.values():
                    modem.success_count = 0
                    modem.failure_count = 0
                logger.info("✅ 重置所有调制解调器统计")

    async def cleanup(self):
        """清理资源"""
        logger.info("🧹 清理串口管理器资源...")

        for modem in self.modems.values():
            try:
                await modem.sender.disconnect()
                logger.debug(f"断开调制解调器连接: {modem.info.port}")
            except Exception as e:
                logger.error(f"断开调制解调器连接失败 {modem.info.port}: {e}")

        self.modems.clear()
        self._init_complete = False
        logger.info("✅ 串口管理器清理完成")
