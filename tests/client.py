"""
SMS 微服务测试客户端
"""
import asyncio
import json
import sys
from pathlib import Path
import grpc
from loguru import logger

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sms import sms_pb2, sms_pb2_grpc


class SMSClient:
    """SMS 微服务客户端"""

    def __init__(self, target: str = "localhost:50052"):
        self.target = target
        self.channel = None
        self.stub = None

    async def connect(self):
        """连接到 gRPC 服务"""
        logger.info(f"🔗 连接到: {self.target}")
        self.channel = grpc.aio.insecure_channel(self.target)
        self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)

        # 测试连接
        try:
            response = await asyncio.wait_for(
                self.stub.HealthCheck(sms_pb2.HealthCheckRequest()),
                timeout=5.0
            )
            logger.info(f"✅ 连接成功: {response.message}")
            return True
        except Exception as e:
            logger.error(f"❌ 连接失败: {e}")
            return False

    async def close(self):
        """关闭连接"""
        if self.channel:
            await self.channel.close()
            logger.info("🔌 连接已关闭")

    async def send_sms(self, phone_number: str, content: str, sender_id: str = "test"):
        """发送短信"""
        logger.info(f"📤 发送短信到: {phone_number}")

        request = sms_pb2.SendSMSRequest(
            phone_number=phone_number,
            content=content,
            sender_id=sender_id,
            delivery_report=True,
            metadata={
                "test": "true",
                "client": "python"
            }
        )

        try:
            response = await self.stub.SendSMS(request)

            logger.info(f"📨 响应: {response.message}")
            logger.info(f"📊 状态码: {response.status}")

            if response.data:
                try:
                    data = json.loads(response.data)
                    logger.info(f"📋 数据: {json.dumps(data, indent=2, ensure_ascii=False)}")
                except:
                    logger.info(f"📋 原始数据: {response.data}")

            return response

        except Exception as e:
            logger.error(f"❌ 发送失败: {e}")
            return None

    async def health_check(self):
        """健康检查"""
        logger.info("🩺 执行健康检查...")

        try:
            response = await self.stub.HealthCheck(sms_pb2.HealthCheckRequest())

            logger.info(f"📊 健康状态: {response.message}")
            logger.info(f"📈 状态码: {response.status}")

            if response.data:
                try:
                    data = json.loads(response.data)
                    logger.info(f"📋 健康数据: {json.dumps(data, indent=2, ensure_ascii=False)}")
                except:
                    pass

            return response

        except Exception as e:
            logger.error(f"❌ 健康检查失败: {e}")
            return None

    async def get_modem_status(self):
        """获取调制解调器状态"""
        logger.info("📡 获取调制解调器状态...")

        try:
            response = await self.stub.GetModemStatus(sms_pb2.ModemStatusRequest())

            logger.info(f"📊 调制解调器状态: {response.message}")

            if response.data:
                try:
                    data = json.loads(response.data)
                    logger.info(f"📋 状态数据: {json.dumps(data, indent=2, ensure_ascii=False)}")
                except:
                    pass

            return response

        except Exception as e:
            logger.error(f"❌ 获取调制解调器状态失败: {e}")
            return None


async def main():
    """主测试函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS 微服务测试客户端")
    parser.add_argument("--target", "-t", default="localhost:50052", help="gRPC 服务器地址")
    parser.add_argument("--phone", "-p", required=True, help="测试手机号码")

    args = parser.parse_args()

    # 配置日志
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
        colorize=True
    )

    client = SMSClient(args.target)

    try:
        # 连接服务
        if not await client.connect():
            logger.error("❌ 无法连接到服务")
            return

        # 健康检查
        await client.health_check()

        # 获取调制解调器状态
        await client.get_modem_status()

        # 发送测试短信
        test_content = f"【SMS微服务测试】\n时间: {asyncio.get_event_loop().time()}\n这是一条测试短信，用于验证 SMS 微服务的功能。\n✅ 中文和英文混合测试"

        await client.send_sms(args.phone, test_content, "test_client")

        # 测试长短信
        logger.info("\n📨 测试长短信...")
        long_content = "这是一个长短信测试，" * 30
        await client.send_sms(args.phone, long_content, "test_client_long")

        logger.info("\n🎉 测试完成！")

    except KeyboardInterrupt:
        logger.info("\n⌨️ 用户中断测试")
    except Exception as e:
        logger.error(f"💥 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
