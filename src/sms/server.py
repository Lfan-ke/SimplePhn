"""
SMS gRPC 服务器实现
"""
import json
import time
import grpc
from concurrent import futures
from loguru import logger

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.common.modem_manager import ModemManager
from src.sms.sender import SMSSender
from src.sms import sms_pb2, sms_pb2_grpc


class SMSService(sms_pb2_grpc.SMSServiceServicer):
    """
    SMS gRPC 服务实现
    """

    def __init__(self, modem_manager: ModemManager, sender: SMSSender):
        self.modem_manager = modem_manager
        self.sender = sender

    async def SendSMS(self, request, context):
        """发送单条短信"""
        logger.info(f"📨 发送短信请求: {request.phone_number}")

        try:
            # 构建元数据
            metadata = dict(request.metadata)
            if request.sender_id:
                metadata['sender_id'] = request.sender_id
            metadata['delivery_report'] = str(request.delivery_report)

            # 发送短信
            result = await self.sender.send(
                phone_number=request.phone_number,
                content=request.content,
                metadata=metadata
            )

            # 构建响应
            status_code = 200 if result['success'] else 500

            return sms_pb2.SendSMSResponse(
                status=status_code,
                message=result['message'],
                data=json.dumps(result, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"💥 处理发送短信请求失败: {e}")

            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_number": request.phone_number,
                "success": False
            }

            return sms_pb2.SendSMSResponse(
                status=500,
                message=f"内部服务器错误: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def SendBatchSMS(self, request, context):
        """批量发送短信"""
        logger.info(f"📦 批量发送短信请求，数量: {len(request.phone_numbers)}")

        try:
            # 构建元数据
            metadata = dict(request.metadata)
            if request.sender_id:
                metadata['sender_id'] = request.sender_id
            metadata['delivery_report'] = str(request.delivery_report)

            # 批量发送短信
            result = await self.sender.send_batch(
                phone_numbers=list(request.phone_numbers),
                content=request.content,
                metadata=metadata
            )

            # 构建响应
            overall_success = result['success_count'] > 0
            status_code = 200 if overall_success else 500

            return sms_pb2.SendBatchSMSResponse(
                status=status_code,
                message=f"批量发送完成，成功 {result['success_count']} 条，失败 {result['failure_count']} 条",
                data=json.dumps(result, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"💥 处理批量发送请求失败: {e}")

            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_numbers_count": len(request.phone_numbers),
                "success": False
            }

            return sms_pb2.SendBatchSMSResponse(
                status=500,
                message=f"内部服务器错误: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def HealthCheck(self, request, context):
        """健康检查"""
        try:
            # 检查调制解调器管理器状态
            modem_status = await self.modem_manager.get_status()
            health_status = await self.modem_manager.health_check()

            health_data = {
                "timestamp": time.time(),
                "service_ready": health_status,
                "health_status": "healthy" if health_status else "unhealthy",
                "modem_status": modem_status,
                "details": {
                    "total_modems": modem_status["total_modems"],
                    "available_modems": modem_status["available_modems"],
                    "in_use_modems": modem_status["in_use_modems"],
                    "initialized": modem_status["initialized"]
                }
            }

            status_code = 200 if health_status else 503

            return sms_pb2.HealthCheckResponse(
                status=status_code,
                message="服务健康" if health_status else "服务不健康",
                data=json.dumps(health_data, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"💥 健康检查失败: {e}")

            error_data = {
                "timestamp": time.time(),
                "service_ready": False,
                "error": str(e),
                "details": "健康检查异常"
            }

            return sms_pb2.HealthCheckResponse(
                status=500,
                message=f"健康检查失败: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def GetModemStatus(self, request, context):
        """获取调制解调器状态"""
        try:
            status = await self.modem_manager.get_status()

            return sms_pb2.ModemStatusResponse(
                status=200,
                message="调制解调器状态获取成功",
                data=json.dumps(status, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"💥 获取调制解调器状态失败: {e}")

            error_data = {
                "timestamp": time.time(),
                "error": str(e),
                "details": "获取调制解调器状态失败"
            }

            return sms_pb2.ModemStatusResponse(
                status=500,
                message=f"获取调制解调器状态失败: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )


def create_server(modem_manager: ModemManager, sender: SMSSender, max_workers: int = 10) -> grpc.aio.Server:
    """
    创建 gRPC 服务器

    Args:
        modem_manager: 调制解调器管理器
        sender: 短信发送器
        max_workers: 最大工作线程数

    Returns:
        gRPC 服务器实例
    """
    server = grpc.aio.server(
        futures.ThreadPoolExecutor(max_workers=max_workers)
    )

    # 添加服务
    sms_service = SMSService(modem_manager, sender)
    sms_pb2_grpc.add_SMSServiceServicer_to_server(sms_service, server)

    return server
