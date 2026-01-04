"""
测试客户端 - 用于测试SMS微服务
"""
import asyncio
import json
import time
import sys
import os
from pathlib import Path
import grpc
from loguru import logger

# 添加项目根目录到Python路径，以便导入src模块
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from src.sms_service import sms_pb2, sms_pb2_grpc
    HAS_GRPC = True
except ImportError as e:
    logger.warning(f"无法导入gRPC模块: {e}")
    logger.warning("请先运行以下命令生成gRPC代码:")
    logger.warning("python -m grpc_tools.protoc -Iproto --python_out=src/sms_service --grpc_python_out=src/sms_service proto/sms.proto")
    logger.warning("或者检查 src/sms_service/ 目录下是否有 sms_pb2.py 和 sms_pb2_grpc.py 文件")
    HAS_GRPC = False

    # 创建模拟的pb2模块用于测试
    class MockProto:
        class SendSMSRequest:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class SendBatchSMSRequest:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        class HealthCheckRequest:
            pass

        class SendSMSResponse:
            def __init__(self):
                self.status = 200
                self.message = "模拟响应"
                self.data = '{"test": true}'

        class SendBatchSMSResponse:
            def __init__(self):
                self.status = 200
                self.message = "模拟响应"
                self.data = '{"test": true}'

        class HealthCheckResponse:
            def __init__(self):
                self.status = 200
                self.message = "服务健康"
                self.data = '{"modem_port": "COM1", "signal_strength": 20}'

    sms_pb2 = MockProto()

    class MockStub:
        async def HealthCheck(self, request):
            return sms_pb2.HealthCheckResponse()

        async def SendSMS(self, request):
            return sms_pb2.SendSMSResponse()

        async def SendBatchSMS(self, request):
            return sms_pb2.SendBatchSMSResponse()

    class MockChannel:
        async def close(self):
            pass

    class MockGRPC:
        class SMSServiceStub:
            def __init__(self, channel):
                pass

    sms_pb2_grpc = MockGRPC()


class SMSTestClient:
    """SMS测试客户端"""

    def __init__(self, target: str = "localhost:50052"):
        self.target = target

        if HAS_GRPC:
            self.channel = grpc.aio.insecure_channel(target)
            self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)
        else:
            logger.warning("⚠️  使用模拟的gRPC客户端（未生成真实gRPC代码）")
            self.channel = type('MockChannel', (), {'close': lambda self: None})()
            self.stub = type('MockStub', (), {
                'HealthCheck': lambda self, req: type('Response', (), {
                    'status': 200,
                    'message': '模拟健康检查',
                    'data': '{"modem_port": "COM1", "signal_strength": 20}'
                })(),
                'SendSMS': lambda self, req: type('Response', (), {
                    'status': 200,
                    'message': '模拟发送成功',
                    'data': '{"message_id": "test-123", "reference": "456"}'
                })(),
                'SendBatchSMS': lambda self, req: type('Response', (), {
                    'status': 200,
                    'message': f'批量模拟成功，数量: {len(req.phone_numbers)}',
                    'data': '{"batch_id": "batch-test", "success_count": 1, "failed_count": 0}'
                })()
            })()

    async def health_check(self):
        """健康检查"""
        logger.info(f"🩺 健康检查: {self.target}")

        try:
            response = await self.stub.HealthCheck(sms_pb2.HealthCheckRequest())

            if response.status == 200:
                logger.info(f"✅ 服务健康: {response.message}")
                try:
                    data = json.loads(response.data)
                    logger.info(f"   调制解调器: {data.get('modem_port', 'N/A')}")
                    logger.info(f"   信号强度: {data.get('signal_strength', 'N/A')}")
                except:
                    pass
            else:
                logger.error(f"❌ 服务不健康: {response.message}")

            return response
        except Exception as e:
            logger.error(f"💥 健康检查失败: {e}")
            return None

    async def send_sms(self, phone_number: str, content: str):
        """发送单条短信"""
        logger.info(f"📨 发送短信到: {phone_number}")

        request = sms_pb2.SendSMSRequest(
            phone_number=phone_number,
            content=content,
            sender_id="test_client",
            delivery_report=True,
            metadata={"test": "true", "timestamp": str(time.time())}
        )

        try:
            response = await self.stub.SendSMS(request)

            if response.status == 200:
                logger.info(f"✅ 短信发送成功: {response.message}")
                try:
                    data = json.loads(response.data)
                    logger.info(f"   消息ID: {data.get('message_id', 'N/A')}")
                    logger.info(f"   参考号: {data.get('reference', 'N/A')}")
                except:
                    pass
            else:
                logger.error(f"❌ 短信发送失败: {response.message}")

            return response
        except Exception as e:
            logger.error(f"💥 发送短信失败: {e}")
            return None

    async def send_batch_sms(self, phone_numbers: list[str], content: str):
        """批量发送短信"""
        logger.info(f"📦 批量发送短信，数量: {len(phone_numbers)}")

        request = sms_pb2.SendBatchSMSRequest(
            phone_numbers=phone_numbers,
            content=content,
            sender_id="batch_test",
            delivery_report=False,
            metadata={"batch_test": "true", "timestamp": str(time.time())}
        )

        try:
            response = await self.stub.SendBatchSMS(request)

            if response.status == 200:
                logger.info(f"✅ 批量发送完成: {response.message}")
                try:
                    data = json.loads(response.data)
                    logger.info(f"   批次ID: {data.get('batch_id', 'N/A')}")
                    logger.info(f"   成功: {data.get('success_count', 0)}, 失败: {data.get('failed_count', 0)}")
                except:
                    pass
            else:
                logger.error(f"❌ 批量发送失败: {response.message}")

            return response
        except Exception as e:
            logger.error(f"💥 批量发送失败: {e}")
            return None

    async def close(self):
        """关闭连接"""
        if hasattr(self.channel, 'close'):
            await self.channel.close()


