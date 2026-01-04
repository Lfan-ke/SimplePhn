"""
SMS短信发送器 - 重构版，基于可工作的示例
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


def to_ucs2_hex(s: str) -> str:
    """将字符串转为 UCS2-BE 的十六进制字符串"""
    return s.encode("utf-16-be").hex().upper()


class SMSSender:
    """短信发送器 - 基于可工作的示例重构"""

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
            await asyncio.sleep(2)

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
                return False

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
        发送短信 - 严格按照示例代码
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
        logger.info(f"📄 内容预览: {content[:60]}...")

        try:
            # 1. 重置调制解调器状态
            await self._send_at_command("AT")
            await self._send_at_command("ATE0")

            # 2. 设置文本模式
            response = await self._send_at_command("AT+CMGF=1", wait_time=1.0)
            if "OK" not in response:
                logger.error("设置文本模式失败")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="设置文本模式失败"
                )

            # 3. 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"', wait_time=1.0)
            if "OK" not in response:
                logger.error("设置UCS2编码失败")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="设置UCS2编码失败"
                )

            # 4. 转换电话号码为UCS2十六进制（去掉+号）
            phone_for_conversion = phone_number
            if phone_for_conversion.startswith('+'):
                phone_for_conversion = phone_for_conversion[1:]

            phone_ucs2 = to_ucs2_hex(phone_for_conversion)
            logger.debug(f"电话号码UCS2: {phone_ucs2}")

            # 5. 发送AT+CMGS命令
            cmd = f'AT+CMGS="{phone_ucs2}"'
            logger.debug(f"发送命令: {cmd}")

            # 发送命令
            self.serial.write(f"{cmd}\r".encode())
            await asyncio.sleep(0.5)

            # 读取响应
            response = self.serial.read_all().decode('utf-8', errors='ignore')
            logger.debug(f"AT+CMGS响应: {response}")

            # 检查是否收到提示符
            if ">" not in response:
                logger.warning("未收到>提示符，继续发送...")

            # 6. 转换内容为UCS2十六进制
            text_ucs2 = to_ucs2_hex(content)
            logger.debug(f"内容UCS2 (前100字符): {text_ucs2[:100]}...")

            # 7. 发送UCS2内容
            logger.info("📤 发送短信内容...")
            self.serial.write((text_ucs2 + "\x1A").encode())  # \x1A = Ctrl+Z

            # 8. 等待响应（长短信需要更多时间）
            wait_time = 8  # 基础等待时间
            if len(content) > 70:  # 长短信
                wait_time += (len(content) // 70) * 5
            logger.info(f"⏳ 等待响应 ({wait_time}秒)...")
            await asyncio.sleep(wait_time)

            # 9. 读取最终响应
            response = self.serial.read_all().decode('utf-8', errors='ignore')
            logger.debug(f"最终响应: {response[:200]}")

            # 10. 解析响应
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
                logger.info("✅ 短信发送成功 (收到OK)")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data="ok"
                )
            elif 'ERROR' in response or '+CMS ERROR:' in response:
                error_match = re.search(r'\+CMS ERROR:\s*(\d+)', response)
                error_code = error_match.group(1) if error_match else "未知"
                error_desc = self._get_error_description(error_code)
                logger.error(f"❌ 短信发送失败: {error_desc}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {error_desc}"
                )
            else:
                logger.warning(f"⚠️ 未知响应: {response[:100]}")
                # 检查是否有任何响应
                if response and len(response.strip()) > 0:
                    logger.info("✅ 短信可能发送成功 (有响应)")
                    return SMSResult(
                        message_id=message_id,
                        success=True,
                        status_code=200,
                        status_message="短信发送成功 (有响应)",
                        data="has_response"
                    )
                else:
                    logger.error("❌ 无响应")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="发送超时，无响应"
                    )

        except Exception as e:
            logger.error(f"发送短信异常: {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}"
            )

    def _get_error_description(self, error_code: str) -> str:
        """获取错误代码描述"""
        error_descriptions = {
            "1": "未分配号码",
            "3": "操作不允许",
            "8": "运营商拒绝",
            "10": "CME错误",
            "20": "内存满",
            "21": "索引无效",
            "22": "内存不足",
            "23": "文本字符串太长",
            "24": "文本字符串无效字符",
            "25": "拨号字符串太长",
            "26": "拨号字符串无效字符",
            "27": "没有网络服务",
            "29": "需要SIM卡PIN码",
            "30": "需要SIM卡PUK码",
            "31": "需要SIM卡认证",
            "32": "SIM卡失败",
            "33": "SIM卡忙",
            "34": "SIM卡错误",
            "35": "SIM卡PIN码需要",
            "36": "SIM卡PUK码需要",
            "37": "SIM卡PIN2码需要",
            "38": "SIM卡PUK2码需要",
            "40": "内存失败",
            "41": "网络个人化PIN码需要",
            "42": "网络个人化PUK码需要",
            "43": "网络子集个人化PIN码需要",
            "44": "网络子集个人化PUK码需要",
            "45": "服务提供商个人化PIN码需要",
            "46": "服务提供商个人化PUK码需要",
            "47": "公司个人化PIN码需要",
            "48": "公司个人化PUK码需要",
            "100": "未知",
            "103": "非法MS",
            "106": "非法ME",
            "107": "GPRS服务不允许",
            "111": "PLMN不允许",
            "112": "位置区域不允许",
            "113": "漫游不允许",
            "132": "服务操作不支持",
            "133": "请求的服务选项不支持",
            "134": "请求的服务选项未订阅",
            "148": "未指定GPRS",
            "149": "PDP认证失败",
            "150": "无效移动类别",
        }
        return error_descriptions.get(error_code, f"未知错误代码: {error_code}")

    async def _send_at_command(self, command: str, wait_time: float = 0.5) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            # 清空输入缓冲区
            self.serial.reset_input_buffer()

            # 发送命令
            if self._debug_mode:
                logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r".encode())

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
