"""
SMS短信发送器
"""
import asyncio
import time
import uuid
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import serial
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


class SMSEncoding(str, Enum):
    """短信编码"""
    GSM7 = "GSM7"
    UCS2 = "UCS2"


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
    """短信发送器"""

    def __init__(self, port: str, baudrate: int = 9600, timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None

    async def connect(self) -> bool:
        """连接到调制解调器"""
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                write_timeout=self.timeout
            )

            # 等待调制解调器初始化
            await asyncio.sleep(1)

            # 测试连接
            await self._send_at_command("AT")

            logger.info(f"✅ 连接到调制解调器: {self.port}")
            return True

        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    async def disconnect(self):
        """断开连接"""
        if self.serial and self.serial.is_open:
            self.serial.close()
            self.serial = None
            logger.info(f"✅ 断开调制解调器连接: {self.port}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """发送短信"""
        message_id = str(uuid.uuid4())

        if not self.serial or not self.serial.is_open:
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message="调制解调器未连接"
            )

        # 验证手机号码
        if not self._validate_phone_number(phone_number):
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=400,
                status_message="手机号码格式无效"
            )

        # 确定编码
        encoding = self._detect_encoding(content)

        try:
            # 设置文本模式
            await self._send_at_command("AT+CMGF=1")

            # 根据编码设置
            if encoding == SMSEncoding.UCS2:
                await self._send_at_command('AT+CSMP=17,167,0,8')

            # 发送短信
            if encoding == SMSEncoding.UCS2:
                content_encoded = content.encode('utf-16-be').hex().upper()
                command = f'AT+CMGS="{phone_number}",129\r\n{content_encoded}\x1A'
            else:
                command = f'AT+CMGS="{phone_number}"\r\n{content}\x1A'

            self.serial.write(command.encode())
            await asyncio.sleep(1)

            response = self.serial.read_all().decode('utf-8', errors='ignore')

            if '+CMGS:' in response:
                match = re.search(r'\+CMGS:\s*(\d+)', response)
                ref_num = match.group(1) if match else "0"

                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num
                )
            elif 'ERROR' in response:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {response}"
                )
            else:
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"意外响应: {response}"
                )

        except Exception as e:
            logger.error(f"短信发送失败: {phone_number} - {e}")
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"短信发送错误: {str(e)}"
            )

    async def _send_at_command(self, command: str) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        self.serial.write(f"{command}\r\n".encode())
        await asyncio.sleep(0.2)
        response = self.serial.read_all().decode('utf-8', errors='ignore')
        return response

    def _validate_phone_number(self, phone_number: str) -> bool:
        """验证手机号码格式"""
        pattern = r'^\+?[1-9]\d{1,14}$'
        return bool(re.match(pattern, phone_number))

    def _detect_encoding(self, content: str) -> SMSEncoding:
        """检测短信编码"""
        try:
            content.encode('ascii')
            return SMSEncoding.GSM7
        except UnicodeEncodeError:
            return SMSEncoding.UCS2

    async def test_connection(self) -> bool:
        """测试调制解调器连接"""
        try:
            response = await self._send_at_command("AT")
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
