"""
SMS gRPC服务器实现 - 支持多调制解调器管理
"""
import json
import time
import uuid
from typing import Optional
import grpc
from loguru import logger

from . import sms_pb2, sms_pb2_grpc
from src.common.serial_manager import SerialManager


class SMSService(sms_pb2_grpc.SMSServiceServicer):
    """SMS服务实现"""

    def __init__(self, serial_manager: SerialManager):
        self.serial_manager = serial_manager

    async def SendSMS(self, request, context) -> sms_pb2.SendSMSResponse:
        """发送单条短信"""
        logger.info(f"📨 发送短信: {request.phone_number}")

        try:
            # 通过串口管理器发送短信
            success, message, modem_port = await self.serial_manager.send_sms(
                phone_number=request.phone_number,
                content=request.content
            )

            # 构建响应数据
            response_data = {
                "message_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "phone_number": request.phone_number,
                "content_length": len(request.content),
                "success": success,
                "message": message
            }

            # 添加调制解调器信息
            if modem_port:
                response_data["modem_port"] = modem_port

            # 添加请求元数据
            if request.metadata:
                response_data["metadata"] = dict(request.metadata)

            if request.sender_id:
                response_data["sender_id"] = request.sender_id

            if request.delivery_report:
                response_data["delivery_report"] = True

            status_code = 200 if success else 500

            return sms_pb2.SendSMSResponse(
                status=status_code,
                message=message,
                data=json.dumps(response_data, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"短信发送失败: {request.phone_number} - {e}")
            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_number": request.phone_number
            }
            return sms_pb2.SendSMSResponse(
                status=500,
                message="内部服务器错误",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def SendBatchSMS(self, request, context) -> sms_pb2.SendBatchSMSResponse:
        """批量发送短信"""
        logger.info(f"📦 批量发送短信，数量: {len(request.phone_numbers)}")

        results = []
        success_count = 0
        failed_count = 0

        try:
            for phone_number in request.phone_numbers:
                try:
                    # 通过串口管理器发送短信
                    success, message, modem_port = await self.serial_manager.send_sms(
                        phone_number=phone_number,
                        content=request.content
                    )

                    # 记录结果
                    result_data = {
                        "message_id": str(uuid.uuid4()),
                        "phone_number": phone_number,
                        "status": 200 if success else 500,
                        "message": message,
                        "timestamp": time.time(),
                        "success": success
                    }

                    if modem_port:
                        result_data["modem_port"] = modem_port

                    results.append(result_data)

                    if success:
                        success_count += 1
                    else:
                        failed_count += 1

                except Exception as e:
                    logger.error(f"批量发送失败 - {phone_number}: {e}")
                    results.append({
                        "phone_number": phone_number,
                        "status": 500,
                        "message": str(e),
                        "timestamp": time.time(),
                        "error": True
                    })
                    failed_count += 1

            # 构建批量响应数据
            batch_data = {
                "batch_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "total_count": len(request.phone_numbers),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
                "content": request.content,
                "content_length": len(request.content)
            }

            # 添加请求元数据
            if request.metadata:
                batch_data["metadata"] = dict(request.metadata)

            if request.sender_id:
                batch_data["sender_id"] = request.sender_id

            if request.delivery_report:
                batch_data["delivery_report"] = True

            overall_status = 200 if success_count > 0 else 500
            overall_message = f"批量发送完成，成功 {success_count} 条，失败 {failed_count} 条"

            return sms_pb2.SendBatchSMSResponse(
                status=overall_status,
                message=overall_message,
                data=json.dumps(batch_data, ensure_ascii=False)
            )

        except Exception as e:
            logger.error(f"批量发送处理失败: {e}")
            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_numbers_count": len(request.phone_numbers)
            }
            return sms_pb2.SendBatchSMSResponse(
                status=500,
                message="批量发送失败",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def HealthCheck(self, request, context) -> sms_pb2.HealthCheckResponse:
        """健康检查"""
        try:
            if not self.serial_manager:
                return sms_pb2.HealthCheckResponse(
                    status=503,
                    message="服务不健康: 串口管理器未初始化",
                    data=json.dumps({
                        "timestamp": time.time(),
                        "service_ready": False,
                        "error": "serial_manager_not_initialized"
                    }, ensure_ascii=False)
                )

            # 获取串口管理器状态
            health_status = await self.serial_manager.get_health_status()

            # 测试连接
            connected = await self.serial_manager.test_all_connections()

            health_data = {
                "timestamp": time.time(),
                "service_ready": connected,
                "available_modems": health_status["available_modems"],
                "total_modems": health_status["total_modems"],
                "in_use_modems": health_status["in_use_modems"],
                "modems": health_status["modems"]
            }

            if connected and health_status["available_modems"] > 0:
                return sms_pb2.HealthCheckResponse(
                    status=200,
                    message="服务健康",
                    data=json.dumps(health_data, ensure_ascii=False)
                )
            else:
                return sms_pb2.HealthCheckResponse(
                    status=503,
                    message=f"服务不健康: {health_status['available_modems']}/{health_status['total_modems']} 个调制解调器可用",
                    data=json.dumps(health_data, ensure_ascii=False)
                )

        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            error_data = {
                "timestamp": time.time(),
                "error": str(e)
            }
            return sms_pb2.HealthCheckResponse(
                status=500,
                message="健康检查失败",
                data=json.dumps(error_data, ensure_ascii=False)
            )
