import asyncio
import socket
import sys
import os
from pathlib import Path
from typing import Optional
from loguru import logger

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.common.config import ConfigManager
from src.common.consul import ConsulClient
from src.common.modem_manager import ModemManager
from src.sms.sender import SMSSender
from src.sms.server import create_server


class SMSMicroservice:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config: Optional[ConfigManager] = None
        self.consul_client: Optional[ConsulClient] = None
        self.modem_manager: Optional[ModemManager] = None
        self.sender: Optional[SMSSender] = None
        self.grpc_server = None
        self._shutting_down = False
        self._tasks = []

    async def start(self) -> bool:
        try:
            logger.info("🚀 启动 SMS 微服务...")

            self.config = ConfigManager()
            if not await self.config.load(self.config_path):
                logger.error("❌ 配置加载失败")
                return False

            cfg = self.config.get()

            await self._setup_logging(cfg.log)

            await self._print_config(cfg)

            logger.info("📡 初始化调制解调器管理器...")
            self.modem_manager = ModemManager(cfg)

            if not await self.modem_manager.initialize():
                logger.error("❌ 调制解调器管理器初始化失败")
                return False

            self.sender = SMSSender(self.modem_manager)

            host, port_str = cfg.server.listen_on.split(":")
            port = int(port_str)

            if host in ["0.0.0.0", "127.0.0.1", "[::]", "[::1]"]:
                host = socket.gethostbyname(socket.gethostname())

            if cfg.consul.host and cfg.consul.host != "localhost:8500":
                logger.info(f"🔗 连接到 Consul: {cfg.consul.host}")

                modem_status = await self.modem_manager.get_status()

                server_data = {
                    "fields": {
                        "phone_number": {
                            "type": "str",
                            "description": "手机号码（国际格式，如+8613800138000）",
                            "required": True,
                            "pattern": "^\\+[1-9]\\d{1,14}$"  # E.164格式
                        },
                        "content": {
                            "type": "str",
                            "description": "短信内容",
                            "required": True,
                        },
                        "sender_id": {
                            "type": "str",
                            "description": "发送者ID（可选）",
                            "required": False
                        },
                        "delivery_report": {
                            "type": "bool",
                            "description": "是否要求送达报告",
                            "required": False,
                            "default": False
                        },
                        "metadata": {
                            "type": "dict",
                            "description": "元数据",
                            "required": False
                        }
                    }
                }

                meta = {
                    "version": "1.0.0",
                    "available_modems": str(modem_status["available_modems"]),
                    "total_modems": str(modem_status["total_modems"]),
                    "host": socket.gethostname(),
                    "pid": str(os.getpid())
                }

                for i, modem in enumerate(modem_status["modems"][:3]):
                    meta[f"modem_{i+1}_port"] = modem["port"]
                    meta[f"modem_{i+1}_model"] = modem["model"]
                    meta[f"modem_{i+1}_signal"] = str(modem["signal_strength"])

                self.consul_client = ConsulClient(
                    host=cfg.consul.host,
                    token=cfg.consul.token,
                    scheme=cfg.consul.scheme
                )

                if await self.consul_client.register_service(
                    service_name=cfg.server.name,
                    address=host,
                    port=port,
                    service_desc="基于 gsmmodem 的 SMS 短信微服务",
                    server_data=server_data,
                    meta=meta
                ):
                    logger.info("✅ Consul 注册成功")
                else:
                    logger.warning("⚠️ Consul 注册失败，服务继续运行")

            logger.info("🌐 创建 gRPC 服务器...")
            self.grpc_server = create_server(
                modem_manager=self.modem_manager,
                sender=self.sender,
                max_workers=cfg.server.max_workers
            )

            self.grpc_server.add_insecure_port(cfg.server.listen_on)
            await self.grpc_server.start()

            logger.info(f"✅ gRPC 服务器启动在 {cfg.server.listen_on}")
            logger.info(f"📱 服务名称: {cfg.server.name}")
            logger.info(f"📡 可用调制解调器: {modem_status['available_modems']}/{modem_status['total_modems']}")

            for modem in modem_status["modems"]:
                status = "✅ 可用" if modem["is_available"] else "❌ 不可用"
                in_use = " (使用中)" if modem["in_use"] else ""
                logger.info(f"   {modem['port']}: {modem['manufacturer']} {modem['model']} - 信号: {modem['signal_strength']} {status}{in_use}")

            self._tasks.append(
                asyncio.create_task(self._health_check_task())
            )

            logger.info("🎉 SMS 微服务启动完成！")
            return True

        except Exception as e:
            logger.error(f"❌ 服务启动失败: {e}")
            import traceback
            logger.error(f"详细错误: {traceback.format_exc()}")
            return False

    async def _health_check_task(self):
        try:
            while not self._shutting_down:
                await asyncio.sleep(30)

                if self.modem_manager:
                    healthy = await self.modem_manager.health_check()
                    if not healthy:
                        logger.warning("⚠️ 健康检查: 部分调制解调器连接失败")

                    status = await self.modem_manager.get_status()
                    logger.debug(f"📊 调制解调器状态: {status['available_modems']}/{status['total_modems']} 可用")

        except asyncio.CancelledError:
            logger.debug("健康检查任务被取消")
        except Exception as e:
            logger.error(f"健康检查任务异常: {e}")

    async def run(self):
        try:
            await self.grpc_server.wait_for_termination()

        except asyncio.CancelledError:
            logger.info("服务任务被取消")
        except Exception as e:
            logger.error(f"gRPC 服务器异常: {e}")

    async def stop(self):
        if self._shutting_down:
            return

        self._shutting_down = True
        logger.info("🛑 停止 SMS 微服务...")

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.consul_client:
            try:
                await self.consul_client.deregister_service()
                logger.info("✅ Consul 服务已注销")
            except Exception as e:
                logger.error(f"❌ Consul 注销失败: {e}")

        if self.grpc_server:
            try:
                await self.grpc_server.stop(grace=5.0)
                logger.info("✅ gRPC 服务器已停止")
            except Exception as e:
                logger.error(f"❌ 停止 gRPC 服务器失败: {e}")

        if self.modem_manager:
            try:
                await self.modem_manager.cleanup()
                logger.info("✅ 调制解调器管理器已清理")
            except Exception as e:
                logger.error(f"❌ 清理调制解调器管理器失败: {e}")

        logger.info("👋 SMS 微服务已停止")

    async def _setup_logging(self, log_config):
        import sys

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
        logger.info("=" * 50)
        logger.info("📋 服务配置:")
        logger.info(f"   服务名称: {cfg.server.name}")
        logger.info(f"   监听地址: {cfg.server.listen_on}")
        logger.info(f"   运行模式: {cfg.server.mode}")
        logger.info(f"   最大工作线程: {cfg.server.max_workers}")

        if cfg.consul.host:
            logger.info(f"   Consul 地址: {cfg.consul.host}")

        logger.info(f"   调制解调器波特率: {cfg.modem.baudrate}")
        logger.info(f"   调制解调器 PIN: {cfg.modem.pin or '无'}")
        logger.info(f"   日志级别: {cfg.log.level}")
        logger.info("=" * 50)
