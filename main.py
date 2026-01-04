"""
SMS微服务主程序 - 支持多调制解调器管理
"""
import asyncio
import signal
import socket
import sys
from concurrent import futures
from pathlib import Path
from typing import Optional
import grpc
from loguru import logger

from src.common.config import ConfigManager
from src.common.serial_manager import SerialManager
from src.sms_service import sms_pb2_grpc
from src.sms_service.server import SMSService
from src.sms_service.consul_client import ConsulClient


class SMSMicroservice:
    """SMS微服务管理器"""

    def __init__(self, config_path: str = "config/sms.yaml"):
        self.config_path = Path(config_path)
        self.config: Optional[ConfigManager] = None
        self.serial_manager: Optional[SerialManager] = None
        self.consul_client: Optional[ConsulClient] = None
        self.grpc_server: Optional[grpc.aio.Server] = None
        self._shutdown_event = asyncio.Event()
        self._shutting_down = False
        self._main_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

    async def start(self) -> bool:
        """启动微服务"""
        try:
            logger.info("🚀 启动SMS微服务...")

            # 1. 加载配置
            self.config = ConfigManager()
            if not await self.config.load_config(self.config_path):
                logger.error("❌ 配置加载失败")
                return False

            cfg = self.config.get_config()

            # 2. 配置日志
            await self._setup_logging(cfg.log)

            # 3. 打印配置信息
            await self._print_config(cfg)

            # 4. 初始化串口管理器（检测并连接所有调制解调器）
            logger.info("📡 初始化串口管理器...")
            self.serial_manager = SerialManager(self.config)

            if not await self.serial_manager.initialize():
                logger.error("❌ 串口管理器初始化失败")
                return False

            # 获取健康状态
            health_status = await self.serial_manager.get_health_status()

            # 5. 解析监听地址
            host, port_str = cfg.server.listen_on.split(":")
            port = int(port_str)

            # 如果是通配符地址，获取本地IP
            if host in ["0.0.0.0", "127.0.0.1", "[::]", "[::1]"]:
                host = socket.gethostbyname(socket.gethostname())

            # 6. 注册到Consul
            if cfg.consul.host and cfg.consul.host != "127.0.0.1:8500":
                logger.info(f"🔗 连接Consul: {cfg.consul.host}")

                server_data = {
                    "fields": {
                        "phone_number": {
                            "required": True,
                            "type": "string",
                            "pattern": r"^\+?[1-9]\d{1,14}$",
                            "description": "手机号码（国际格式）"
                        },
                        "content": {
                            "required": True,
                            "type": "string",
                            "maxLength": 1000,
                            "description": "短信内容"
                        },
                        "sender_id": {
                            "required": False,
                            "type": "string",
                            "description": "发送者标识"
                        },
                        "delivery_report": {
                            "required": False,
                            "type": "boolean",
                            "description": "是否要求送达报告"
                        }
                    }
                }

                self.consul_client = ConsulClient(
                    host=cfg.consul.host,
                    token=cfg.consul.token,
                    scheme=cfg.consul.scheme
                )

                meta = {
                    "version": "1.0.0",
                    "available_modems": str(health_status["available_modems"]),
                    "total_modems": str(health_status["total_modems"])
                }

                # 添加调制解调器信息
                for i, modem in enumerate(health_status["modems"][:3]):  # 只显示前3个
                    meta[f"modem_{i+1}_port"] = modem["port"]
                    meta[f"modem_{i+1}_signal"] = modem["signal_strength"]

                if await self.consul_client.register_service(
                    service_name=cfg.server.name,
                    address=host,
                    port=port,
                    service_desc="短信发送微服务，支持中文短信",
                    server_data=server_data,
                    meta=meta
                ):
                    logger.info("✅ Consul注册成功")
                else:
                    logger.warning("⚠️ Consul注册失败，服务继续运行")

            # 7. 启动gRPC服务器
            server_config = cfg.server
            self.grpc_server = grpc.aio.server(
                futures.ThreadPoolExecutor(max_workers=server_config.max_workers)
            )

            # 使用串口管理器创建服务
            sms_service = SMSService(self.serial_manager)
            sms_pb2_grpc.add_SMSServiceServicer_to_server(sms_service, self.grpc_server)

            self.grpc_server.add_insecure_port(server_config.listen_on)
            await self.grpc_server.start()

            logger.info(f"✅ gRPC服务器启动在 {server_config.listen_on}")
            logger.info(f"📱 服务名称: {server_config.name}")
            logger.info(f"🔧 运行模式: {server_config.mode}")
            logger.info(f"📡 可用调制解调器: {health_status['available_modems']}/{health_status['total_modems']}")

            # 打印调制解调器详情
            for modem in health_status["modems"]:
                status = "✅ 可用" if modem["is_available"] else "❌ 不可用"
                in_use = " (使用中)" if modem["in_use"] else ""
                logger.info(f"   {modem['port']}: {modem['manufacturer']} {modem['model']} - 信号: {modem['signal_strength']} {status}{in_use}")

            if cfg.consul.host and cfg.consul.host != "127.0.0.1:8500":
                logger.info(f"🌐 Consul地址: {cfg.consul.host}")
                logger.info(f"🗂️ KV路径: echo_wing/{cfg.server.name}")

            # 8. 启动健康检查任务
            self._health_task = asyncio.create_task(self._health_check_task())

            return True

        except Exception as e:
            logger.error(f"❌ 服务启动失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def _health_check_task(self):
        """健康检查任务"""
        try:
            while not self._shutting_down:
                await asyncio.sleep(30)  # 每30秒检查一次

                if not self.serial_manager:
                    continue

                # 测试连接
                connected = await self.serial_manager.test_all_connections()
                if not connected:
                    logger.warning("⚠️ 健康检查: 部分调制解调器连接失败")

                # 打印状态
                health_status = await self.serial_manager.get_health_status()
                logger.debug(f"📊 调制解调器状态: {health_status['available_modems']}/{health_status['total_modems']} 可用")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"健康检查任务异常: {e}")

    async def run(self):
        """运行服务主循环"""
        self._main_task = asyncio.current_task()
        try:
            await self.grpc_server.wait_for_termination()
        except asyncio.CancelledError:
            logger.info("服务任务被取消")
            raise
        except Exception as e:
            logger.error(f"gRPC服务器异常: {e}")
        finally:
            self._main_task = None

    async def stop(self):
        """停止微服务"""
        if self._shutting_down:
            return

        self._shutting_down = True
        logger.info("🛑 停止SMS微服务...")

        # 取消健康检查任务
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # 取消主任务
        if self._main_task:
            self._main_task.cancel()
            try:
                await self._main_task
            except asyncio.CancelledError:
                pass

        # 注销Consul服务
        if self.consul_client:
            try:
                await self.consul_client.deregister_service()
                logger.info("✅ Consul服务已注销")
            except Exception as e:
                logger.error(f"❌ Consul注销失败: {e}")

        # 停止gRPC服务器
        if self.grpc_server:
            try:
                await self.grpc_server.stop(grace=0)
                logger.info("✅ gRPC服务器已停止")
            except Exception as e:
                logger.error(f"❌ 停止gRPC服务器失败: {e}")

        # 清理串口管理器
        if self.serial_manager:
            try:
                await self.serial_manager.cleanup()
                logger.info("✅ 串口管理器已清理")
            except Exception as e:
                logger.error(f"❌ 清理串口管理器失败: {e}")

        logger.info("👋 SMS微服务已停止")

    def request_shutdown(self):
        """请求关闭服务"""
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()
            if self._main_task:
                self._main_task.cancel()
            if self._health_task:
                self._health_task.cancel()

    async def wait_for_shutdown(self):
        """等待关闭信号"""
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    async def _setup_logging(self, log_config):
        """配置日志"""
        logger.remove()

        if log_config.mode in ["console", "both"]:
            logger.add(
                sys.stdout,
                format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                       "<level>{level: <8}</level> | "
                       "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                       "<level>{message}</level>",
                level=log_config.level.upper(),
                colorize=True
            )

        if log_config.mode in ["file", "both"] and log_config.file_path:
            log_file = Path(log_config.file_path)
            log_file.parent.mkdir(parents=True, exist_ok=True)

            logger.add(
                str(log_file),
                format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
                       "{name}:{function}:{line} - {message}",
                level=log_config.level.upper(),
                rotation="1 day",
                retention="7 days",
                encoding=log_config.encoding
            )

    async def _print_config(self, cfg):
        """打印配置信息"""
        logger.info("\n" + "="*50)
        logger.info("📋 服务配置:")
        logger.info(f"   服务名称: {cfg.server.name}")
        logger.info(f"   监听地址: {cfg.server.listen_on}")
        logger.info(f"   运行模式: {cfg.server.mode}")

        if cfg.consul.host and cfg.consul.host != "127.0.0.1:8500":
            logger.info(f"   Consul地址: {cfg.consul.host}")

        logger.info(f"   串口波特率: {cfg.serial.baudrate}")
        logger.info(f"   日志级别: {cfg.log.level}")
        logger.info("="*50 + "\n")


