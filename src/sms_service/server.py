"""
SMS gRPC服务器实现
"""
import json
import time
import uuid
from typing import Optional
import grpc
from loguru import logger

from . import sms_pb2, sms_pb2_grpc
from .sms_sender import SMSSender


class SMSService(sms_pb2_grpc.SMSServiceServicer):
    """SMS服务实现"""

    def __init__(self, sms_sender: SMSSender):
        self.sms_sender = sms_sender

    async def SendSMS(self, request, context) -> sms_pb2.SendSMSResponse:
        """发送单条短信"""
        logger.info(f"📨 发送短信: {request.phone_number}")

        try:
            # 发送短信
            result = await self.sms_sender.send_sms(
                phone_number=request.phone_number,
                content=request.content
            )

            # 构建响应数据
            response_data = {
                "message_id": result.message_id,
                "timestamp": result.timestamp,
                "modem_port": self.sms_sender.port,
                "phone_number": request.phone_number,
                "content_length": len(request.content),
                "success": result.success,
                "reference": result.data
            }

            # 添加请求元数据
            if request.metadata:
                response_data["metadata"] = dict(request.metadata)

            if request.sender_id:
                response_data["sender_id"] = request.sender_id

            if request.delivery_report:
                response_data["delivery_report"] = True

            return sms_pb2.SendSMSResponse(
                status=result.status_code,
                message=result.status_message,
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
                    # 发送单条短信
                    result = await self.sms_sender.send_sms(
                        phone_number=phone_number,
                        content=request.content
                    )

                    # 记录结果
                    result_data = {
                        "message_id": result.message_id,
                        "phone_number": phone_number,
                        "status": result.status_code,
                        "message": result.status_message,
                        "timestamp": result.timestamp,
                        "success": result.success
                    }

                    results.append(result_data)

                    if result.success:
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
            # 检查调制解调器连接
            modem_ok = await self.sms_sender.test_connection()

            if modem_ok:
                # 获取信号强度
                signal_strength = await self.sms_sender.get_signal_strength()

                health_data = {
                    "timestamp": time.time(),
                    "modem_connected": True,
                    "modem_port": self.sms_sender.port,
                    "signal_strength": signal_strength,
                    "service_ready": True
                }

                return sms_pb2.HealthCheckResponse(
                    status=200,
                    message="服务健康",
                    data=json.dumps(health_data, ensure_ascii=False)
                )
            else:
                health_data = {
                    "timestamp": time.time(),
                    "modem_connected": False,
                    "service_ready": False,
                    "error": "调制解调器未连接"
                }

                return sms_pb2.HealthCheckResponse(
                    status=503,
                    message="服务不健康: 调制解调器未连接",
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
