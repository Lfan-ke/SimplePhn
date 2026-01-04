"""
SMS短信发送器 - UTF-8统一编码版本
处理中英文混合的UTF-8短信
"""
import asyncio
import time
import uuid
import re
import binascii
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
    """短信发送器 - 统一使用UCS2/UTF-16BE编码处理UTF-8短信"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.is_quectel = False  # 标记是否为Quectel调制解调器

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
            response = await self._send_at_command("AT", wait_time=1.0)
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

            logger.info(f"✅ 连接到调制解调器: {self.port}")
            return True

        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    async def disconnect(self):
        """断开连接"""
        if self.serial and self.serial.is_open:
            try:
                self.serial.close()
                self.serial = None
                logger.info(f"✅ 断开调制解调器连接: {self.port}")
            except Exception as e:
                logger.error(f"断开连接失败: {e}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 统一使用UCS2编码处理UTF-8内容

        所有短信内容都是UTF-8编码的，包含中英文混合。
        统一转换为UCS2/UTF-16BE编码发送。
        """
        message_id = str(uuid.uuid4())

        if not self.serial or not self.serial.is_open:
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message="调制解调器未连接"
            )

        try:
            logger.info(f"📱 发送短信到: {phone_number}")
            logger.info(f"📝 内容长度: {len(content)} 字符")
            logger.info(f"📄 内容预览: {content[:50]}...")

            # 统一使用UCS2编码发送
            return await self._send_ucs2_sms(phone_number, content, message_id)

        except Exception as e:
            logger.error(f"短信发送异常: {e}")
            import traceback
            traceback.print_exc()
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}"
            )

    async def _send_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """
        使用UCS2编码发送短信
        支持中英文混合的UTF-8内容
        """
        try:
            # 1. 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 2. 设置UCS2编码
            await self._send_at_command('AT+CSCS="UCS2"')

            # 3. 设置UCS2短信参数
            # 对于Quectel调制解调器，需要特定的参数
            if self.is_quectel:
                await self._send_at_command('AT+CSMP=17,167,0,8')
            else:
                await self._send_at_command('AT+CSMP=17,167,0,8')

            # 4. 准备电话号码和内容
            # 去除电话号码的+号
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 确保内容是有效的UTF-8
            try:
                content.encode('utf-8').decode('utf-8')
            except UnicodeError:
                logger.warning("内容包含无效UTF-8字符，进行清理...")
                content = content.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')

            # 5. 发送短信
            logger.info(f"📤 发送UCS2短信...")

            # 发送AT+CMGS命令
            # 注意：对于UCS2编码，电话号码也需要用UCS2格式
            phone_ucs2 = ""
            for char in formatted_number:
                phone_ucs2 += f"{ord(char):04X}"

            cmd = f'AT+CMGS="{phone_ucs2}"'

            if self.is_quectel:
                # Quectel需要指定消息类型为UCS2 (145 = 0x91)
                cmd = f'AT+CMGS="{phone_ucs2}",145'

            logger.debug(f"发送命令: {cmd}")
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response and ">" not in self.serial.read_all().decode('utf-8', errors='ignore'):
                logger.error(f"❌ 未收到提示符，响应: {response}")
                # 尝试直接发送内容
                return await self._send_simple_ucs2(phone_number, content, message_id)

            # 6. 发送UCS2编码的内容
            # 将UTF-8内容转换为UCS2/UTF-16BE
            content_ucs2 = ""
            try:
                # 方法1: 使用UTF-16BE编码
                content_bytes = content.encode('utf-16-be')
                content_hex = content_bytes.hex().upper()

                # 确保十六进制字符串长度为偶数
                if len(content_hex) % 2 != 0:
                    content_hex += "0"

                content_ucs2 = content_hex

            except Exception as e:
                logger.error(f"UTF-16BE编码失败: {e}")
                # 方法2: 手动转换每个字符
                content_ucs2 = ""
                for char in content:
                    try:
                        code = ord(char)
                        content_ucs2 += f"{code:04X}"
                    except:
                        # 替换无法编码的字符
                        content_ucs2 += "003F"  # 问号

            logger.debug(f"UCS2内容长度: {len(content_ucs2)} 十六进制字符")

            # 发送内容
            self.serial.write(content_ucs2.encode() + b'\x1A')
            logger.debug(f"已发送UCS2内容，等待响应...")

            # 7. 等待响应（UCS2短信需要更长时间）
            await asyncio.sleep(8)

            # 读取响应
            response = self.serial.read_all().decode('utf-8', errors='ignore')
            logger.debug(f"响应: {response}")

            # 8. 解析响应
            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"

                logger.info(f"✅ UCS2短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            elif 'ERROR' in response or 'CMS ERROR' in response:
                error_msg = response.split('\n')[-1].strip()
                logger.error(f"❌ 发送错误: {error_msg}")

                # 尝试替代方法
                return await self._send_simple_ucs2(phone_number, content, message_id)

            else:
                logger.warning(f"⚠️  未收到标准响应: {response[:200]}")
                # 检查是否有隐含的成功
                if 'OK' in response or not response.strip():
                    logger.info("✅ 短信可能发送成功（收到OK或无错误）")
                    return SMSResult(
                        message_id=message_id,
                        success=True,
                        status_code=200,
                        status_message="短信可能发送成功",
                        data="unknown"
                    )
                else:
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message=f"意外响应: {response[:100]}"
                    )

        except Exception as e:
            logger.error(f"UCS2发送异常: {e}")
            # 尝试最后的手段
            return await self._send_simple_ucs2(phone_number, content, message_id)

    async def _send_simple_ucs2(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """
        简化的UCS2发送方法
        作为备用方案
        """
        try:
            logger.info("尝试简化UCS2发送方法...")

            # 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 设置GSM编码（回退方案）
            await self._send_at_command('AT+CSCS="GSM"')

            # 发送短信
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="简化方法失败：未收到提示符"
                )

            # 发送内容（使用ASCII安全版本）
            safe_content = ""
            for char in content:
                if ord(char) < 128:
                    safe_content += char
                else:
                    safe_content += "?"  # 替换非ASCII字符

            self.serial.write(safe_content.encode() + b'\x1A')
            await asyncio.sleep(5)

            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"

                logger.info(f"✅ 简化方法发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功（简化方法）",
                    data=ref_num
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"简化方法失败: {response[:100]}"
                )

        except Exception as e:
            logger.error(f"简化方法异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"所有发送方法都失败: {str(e)}"
            )

    async def _send_at_command(self, command: str, wait_time: float = 1.0) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            # 清空输入缓冲区
            self.serial.reset_input_buffer()

            # 发送命令
            logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r\n".encode())

            # 等待响应
            await asyncio.sleep(wait_time)

            # 读取响应
            response_bytes = self.serial.read_all()
            response = response_bytes.decode('utf-8', errors='ignore')

            # 记录调试信息
            if response.strip() and not response.strip().endswith("OK"):
                logger.debug(f"AT命令响应: {response.strip()}")

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
