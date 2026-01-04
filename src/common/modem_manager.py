"""
调制解调器管理器（支持多调制解调器、负载均衡、异步锁）
"""
import asyncio
import glob
import time
import os
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

# 导入 gsmmodem
try:
    from gsmmodem.modem import GsmModem
    from gsmmodem.exceptions import TimeoutException, PinRequiredError, CommandError
    HAS_GSMMODEM = True
except ImportError:
    HAS_GSMMODEM = False
    logger.warning("未安装 python-gsmmodem-2025，使用模拟模式")


@dataclass
class ModemInfo:
    """调制解调器信息"""
    port: str
    manufacturer: str = "Unknown"
    model: str = "Unknown"
    imei: str = ""
    signal_strength: int = 0
    is_connected: bool = False


@dataclass
class ManagedModem:
    """托管的调制解调器"""
    info: ModemInfo
    modem: Optional[Any] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    is_available: bool = True
    last_used: float = field(default_factory=time.time)
    success_count: int = 0
    failure_count: int = 0
    in_use: bool = False

    @property
    def success_rate(self) -> float:
        """计算成功率"""
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 1.0

    def is_alive(self) -> bool:
        """检查调制解调器是否存活"""
        try:
            if self.modem and hasattr(self.modem, 'serial') and self.modem.serial:
                return self.modem.serial.is_open
            return False
        except:
            return False


