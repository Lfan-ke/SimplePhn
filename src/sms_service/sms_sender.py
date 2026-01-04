"""
SMS短信发送器 - 增强版，支持自动拆分长短信
"""
import asyncio
import time
import uuid
import re
import math
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
    segment_number: int = 1  # 新增：第几段
    total_segments: int = 1  # 新增：总段数


def to_ucs2_hex(s: str) -> str:
    """将字符串转为 UCS2-BE 的十六进制字符串"""
    try:
        return s.encode("utf-16-be").hex().upper()
    except Exception as e:
        logger.error(f"编码字符串到UCS2失败: {e}")
        # 尝试用替代字符替换非法字符
        s_clean = s.encode('utf-8', 'replace').decode('utf-8')
        return s_clean.encode("utf-16-be").hex().upper()


def split_long_message(content: str, max_chars_per_segment: int = 67) -> List[str]:
    """
    将长短信内容分割成多个段落，适用于UCS2编码。
    注意：在UCS2长短信中，第一条最多70字符，后续每条最多67字符[citation:2]。
    为简化处理，这里统一按67字符分割，实际第一条会略有空间浪费但更安全。

    Args:
        content: 原始短信内容
        max_chars_per_segment: 每个段落的字符数限制

    Returns:
        分割后的字符串列表
    """
    if not content:
        return [""]

    # 如果内容长度不超过70个字符，直接返回单条
    if len(content) <= 70:
        return [content]

    segments = []
    total_length = len(content)

    # 计算需要分割成多少段
    # 第一条短信最多70字符，后续每条最多67字符
    # 简化：全部按67字符计算，最后一段可能少于67字符
    num_segments = math.ceil(total_length / max_chars_per_segment)

    for i in range(num_segments):
        start = i * max_chars_per_segment
        end = start + max_chars_per_segment
        segment = content[start:end]
        segments.append(segment)

    logger.info(f"📊 长短信分割完成：原始内容 {total_length} 字符，分割为 {len(segments)} 条短信")
    for idx, seg in enumerate(segments):
        logger.info(f"  第 {idx+1}/{len(segments)} 段：{len(seg)} 字符")
        if len(seg) > 70:
            logger.warning(f"  警告：第 {idx+1} 段长度 {len(seg)} 超过单条短信上限 70，可能需要进一步分割")

    return segments


def calculate_sms_segments(content: str) -> Tuple[int, int]:
    """
    计算短信需要分割成多少段（基于UCS2编码）

    Args:
        content: 短信内容

    Returns:
        (总段数, 总字符数)
    """
    total_chars = len(content)

    if total_chars <= 70:
        return 1, total_chars

    # 第一条短信最多70字符，后续每条最多67字符
    remaining_chars = total_chars - 70
    additional_segments = math.ceil(remaining_chars / 67) if remaining_chars > 0 else 0
    total_segments = 1 + additional_segments

    return total_segments, total_chars