async def shutdown_handler(service: SMSMicroservice, signum):
    """异步信号处理函数"""
    logger.info(f"📶 收到信号 {signum}，正在关闭...")
    service.request_shutdown()


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS微服务")
    parser.add_argument("--config", "-c", default="config/sms.yaml",
                       help="配置文件路径")
    args = parser.parse_args()

    # 创建微服务实例
    service = SMSMicroservice(args.config)

    # 创建事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # 设置信号处理
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown_handler(service, s))
        )

    async def main_async():
        """异步主函数"""
        # 启动服务
        started = await service.start()
        if not started:
            logger.error("❌ 服务启动失败")
            sys.exit(1)

        try:
            # 创建并运行服务任务
            service_task = asyncio.create_task(service.run())

            # 等待关闭信号
            await service.wait_for_shutdown()

            # 停止服务
            await service.stop()

            # 等待服务任务完成
            await service_task
        except asyncio.CancelledError:
            logger.info("主任务被取消")
        except Exception as e:
            logger.error(f"主程序异常: {e}")
        finally:
            # 清理信号处理器
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

            logger.info("🏁 服务关闭完成")

    try:
        # 运行主循环
        loop.run_until_complete(main_async())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常: {e}")
        sys.exit(1)
    finally:
        # 关闭事件循环
        loop.close()


if __name__ == "__main__":
    main()