class ModemManager:
    """
    调制解调器管理器

    特性：
    1. 自动检测可用的调制解调器
    2. 负载均衡（轮询和最佳选择）
    3. 异步锁防止并发冲突
    4. 连接状态监控和自动重连
    """

    def __init__(self, config):
        self.config = config
        self.modems: Dict[str, ManagedModem] = {}
        self._lock = asyncio.Lock()
        self._round_robin_index = 0
        self._initialized = False

    async def initialize(self) -> bool:
        """
        初始化调制解调器管理器

        Returns:
            是否初始化成功
        """
        try:
            logger.info("🔄 初始化调制解调器管理器...")

            if not HAS_GSMMODEM:
                logger.error("❌ 未安装 python-gsmmodem-2025")
                return False

            # 检测调制解调器
            detected_modems = await self._detect_modems()

            if not detected_modems:
                logger.error("❌ 未检测到可用的调制解调器")
                return False

            logger.info(f"📡 检测到 {len(detected_modems)} 个调制解调器")

            # 初始化调制解调器
            async with self._lock:
                for modem_info in detected_modems:
                    try:
                        # 连接调制解调器
                        modem = await self._connect_modem(modem_info)

                        if modem:
                            managed_modem = ManagedModem(
                                info=modem_info,
                                modem=modem
                            )
                            self.modems[modem_info.port] = managed_modem
                            logger.info(f"✅ 初始化调制解调器: {modem_info.port}")
                        else:
                            logger.warning(f"⚠️ 调制解调器连接失败: {modem_info.port}")
                    except Exception as e:
                        logger.error(f"❌ 初始化调制解调器失败 {modem_info.port}: {e}")

                if not self.modems:
                    logger.error("❌ 没有可用的调制解调器连接")
                    return False

                self._initialized = True
                logger.info(f"✅ 调制解调器管理器初始化完成，{len(self.modems)} 个调制解调器可用")
                return True

        except Exception as e:
            logger.error(f"❌ 调制解调器管理器初始化失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def _detect_modems(self) -> List[ModemInfo]:
        """检测可用的调制解调器"""
        modems = []

        # 展开所有端口模式
        all_ports = []
        for pattern in self.config.modem.port_patterns:
            try:
                matched_ports = glob.glob(pattern)
                all_ports.extend(matched_ports)
            except Exception as e:
                logger.warning(f"Glob模式 {pattern} 错误: {e}")

        # 去重并排序
        all_ports = sorted(set(all_ports))

        if not all_ports:
            logger.warning(f"未找到匹配的串口: {self.config.modem.port_patterns}")
            return []

        logger.debug(f"找到串口: {all_ports}")

        # 并发检测所有端口
        tasks = [self._test_modem_port(port) for port in all_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效的调制解调器
        for result in results:
            if isinstance(result, ModemInfo) and result.is_connected:
                modems.append(result)

        return modems

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=0.5, max=2)
    )
    async def _test_modem_port(self, port: str) -> Optional[ModemInfo]:
        """测试调制解调器端口"""
        try:
            # 检查端口是否存在（Linux/Unix）
            if os.name != 'nt' and not os.path.exists(port):
                return None

            if not HAS_GSMMODEM:
                return None

            # 创建调制解调器实例（根据官方文档）
            modem = GsmModem(
                port=port,
                baudrate=self.config.modem.baudrate
            )

            # 尝试连接
            try:
                modem.connect(self.config.modem.pin)
                connected = True
            except PinRequiredError:
                logger.debug(f"需要 PIN 码: {port}")
                return None
            except Exception as e:
                logger.debug(f"连接失败 {port}: {e}")
                return None

            if not connected:
                return None

            # 获取调制解调器信息
            try:
                manufacturer = modem.manufacturer or "Unknown"
                model = modem.model or "Unknown"
                imei = modem.imei or ""

                # 获取信号强度
                signal_strength = 0
                try:
                    signal_strength = modem.signalStrength
                except:
                    pass

                modem_info = ModemInfo(
                    port=port,
                    manufacturer=manufacturer,
                    model=model,
                    imei=imei,
                    signal_strength=signal_strength,
                    is_connected=True
                )

                # 关闭调制解调器（测试完成后）
                try:
                    modem.close()
                except:
                    pass

                return modem_info

            except Exception as e:
                logger.debug(f"获取调制解调器信息失败 {port}: {e}")
                try:
                    modem.close()
                except:
                    pass
                return None

        except Exception as e:
            logger.debug(f"测试端口 {port} 失败: {e}")
            return None

    async def _connect_modem(self, modem_info: ModemInfo) -> Optional[Any]:
        """连接调制解调器"""
        try:
            # 创建调制解调器实例
            modem = GsmModem(
                port=modem_info.port,
                baudrate=self.config.modem.baudrate
            )

            # 连接调制解调器
            modem.connect(self.config.modem.pin)

            # 等待网络覆盖
            try:
                signal = modem.waitForNetworkCoverage(30)
                modem_info.signal_strength = signal
                logger.info(f"📶 调制解调器 {modem_info.port} 网络覆盖正常，信号强度: {signal}")
            except Exception as e:
                logger.warning(f"⚠️ 调制解调器 {modem_info.port} 网络覆盖检查失败: {e}")
                # 继续使用，可能信号弱或暂时无网络

            logger.info(f"✅ 连接调制解调器: {modem_info.port}")
            return modem

        except PinRequiredError:
            logger.error(f"❌ 需要 PIN 码: {modem_info.port}")
            return None
        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败 {modem_info.port}: {e}")
            return None

    @asynccontextmanager
    async def acquire_modem(self) -> ManagedModem:
        """
        获取可用的调制解调器（上下文管理器）

        Yields:
            可用的调制解调器

        Raises:
            RuntimeError: 没有可用的调制解调器
        """
        if not self._initialized:
            raise RuntimeError("调制解调器管理器未初始化")

        # 选择调制解调器策略
        modem = await self._select_modem()

        if not modem:
            raise RuntimeError("没有可用的调制解调器")

        # 获取锁
        try:
            await asyncio.wait_for(
                modem.lock.acquire(),
                timeout=self.config.modem.lock_timeout
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"获取调制解调器锁超时: {modem.info.port}")

        modem.in_use = True
        modem.last_used = time.time()

        try:
            # 确保调制解调器连接
            if not modem.is_alive():
                logger.info(f"重新连接调制解调器: {modem.info.port}")
                try:
                    if modem.modem:
                        modem.modem.close()
                except:
                    pass

                modem.modem = await self._connect_modem(modem.info)

                if not modem.modem:
                    modem.is_available = False
                    raise RuntimeError(f"调制解调器连接失败: {modem.info.port}")

            yield modem

        finally:
            # 释放锁
            modem.in_use = False
            if modem.lock.locked():
                modem.lock.release()

    async def _select_modem(self) -> Optional[ManagedModem]:
        """选择可用的调制解调器"""
        if not self.modems:
            return None

        # 先尝试最佳调制解调器（基于成功率和信号强度）
        available_modems = [
            m for m in self.modems.values()
            if m.is_available and not m.in_use
        ]

        if not available_modems:
            return None

        # 按成功率和信号强度排序
        sorted_modems = sorted(
            available_modems,
            key=lambda m: (
                m.success_rate,
                m.info.signal_strength,
                -m.last_used  # 最近使用的时间戳越小越好
            ),
            reverse=True
        )

        return sorted_modems[0]

    async def send_sms(self, phone_number: str, content: str) -> Tuple[bool, str, str]:
        """
        发送短信

        Args:
            phone_number: 手机号码
            content: 短信内容

        Returns:
            (是否成功, 消息, 调制解调器端口)
        """
        async with self.acquire_modem() as modem:
            try:
                logger.info(f"📱 使用调制解调器 {modem.info.port} 发送短信到: {phone_number}")
                logger.info(f"📄 内容长度: {len(content)} 字符")

                start_time = time.time()

                # 发送短信（根据官方文档，使用 unicode=True 处理中文）
                response = modem.modem.sendSms(
                    destination=phone_number,
                    text=content,
                    waitForDeliveryReport=False,
                    unicode=True  # 启用 Unicode 支持
                )

                elapsed_time = time.time() - start_time

                # 处理响应（根据官方文档，sendSms 返回 SentSms 对象）
                if isinstance(response, list):
                    # 长短信，返回多个消息
                    success = all(msg.status in ['ENROUTE', 'DELIVERED'] for msg in response)
                    message = f"长短信发送完成 ({len(response)} 段)"
                    total_segments = len(response)
                else:
                    # 单条短信
                    success = response.status in ['ENROUTE', 'DELIVERED']
                    message = "短信发送成功" if success else f"发送失败: {response.status}"
                    total_segments = 1

                # 更新统计
                async with self._lock:
                    if success:
                        modem.success_count += 1
                        logger.info(f"✅ 发送成功 ({elapsed_time:.2f}s，{total_segments} 段)")
                    else:
                        modem.failure_count += 1
                        logger.error(f"❌ 发送失败: {message}")

                return success, message, modem.info.port

            except CommandError as e:
                logger.error(f"❌ AT 命令错误 {modem.info.port}: {e}")
                async with self._lock:
                    modem.failure_count += 1
                return False, f"AT命令错误: {str(e)}", modem.info.port

            except TimeoutException as e:
                logger.error(f"⏰ 发送超时 {modem.info.port}: {e}")
                async with self._lock:
                    modem.failure_count += 1
                return False, f"发送超时: {str(e)}", modem.info.port

            except Exception as e:
                logger.error(f"💥 发送短信失败 {modem.info.port}: {e}")

                # 更新失败统计
                async with self._lock:
                    modem.failure_count += 1

                return False, f"发送失败: {str(e)}", modem.info.port

    async def get_status(self) -> Dict[str, Any]:
        """获取调制解调器状态"""
        status = {
            "total_modems": len(self.modems),
            "available_modems": sum(1 for m in self.modems.values() if m.is_available),
            "in_use_modems": sum(1 for m in self.modems.values() if m.in_use),
            "initialized": self._initialized,
            "modems": []
        }

        for modem in self.modems.values():
            # 获取当前信号强度
            signal_strength = modem.info.signal_strength
            is_alive = modem.is_alive()

            status["modems"].append({
                "port": modem.info.port,
                "manufacturer": modem.info.manufacturer,
                "model": modem.info.model,
                "imei": modem.info.imei[:8] + "****" if modem.info.imei else "",
                "signal_strength": signal_strength,
                "is_available": modem.is_available,
                "in_use": modem.in_use,
                "success_count": modem.success_count,
                "failure_count": modem.failure_count,
                "success_rate": round(modem.success_rate, 3),
                "last_used": round(time.time() - modem.last_used, 1),
                "is_alive": is_alive
            })

        return status

    async def health_check(self) -> bool:
        """健康检查"""
        if not self._initialized:
            return False

        healthy_count = 0
        total_count = len(self.modems)

        for modem in self.modems.values():
            try:
                if modem.is_alive():
                    # 尝试发送 AT 命令测试
                    modem.modem.write("AT", waitForResponse=True, timeout=2.0)
                    healthy_count += 1
            except:
                pass

        logger.debug(f"健康检查: {healthy_count}/{total_count} 个调制解调器正常")
        return healthy_count > 0

    async def cleanup(self):
        """清理资源"""
        logger.info("🧹 清理调制解调器资源...")

        for modem in self.modems.values():
            try:
                if modem.modem:
                    modem.modem.close()
                    logger.debug(f"关闭调制解调器: {modem.info.port}")
            except Exception as e:
                logger.error(f"关闭调制解调器失败 {modem.info.port}: {e}")

        self.modems.clear()
        self._initialized = False
        logger.info("✅ 调制解调器管理器清理完成")
