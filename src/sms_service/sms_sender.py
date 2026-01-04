"""
SMS短信发送器 - 修复UCS2编码问题
"""
import asyncio
import time
import uuid
import re
import binascii
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
    """短信发送器 - 优化UCS2/UTF-16BE编码处理"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.is_quectel = False  # 标记是否为Quectel调制解调器
        self._last_successful_method = None  # 记录最后成功的方法

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

            # 测试各种短信模式
            test_results = await self._test_sms_modes()
            if test_results:
                logger.info(f"✅ 连接到调制解调器: {self.port}")
                logger.info(f"✅ 测试成功的方法: {self._last_successful_method}")
                return True
            else:
                logger.error("❌ 所有短信模式测试失败")
                return False

        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    async def _test_sms_modes(self) -> bool:
        """测试各种短信模式，找到可用的方法"""
        test_methods = [
            ("PDU_UCS2", self._test_pdu_ucs2_mode),
            ("TEXT_UCS2", self._test_text_ucs2_mode),
            ("PDU_GSM", self._test_pdu_gsm_mode),
            ("TEXT_GSM", self._test_text_gsm_mode),
        ]

        for method_name, test_func in test_methods:
            try:
                logger.info(f"测试短信模式: {method_name}")
                if await test_func():
                    self._last_successful_method = method_name
                    logger.info(f"✅ 模式 {method_name} 测试成功")
                    return True
                else:
                    logger.warning(f"❌ 模式 {method_name} 测试失败")
            except Exception as e:
                logger.debug(f"模式 {method_name} 测试异常: {e}")

        return False

    async def _test_pdu_ucs2_mode(self) -> bool:
        """测试PDU UCS2模式"""
        try:
            # 设置PDU模式
            response = await self._send_at_command("AT+CMGF=0")
            if "OK" not in response:
                return False

            # 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"')
            return "OK" in response
        except:
            return False

    async def _test_text_ucs2_mode(self) -> bool:
        """测试文本UCS2模式"""
        try:
            # 设置文本模式
            response = await self._send_at_command("AT+CMGF=1")
            if "OK" not in response:
                return False

            # 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"')
            return "OK" in response
        except:
            return False

    async def _test_pdu_gsm_mode(self) -> bool:
        """测试PDU GSM模式"""
        try:
            # 设置PDU模式
            response = await self._send_at_command("AT+CMGF=0")
            if "OK" not in response:
                return False

            # 设置GSM编码
            response = await self._send_at_command('AT+CSCS="GSM"')
            return "OK" in response
        except:
            return False

    async def _test_text_gsm_mode(self) -> bool:
        """测试文本GSM模式"""
        try:
            # 设置文本模式
            response = await self._send_at_command("AT+CMGF=1")
            if "OK" not in response:
                return False

            # 设置GSM编码
            response = await self._send_at_command('AT+CSCS="GSM"')
            return "OK" in response
        except:
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
        发送短信 - 根据测试结果选择最佳发送方法
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

        # 根据最后成功的方法选择发送方式
        if self._last_successful_method == "PDU_UCS2":
            return await self._send_pdu_ucs2_sms(phone_number, content, message_id)
        elif self._last_successful_method == "TEXT_UCS2":
            return await self._send_text_ucs2_sms(phone_number, content, message_id)
        elif self._last_successful_method == "PDU_GSM":
            return await self._send_pdu_gsm_sms(phone_number, content, message_id)
        elif self._last_successful_method == "TEXT_GSM":
            return await self._send_text_gsm_sms(phone_number, content, message_id)
        else:
            # 尝试所有方法
            logger.warning("⚠️ 未找到已知的成功方法，尝试所有方法...")
            return await self._try_all_methods(phone_number, content, message_id)

    async def _send_text_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送文本模式的UCS2短信"""
        try:
            # 1. 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 2. 设置UCS2编码
            await self._send_at_command('AT+CSCS="UCS2"')

            # 3. 准备电话号码
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 4. 发送AT+CMGS命令
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                # 如果没有收到提示符，尝试直接发送
                await self._send_at_command(content + "\x1A", wait_time=5.0)
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信可能已发送",
                    data="unknown"
                )

            # 5. 发送UCS2编码的内容
            try:
                # 转换为UCS2编码（UTF-16BE）
                content_bytes = content.encode('utf-16-be')
                self.serial.write(content_bytes)
                self.serial.write(b'\x1A')
            except:
                # 如果UTF-16BE失败，使用ASCII替代
                safe_content = "".join(c if ord(c) < 128 else "?" for c in content)
                self.serial.write(safe_content.encode() + b'\x1A')

            # 6. 等待响应
            await asyncio.sleep(8)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ 文本UCS2短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            elif 'OK' in response:
                logger.info("✅ 文本UCS2短信可能发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信可能发送成功",
                    data="unknown"
                )
            else:
                error_msg = response.split('\n')[-1].strip() if response else "未知错误"
                logger.error(f"❌ 文本UCS2发送失败: {error_msg}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {error_msg}"
                )

        except Exception as e:
            logger.error(f"文本UCS2发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"异常: {str(e)}"
            )

    async def _send_text_gsm_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送文本模式的GSM短信"""
        try:
            # 1. 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 2. 设置GSM编码
            await self._send_at_command('AT+CSCS="GSM"')

            # 3. 准备电话号码
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 4. 发送AT+CMGS命令
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到提示符"
                )

            # 5. 发送内容（过滤非GSM字符）
            gsm_chars = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1BÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
                       "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")

            safe_content = ""
            for char in content:
                if char in gsm_chars:
                    safe_content += char
                else:
                    safe_content += "?"  # 替换不支持的字

            self.serial.write(safe_content.encode() + b'\x1A')

            # 6. 等待响应
            await asyncio.sleep(5)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ 文本GSM短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {response[:100]}"
                )

        except Exception as e:
            logger.error(f"文本GSM发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"异常: {str(e)}"
            )

    async def _try_all_methods(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """尝试所有发送方法"""
        methods = [
            ("文本UCS2", self._send_text_ucs2_sms),
            ("文本GSM", self._send_text_gsm_sms),
            ("简单文本", self._send_simple_text),
        ]

        for method_name, method_func in methods:
            try:
                logger.info(f"尝试方法: {method_name}")
                result = await method_func(phone_number, content, message_id)
                if result.success:
                    self._last_successful_method = method_name
                    logger.info(f"✅ 方法 {method_name} 成功")
                    return result
                else:
                    logger.warning(f"❌ 方法 {method_name} 失败: {result.status_message}")
            except Exception as e:
                logger.error(f"方法 {method_name} 异常: {e}")

        return SMSResult(
            message_id=message_id,
            success=False,
            status_code=500,
            status_message="所有发送方法都失败"
        )

    async def _send_simple_text(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送最简单的文本短信"""
        try:
            # 重置到默认设置
            await self._send_at_command("ATZ")
            await asyncio.sleep(2)

            # 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 设置GSM编码
            await self._send_at_command('AT+CSCS="GSM"')

            # 准备电话号码
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 发送短信命令
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到提示符"
                )

            # 发送简化内容（仅ASCII）
            simple_content = "".join(c if ord(c) < 128 else " " for c in content[:140])
            self.serial.write(simple_content.encode() + b'\x1A')

            # 等待响应
            await asyncio.sleep(5)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response or 'OK' in response:
                logger.info("✅ 简单文本短信发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data="simple_text"
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"简单方法失败: {response[:100]}"
                )
        except Exception as e:
            logger.error(f"简单文本发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"异常: {str(e)}"
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
            response = response_bytes.decode('utf-8', errors='ignore').strip()

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

    # 占位符方法，后续实现
    async def _send_pdu_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        return await self._send_text_ucs2_sms(phone_number, content, message_id)

    async def _send_pdu_gsm_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        return await self._send_text_gsm_sms(phone_number, content, message_id)
