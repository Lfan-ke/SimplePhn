"""
SMS微服务主程序 - 修复信号处理版本
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
from src.common.serial_detector import SerialDetector
from src.sms_service import sms_pb2_grpc
from src.sms_service.server import SMSService
from src.sms_service.sms_sender import SMSSender
from src.sms_service.consul_client import ConsulClient


class SMSMicroservice:
    """SMS微服务管理器"""

    def __init__(self, config_path: str = "config/sms.yaml"):
        self.config_path = Path(config_path)
        self.config: Optional[ConfigManager] = None
        self.serial_detector: Optional[SerialDetector] = None
        self.sms_sender: Optional[SMSSender] = None
        self.consul_client: Optional[ConsulClient] = None
        self.grpc_server: Optional[grpc.aio.Server] = None
        self._shutdown_event = asyncio.Event()
        self._shutting_down = False

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

            # 4. 检测调制解调器
            logger.info("📡 检测调制解调器...")
            self.serial_detector = SerialDetector(self.config)
            modems = await self.serial_detector.detect_modems()

            if not modems:
                logger.error("❌ 未检测到可用的调制解调器")
                return False

            # 选择最佳调制解调器
            best_modem = self.serial_detector.get_best_modem()
            if not best_modem:
                logger.error("❌ 无法选择调制解调器")
                return False

            logger.info(f"✅ 使用调制解调器: {best_modem.port} ({best_modem.manufacturer} {best_modem.model})")

            # 5. 初始化短信发送器
            serial_config = cfg.serial
            self.sms_sender = SMSSender(
                port=best_modem.port,
                baudrate=serial_config.baudrate,
                timeout=serial_config.timeout
            )

            # 连接到调制解调器
            connected = await self.sms_sender.connect()
            if not connected:
                logger.error("❌ 调制解调器连接失败")
                return False

            # 6. 解析监听地址
            host, port_str = cfg.server.listen_on.split(":")
            port = int(port_str)

            # 如果是通配符地址，获取本地IP
            if host in ["0.0.0.0", "127.0.0.1", "[::]", "[::1]"]:
                host = socket.gethostbyname(socket.gethostname())

            # 7. 注册到Consul
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

                if await self.consul_client.register_service(
                    service_name=cfg.server.name,
                    address=host,
                    port=port,
                    service_desc="短信发送微服务，支持中文短信",
                    server_data=server_data,
                    meta={
                        "version": "1.0.0",
                        "modem_port": best_modem.port,
                        "modem_model": best_modem.model,
                        "signal": best_modem.signal_strength
                    }
                ):
                    logger.info("✅ Consul注册成功")
                else:
                    logger.warning("⚠️ Consul注册失败，服务继续运行")

            # 8. 启动gRPC服务器
            server_config = cfg.server
            self.grpc_server = grpc.aio.server(
                futures.ThreadPoolExecutor(max_workers=server_config.max_workers)
            )

            sms_service = SMSService(self.sms_sender)
            sms_pb2_grpc.add_SMSServiceServicer_to_server(sms_service, self.grpc_server)

            self.grpc_server.add_insecure_port(server_config.listen_on)
            await self.grpc_server.start()

            logger.info(f"✅ gRPC服务器启动在 {server_config.listen_on}")
            logger.info(f"📱 服务名称: {server_config.name}")
            logger.info(f"🔧 运行模式: {server_config.mode}")

            if cfg.consul.host and cfg.consul.host != "127.0.0.1:8500":
                logger.info(f"🌐 Consul地址: {cfg.consul.host}")
                logger.info(f"🗂️ KV路径: echo_wing/{cfg.server.name}")

            return True

        except Exception as e:
            logger.error(f"❌ 服务启动失败: {e}")
            return False

    async def stop(self):
        """停止微服务"""
        if self._shutting_down:
            return

        self._shutting_down = True
        logger.info("🛑 停止SMS微服务...")

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
                # 设置较短的grace时间
                await self.grpc_server.stop(grace=2)
                logger.info("✅ gRPC服务器已停止")
            except Exception as e:
                logger.error(f"❌ 停止gRPC服务器失败: {e}")

        # 断开调制解调器连接
        if self.sms_sender:
            try:
                await self.sms_sender.disconnect()
                logger.info("✅ 调制解调器连接已断开")
            except Exception as e:
                logger.error(f"❌ 断开调制解调器连接失败: {e}")

        logger.info("👋 SMS微服务已停止")

    async def wait_for_shutdown(self):
        """等待关闭信号"""
        await self._shutdown_event.wait()

    def request_shutdown(self):
        """请求关闭服务"""
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()

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


async def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS微服务")
    parser.add_argument("--config", "-c", default="config/sms.yaml",
                       help="配置文件路径")
    args = parser.parse_args()

    # 创建微服务实例
    service = SMSMicroservice(args.config)

    # 启动服务
    started = await service.start()
    if not started:
        logger.error("❌ 服务启动失败")
        sys.exit(1)

    # 设置信号处理
    def signal_handler(signum, frame):
        logger.info(f"📶 收到信号 {signum}，正在关闭...")
        # 直接设置关闭事件，而不是创建新任务
        service.request_shutdown()

    # 使用signal.signal而不是asyncio的信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # 等待关闭信号
        await service.wait_for_shutdown()
    except KeyboardInterrupt:
        logger.info("⌨️ 收到键盘中断")
        service.request_shutdown()
    except Exception as e:
        logger.error(f"💥 服务器异常: {e}")
        service.request_shutdown()
    finally:
        # 确保服务被停止
        await service.stop()

    logger.info("🏁 服务关闭完成")


if __name__ == "__main__":
    # 使用asyncio.run但处理信号正确
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"主程序异常: {e}")
        sys.exit(1)
