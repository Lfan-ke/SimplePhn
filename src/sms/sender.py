"""
基于 gsmmodem 的短信发送器
"""
import time
import uuid
from typing import Dict, Any, List
from loguru import logger

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class SMSSender:
    """
    短信发送器

    封装 gsmmodem 的短信发送功能，提供更友好的接口
    """

    def __init__(self, modem_manager):
        """
        初始化短信发送器

        Args:
            modem_manager: 调制解调器管理器实例
        """
        self.modem_manager = modem_manager

    async def send(self, phone_number: str, content: str, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        发送短信

        Args:
            phone_number: 手机号码
            content: 短信内容
            metadata: 元数据

        Returns:
            发送结果
        """
        start_time = time.time()
        message_id = str(uuid.uuid4())

        try:
            # 使用调制解调器管理器发送短信
            success, message, modem_port = await self.modem_manager.send_sms(
                phone_number, content
            )

            elapsed_time = time.time() - start_time

            # 构建响应
            result = {
                "message_id": message_id,
                "success": success,
                "phone_number": phone_number,
                "content_length": len(content),
                "message": message,
                "modem_port": modem_port,
                "elapsed_time": round(elapsed_time, 2),
                "timestamp": time.time(),
                "metadata": metadata or {}
            }

            # 记录日志
            if success:
                logger.info(f"✅ 短信发送成功: {phone_number} via {modem_port} ({elapsed_time:.2f}s)")
            else:
                logger.error(f"❌ 短信发送失败: {phone_number} - {message}")

            return result

        except Exception as e:
            elapsed_time = time.time() - start_time
            logger.error(f"💥 短信发送异常: {phone_number} - {e}")

            return {
                "message_id": message_id,
                "success": False,
                "phone_number": phone_number,
                "content_length": len(content),
                "message": f"发送异常: {str(e)}",
                "modem_port": None,
                "elapsed_time": round(elapsed_time, 2),
                "timestamp": time.time(),
                "metadata": metadata or {},
                "error": str(e)
            }

    async def send_batch(self, phone_numbers: List[str], content: str, metadata: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        批量发送短信

        Args:
            phone_numbers: 手机号码列表
            content: 短信内容
            metadata: 元数据

        Returns:
            批量发送结果
        """
        batch_id = str(uuid.uuid4())
        start_time = time.time()

        logger.info(f"📦 批量发送短信，数量: {len(phone_numbers)}")
        logger.info(f"📄 内容长度: {len(content)} 字符")

        results = []
        success_count = 0
        failure_count = 0
        modem_usage = {}

        for i, phone_number in enumerate(phone_numbers):
            try:
                logger.debug(f"   [{i+1}/{len(phone_numbers)}] 发送到: {phone_number}")

                # 发送单条短信
                result = await self.send(phone_number, content, metadata)

                # 记录调制解调器使用情况
                modem_port = result.get('modem_port')
                if modem_port:
                    modem_usage[modem_port] = modem_usage.get(modem_port, 0) + 1

                results.append(result)

                if result['success']:
                    success_count += 1
                else:
                    failure_count += 1

            except Exception as e:
                logger.error(f"批量发送失败 - {phone_number}: {e}")

                results.append({
                    "message_id": str(uuid.uuid4()),
                    "success": False,
                    "phone_number": phone_number,
                    "content_length": len(content),
                    "message": f"发送异常: {str(e)}",
                    "modem_port": None,
                    "timestamp": time.time(),
                    "error": str(e)
                })
                failure_count += 1

        elapsed_time = time.time() - start_time

        batch_result = {
            "batch_id": batch_id,
            "total_count": len(phone_numbers),
            "success_count": success_count,
            "failure_count": failure_count,
            "success_rate": success_count / len(phone_numbers) if phone_numbers else 0,
            "results": results,
            "content": content,
            "content_length": len(content),
            "elapsed_time": round(elapsed_time, 2),
            "modem_usage": modem_usage,
            "timestamp": time.time(),
            "metadata": metadata or {}
        }

        logger.info(f"📊 批量发送完成: 成功 {success_count} 条，失败 {failure_count} 条 ({elapsed_time:.2f}s)")

        return batch_result