class SMSSender:
    """短信发送器 - 支持长短信自动分割"""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None
        self._debug_mode = True  # 默认启用调试模式
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

            # 等待调制解调器初始化
            await asyncio.sleep(1)

            # 清空缓冲区
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            # 测试连接
            response = await self._send_at_command("AT")
            if "OK" not in response:
                logger.error(f"AT命令无响应，收到: {response}")
                return False

            # 关闭回显
            await self._send_at_command("ATE0")
            # 启用详细错误
            await self._send_at_command("AT+CMEE=2")

            # 检查支持的短信模式
            response = await self._send_at_command("AT+CMGF=?")
            logger.info(f"支持的短信模式: {response}")

            # 设置文本模式
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=3)
    )
    async def send_sms(self, phone_number: str, content: str) -> SMSResult:
        """
        发送短信 - 自动处理长短信分割

        注意：这个方法会返回最后一段的发送结果。
        对于多段短信，建议使用 send_long_sms() 方法。
        """
        # 检查是否需要分割
        segments = split_long_message(content)

        if len(segments) == 1:
            # 单条短信，直接发送
            return await self._send_single_sms(phone_number, content, 1, 1)
        else:
            # 长短信，发送所有段落
            logger.warning(f"⚠️ 内容长度 {len(content)} 字符，需要分割成 {len(segments)} 条短信发送")
            logger.warning("⚠️ 请使用 send_long_sms() 方法发送长短信以获得完整结果")

            # 默认只发送第一段（向后兼容）
            return await self._send_single_sms(phone_number, segments[0], 1, len(segments))

    async def send_long_sms(self, phone_number: str, content: str) -> List[SMSResult]:
        """
        发送长短信（自动分割）

        Args:
            phone_number: 手机号码
            content: 短信内容

        Returns:
            所有段落的发送结果列表
        """
        # 分割短信
        segments = split_long_message(content)
        total_segments = len(segments)

        if total_segments == 1:
            # 单条短信
            result = await self._send_single_sms(phone_number, content, 1, 1)
            return [result]

        logger.info(f"📨 开始发送长短信：{len(content)} 字符，分割为 {total_segments} 段")

        results = []
        for i, segment in enumerate(segments):
            segment_num = i + 1

            logger.info(f"🔄 发送第 {segment_num}/{total_segments} 段 ({len(segment)} 字符)")

            # 发送当前段落
            result = await self._send_single_sms(
                phone_number,
                segment,
                segment_num,
                total_segments
            )

            results.append(result)

            # 如果不是最后一段，等待一下再发送下一段
            if segment_num < total_segments:
                await asyncio.sleep(1)

        # 统计结果
        success_count = sum(1 for r in results if r.success)
        logger.info(f"📊 长短信发送完成：成功 {success_count}/{total_segments} 段")

        return results

    async def _send_single_sms(self, phone_number: str, content: str,
                              segment_num: int = 1, total_segments: int = 1) -> SMSResult:
        """
        发送单条短信（内部方法）
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
        logger.info(f"📄 内容长度: {len(content)} 字符")
        if total_segments > 1:
            logger.info(f"📑 段落: {segment_num}/{total_segments}")

        if len(content) > 50:
            logger.info(f"📝 内容预览: {content[:50]}...")
        else:
            logger.info(f"📝 内容: {content}")

        # 保存完整响应用于调试
        full_response = ""

        try:
            # 1. 确保调制解调器就绪
            response1 = await self._send_at_command("AT", wait_time=1.0)
            full_response += f"AT响应: {response1}\n"

            response2 = await self._send_at_command("ATE0", wait_time=1.0)
            full_response += f"ATE0响应: {response2}\n"

            # 2. 确保文本模式
            response3 = await self._send_at_command("AT+CMGF=1", wait_time=1.0)
            full_response += f"AT+CMGF=1响应: {response3}\n"
            if "OK" not in response3:
                logger.error(f"设置文本模式失败: {response3}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"设置文本模式失败: {response3}",
                    raw_response=full_response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )

            # 3. 确保UCS2编码
            response4 = await self._send_at_command('AT+CSCS="UCS2"', wait_time=1.0)
            full_response += f'AT+CSCS="UCS2"响应: {response4}\n'
            if "OK" not in response4:
                logger.error(f"设置UCS2编码失败: {response4}")
                return SMSResult(
                    message_id=message_id,
                    success=False,
                    status_code=500,
                    status_message=f"设置UCS2编码失败: {response4}",
                    raw_response=full_response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )

            # 4. 转换电话号码为UCS2十六进制
            phone_ucs2 = to_ucs2_hex(phone_number)
            logger.debug(f"🔢 电话号码UCS2: {phone_ucs2}")

            # 5. 发送AT+CMGS命令
            cmd = f'AT+CMGS="{phone_ucs2}"'
            logger.debug(f"📤 发送命令: {cmd}")

            # 清空缓冲区
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()

            # 发送命令
            self.serial.write(f"{cmd}\r".encode())

            # 读取响应，查找提示符
            await asyncio.sleep(1.0)
            response5 = self.serial.read_all().decode('utf-8', errors='ignore')
            full_response += f"AT+CMGS初始响应: {response5}\n"
            logger.debug(f"AT+CMGS初始响应: {response5}")

            # 检查是否收到提示符 ">"
            if ">" not in response5:
                # 尝试等待更多时间
                logger.warning("未收到>提示符，等待更多时间...")
                await asyncio.sleep(1.0)
                response5_extra = self.serial.read_all().decode('utf-8', errors='ignore')
                full_response += f"AT+CMGS额外响应: {response5_extra}\n"
                response5 += response5_extra

                if ">" not in response5:
                    logger.error(f"仍然未收到>提示符，完整响应: {response5}")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message="调制解调器未准备好接收短信内容",
                        raw_response=full_response,
                        segment_number=segment_num,
                        total_segments=total_segments
                    )

            # 6. 转换内容为UCS2十六进制
            text_ucs2 = to_ucs2_hex(content)
            logger.debug(f"📝 内容UCS2长度: {len(text_ucs2)} 字符")

            # 7. 发送UCS2内容
            logger.info("📤 发送短信内容...")

            # 发送内容
            self.serial.write(text_ucs2.encode())
            await asyncio.sleep(0.5)

            # 发送Ctrl+Z (结束符)
            self.serial.write(b'\x1A')

            logger.info("✅ 已发送内容 + Ctrl+Z")

            # 8. 等待响应
            total_wait_time = 0
            max_wait_time = 15
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

            full_response += f"最终响应: {final_response}\n"
            logger.debug(f"📨 最终响应: {final_response[:500]}")

            # 9. 解析响应
            if '+CMGS:' in final_response:
                # 提取消息参考号
                match = re.search(r'\+CMGS:\s*(\d+)', final_response)
                ref_num = match.group(1) if match else "0"
                logger.info(f"✅ 短信发送成功，参考号: {ref_num}")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data=ref_num,
                    raw_response=full_response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )
            elif 'OK' in final_response:
                logger.info("✅ 短信发送成功 (收到OK)")
                return SMSResult(
                    message_id=message_id,
                    success=True,
                    status_code=200,
                    status_message="短信发送成功",
                    data="ok",
                    raw_response=full_response,
                    segment_number=segment_num,
                    total_segments=total_segments
                )
            elif 'ERROR' in final_response or '+CMS ERROR:' in final_response:
                # 提取错误代码
                error_match = re.search(r'\+CMS ERROR:\s*(\d+)', final_response)
                if error_match:
                    error_code = error_match.group(1)
                    error_desc = self._get_error_description(error_code)
                    logger.error(f"❌ 短信发送失败 (CMS错误 {error_code}): {error_desc}")
                    return SMSResult(
                        message_id=message_id,
                        success=False,
                        status_code=500,
                        status_message=f"发送失败: {error_desc} (代码: {error_code})",
                        raw_response=full_response,
                        segment_number=segment_num,
                        total_segments=total_segments
                    )
                else:
                    error_match = re.search(r'ERROR:\s*(.+)', final_response)
                    if error_match:
                        error_msg = error_match.group(1)
                        logger.error(f"❌ 短信发送失败: {error_msg}")
                        return SMSResult(
                            message_id=message_id,
                            success=False,
                            status_code=500,
                            status_message=f"发送失败: {error_msg}",
                            raw_response=full_response,
                            segment_number=segment_num,
                            total_segments=total_segments
                        )
                    else:
                        logger.error(f"❌ 短信发送失败: {final_response}")
                        return SMSResult(
                            message_id=message_id,
                            success=False,
                            status_code=500,
                            status_message=f"发送失败: {final_response[:100]}",
                            raw_response=full_response,
                            segment_number=segment_num,
                            total_segments=total_segments
                        )
            else:
                if final_response and len(final_response.strip()) > 0:
                    logger.warning(f"⚠️ 未知响应: {final_response[:200]}")
                    logger.info("✅ 短信可能发送成功 (有响应)")
                    return SMSResult(
                        message_id=message_id,
                        success=True,
                        status_code=200,
                        status_message="短信发送成功 (有响应)",
                        data="has_response",
                        raw_response=full_response,
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
                        raw_response=full_response,
                        segment_number=segment_num,
                        total_segments=total_segments
                    )

        except Exception as e:
            logger.error(f"💥 发送短信异常: {e}", exc_info=True)
            return SMSResult(
                message_id=message_id,
                success=False,
                status_code=500,
                status_message=f"发送异常: {str(e)}",
                raw_response=full_response + f"\n异常: {str(e)}",
                segment_number=segment_num,
                total_segments=total_segments
            )

    def _get_error_description(self, error_code: str) -> str:
        """获取错误代码描述"""
        error_descriptions = {
            "23": "文本字符串太长[citation:2]",
            "300": "电话号码格式错误",
            "301": "电话号码无效",
            "500": "未知错误",
            "516": "文本字符串太长[citation:2]",
        }
        return error_descriptions.get(error_code, f"错误代码: {error_code}")

    async def _send_at_command(self, command: str, wait_time: float = 1.0) -> str:
        """发送AT命令"""
        if not self.serial:
            raise RuntimeError("串口未连接")

        try:
            # 清空输入缓冲区
            self.serial.reset_input_buffer()

            # 发送命令
            logger.debug(f"发送AT命令: {command}")
            self.serial.write(f"{command}\r".encode())

            # 等待响应
            await asyncio.sleep(wait_time)

            # 读取响应
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
            # 获取制造商信息
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

            # 获取型号
            response = await self._send_at_command("AT+GMM")
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith('AT') and 'OK' not in line:
                        info["model"] = line
                        break

            # 获取IMEI
            response = await self._send_at_command("AT+GSN")
            if response:
                lines = response.strip().split('\n')
                for line in lines:
                    line = line.strip()
                    if line.isdigit() and 15 <= len(line) <= 17:
                        info["imei"] = line
                        break

            # 获取信号强度
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
