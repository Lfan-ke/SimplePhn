"""
Consul客户端
"""
import asyncio
import json
import time
import socket
import os
from typing import Optional
from dataclasses import dataclass, asdict, field
import consul
from loguru import logger


@dataclass
class KVServiceMeta:
    """KV存储的服务元信息"""
    ServerName: str
    ServerDesc: str = ""
    ServerData: dict = field(default_factory=dict)
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))


class ConsulClient:
    """
    Consul客户端
    """

    def __init__(
        self,
        host: str,
        token: str = "",
        scheme: str = "http",
        kv_base_path: str = "echo_wing/"
    ):
        # 解析主机和端口
        if ":" in host:
            host_str, port_str = host.split(":", 1)
            port = int(port_str)
        else:
            host_str = host
            port = 8500

        # 创建Consul客户端
        self.client = consul.Consul(
            host=host_str,
            port=port,
            token=token if token else None,
            scheme=scheme,
            verify=False
        )

        self.kv_base_path = kv_base_path.rstrip("/") + "/"
        self.service_id: Optional[str] = None
        self.service_name: Optional[str] = None
        self.kv_path: Optional[str] = None
        self.registered: bool = False
        self.kv_registered: bool = False

    async def register_service(
        self,
        service_name: str,
        address: str,
        port: int,
        service_desc: str = "",
        server_data: Optional[dict] = None,
        meta: Optional[dict] = None
    ) -> bool:
        """注册服务到Consul"""
        if self.registered:
            return True

        self.service_name = service_name
        self.kv_path = f"{self.kv_base_path}{service_name}"

        # 生成唯一的服务ID
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = int(time.time())
        self.service_id = f"{service_name}-{hostname}-{pid}-{port}-{timestamp}"

        # 准备注册数据
        tags = ["sms", "notification", "grpc"]

        if meta is None:
            meta = {
                "kv_path": self.kv_path,
                "version": "1.0.0",
                "host": hostname,
                "pid": str(pid),
                "started": str(timestamp),
            }
        else:
            meta["kv_path"] = self.kv_path

        try:
            # 使用TCP检查而不是TTL检查，这样更简单
            check = {
                "TCP": f"{address}:{port}",
                "Interval": "10s",
                "Timeout": "5s",
                "DeregisterCriticalServiceAfter": "30s"
            }

            # 注册服务，包含TCP检查
            self.client.agent.service.register(
                name=service_name,
                service_id=self.service_id,
                address=address,
                port=port,
                tags=tags,
                meta=meta,
                check=check
            )

            self.registered = True
            logger.info(f"服务 {service_name} 注册成功 (ID: {self.service_id})")
            logger.info(f"检查类型: TCP, 检查地址: {address}:{port}")

            # 注册KV
            await self._register_kv(service_desc, server_data)
            return True

        except Exception as e:
            logger.error(f"服务注册失败: {e}")
            return False

    async def _register_kv(self, service_desc: str, server_data: Optional[dict]):
        """注册KV"""
        try:
            # 检查KV是否已存在
            index, data = self.client.kv.get(self.kv_path)

            # 准备KV元数据
            kv_meta = KVServiceMeta(
                ServerName=self.service_name,
                ServerDesc=service_desc,
                ServerData=server_data or {}
            )

            # 注册KV
            data_str = json.dumps(asdict(kv_meta), ensure_ascii=False)
            self.client.kv.put(self.kv_path, data_str)

            self.kv_registered = True
            logger.info(f"KV {self.kv_path} 注册成功")

        except Exception as e:
            logger.warning(f"KV注册失败: {e}")

    async def deregister_service(self) -> bool:
        """从Consul注销服务"""
        if not self.registered or not self.service_id:
            return True

        try:
            self.client.agent.service.deregister(self.service_id)
            logger.info(f"服务 {self.service_name} 注销成功")

            # 检查是否需要删除KV
            await self._delete_kv_if_no_instances()

            self.registered = False
            self.service_id = None
            return True

        except Exception as e:
            logger.error(f"服务注销失败: {e}")
            return False

    async def _delete_kv_if_no_instances(self):
        """如果没有活跃实例，删除KV"""
        if not self.kv_registered or not self.service_name:
            return

        try:
            index, nodes = self.client.health.service(
                service=self.service_name,
                passing=True
            )

            # 查找其他活跃实例
            active_services = [
                s for s in (nodes or [])
                if s.get('Service', {}).get('ID') != self.service_id
            ]

            if not active_services:
                self.client.kv.delete(self.kv_path)
                self.kv_registered = False
                logger.info(f"KV {self.kv_path} 已删除（无活跃实例）")

        except Exception as e:
            logger.warning(f"检查活跃实例失败: {e}")
