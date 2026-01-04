"""
SMS短信发送器 - 简化版，只使用UCS2编码
"""
import asyncio
import time
import uuid
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple
import serial
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass
class SMSResult:
    """短信发送结果"""
    message_id: str
    success: bool
    status_code: int
    status_message: str
    data: str = ""
    timestamp: float = field(default_factory=time.time)


class SMSSender:
    """短信发送器 - 只使用UCS2编码"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.is_quectel = False
        self._debug_mode = False
        # UCS2编码最大长度（单条短信）
        self.UCS2_MAX_LENGTH = 70
        # 长短信分片阈值
        self.LONG_SMS_THRESHOLD = 140

    def enable_debug(self):
        """启用调试模式"""
        self._debug_mode = True
        logger.info(f"🔍 启用调试模式: {self.port}")

    async def connect(self) -> bool:
        """连接到调制解调器"""
        try:
            logger.info(f"正在连接调制解调器: {self.port} (波特率: {self.baudrate})...")
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )

            # 等待调制解调器初始化
            await asyncio.sleep(3)

            # 清空缓冲区
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            # 测试连接
            response = await self._send_at_command("AT")
            if "OK" not in response:
                logger.error("AT命令无响应")
                return False

            # 关闭回显
            await self._send_at_command("ATE0")
            # 启用详细错误
            await self._send_at_command("AT+CMEE=2")

            # 检测调制解调器类型
            response = await self._send_at_command("ATI")
            if "Quectel" in response or "EC20" in response:
                self.is_quectel = True
                logger.info("检测到Quectel调制解调器")

            # 设置短信存储
            await self._send_at_command('AT+CPMS="SM","SM","SM"')

            # 测试UCS2模式
            if await self._test_ucs2_mode():
                logger.info(f"✅ 连接到调制解调器: {self.port}")
                logger.info("✅ UCS2模式测试成功")
                return True
            else:
                logger.error("❌ UCS2模式测试失败")
                return False

        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    async def _test_ucs2_mode(self) -> bool:
        """测试UCS2模式"""
        try:
            # 设置文本模式
            response = await self._send_at_command("AT+CMGF=1")
            if "OK" not in response:
                logger.warning("文本模式设置失败，尝试PDU模式")
                # 尝试PDU模式
                response = await self._send_at_command("AT+CMGF=0")
                return "OK" in response

            # 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"')
            return "OK" in response
        except:
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 全部使用UCS2编码
        """
        message_id = str(uuid.uuid4())

        if not self.serial or not self.serial.is_open:
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message="调制解调器未连接"
            )

        logger.info(f"📱 发送短信到: {phone_number}")
        logger.info(f"📝 内容长度: {len(content)} 字符")

        # 检查是否需要分片（长短信）
        if len(content) > self.UCS2_MAX_LENGTH:
            return await self._send_long_sms(phone_number, content, message_id)
        else:
            return await self._send_single_sms(phone_number, content, message_id)

    async def _send_single_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送单条短信"""
        try:
            logger.info(f"📤 发送单条短信...")

            # 重置调制解调器状态
            await self._send_at_command("AT")
            await self._send_at_command("ATE0")

            # 首先尝试文本模式
            response = await self._send_at_command("AT+CMGF=1", wait_time=1.0)
            if "OK" in response:
                # 文本模式成功
                return await self._send_text_ucs2_sms(phone_number, content, message_id)
            else:
                # 文本模式失败，尝试PDU模式
                logger.info("文本模式失败，尝试PDU模式...")
                return await self._send_pdu_ucs2_sms(phone_number, content, message_id)

        except Exception as e:
            logger.error(f"发送短信异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}"
            )

    async def _send_text_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送文本UCS2短信"""
        try:
            logger.info(f"🔧 使用文本UCS2模式...")

            # 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"', wait_time=1.0)
            if "OK" not in response:
                logger.warning("设置UCS2编码失败，尝试直接发送...")

            # 准备电话号码（去掉+号）
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 尝试直接发送（不转换电话号码为UCS2）
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                logger.warning("未收到提示符，尝试其他方式...")
                # 尝试不同的格式
                cmd = f'AT+CMGS={formatted_number}'
                response = await self._send_at_command(cmd, wait_time=2.0)
                if ">" not in response:
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="未收到发送提示符"
                    )

            # 发送内容
            # 首先尝试发送原始字节
            try:
                content_bytes = content.encode('utf-16-be')
                self.serial.write(content_bytes)
                self.serial.write(b'\x1A')
            except:
                # 如果字节发送失败，尝试发送文本
                self.serial.write(content.encode('utf-8', errors='ignore'))
                self.serial.write(b'\x1A')

            # 等待响应
            await asyncio.sleep(10)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            logger.debug(f"文本UCS2响应: {response[:200]}")

            if '+CMGS:' in response or 'OK' in response:
                logger.info(f"✅ 短信发送成功")
                # 提取参考号
                ref_num = "0"
                if '+CMGS:' in response:
                    match = re.search(r'\+CMGS:\s*(\d+)', response)
                    if match:
                        ref_num = match.group(1)

                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            else:
                logger.error(f"❌ 发送失败，响应: {response}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {response[:100]}"
                )

        except Exception as e:
            logger.error(f"文本UCS2发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"文本UCS2异常: {str(e)}"
            )

    async def _send_pdu_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送PDU UCS2短信（简化版）"""
        try:
            logger.info(f"🔧 使用PDU UCS2模式...")

            # 设置PDU模式
            response = await self._send_at_command("AT+CMGF=0", wait_time=1.0)
            if "OK" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="设置PDU模式失败"
                )

            # 构建极简化的PDU
            pdu = self._build_minimal_pdu(phone_number, content)
            if not pdu:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="PDU构建失败"
                )

            # 发送PDU长度
            pdu_length = len(pdu) // 2
            cmd = f"AT+CMGS={pdu_length}"
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                logger.warning("未收到PDU提示符")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到PDU提示符"
                )

            # 发送PDU数据
            logger.debug(f"发送PDU数据，长度: {pdu_length} 字节")
            self.serial.write(pdu.encode() + b'\x1A')

            # 等待响应
            await asyncio.sleep(15)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            logger.debug(f"PDU响应: {response[:200]}")

            if '+CMGS:' in response or 'OK' in response:
                logger.info(f"✅ PDU短信发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="PDU短信发送成功",
                    data="pdu_success"
                )
            else:
                logger.error(f"❌ PDU发送失败，响应: {response}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="PDU发送失败"
                )

        except Exception as e:
            logger.error(f"PDU发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"PDU异常: {str(e)}"
            )

    def _build_minimal_pdu(self, phone_number: str, content: str) -> str:
        """构建极简化的PDU"""
        try:
            # 服务中心地址（留空）
            sca = "00"

            # PDU类型
            pdu_type = "01"

            # 目标地址
            phone = phone_number
            if phone.startswith('+'):
                phone = phone[1:]

            # 电话号码长度
            phone_len = f"{len(phone):02X}"
            # 电话号码类型（国际）
            phone_type = "91"

            # 反转电话号码
            phone_rev = ""
            if len(phone) % 2 == 1:
                phone += "F"
            for i in range(0, len(phone), 2):
                phone_rev += phone[i+1] + phone[i]

            # 协议标识
            pid = "00"

            # 数据编码方案（UCS2）
            dcs = "08"

            # 有效期
            vp = "AA"

            # 用户数据（UCS2编码）
            content_bytes = content.encode('utf-16-be')
            content_hex = content_bytes.hex().upper()

            # 用户数据长度
            udl = f"{len(content_bytes):02X}"

            # 构建PDU
            pdu = f"{sca}{pdu_type}{phone_len}{phone_type}{phone_rev}{pid}{dcs}{vp}{udl}{content_hex}"

            logger.debug(f"PDU构建完成: {pdu[:50]}...")
            return pdu

        except Exception as e:
            logger.error(f"PDU构建失败: {e}")
            return ""

    async def _send_long_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送长短信（分片）"""
        logger.info(f"📦 检测到长短信，需要分片发送...")

        # 简单分割短信
        fragments = []
        for i in range(0, len(content), self.UCS2_MAX_LENGTH):
            fragment = content[i:i + self.UCS2_MAX_LENGTH]
            fragments.append(fragment)

        logger.info(f"📊 分割为 {len(fragments)} 个分片")

        results = []
        for i, fragment in enumerate(fragments):
            logger.info(f"📤 发送分片 {i+1}/{len(fragments)}: {len(fragment)} 字符")

            # 添加分片指示
            if len(fragments) > 1:
                fragment_with_indicator = f"({i+1}/{len(fragments)}) {fragment}"
            else:
                fragment_with_indicator = fragment

            result = await self._send_single_sms(phone_number, fragment_with_indicator, f"{message_id}_{i+1}")
            results.append(result)

            # 如果不是最后一个分片，等待一下
            if i < len(fragments) - 1:
                await asyncio.sleep(2)

        # 汇总结果
        success_count = sum(1 for r in results if r.success)

        if success_count == len(fragments):
            logger.info(f"✅ 所有 {len(fragments)} 个分片发送成功")
            return SMSResult(
                message_id=message_id,
                success=True,
                status_code=200,
                status_message=f"长短信发送成功 ({len(fragments)}个分片)",
                data=f"{len(fragments)}"
            )
        elif success_count > 0:
            logger.warning(f"⚠️ 部分分片发送成功: {success_count}/{len(fragments)}")
            return SMSResult(
                message_id=message_id,
                success=True,  # 部分成功也算成功
                status_code=206,
                status_message=f"部分分片发送成功 ({success_count}/{len(fragments)})",
                data=f"{success_count}/{len(fragments)}"
            )
        else:
            logger.error(f"❌ 所有分片发送失败")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"所有分片发送失败",
                data=f"0/{len(fragments)}"
            )

    async def _send_at_command(self, command: str, wait_time: float = 1.0) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            # 清空输入缓冲区
            self.serial.reset_input_buffer()

            # 发送命令
            if self._debug_mode:
                logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r\n".encode())

            # 等待响应
            await asyncio.sleep(wait_time)

            # 读取响应
            response_bytes = self.serial.read_all()
            response = response_bytes.decode('utf-8', errors='ignore').strip()

            if self._debug_mode and response:
                logger.debug(f"AT响应: {response}")

            return response

        except Exception as e:
            logger.error(f"发送AT命令失败: {command} - {e}")
            return ""

    async def test_connection(self) -> bool:
        """测试调制解调器连接"""
        try:
            response = await self._send_at_command("AT", 1.0)
            return 'OK' in response
        except Exception:
            return False

    async def get_signal_strength(self) -> Optional[int]:
        """获取信号强度"""
        try:
            response = await self._send_at_command("AT+CSQ")
            if '+CSQ:' in response:
                match = re.search(r'\+CSQ:\s*(\d+)', response)
                if match:
                    return int(match.group(1))
        except Exception as e:
            logger.warning(f"获取信号强度失败: {e}")

        return None

    async def disconnect(self):
        """断开连接"""
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info(f"断开调制解调器连接: {self.port}")
