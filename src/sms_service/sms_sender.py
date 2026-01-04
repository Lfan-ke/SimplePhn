"""
SMS短信发送器 - 修复版，支持在文本模式下发送长短信（带UDH头，全部使用UCS2编码）
"""
import asyncio
import time
import uuid
import re
import math
import random
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
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
    raw_response: str = ""
    segment_number: int = 1
    total_segments: int = 1


def to_ucs2_hex(s: str) -> str:
    """将字符串转为 UCS2-BE 的十六进制字符串"""
    try:
        return s.encode("utf-16-be").hex().upper()
    except Exception as e:
        logger.error(f"编码字符串到UCS2失败: {e}")
        # 尝试用替代字符替换非法字符
        s_clean = s.encode('utf-8', 'replace').decode('utf-8')
        return s_clean.encode("utf-16-be").hex().upper()


class SMSSender:
    """短信发送器 - 支持在文本模式下发送长短信（带UDH头，全部使用UCS2编码）"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self._debug_mode = True
        self._is_connected = False

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

            await asyncio.sleep(1)
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            response = await self._send_at_command("AT")
            if "OK" not in response:
                logger.error(f"AT命令无响应，收到: {response}")
                return False

            await self._send_at_command("ATE0")
            await self._send_at_command("AT+CMEE=2")

            # 检查调制解调器能力
            response = await self._send_at_command("AT+CMGF=?")
            logger.info(f"支持的短信模式: {response}")

            # 始终使用文本模式
            response = await self._send_at_command("AT+CMGF=1")
            if "OK" not in response:
                logger.error(f"设置文本模式失败，响应: {response}")
                return False

            # 设置UCS2编码
            response = await self._send_at_command('AT+CSCS="UCS2"')
            if "OK" not in response:
                logger.error(f"设置UCS2编码失败，响应: {response}")
                return False

            # 设置短信存储
            await self._send_at_command('AT+CPMS="SM","SM","SM"')

            # 获取调制解调器信息
            info = await self.get_modem_info()
            logger.info(f"调制解调器信息: {info.get('manufacturer', 'Unknown')} {info.get('model', 'Unknown')}")

            logger.info(f"✅ 连接到调制解调器: {self.port}")
            self._is_connected = True
            return True

        except serial.SerialException as e:
            logger.error(f"❌ 串口连接失败: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ 连接调制解调器失败: {e}")
            return False

    def _encode_ucs2_segment(self, text: str, segment_num: int, total_segments: int,
                            reference_num: int = None) -> str:
        """
        将文本编码为UCS2格式，并添加UDH头（长短信用）

        Args:
            text: 原始文本
            segment_num: 当前段序号 (1-based)
            total_segments: 总段数
            reference_num: 参考号，如果为None则随机生成

        Returns:
            包含UDH头的完整UCS2十六进制字符串
        """
        if reference_num is None:
            reference_num = random.randint(1, 255)

        # 构建UDH (User Data Header)
        # 对于文本模式中的长短信，UDH作为特殊字符放在短信开头
        # UDH格式: \x05\x00\x03\xRR\xTT\xSS
        # 05: UDH长度 (5字节)
        # 00: 信息元素标识 (连接短信)
        # 03: 信息元素数据长度 (3字节)
        # RR: 参考号 (0-255)
        # TT: 总段数
        # SS: 当前段序号 (1-based)
        ref_byte = reference_num & 0xFF
        total_segments_byte = total_segments & 0xFF
        current_segment_byte = segment_num & 0xFF

        # 创建UDH字符串（Unicode字符）
        # 这些是控制字符，在UCS2中编码为相应的码点
        udh_chars = [
            chr(0x0500),  # UDH长度指示
            chr(0x0000),  # 信息元素标识
            chr(0x0003),  # 信息元素数据长度
            chr(ref_byte),  # 参考号
            chr(total_segments_byte),  # 总段数
            chr(current_segment_byte)  # 当前段序号
        ]
        udh_string = ''.join(udh_chars)

        # 将UDH字符串和原始文本组合
        full_text = udh_string + text

        # 转换为UCS2十六进制
        return to_ucs2_hex(full_text)

    def _split_content_with_udh(self, content: str, reference_num: int = None) -> List[Tuple[int, str, int, int]]:
        """
        将长短信内容分割并添加UDH头

        Args:
            content: 原始短信内容
            reference_num: 长短信的唯一参考号

        Returns:
            列表，每个元素为(段落序号, 编码后的UCS2十六进制, 段落序号, 总段落数)
        """
        if reference_num is None:
            reference_num = random.randint(1, 255)

        MAX_CHARS_PER_SEGMENT = 67  # 每段最多67个字符（因为有6个字符被UDH占用）
        total_chars = len(content)

        if total_chars <= 70:
            # 短消息，直接返回单条
            text_ucs2 = to_ucs2_hex(content)
            return [(1, text_ucs2, 1, 1)]

        # 计算需要多少段
        num_segments = math.ceil(total_chars / MAX_CHARS_PER_SEGMENT)

        segments = []

        for segment_index in range(num_segments):
            segment_num = segment_index + 1
            start = segment_index * MAX_CHARS_PER_SEGMENT
            end = start + MAX_CHARS_PER_SEGMENT
            segment_text = content[start:end]

            # 编码当前段落（带UDH头）
            segment_ucs2 = self._encode_ucs2_segment(
                segment_text, segment_num, num_segments, reference_num
            )

            segments.append((segment_num, segment_ucs2, segment_num, num_segments))

            logger.debug(f"📑 编码第 {segment_num}/{num_segments} 段，参考号: {reference_num}，长度: {len(segment_text)}字符")

        logger.info(f"📨 长短信分割完成：{total_chars} 字符 -> {len(segments)} 段，参考号: {reference_num}")
        return segments

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 自动处理长短信分割

        Args:
            phone_number: 手机号码
            content: 短信内容

        Returns:
            发送结果（如果是长短信，返回第一段的发送结果）
        """
        total_chars = len(content)

        if total_chars <= 70:
            # 单条短信
            return await self._send_single_sms(phone_number, content, 1, 1)
        else:
            # 长短信，使用新方法
            logger.warning(f"⚠️ 内容长度 {total_chars} 字符，需要分割发送")
            results = await self.send_long_sms(phone_number, content)

            if results:
                return results[0]  # 返回第一段的结果（向后兼容）
            else:
                return SMSResult(
                    message_id=str(uuid.uuid4()),
                    success=False,
                    status_code=500,
                    status_message="长短信发送失败",
                    segment_number=1,
                    total_segments=1
                )

    async def send_long_sms(self, phone_number: str, content: str) -> List[SMSResult]:
        """
        发送长短信（自动分割和添加UDH头）

        Args:
            phone_number: 手机号码
            content: 短信内容

        Returns:
            所有段落的发送结果列表
        """
        total_chars = len(content)

        if total_chars <= 70:
            # 单条短信
            result = await self._send_single_sms(phone_number, content, 1, 1)
            return [result]

        logger.info(f"📨 开始发送长短信：{total_chars} 字符")

        # 分割并编码短信内容
        encoded_segments = self._split_content_with_udh(content)
        total_segments = len(encoded_segments)

        results = []

        for segment_num, segment_ucs2, seg_num, total_segs in encoded_segments:
            logger.info(f"🔄 发送第 {segment_num}/{total_segments} 段")

            # 发送当前段落
            result = await self._send_encoded_sms(
                phone_number, segment_ucs2, segment_num, total_segments
            )
            results.append(result)

            # 如果不是最后一段，等待一下再发送下一段
            if segment_num < total_segments:
                await asyncio.sleep(2)  # 增加等待时间，避免调制解调器过载

        # 统计结果
        success_count = sum(1 for r in results if r.success)
        logger.info(f"📊 长短信发送完成：成功 {success_count}/{total_segments} 段")

        return results

    async def _send_single_sms(self, phone_number: str, content: str,
                               segment_num: int = 1, total_segments: int = 1) -> SMSResult:
        """
        发送单条短信（内部方法）

        Args:
            phone_number: 手机号码
            content: 短信内容
            segment_num: 段落序号
            total_segments: 总段落数

        Returns:
            发送结果
        """
        # 将内容编码为UCS2
        content_ucs2 = to_ucs2_hex(content)

        # 发送编码后的短信
        return await self._send_encoded_sms(
            phone_number, content_ucs2, segment_num, total_segments
        )

    async def _send_encoded_sms(self, phone_number: str, content_ucs2: str,
                               segment_num: int = 1, total_segments: int = 1) -> SMSResult:
        """
        发送已编码的短信（内部方法）

        Args:
            phone_number: 手机号码
            content_ucs2: 已编码的UCS2十六进制字符串
            segment_num: 段落序号
            total_segments: 总段落数

        Returns:
            发送结果
        """
        message_id = str(uuid.uuid4())

        if not self.serial or not self.serial.is_open:
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message="调制解调器未连接",
                segment_number=segment_num,
                total_segments=total_segments
            )

        logger.info(f"📱 发送短信到: {phone_number}")
        if total_segments > 1:
            logger.info(f"📑 段落: {segment_num}/{total_segments}")

        try:
            # 1. 确保调制解调器就绪
            await self._send_at_command("AT", wait_time=0.5)
            await self._send_at_command("ATE0", wait_time=0.5)

            # 2. 确保文本模式和UCS2编码
            response = await self._send_at_command("AT+CMGF=1", wait_time=0.5)
            if "OK" not in response:
                logger.error(f"设置文本模式失败: {response}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"设置文本模式失败: {response}",
                    segment_number=segment_num,
                    total_segments=total_segments
                )

            response = await self._send_at_command('AT+CSCS="UCS2"', wait_time=0.5)
            if "OK" not in response:
                logger.error(f"设置UCS2编码失败: {response}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"设置UCS2编码失败: {response}",
                    segment_number=segment_num,
                    total_segments=total_segments
                )

            # 3. 转换电话号码为UCS2十六进制
            phone_ucs2 = to_ucs2_hex(phone_number)

            # 4. 发送AT+CMGS命令
            cmd = f'AT+CMGS="{phone_ucs2}"'
            logger.debug(f"📤 发送命令: {cmd}")

            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            self.serial.write(f"{cmd}\r".encode())
            await asyncio.sleep(1.0)

            # 5. 等待提示符
            response = self.serial.read_all().decode('utf-8', errors='ignore')
            if ">" not in response:
                await asyncio.sleep(1.0)
                response += self.serial.read_all().decode('utf-8', errors='ignore')

                if ">" not in response:
                    logger.error(f"未收到>提示符，响应: {response}")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="调制解调器未准备好",
                        raw_response=response,
                        segment_number=segment_num,
                        total_segments=total_segments
                    )

            # 6. 发送已编码的内容
            logger.info("📤 发送短信内容...")

            # 将十六进制字符串转换为字节并发送
            data_bytes = bytes.fromhex(content_ucs2)
            self.serial.write(data_bytes)
            await asyncio.sleep(0.5)

            # 发送Ctrl+Z结束符
            self.serial.write(b'\x1A')

            # 7. 等待响应
            final_response = await self._wait_for_response()

            # 8. 解析响应
            return self._parse_response(
                message_id, final_response, segment_num, total_segments
            )

        except Exception as e:
            logger.error(f"💥 发送短信异常: {e}", exc_info=True)
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}",
                segment_number=segment_num,
                total_segments=total_segments
            )

    async def _wait_for_response(self, max_wait_time: int = 15) -> str:
        """等待调制解调器响应"""
        total_wait_time = 0
        final_response = ""

        while total_wait_time < max_wait_time:
            await asyncio.sleep(1.0)
            total_wait_time += 1

            chunk = self.serial.read_all().decode('utf-8', errors='ignore')
            if chunk:
                final_response += chunk
                logger.debug(f"⏳ 等待 {total_wait_time}s, 收到: {chunk[:100]}...")

                # 检查是否收到完整响应
                if '+CMGS:' in final_response or 'OK' in final_response or 'ERROR' in final_response:
                    logger.debug(f"✅ 收到完整响应，停止等待")
                    break

        return final_response

    def _parse_response(self, message_id: str, response: str,
                       segment_num: int, total_segments: int) -> SMSResult:
        """解析调制解调器响应"""
        if '+CMGS:' in response:
            match = re.search(r'\+CMGS:\s*(\d+)', response)
            ref_num = match.group(1) if match else "0"
            logger.info(f"✅ 短信发送成功，参考号: {ref_num}")
            return SMSResult(
                message_id=message_id,
                success=True,
                status_code=200,
                status_message="短信发送成功",
                data=ref_num,
                raw_response=response,
                segment_number=segment_num,
                total_segments=total_segments
            )
        elif 'OK' in response:
            logger.info("✅ 短信发送成功 (收到OK)")
            return SMSResult(
                message_id=message_id,
                success=True,
                status_code=200,
                status_message="短信发送成功",
                data="ok",
                raw_response=response,
                segment_number=segment_num,
                total_segments=total_segments
            )
        elif 'ERROR' in response or '+CMS ERROR:' in response:
            error_match = re.search(r'\+CMS ERROR:\s*(\d+)', response)
            if error_match:
                error_code = error_match.group(1)
                error_desc = self._get_error_description(error_code)
                logger.error(f"❌ 短信发送失败 (CMS错误 {error_code}): {error_desc}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {error_desc} (代码: {error_code})",
                    raw_response=response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )
            else:
                logger.error(f"❌ 短信发送失败: {response}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"发送失败: {response[:100]}",
                    raw_response=response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )
        else:
            if response and len(response.strip()) > 0:
                logger.warning(f"⚠️ 未知响应: {response[:200]}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功 (有响应)",
                    data="has_response",
                    raw_response=response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )
            else:
                logger.error("❌ 无响应，可能超时")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message="发送超时，无响应",
                    raw_response=response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )

    def _get_error_description(self, error_code: str) -> str:
        """获取错误代码描述"""
        error_descriptions = {
            "23": "文本字符串太长",
            "300": "电话号码格式错误",
            "301": "电话号码无效",
            "500": "未知错误",
            "516": "文本字符串太长",
        }
        return error_descriptions.get(error_code, f"错误代码: {error_code}")

    async def _send_at_command(self, command: str, wait_time: float = 1.0) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            self.serial.reset_input_buffer()
            logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r".encode())
            await asyncio.sleep(wait_time)
            response_bytes = self.serial.read_all()
            response = response_bytes.decode('utf-8', errors='ignore').strip()
            logger.debug(f"AT响应: {response}")
            return response
        except Exception as e:
            logger.error(f"发送AT命令失败: {command} - {e}")
            return f"ERROR: {str(e)}"

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

    async def get_modem_info(self) -> dict:
        """获取调制解调器信息"""
        info = {
            "port": self.port,
            "manufacturer": "Unknown",
            "model": "Unknown",
            "imei": "",
            "signal_strength": "0",
            "is_connected": False
        }

        try:
            response = await self._send_at_command("ATI")
            if response:
                response_upper = response.upper()
                if 'HUAWEI' in response_upper:
                    info["manufacturer"] = 'Huawei'
                elif 'ZTE' in response_upper:
                    info["manufacturer"] = 'ZTE'
                elif 'QUECTEL' in response_upper:
                    info["manufacturer"] = 'Quectel'
                elif 'EC20' in response_upper:
                    info["manufacturer"] = 'Quectel'
                    info["model"] = 'EC20'
                elif 'SIERRA' in response_upper:
                    info["manufacturer"] = 'Sierra'
                elif 'SIMCOM' in response_upper:
                    info["manufacturer"] = 'SIMCom'

            response = await self._send_at_command("AT+GMM")
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('AT') and 'OK' not in line:
                        info["model"] = line
                        break

            response = await self._send_at_command("AT+GSN")
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line.isdigit() and 15 <= len(line) <= 17:
                        info["imei"] = line
                        break

            response = await self._send_at_command("AT+CSQ")
            if '+CSQ:' in response:
                match = re.search(r'\+CSQ:\s*(\d+)', response)
                if match:
                    info["signal_strength"] = match.group(1)

            info["is_connected"] = True

        except Exception as e:
            logger.warning(f"获取调制解调器信息失败: {e}")

        return info

    async def disconnect(self):
        """断开连接"""
        if self.serial and self.serial.is_open:
            self.serial.close()
            logger.info(f"断开调制解调器连接: {self.port}")
            self._is_connected = False
