"""
测试客户端 - 用于测试SMS微服务
"""
import asyncio
import json
import time
from pathlib import Path
import grpc
from loguru import logger

from src.sms_service import sms_pb2, sms_pb2_grpc


class SMSTestClient:
    """SMS测试客户端"""

    def __init__(self, target: str = "localhost:50052"):
        self.target = target
        self.channel = grpc.aio.insecure_channel(target)
        self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)

    async def health_check(self):
        """健康检查"""
        logger.info(f"🩺 健康检查: {self.target}")

        try:
            response = await self.stub.HealthCheck(sms_pb2.HealthCheckRequest())

            if response.status == 200:
                logger.info(f"✅ 服务健康: {response.message}")
                data = json.loads(response.data)
                logger.info(f"   调制解调器: {data.get('modem_port')}")
                logger.info(f"   信号强度: {data.get('signal_strength')}")
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
                data = json.loads(response.data)
                logger.info(f"   消息ID: {data.get('message_id')}")
                logger.info(f"   参考号: {data.get('reference')}")
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
                data = json.loads(response.data)
                logger.info(f"   批次ID: {data.get('batch_id')}")
                logger.info(f"   成功: {data.get('success_count')}, 失败: {data.get('failed_count')}")
            else:
                logger.error(f"❌ 批量发送失败: {response.message}")

            return response
        except Exception as e:
            logger.error(f"💥 批量发送失败: {e}")
            return None

    async def close(self):
        """关闭连接"""
        await self.channel.close()


async def main():
    """主测试函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS微服务测试客户端")
    parser.add_argument("--target", "-t", default="localhost:50052",
                       help="gRPC服务器地址")
    parser.add_argument("--phone", "-p", default="+8613800138000",
                       help="测试手机号码")
    parser.add_argument("--batch", "-b", action="store_true",
                       help="批量发送测试")

    args = parser.parse_args()

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
        await client.send_sms(args.phone, content)
        await asyncio.sleep(2)

        # 3. 批量发送测试（如果指定）
        if args.batch:
            logger.info("\n" + "="*50)
            logger.info("3. 批量发送测试")
            logger.info("="*50)

            phone_numbers = [
                args.phone,
                "+8613813813813",  # 测试号码
                "+8613913913913"   # 测试号码
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
