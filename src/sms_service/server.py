"""
SMS gRPC服务器实现 - 支持多调制解调器管理，优化锁定时间
"""
import json
import time
import uuid
from typing import Optional, List
import grpc
from loguru import logger

from . import sms_pb2, sms_pb2_grpc
from src.common.serial_manager import SerialManager


class SMSService(sms_pb2_grpc.SMSServiceServicer):
    """SMS服务实现"""

    def __init__(self, serial_manager: SerialManager):
        self.serial_manager = serial_manager
        self._send_timeout = 30  # 发送短信的超时时间（秒）
        self._lock_timeout = 25  # 获取锁的超时时间（秒）

    async def SendSMS(self, request, context) -> sms_pb2.SendSMSResponse:
        """发送单条短信"""
        logger.info(f"📨 发送短信: {request.phone_number}")
        start_time = time.time()

        try:
            # 通过串口管理器发送短信
            success, message, modem_port = await self.serial_manager.send_sms(
                phone_number=request.phone_number,
                content=request.content
            )

            elapsed_time = time.time() - start_time

            # 构建响应数据
            response_data = {
                "message_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "phone_number": request.phone_number,
                "content_length": len(request.content),
                "success": success,
                "message": message,
                "elapsed_time": round(elapsed_time, 2),
                "modem_port": modem_port if modem_port else "unknown"
            }

            # 添加请求元数据
            if request.metadata:
                response_data["metadata"] = dict(request.metadata)

            if request.sender_id:
                response_data["sender_id"] = request.sender_id

            if request.delivery_report:
                response_data["delivery_report"] = True

            status_code = 200 if success else 500

            # 根据处理时间记录日志
            if elapsed_time > 20:
                logger.warning(f"⚠️ 短信发送时间较长: {elapsed_time:.1f}秒，使用调制解调器: {modem_port}")
            elif elapsed_time > 10:
                logger.info(f"📊 短信发送时间: {elapsed_time:.1f}秒，使用调制解调器: {modem_port}")
            else:
                logger.info(f"✅ 短信发送完成: {elapsed_time:.1f}秒，使用调制解调器: {modem_port}")

            return sms_pb2.SendSMSResponse(
                status=status_code,
                message=message,
                data=json.dumps(response_data, ensure_ascii=False)
            )

        except asyncio.TimeoutError:
            elapsed_time = time.time() - start_time
            logger.error(f"⏰ 短信发送超时: {request.phone_number} ({elapsed_time:.1f}秒)")
            error_data = {
                "error": "发送超时",
                "timestamp": time.time(),
                "phone_number": request.phone_number,
                "elapsed_time": round(elapsed_time, 2)
            }
            return sms_pb2.SendSMSResponse(
                status=504,
                message="短信发送超时",
                data=json.dumps(error_data, ensure_ascii=False)
            )

        except Exception as e:
            elapsed_time = time.time() - start_time
            logger.error(f"短信发送失败: {request.phone_number} - {e} ({elapsed_time:.1f}秒)")
            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_number": request.phone_number,
                "elapsed_time": round(elapsed_time, 2)
            }
            return sms_pb2.SendSMSResponse(
                status=500,
                message=f"内部服务器错误: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def SendBatchSMS(self, request, context) -> sms_pb2.SendBatchSMSResponse:
        """批量发送短信"""
        logger.info(f"📦 批量发送短信，数量: {len(request.phone_numbers)}")
        batch_start_time = time.time()

        results: List[dict] = []
        success_count = 0
        failed_count = 0
        modem_usage = {}

        try:
            # 为批量发送设置超时
            batch_timeout = self._send_timeout * len(request.phone_numbers)

            for i, phone_number in enumerate(request.phone_numbers):
                try:
                    logger.info(f"   [{i+1}/{len(request.phone_numbers)}] 发送到: {phone_number}")

                    # 通过串口管理器发送短信
                    success, message, modem_port = await self.serial_manager.send_sms(
                        phone_number=phone_number,
                        content=request.content
                    )

                    # 记录调制解调器使用情况
                    if modem_port:
                        modem_usage[modem_port] = modem_usage.get(modem_port, 0) + 1

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
                        logger.info(f"   ✅ 第 {i+1} 条发送成功")
                    else:
                        failed_count += 1
                        logger.error(f"   ❌ 第 {i+1} 条发送失败: {message}")

                except asyncio.TimeoutError:
                    logger.error(f"   ⏰ 第 {i+1} 条发送超时")
                    results.append({
                        "phone_number": phone_number,
                        "status": 504,
                        "message": "发送超时",
                        "timestamp": time.time(),
                        "error": True
                    })
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
            batch_elapsed_time = time.time() - batch_start_time
            batch_data = {
                "batch_id": str(uuid.uuid4()),
                "timestamp": time.time(),
                "total_count": len(request.phone_numbers),
                "success_count": success_count,
                "failed_count": failed_count,
                "results": results,
                "content": request.content,
                "content_length": len(request.content),
                "batch_elapsed_time": round(batch_elapsed_time, 2),
                "modem_usage": modem_usage
            }

            # 添加请求元数据
            if request.metadata:
                batch_data["metadata"] = dict(request.metadata)

            if request.sender_id:
                batch_data["sender_id"] = request.sender_id

            if request.delivery_report:
                batch_data["delivery_report"] = True

            overall_status = 200 if success_count > 0 else 500
            overall_message = f"批量发送完成 ({batch_elapsed_time:.1f}秒)，成功 {success_count} 条，失败 {failed_count} 条"

            # 记录调制解调器使用统计
            if modem_usage:
                modem_stats = ", ".join([f"{k}: {v}条" for k, v in modem_usage.items()])
                logger.info(f"📊 调制解调器使用统计: {modem_stats}")

            return sms_pb2.SendBatchSMSResponse(
                status=overall_status,
                message=overall_message,
                data=json.dumps(batch_data, ensure_ascii=False)
            )

        except Exception as e:
            batch_elapsed_time = time.time() - batch_start_time
            logger.error(f"批量发送处理失败 ({batch_elapsed_time:.1f}秒): {e}")
            error_data = {
                "error": str(e),
                "timestamp": time.time(),
                "phone_numbers_count": len(request.phone_numbers),
                "batch_elapsed_time": round(batch_elapsed_time, 2)
            }
            return sms_pb2.SendBatchSMSResponse(
                status=500,
                message=f"批量发送失败: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def HealthCheck(self, request, context) -> sms_pb2.HealthCheckResponse:
        """健康检查 - 提供详细状态信息"""
        try:
            if not self.serial_manager:
                return sms_pb2.HealthCheckResponse(
                    status=503,
                    message="服务不健康: 串口管理器未初始化",
                    data=json.dumps({
                        "timestamp": time.time(),
                        "service_ready": False,
                        "error": "serial_manager_not_initialized",
                        "details": "Serial manager is not initialized"
                    }, ensure_ascii=False)
                )

            # 获取串口管理器状态
            health_status = await self.serial_manager.get_health_status()

            # 测试连接（使用较短的超时）
            connected = await self.serial_manager.test_all_connections()

            # 分析调制解调器状态
            available_modems = []
            busy_modems = []
            offline_modems = []

            for modem in health_status["modems"]:
                if modem["is_available"]:
                    if modem["in_use"]:
                        busy_modems.append(modem)
                    else:
                        available_modems.append(modem)
                else:
                    offline_modems.append(modem)

            # 找出最佳调制解调器
            best_modem = None
            if available_modems:
                # 按信号强度和成功率排序
                sorted_modems = sorted(
                    available_modems,
                    key=lambda m: (
                        int(m.get("signal_strength", 0)) if m.get("signal_strength", "0").isdigit() else 0,
                        m.get("success_rate", 0),
                        -m.get("last_used", 0)  # 最近使用时间越小越好
                    ),
                    reverse=True
                )
                best_modem = sorted_modems[0]

            # 构建详细健康数据
            health_data = {
                "timestamp": time.time(),
                "service_ready": connected and len(available_modems) > 0,
                "available_modems": len(available_modems),
                "busy_modems": len(busy_modems),
                "offline_modems": len(offline_modems),
                "total_modems": health_status["total_modems"],
                "best_modem": best_modem if best_modem else {},
                "available_modems_list": available_modems,
                "busy_modems_list": busy_modems,
                "offline_modems_list": offline_modems,
                "connection_test_passed": connected,
                "lock_timeout": self._lock_timeout,
                "send_timeout": self._send_timeout
            }

            # 构建状态消息
            if connected:
                if len(available_modems) > 0:
                    status_message = f"服务健康 ({len(available_modems)}/{health_status['total_modems']} 个调制解调器可用"

                    if best_modem:
                        signal_str = best_modem.get("signal_strength", "N/A")
                        model = best_modem.get("model", "Unknown")
                        status_message += f"，最佳: {best_modem.get('port', 'N/A')} ({model}, 信号: {signal_str})"

                    if len(busy_modems) > 0:
                        status_message += f"，{len(busy_modems)} 个忙碌中"

                    return sms_pb2.HealthCheckResponse(
                        status=200,
                        message=status_message + ")",
                        data=json.dumps(health_data, ensure_ascii=False)
                    )
                else:
                    return sms_pb2.HealthCheckResponse(
                        status=503,
                        message=f"服务不健康: 0/{health_status['total_modems']} 个调制解调器可用，{len(busy_modems)} 个忙碌中",
                        data=json.dumps(health_data, ensure_ascii=False)
                    )
            else:
                return sms_pb2.HealthCheckResponse(
                    status=503,
                    message=f"服务不健康: 连接测试失败，{len(available_modems)}/{health_status['total_modems']} 个调制解调器可用",
                    data=json.dumps(health_data, ensure_ascii=False)
                )

        except Exception as e:
            logger.error(f"健康检查失败: {e}")
            error_data = {
                "timestamp": time.time(),
                "error": str(e),
                "service_ready": False,
                "details": "Health check failed with exception"
            }
            return sms_pb2.HealthCheckResponse(
                status=500,
                message=f"健康检查失败: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )

    async def GetSystemStatus(self, request, context) -> sms_pb2.HealthCheckResponse:
        """获取系统状态（扩展的健康检查）"""
        try:
            # 调用标准的健康检查
            health_response = await self.HealthCheck(request, context)

            # 添加额外的系统信息
            try:
                import asyncio
                import sys
                import platform
                import psutil

                health_data = json.loads(health_response.data)

                # 添加系统信息
                health_data["system_info"] = {
                    "python_version": sys.version,
                    "platform": platform.platform(),
                    "asyncio_tasks": len(asyncio.all_tasks()),
                    "cpu_percent": psutil.cpu_percent(interval=0.1),
                    "memory_percent": psutil.virtual_memory().percent,
                    "disk_usage": psutil.disk_usage('/').percent
                }

                # 更新响应数据
                health_response.data = json.dumps(health_data, ensure_ascii=False)

            except ImportError:
                # 如果psutil不可用，跳过系统信息
                pass

            return health_response

        except Exception as e:
            logger.error(f"获取系统状态失败: {e}")
            error_data = {
                "timestamp": time.time(),
                "error": str(e),
                "service_ready": False
            }
            return sms_pb2.HealthCheckResponse(
                status=500,
                message=f"获取系统状态失败: {str(e)}",
                data=json.dumps(error_data, ensure_ascii=False)
            )
