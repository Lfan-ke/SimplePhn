"""
SMS短信发送器 - 添加调试和修复
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
    method_used: str = "unknown"  # 记录使用的方法


class SMSSender:
    """短信发送器 - 修复UCS2发送问题"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self.is_quectel = False
        self._last_successful_method = None
        self._debug_mode = False  # 调试模式

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 优先使用PDU_UCS2模式
        """
        message_id = str(uuid.uuid4())

        if not self.serial or not self.serial.is_open:
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message="调制解调器未连接",
                method_used="none"
            )

        logger.info(f"📱 发送短信到: {phone_number}")
        logger.info(f"📝 内容长度: {len(content)} 字符")
        logger.info(f"📄 内容预览: {content[:60]}...")

        # 根据最后成功的方法选择发送方式
        if self._last_successful_method == "PDU_UCS2":
            return await self._send_real_pdu_ucs2_sms(phone_number, content, message_id)
        elif self._last_successful_method == "TEXT_UCS2":
            return await self._send_real_text_ucs2_sms(phone_number, content, message_id)
        elif self._last_successful_method == "PDU_GSM":
            return await self._send_real_pdu_gsm_sms(phone_number, content, message_id)
        elif self._last_successful_method == "TEXT_GSM":
            return await self._send_real_text_gsm_sms(phone_number, content, message_id)
        else:
            # 尝试所有方法
            logger.warning("⚠️ 未找到已知的成功方法，尝试PDU UCS2模式...")
            return await self._try_real_methods(phone_number, content, message_id)

    async def _send_real_pdu_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """真正的PDU UCS2发送"""
        try:
            logger.info(f"🔧 使用PDU UCS2模式发送...")

            # 1. 设置PDU模式
            await self._send_at_command("AT+CMGF=0")

            # 2. 设置UCS2编码
            await self._send_at_command('AT+CSCS="UCS2"')

            # 3. 准备PDU数据
            pdu_data = await self._build_pdu_ucs2(phone_number, content)
            if not pdu_data:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="PDU数据构建失败",
                    method_used="pdu_ucs2"
                )

            # 4. 发送PDU长度
            pdu_length = len(pdu_data) // 2  # PDU长度是字节数
            cmd = f"AT+CMGS={pdu_length}"
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                logger.warning("未收到PDU提示符，尝试文本模式...")
                return await self._send_real_text_ucs2_sms(phone_number, content, message_id)

            # 5. 发送PDU数据
            self.serial.write((pdu_data + "\x1A").encode())

            # 6. 等待响应（PDU模式需要更长时间）
            await asyncio.sleep(15)  # PDU UCS2需要更长时间

            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if self._debug_mode:
                logger.debug(f"PDU响应: {response}")

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ PDU UCS2短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (PDU UCS2)",
                    data=ref_num,
                    method_used="pdu_ucs2"
                )
            elif 'OK' in response:
                logger.info("✅ PDU UCS2短信可能发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信可能发送成功 (PDU UCS2)",
                    data="pending",
                    method_used="pdu_ucs2"
                )
            else:
                error_msg = response.split('\n')[-1].strip() if response else "未知错误"
                logger.error(f"❌ PDU UCS2发送失败: {error_msg}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"PDU UCS2失败: {error_msg}",
                    method_used="pdu_ucs2"
                )

        except Exception as e:
            logger.error(f"PDU UCS2发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"PDU UCS2异常: {str(e)}",
                method_used="pdu_ucs2"
            )

    async def _build_pdu_ucs2(self, phone_number: str, content: str) -> str:
        """构建PDU UCS2数据"""
        try:
            # 简化PDU构建 - 只包含基本字段
            # 实际的PDU构建比较复杂，这里先返回简单的测试PDU
            sca = "00"  # 服务中心地址（留空）
            pdu_type = "01"  # 发送方地址类型

            # 电话号码（国际格式）
            phone = phone_number
            if phone.startswith('+'):
                phone = phone[1:]

            # 电话号码长度和类型
            phone_len = f"{len(phone):02X}"
            phone_type = "91"  # 国际号码

            # 反转电话号码（PDU格式）
            phone_rev = ""
            if len(phone) % 2 == 1:
                phone += "F"
            for i in range(0, len(phone), 2):
                phone_rev += phone[i+1] + phone[i]

            # 协议标识
            pid = "00"

            # 数据编码方案 (UCS2 = 0x08)
            dcs = "08"

            # 有效期
            vp = "AA"

            # 用户数据（UCS2编码）
            content_bytes = content.encode('utf-16-be')
            content_hex = content_bytes.hex().upper()

            # 用户数据长度
            udl = f"{len(content_bytes):02X}"

            # 构建完整PDU
            pdu = f"{sca}{pdu_type}{phone_len}{phone_type}{phone_rev}{pid}{dcs}{vp}{udl}{content_hex}"

            logger.debug(f"PDU构建: {pdu[:100]}...")
            return pdu

        except Exception as e:
            logger.error(f"PDU构建失败: {e}")
            return ""

    async def _send_real_text_ucs2_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """真正的文本UCS2发送"""
        try:
            logger.info(f"🔧 使用文本UCS2模式发送...")

            # 1. 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 2. 设置UCS2编码
            await self._send_at_command('AT+CSCS="UCS2"')

            # 3. 准备电话号码
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 4. 对于UCS2，电话号码也需要转换
            phone_ucs2 = ""
            for char in formatted_number:
                phone_ucs2 += f"{ord(char):04X}"

            cmd = f'AT+CMGS="{phone_ucs2}"'
            response = await self._send_at_command(cmd, wait_time=3.0)

            if ">" not in response:
                logger.warning("未收到文本提示符")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到提示符",
                    method_used="text_ucs2"
                )

            # 5. 发送UCS2内容
            content_bytes = content.encode('utf-16-be')
            content_hex = content_bytes.hex().upper()

            # 先发送十六进制格式
            self.serial.write(content_hex.encode())
            self.serial.write(b'\x1A')

            # 6. 等待响应
            await asyncio.sleep(12)

            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if self._debug_mode:
                logger.debug(f"文本UCS2响应: {response}")

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ 文本UCS2短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (文本UCS2)",
                    data=ref_num,
                    method_used="text_ucs2"
                )
            else:
                # 尝试发送原始字节
                self.serial.reset_input_buffer()
                await self._send_at_command("AT+CMGF=1")
                await self._send_at_command('AT+CSCS="UCS2"')

                response2 = await self._send_at_command(cmd, wait_time=2.0)
                if ">" in response2:
                    self.serial.write(content_bytes)
                    self.serial.write(b'\x1A')
                    await asyncio.sleep(12)
                    response3 = self.serial.read_all().decode('utf-8', errors='ignore')

                    if '+CMGS:' in response3 or 'OK' in response3:
                        logger.info("✅ 文本UCS2短信发送成功（字节方式）")
                        return SMSResult(
                            message_id=message_id,
                            success=True,
                            status_code=200,
                            status_message="短信发送成功 (文本UCS2 字节)",
                            data="unknown",
                            method_used="text_ucs2_bytes"
                        )

                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="文本UCS2发送失败",
                    method_used="text_ucs2"
                )

        except Exception as e:
            logger.error(f"文本UCS2发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"文本UCS2异常: {str(e)}",
                method_used="text_ucs2"
            )

    async def _try_real_methods(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """尝试所有真实方法"""
        methods = [
            ("文本GSM", self._send_real_text_gsm_sms),
            ("简化文本", self._send_simple_text_sms),
        ]

        for method_name, method_func in methods:
            try:
                logger.info(f"尝试方法: {method_name}")
                result = await method_func(phone_number, content, message_id)
                if result.success:
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
            status_message="所有发送方法都失败",
            method_used="all_failed"
        )

    async def _send_real_text_gsm_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送文本GSM短信"""
        try:
            # 确保调制解调器准备好
            await self._send_at_command("AT")
            await self._send_at_command("ATE0")

            # 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 设置GSM编码
            await self._send_at_command('AT+CSCS="GSM"')

            # 准备电话号码
            formatted_number = phone_number
            if formatted_number.startswith('+'):
                formatted_number = formatted_number[1:]

            # 发送命令
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到GSM提示符",
                    method_used="text_gsm"
                )

            # 过滤内容为GSM字符集
            gsm_chars = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1BÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
                       "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")

            safe_content = ""
            for char in content:
                if char in gsm_chars:
                    safe_content += char
                elif ord(char) < 128:
                    safe_content += char
                else:
                    safe_content += "?"  # 替换不支持字符

            # 发送内容
            self.serial.write(safe_content.encode() + b'\x1A')

            # 等待响应
            await asyncio.sleep(8)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response or 'OK' in response:
                logger.info("✅ GSM短信发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (GSM)",
                    data="gsm",
                    method_used="text_gsm"
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="GSM发送失败",
                    method_used="text_gsm"
                )

        except Exception as e:
            logger.error(f"GSM发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"GSM异常: {str(e)}",
                method_used="text_gsm"
            )

    async def _send_simple_text_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        """发送最简单的ASCII文本短信"""
        try:
            # 重置调制解调器
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

            # 发送命令
            cmd = f'AT+CMGS="{formatted_number}"'
            response = await self._send_at_command(cmd, wait_time=2.0)

            if ">" not in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="未收到简单模式提示符",
                    method_used="simple_text"
                )

            # 只发送ASCII字符
            simple_content = "".join(c if ord(c) < 128 else " " for c in content[:140])

            self.serial.write(simple_content.encode() + b'\x1A')

            # 等待响应
            await asyncio.sleep(6)
            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response or 'OK' in response:
                logger.info("✅ 简单文本短信发送成功")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (简单文本)",
                    data="simple",
                    method_used="simple_text"
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="简单文本发送失败",
                    method_used="simple_text"
                )

        except Exception as e:
            logger.error(f"简单文本发送异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"简单文本异常: {str(e)}",
                method_used="simple_text"
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

    async def _send_real_pdu_gsm_sms(self, phone_number: str, content: str, message_id: str) -> SMSResult:
        return await self._send_real_text_gsm_sms(phone_number, content, message_id)
