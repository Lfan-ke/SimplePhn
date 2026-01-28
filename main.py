import asyncio
from common import (
    ConfigLoader, ConsulKVClient, PulsarService, KVServiceMeta,
)
from logger import logger
from service import (
    create_sms_task, sms_field_description, SMSMessage,
)

config = ConfigLoader()

async def sms_handler(payload: dict[str, ...]) -> bool:
    """邮件服务处理器"""
    try:
        mail = SMSMessage.from_dict(payload)
        task = create_sms_task(mail)
        return await task
    except Exception as e:
        await logger.error(f"💥 [sms] 处理异常: {e}")
        return False

async def main():
    logger.set_app_name("EchoWing PHN Service")

    await logger.info(f"⭐ 初始化 USB ...")

    config.init_port()

    await asyncio.sleep(15)

    sms_service = PulsarService(
        service_name="sms",
        pulsar_url=config.config.Pulsar.Url,
        main_topic=config.main_topic("sms"),
        dlq_topic=config.dlq_topic,
    )

    await sms_service.start(
        message_handler=sms_handler,
    )

    consul = ConsulKVClient(
        host=config.config.Consul.Host,
        port=config.config.Consul.Port,
        token=config.config.Consul.Token,
        scheme=config.config.Consul.Scheme,
        kv_base_path=config.config.Consul.Base,
    )

    sms_schema = KVServiceMeta(
        ServerName="sms",
        ServerDesc="EchoWing 通用短信服务",
        ServerIcon=None,
        ServerPath=config.main_topic("sms"),
        ServerData={"fields": {
            **sms_field_description
        }}
    )

    await consul.register_kv("sms", sms_schema.to_dict())

    await logger.info(f"📧 已注册 KV 到 Consul ...")
    await logger.info("🎯 短信服务已启动，配置了自动重试和死信队列")

    await logger.info(f"✉️ 开始扫描串口 ...")

    port_files = config.port_files

    await logger.info(f"ℹ️ 发现 {len(port_files)} 个串口： {tuple(port_files.keys())}")

    try:
        await asyncio.gather(sms_service.task)
    except asyncio.CancelledError:
        await logger.info("🛑 服务被终止")
    except Exception as e:
        await logger.error(f"💥 主程序异常: {e}")
    finally:
        await sms_service.stop()
        await consul.deregister_kv(config.config.Name)
        await logger.info(f"🚮 已注销 KV 从 Consul ...")

if __name__ == "__main__":
    asyncio.run(main())
