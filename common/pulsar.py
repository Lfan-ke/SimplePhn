import asyncio
import json
import pulsar
from typing import Any, Callable, Awaitable
from logger import logger

# 消息处理器类型定义
MessageHandler = Callable[[Any], Awaitable[bool]]


class PulsarService:
    """
    Pulsar服务基类，遵循 echo-wing/{main, retry}/{service_name} 命名规范
    """

    def __init__(
            self,
            service_name: str,  # 服务名称: mail, sms 等
            pulsar_url: str = "pulsar://localhost:6650",
            main_topic: str = "echo-wing/main",
            dlq_topic: str = "echo-wing/dlq",
            subscription_name: str = None,
            consumer_name: str = None,
            pulsar_token: str = None,
    ):
        self.service_name = service_name
        self.pulsar_url = pulsar_url
        self.pulsar_token = pulsar_token

        self.main_topic = f"persistent://echo-wing/main/{service_name}" if not main_topic else main_topic
        self.dlq_topic = "persistent://echo-wing/dlq/all" if not dlq_topic else dlq_topic

        self.subscription_name = subscription_name or f"{service_name}-subscription"
        self.consumer_name = consumer_name or f"{service_name}-consumer"

        self.max_redelivery_count = 3

        self.client = None
        self.consumer = None
        self.task = None

    async def start(
            self,
            message_handler: MessageHandler,
            max_redelivery_count: int = 3,
            negative_ack_delay_ms: int = 90000,   # 负确认重试延迟
            ack_timeout_ms: int = 600000,         # ACK超时时间
            receiver_queue_size: int = 1000
    ) -> asyncio.Task:
        """
        启动Pulsar监听服务

        Args:
            message_handler: 消息处理函数
            max_redelivery_count: 最大重试次数（包含首次消费）
            negative_ack_delay_ms: 负确认后重试延迟（毫秒）
            ack_timeout_ms: ACK超时时间（毫秒）
            receiver_queue_size: 接收队列大小
        """

        self.max_redelivery_count = max_redelivery_count

        async def _pulsar_listener() -> None:
            """Pulsar监听主函数"""
            try:
                client_kwargs = {
                    "service_url": self.pulsar_url,
                    "io_threads": 1,
                    "operation_timeout_seconds": 30,
                }

                if self.pulsar_token:
                    client_kwargs |= { "authentication": pulsar.AuthenticationToken(self.pulsar_token) }

                self.client = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: pulsar.Client(**client_kwargs),
                )

                # 配置死信策略
                dead_letter_policy = pulsar.ConsumerDeadLetterPolicy(
                    max_redeliver_count=max_redelivery_count,
                    dead_letter_topic=self.dlq_topic,
                )

                # 创建消费者
                self.consumer = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.client.subscribe(
                        topic=self.main_topic,
                        subscription_name=self.subscription_name,
                        consumer_name=self.consumer_name,
                        consumer_type=pulsar.ConsumerType.Shared,
                        receiver_queue_size=receiver_queue_size,
                        dead_letter_policy=dead_letter_policy,                       # 应用死信策略
                        negative_ack_redelivery_delay_ms=negative_ack_delay_ms,      # 负确认延迟
                        unacked_messages_timeout_ms=ack_timeout_ms,                  # ACK超时
                    )
                )

                await logger.info(f"✅ {self.service_name} 服务已就绪")

                # 主监听循环
                while True:
                        msg = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self.consumer.receive(),
                        )

                        if msg is None:
                            continue

                        await self._process_message(msg, message_handler)

            except Exception as e:
                await logger.error(f"💥 {self.service_name} 服务启动失败: {e}")
                raise
            finally:
                await self._cleanup()

        # 创建并启动任务
        self.task = asyncio.create_task(_pulsar_listener())
        return self.task

    async def _process_message(
            self,
            msg: pulsar.Message,
            message_handler: MessageHandler
    ) -> None:
        """处理单条消息"""
        msg_id = msg.message_id()

        try:
            await logger.info(f"📨 [{self.service_name}] 收到消息: {msg_id}")

            redelivery_count = msg.redelivery_count()
            if redelivery_count > 0:
                await logger.warn(f"🔄 [{self.service_name}] 第{redelivery_count}次重试")

            # 检查是否已超过最大重试次数
            if redelivery_count >= self.max_redelivery_count:
                await logger.warn(f"💀 [{self.service_name}] 已达到最大重试次数({self.max_redelivery_count})，进入死信队列: {msg_id}")
                await self._negative_ack(msg)
                return

            # 解析JSON
            try:
                payload = json.loads(msg.data().decode('utf-8')) if msg.data() else {}
            except json.JSONDecodeError as e:
                await logger.error(f"📄 [{self.service_name}] JSON解析失败: {e}")
                await self._negative_ack(msg)
                return

            # 添加服务标识
            payload["_service"] = self.service_name
            payload["_msg_id"] = str(msg_id)

            # 执行业务处理
            success = await message_handler(payload)

            if success:
                await self._ack(msg)
                await logger.info(f"✅ [{self.service_name}] 处理成功: {msg_id}")
            else:
                # 处理失败，负确认 - Pulsar会自动重试
                await self._negative_ack(msg)
                await logger.warn(f"🔄 [{self.service_name}] 处理失败，触发自动重试: {msg_id}")

        except Exception as e:
            await logger.error(f"⚠️  [{self.service_name}] 消息处理异常: {e}")
            await self._negative_ack(msg)

    async def _ack(self, msg: pulsar.Message) -> None:
        """确认消息"""
        await asyncio.get_event_loop().run_in_executor(
            None, self.consumer.acknowledge, msg
        )

    async def _negative_ack(self, msg: pulsar.Message) -> None:
        """负确认消息 - 触发自动重试"""
        await asyncio.get_event_loop().run_in_executor(
            None, self.consumer.negative_acknowledge, msg
        )

    async def _cleanup(self) -> None:
        """清理资源"""
        try:
            if self.consumer:
                await asyncio.get_event_loop().run_in_executor(None, self.consumer.close)
                await logger.info(f"🔌 [{self.service_name}] 消费者已关闭")
            if self.client:
                await asyncio.get_event_loop().run_in_executor(None, self.client.close)
                await logger.info(f"🔌 [{self.service_name}] 客户端已关闭")
        except Exception as e:
            await logger.error(f"🧹 [{self.service_name}] 清理资源出错: {e}")

    async def stop(self) -> None:
        """停止服务"""
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                await logger.info(f"🛑 [{self.service_name}] 服务已停止")

            await self._cleanup()