async def main():
    """主测试函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS微服务测试客户端")
    parser.add_argument("--target", "-t", default="localhost:50052",
                       help="gRPC服务器地址")
    parser.add_argument("--phone", "-p", default="+8619834717434",
                       help="测试手机号码")
    parser.add_argument("--batch", "-b", action="store_true",
                       help="批量发送测试")

    args = parser.parse_args()

    # 配置日志
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<level>{message}</level>",
        level="INFO",
        colorize=True
    )

    # 创建客户端
    client = SMSTestClient(args.target)

    try:
        # 1. 健康检查
        logger.info("\n" + "="*50)
        logger.info("1. 健康检查")
        logger.info("="*50)
        await client.health_check()
        await asyncio.sleep(1)

        # 2. 发送单条短信
        logger.info("\n" + "="*50)
        logger.info("2. 发送单条短信")
        logger.info("="*50)
        content = f"测试短信 {time.strftime('%Y-%m-%d %H:%M:%S')} - 这是一条来自Python SMS微服务的测试短信。"
        content+= f"\n" + "-"*15 + "\n"
        content+= f"Test SMS {time.strftime('%Y-%m-%d %H:%M:%S')} - This is a test from Python SMS service."
        await client.send_sms(args.phone, content)
        await asyncio.sleep(2)

        # 3. 批量发送测试（如果指定）
        if args.batch:
            logger.info("\n" + "="*50)
            logger.info("3. 批量发送测试")
            logger.info("="*50)

            phone_numbers = [
                args.phone,
                "19834717434",  # 测试号码
                "+8619834717434"   # 测试号码
            ]

            batch_content = f"批量测试短信 {time.strftime('%Y-%m-%d %H:%M:%S')} - 这是批量测试短信内容。"
            await client.send_batch_sms(phone_numbers, batch_content)

        logger.info("\n" + "="*50)
        logger.info("✅ 所有测试完成")
        logger.info("="*50)

    except KeyboardInterrupt:
        logger.info("\n⌨️ 用户中断测试")
    except Exception as e:
        logger.error(f"💥 测试过程中发生错误: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
