"""
SMS短信发送器 - 简化版，全部使用UCS2编码
"""
import asyncio
import time
import uuid
import re
from dataclasses import dataclass, field
from typing import Optional
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
    """短信发送器 - 全部使用UCS2编码，直接发送不分片"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.is_quectel = False
        self._debug_mode = False

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

            # 测试基本AT命令
            logger.info(f"✅ 连接到调制解调器: {self.port}")
            return True

        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 全部使用UCS2编码，直接发送不分片
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
        logger.info(f"📄 内容预览: {content[:80]}...")

        # 直接发送，不分片（调制解调器会自动处理长短信）
        return await self._send_simple_sms(phone_number, content, message_id)

    async def _send_simple_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """简单发送短信 - 使用文本模式"""
        try:
            # 重置调制解调器状态
            await self._send_at_command("AT")
            await self._send_at_command("ATE0")

            # 设置文本模式
            response = await self._send_at_command("AT+CMGF=1", wait_time=1.0)
            if "OK" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="设置文本模式失败"
                )

            # 根据内容决定字符集：若包含非 ASCII 字符，则使用 UCS2（可以支持中文/Emoji）
            use_ucs2 = any(ord(ch) > 127 for ch in content)
            if use_ucs2:
                response = await self._send_at_command('AT+CSCS="UCS2"', wait_time=1.0)
                if "OK" not in response:
                    logger.warning("设置 CSCS 为 UCS2 失败，尝试继续（调制解调器可能不支持 UCS2）")
            else:
                response = await self._send_at_command('AT+CSCS="GSM"', wait_time=1.0)
                if "OK" not in response:
                    logger.debug("未能设置 CSCS 为 GSM，使用调制解调器默认字符集")

            # 准备电话号码（去掉+号）
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 发送AT+CMGS命令
            cmd = f'AT+CMGS="{formatted_number}"'

            # 尝试多次发送命令，确保收到提示符
            # 等待 '>' 提示符，最多尝试 3 次
            for attempt in range(3):
                response = await self._send_at_command(cmd, wait_time=5.0, expect='>')

                if ">" in response:
                    logger.info("✅ 收到发送提示符 >")
                    break
                elif attempt == 2:
                    logger.error("❌ 未收到发送提示符")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="未收到发送提示符"
                    )
                else:
                    logger.warning(f"⚠️ 第{attempt+1}次尝试未收到提示符，重试...")
                    await asyncio.sleep(1)

            # 发送短信内容（UTF-8编码）
            logger.info("📤 发送短信内容...")

            # 根据字符集决定发送格式：
            # - UCS2: 发送 UTF-16BE 的十六进制表示（多数调制解调器在文本模式下要求以 hex 形式发送 UCS2）
            # - 非 UCS2: 直接发送 UTF-8（对基本 GSM/ASCII 文本多数调制解调器兼容）
            try:
                if use_ucs2:
                    hex_payload = content.encode('utf-16-be').hex().upper()
                    self.serial.write(hex_payload.encode('ascii'))
                else:
                    self.serial.write(content.encode('utf-8'))

                self.serial.write(b'\x1A')  # Ctrl+Z 结束
            except Exception as e:
                logger.error(f"发送内容失败: {e}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送内容失败: {e}"
                )

            # 等待并读取响应（长短信需要更多时间），循环读取直到出现终结标志或超时
            wait_time = min(30, 5 + len(content) // 20)  # 根据内容长度动态调整等待时间
            logger.info(f"⏳ 等待响应 ({wait_time}秒)...")
            deadline = time.time() + wait_time
            buffer = b""
            response = ""
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                chunk = self.serial.read_all()
                if chunk:
                    buffer += chunk
                    try:
                        response = buffer.decode('utf-8', errors='ignore')
                    except Exception:
                        response = buffer.decode('latin1', errors='ignore')

                    logger.debug(f"响应片段: {response[:200]}")

                    if any(k in response for k in ("+CMGS:", "OK", "ERROR", "+CMS ERROR:")):
                        break

            # 检查响应
            if '+CMGS:' in response:
                # 提取消息参考号
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ 短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            elif 'OK' in response:
                logger.info("✅ 短信可能发送成功 (收到OK)")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (收到OK)",
                    data="ok"
                )
            elif 'ERROR' in response or '+CMS ERROR:' in response:
                error_match = re.search(r'\+CMS ERROR:\s*(\d+)', response)
                error_code = error_match.group(1) if error_match else "未知"
                logger.error(f"❌ 短信发送失败，错误代码: {error_code}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败，错误代码: {error_code}"
                )
            else:
                logger.warning(f"⚠️ 未知响应: {response[:100]}")
                # 如果有响应但不确定是否成功，先认为是成功的
                if response:
                    logger.info("✅ 假设短信发送成功 (有响应)")
                    return SMSResult(
                        message_id=message_id,
                        success=True,
                        status_code=200,
                        status_message="短信发送成功 (假设)",
                        data="assumed"
                    )
                else:
                    logger.error("❌ 无响应")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="无响应"
                    )

        except Exception as e:
            logger.error(f"发送短信异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}"
            )

    async def _send_at_command(self, command: str, wait_time: float = 1.0, expect: Optional[str] = None) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            # 清空输入缓冲区并发送命令
            self.serial.reset_input_buffer()
            if self._debug_mode:
                logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r\n".encode())

            # 主动轮询读取，直到超时或收到期望内容（如 '>', 'OK', 'ERROR' 等）
            deadline = time.time() + wait_time
            buffer = b""
            while time.time() < deadline:
                await asyncio.sleep(0.1)
                chunk = self.serial.read_all()
                if chunk:
                    buffer += chunk
                    try:
                        text = buffer.decode('utf-8', errors='ignore')
                    except Exception:
                        text = buffer.decode('latin1', errors='ignore')

                    if self._debug_mode and text:
                        logger.debug(f"AT部分响应: {text[:200]}")

                    # 如果 caller 指定了期望字符串，则优先匹配
                    if expect and expect in text:
                        return text.strip()

                    # 否则检测常见终结标志
                    if any(k in text for k in ("OK", "ERROR", ">", "+CMGS:", "+CMS ERROR:")):
                        return text.strip()

            # 超时，返回已读取的数据（可能为空）
            try:
                return buffer.decode('utf-8', errors='ignore').strip()
            except Exception:
                return buffer.decode('latin1', errors='ignore').strip()

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
