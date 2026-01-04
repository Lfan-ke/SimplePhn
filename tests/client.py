"""
gRPC连接测试 - 多种短信内容测试
通过gRPC连接SMS服务，测试多种短信内容
"""
import asyncio
import json
import time
import sys
import grpc
from pathlib import Path
from loguru import logger

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# 尝试导入gRPC模块
try:
    from src.sms_service import sms_pb2, sms_pb2_grpc
    HAS_GRPC = True
except ImportError as e:
    logger.error(f"❌ 无法导入gRPC模块: {e}")
    logger.error("请先运行以下命令生成gRPC代码:")
    logger.error("python -m grpc_tools.protoc -Iproto --python_out=src/sms_service --grpc_python_out=src/sms_service proto/sms.proto")
    sys.exit(1)


class GRPCSMSTester:
    """gRPC SMS测试器"""

    def __init__(self, target: str = "localhost:50052"):
        self.target = target
        self.channel = None
        self.stub = None

    async def connect(self) -> bool:
        """连接到gRPC服务"""
        try:
            logger.info(f"🔗 连接到gRPC服务: {self.target}")
            self.channel = grpc.aio.insecure_channel(self.target)
            self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)

            # 测试连接
            try:
                await asyncio.wait_for(
                    self.stub.HealthCheck(sms_pb2.HealthCheckRequest()),
                    timeout=5.0
                )
                logger.info("✅ gRPC连接成功")
                return True
            except asyncio.TimeoutError:
                logger.error("⏰ 连接超时")
                return False
            except Exception as e:
                logger.error(f"❌ 连接测试失败: {e}")
                return False

        except Exception as e:
            logger.error(f"💥 连接gRPC服务失败: {e}")
            return False

    async def close(self):
        """关闭连接"""
        if self.channel:
            await self.channel.close()
            logger.info("🔌 已关闭gRPC连接")

    async def health_check(self):
        """健康检查"""
        try:
            logger.info("🩺 执行健康检查...")
            response = await self.stub.HealthCheck(sms_pb2.HealthCheckRequest())

            if response.status == 200:
                logger.info(f"✅ 服务健康: {response.message}")
                try:
                    data = json.loads(response.data)
                    logger.info(f"   调制解调器: {data.get('modem_port', 'N/A')}")
                    logger.info(f"   信号强度: {data.get('signal_strength', 'N/A')}")
                    logger.info(f"   连接状态: {data.get('modem_connected', 'N/A')}")
                except:
                    logger.info(f"   原始数据: {response.data[:100]}...")
                return True
            else:
                logger.error(f"❌ 服务不健康: {response.message}")
                return False

        except Exception as e:
            logger.error(f"💥 健康检查失败: {e}")
            return False

    async def send_test_sms(self, test_case: dict) -> bool:
        """发送测试短信"""
        try:
            logger.info(f"\n📋 测试用例: {test_case['name']}")
            logger.info(f"📱 发送到: {test_case['phone']}")
            logger.info(f"📄 内容预览: {test_case['content'][:80]}...")
            logger.info(f"📏 内容长度: {len(test_case['content'])} 字符")

            # 创建请求
            request = sms_pb2.SendSMSRequest(
                phone_number=test_case['phone'],
                content=test_case['content'],
                sender_id=f"test_{test_case['strategy']}",
                delivery_report=True,
                metadata={
                    "test_case": test_case['name'],
                    "strategy": test_case['strategy'],
                    "timestamp": str(time.time()),
                    "length": str(len(test_case['content']))
                }
            )

            # 发送短信
            logger.info("🚀 发送短信中...")
            start_time = time.time()
            response = await self.stub.SendSMS(request)
            elapsed_time = time.time() - start_time

            if response.status == 200:
                logger.info(f"✅ 发送成功 ({elapsed_time:.2f}s)")
                logger.info(f"   状态: {response.message}")
                try:
                    data = json.loads(response.data)
                    logger.info(f"   消息ID: {data.get('message_id', 'N/A')}")
                    logger.info(f"   参考号: {data.get('reference', 'N/A')}")
                    logger.info(f"   调制解调器端口: {data.get('modem_port', 'N/A')}")
                except:
                    logger.info(f"   数据: {response.data[:100]}...")
                return True
            else:
                logger.error(f"❌ 发送失败 ({elapsed_time:.2f}s)")
                logger.error(f"   错误: {response.message}")
                logger.error(f"   状态码: {response.status}")
                return False

        except Exception as e:
            logger.error(f"💥 发送过程中出错: {e}")
            return False


