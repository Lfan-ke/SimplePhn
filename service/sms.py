import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

from common.config import ModemWrapper
from logger import logger


@dataclass
class SMSMessage:
    phone: str
    content: str
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, json_data: dict) -> 'SMSMessage':
        data = json_data.copy()
        phone = data.get('phone', '').strip()
        if phone.startswith('+'):
            data['phone'] = phone
        else:
            data['phone'] = f"+86{phone}"
        return cls(**{k: data[k] for k in data if k in cls.__annotations__})


sms_field_description = {
    "phone": {
        "type": "str",
        "description": "手机号码",
        "required": True,
        "pattern": r"^(\+\d{10,15}|1[3-9]\d{9})$",
    },
    "content": {
        "type": "str",
        "description": "短信内容",
        "required": True,
        "minLength": 1,
    },
    "metadata": {
        "type": "dict",
        "description": "可选元数据",
        "required": False,
        "default": {},
    }
}

async def _wait_for_modem(max_attempts: int = 5) -> Optional[ModemWrapper]:
    """
    等待获取可用的调制解调器，使用指数退避策略

    Args:
        max_attempts: 最大尝试次数

    Returns:
        ModemWrapper or None
    """
    wait_times = [60, 180, 300, 420, 540]  # 1, 3, 5, 7, 9分钟

    for attempt in range(max_attempts):
        modem_wrapper = ModemWrapper.try_new()

        if modem_wrapper:
            return modem_wrapper

        if attempt < len(wait_times):
            wait_time = wait_times[attempt]
            logger.warn_sync(f"📱 没有可用的调制解调器，第{attempt + 1}次等待 {wait_time}秒...")
            await asyncio.sleep(wait_time)
        else:
            default_wait = 60
            logger.warn_sync(f"📱 等待超时，使用默认等待时间 {default_wait}秒...")
            await asyncio.sleep(default_wait)

    return None


def create_sms_task(sms_msg: SMSMessage) -> asyncio.Task[bool]:
    """
    创建短信发送任务

    Args:
        sms_msg: 短信消息对象

    Returns:
        asyncio.Task[bool]: 短信发送任务本次是否成功
    """

    async def __send_sms() -> bool:
        """短信发送函数"""
        start_time = time.time()
        message_id = uuid.uuid4()

        result = {
            "success": False,
            "message_id": message_id,
            "phone": sms_msg.phone,
            "content": sms_msg.content,
            "timestamp": datetime.now().isoformat(),
            "elapsed_time": 0.0,
            "attempts": 0,
            "metadata": sms_msg.metadata.copy()
        }

        try:
            await logger.info(f"开始发送短信: {message_id}")
            await logger.info(f"收件人: {sms_msg.phone}")
            await logger.info(f"内容长度: {len(sms_msg.content)} 字符")
            await logger.info(f"短信预览: {sms_msg.content[:15]} ... {sms_msg.content[-15:]}")

            result["attempts"] += 1
            modem_wrapper = await _wait_for_modem()

            if not modem_wrapper:
                error_msg = "获取调制解调器失败：所有调制解调器都在忙或不可用"
                result["message"] = error_msg
                result["error"] = "MODEM_BUSY"
                result["elapsed_time"] = time.time() - start_time

                await logger.error(f"❌ 短信发送失败 {message_id}: {error_msg}")
                return result["success"]

            modem_info = modem_wrapper.get_info()
            result["modem_info"] = {
                "port": modem_info["port"],
                "imsi": modem_info["imsi"][:8] + "..." if len(modem_info["imsi"]) > 8 else modem_info["imsi"],
                "signal": modem_info["signal"],
                "model": modem_info["model"]
            }

            await logger.trace(f"📱 使用调制解调器: {modem_info['port']}")
            await logger.trace(f"  信号强度: {modem_info['signal']}")
            await logger.trace(f"  设备型号: {modem_info['model']}")

            # 短信附加元数据！
            if sms_msg.metadata:
                special_fields = ('user_id', 'app_id', 'function')
                formatted_lines = []
                for fld in special_fields:
                    if fld in sms_msg.metadata:
                        formatted_lines.append(f"{fld}: {sms_msg.metadata[fld]}")
                if formatted_lines:
                    formatted_lines = ["| "+" | ".join(formatted_lines)+" |"]
                other_fields = {k: v for k, v in sms_msg.metadata.items() if k not in special_fields}
                if other_fields:
                    formatted_lines.append(f"其他元数据:\n{other_fields}")
                if formatted_lines:
                    sms_msg.content += (
                        "\n" if not sms_msg.content.endswith("\n") else ""
                    ) + "\n".join(formatted_lines)

            # 发送短信
            send_result = await modem_wrapper.send_sms(sms_msg.phone, sms_msg.content)

            # 合并结果
            for key, value in send_result.items():
                if key not in result:
                    result[key] = value

            # 记录最终结果
            result["elapsed_time"] = time.time() - start_time

            if send_result.get("success"):
                result["success"] = True
                result["message"] = "短信发送成功"
                await logger.info(f"✅ 短信发送成功 {message_id}: {sms_msg.phone}")
                await logger.info(f"  耗时: {result['elapsed_time']:.2f}秒")
            else:
                result["success"] = False
                result["message"] = send_result.get("error", "短信发送失败")
                result["error"] = send_result.get("error_type", "SEND_FAILED")

                # 记录详细错误
                error_detail = {
                    "error_message": send_result.get("error", ""),
                    "error_type": send_result.get("error_type", ""),
                    "error_category": send_result.get("error_category", ""),
                    "retry_count": send_result.get("retry_count", 0)
                }
                result["error_detail"] = error_detail

                await logger.error(f"❌ 短信发送失败 {message_id}: {result}")

        except asyncio.CancelledError:
            # 任务被取消
            result["success"] = False
            result["message"] = "短信发送任务被取消"
            result["error"] = "TASK_CANCELLED"
            result["elapsed_time"] = time.time() - start_time

            await logger.warn(f"⏹️ 短信发送任务取消 {message_id}: {sms_msg.phone}")

        except Exception as e:
            result["success"] = False
            result["message"] = f"短信发送异常: {str(e)}"
            result["error"] = "UNKNOWN_ERROR"
            result["error_detail"] = {"exception": str(e), "type": type(e).__name__}
            result["elapsed_time"] = time.time() - start_time

            await logger.error(f"💥 短信发送异常 {message_id}: {e.__traceback__}")

        finally:
            if 'modem_wrapper' in locals():
                del modem_wrapper

            # 记录最终状态
            result["completed_at"] = datetime.now().isoformat()
            await logger.trace(
                f"📝 短信任务完成 {message_id}: 成功={result['success']}, 耗时={result['elapsed_time']:.2f}s")

        return result["success"]

    return asyncio.create_task(__send_sms(), name=f"sms-task-{uuid.uuid4()}")
