import json
import time
from typing import Any
from dataclasses import dataclass, field, asdict
import consul

from logger import logger

@dataclass
class KVServiceMeta:
    ServerName: str
    ServerPath: str
    ServerIcon: str  | None = None
    ServerDesc: str  = ""
    ServerData: dict = field(default_factory=dict)
    created_at: int  = field(default_factory=lambda: int(time.time()))
    updated_at: int  = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)

class ConsulKVClient:
    def __init__(
        self,
        host: str, port: int,
        token: str = "",
        scheme: str = "http",
        kv_base_path: str = "echo_wing/"
    ):
        self.client = consul.Consul(
            host=host,
            port=port,
            token=token if token else None,
            scheme=scheme,
            verify=False
        )

        self.kv_base_path = kv_base_path.rstrip("/") + "/"

    async def register_kv(
        self,
        key: str,
        value: Any,
    ) -> bool:
        full_key = f"{self.kv_base_path}{key}"
        result = self.client.kv.put(full_key, json.dumps(value))

        if result:
            await logger.info(f"✅ KV '{full_key}' 注册成功")
            return True
        else:
            await logger.error(f"❌ KV '{full_key}' 注册失败")
            return False

    async def deregister_kv(
        self,
        key: str,
        recurse: bool = False
    ) -> bool:
        try:
            full_key = f"{self.kv_base_path}{key}"
            result = self.client.kv.delete(full_key, recurse=recurse)

            if result:
                await logger.info(f"🗑️  KV '{full_key}' 注销成功")
                return True
            else:
                await logger.warn(f"⚠️  KV '{full_key}' 不存在或注销失败")
                return False

        except Exception as e:
            await logger.error(f"❌ KV注销异常: {e}")
            return False