async def main():
    """主测试函数"""
    import argparse

    parser = argparse.ArgumentParser(description="gRPC SMS多内容测试")
    parser.add_argument("--target", "-t", default="localhost:50052",
                       help="gRPC服务器地址")
    parser.add_argument("--phone", "-p", default="+8619834717434",
                       help="测试手机号码")

    args = parser.parse_args()

    # 配置日志
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan> - "
               "<level>{message}</level>",
        level="INFO",
        colorize=True
    )

    # 创建测试器
    tester = GRPCSMSTester(args.target)

    try:
        # 1. 连接到gRPC服务
        logger.info("\n" + "="*60)
        logger.info("步骤1: 连接到gRPC服务")
        logger.info("="*60)

        if not await tester.connect():
            logger.error("❌ 无法连接到gRPC服务，测试终止")
            return

        # 2. 健康检查
        logger.info("\n" + "="*60)
        logger.info("步骤2: 服务健康检查")
        logger.info("="*60)

        if not await tester.health_check():
            logger.warning("⚠️  服务健康检查失败，但继续测试...")

        # 3. 准备测试用例（使用与客户端相同的内容格式）
        test_cases = [
            {
                "name": "纯英文短信",
                "strategy": "english_only",
                "phone": args.phone,
                "content": f"Hello World! Test SMS from Python gRPC client at {time.strftime('%H:%M:%S')}. This is a pure English test message to verify the SMS service functionality."
            },
            {
                "name": "纯中文短信",
                "strategy": "chinese_only",
                "phone": args.phone,
                "content": f"测试短信 {time.strftime('%Y-%m-%d %H:%M:%S')} - 这是一条纯中文测试短信，用于验证Python SMS微服务的gRPC接口功能。短信服务应该正确处理中文编码。"
            },
            {
                "name": "中英文混合短信",
                "strategy": "mixed_content",
                "phone": args.phone,
                "content": f"测试短信 Test Message {time.strftime('%H:%M:%S')} - 这是一条中英文混合的测试短信 Mixed Chinese and English test message.\n\n"
                          f"English part: This SMS is sent via gRPC interface.\n"
                          f"中文部分：这条短信通过gRPC接口发送。\n"
                          f"混合测试：Test 测试 Mixed 混合 Message 消息"
            },
            {
                "name": "带特殊符号的短信",
                "strategy": "special_chars",
                "phone": args.phone,
                "content": f"【重要通知】Test SMS {time.strftime('%Y-%m-%d')} 14:30\n"
                          f"📱 测试内容包含特殊符号：\n"
                          f"• 项目符号\n"
                          f"★ 星号符号\n"
                          f"→ 箭头符号\n"
                          f"💰 货币符号\n"
                          f"✅ 完成符号\n\n"
                          f"Special chars: !@#$%^&*()_+-=[]{{}}|;:',.<>/?~`"
            },
            {
                "name": "长文本短信测试",
                "strategy": "long_text",
                "phone": args.phone,
                "content": f"长文本短信测试 {time.strftime('%H:%M:%S')}\n\n"
                          f"这是一条较长的测试短信，用于测试短信服务的文本处理能力。"
                          f"短信内容包含多个段落和换行符，确保服务能够正确处理。\n\n"
                          f"English paragraph: This is a longer test message to verify the SMS service's "
                          f"ability to handle extended text content with mixed Chinese and English characters. "
                          f"The message includes multiple paragraphs and line breaks.\n\n"
                          f"最后一段：测试结束。感谢使用Python SMS微服务。"
                          f"参考编号：TEST-{int(time.time())}"
            },
            {
                "name": "客户端实际内容测试",
                "strategy": "client_actual",
                "phone": args.phone,
                "content": f"测试短信 {time.strftime('%Y-%m-%d %H:%M:%S')} - 这是一条来自Python SMS微服务的测试短信。\n" +
                          f"{'-'*15}\n" +
                          f"Test SMS {time.strftime('%Y-%m-%d %H:%M:%S')} - This is a test from Python SMS service."
            }
        ]

        # 4. 执行测试用例
        logger.info("\n" + "="*60)
        logger.info("步骤3: 执行多种短信内容测试")
        logger.info("="*60)

        total_cases = len(test_cases)
        success_count = 0
        failure_count = 0

        for i, test_case in enumerate(test_cases):
            logger.info(f"\n📊 测试进度: {i+1}/{total_cases}")
            logger.info("-" * 40)

            success = await tester.send_test_sms(test_case)

            if success:
                success_count += 1
            else:
                failure_count += 1

            # 等待一下再发送下一条（避免太快）
            if i < total_cases - 1:
                logger.info(f"⏳ 等待3秒后继续...")
                await asyncio.sleep(3)

        # 5. 测试结果汇总
        logger.info("\n" + "="*60)
        logger.info("测试结果汇总")
        logger.info("="*60)

        logger.info(f"📈 总测试用例: {total_cases}")
        logger.info(f"✅ 成功: {success_count}")
        logger.info(f"❌ 失败: {failure_count}")
        logger.info(f"📊 成功率: {success_count/total_cases*100:.1f}%")

        if success_count == total_cases:
            logger.info("🎉 所有测试用例都通过了！gRPC SMS服务运行正常！")
        elif success_count > 0:
            logger.info(f"⚠️  部分测试通过 ({success_count}/{total_cases})")
        else:
            logger.error("💥 所有测试都失败了！请检查SMS服务配置和调制解调器连接。")

        # 6. 批量发送测试（可选）
        logger.info("\n" + "="*60)
        logger.info("步骤4: 批量发送测试（可选）")
        logger.info("="*60)

        try:
            batch_request = sms_pb2.SendBatchSMSRequest(
                phone_numbers=[args.phone, args.phone],  # 同一个号码两次，测试批量功能
                content=f"批量测试短信 {time.strftime('%H:%M:%S')} - 这是批量功能测试",
                sender_id="batch_test",
                delivery_report=False,
                metadata={"batch_test": "true", "timestamp": str(time.time())}
            )

            logger.info("🚀 发送批量短信测试...")
            batch_response = await tester.stub.SendBatchSMS(batch_request)

            if batch_response.status == 200:
                logger.info(f"✅ 批量发送成功: {batch_response.message}")
                try:
                    batch_data = json.loads(batch_response.data)
                    logger.info(f"   批次ID: {batch_data.get('batch_id', 'N/A')}")
                    logger.info(f"   总数量: {batch_data.get('total_count', 0)}")
                    logger.info(f"   成功: {batch_data.get('success_count', 0)}")
                    logger.info(f"   失败: {batch_data.get('failed_count', 0)}")
                except:
                    pass
            else:
                logger.warning(f"⚠️  批量发送失败: {batch_response.message}")

        except Exception as e:
            logger.warning(f"⚠️  批量发送测试跳过: {e}")

        logger.info("\n" + "="*60)
        logger.info("🎯 测试完成")
        logger.info("="*60)

    except KeyboardInterrupt:
        logger.info("\n⌨️ 用户中断测试")
    except Exception as e:
        logger.error(f"💥 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await tester.close()


if __name__ == "__main__":
    asyncio.run(main())
