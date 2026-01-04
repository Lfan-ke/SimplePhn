"""
SMS 微服务主程序
"""
import asyncio
import signal
import sys
from pathlib import Path
from loguru import logger

from src.sms.service import SMSMicroservice


async def shutdown_handler(service: SMSMicroservice, signum):
    """异步信号处理函数"""
    logger.info(f"📶 收到信号 {signum}，正在关闭服务...")
    await service.stop()


async def main_async(config_path: str):
    """异步主函数"""
    # 创建微服务实例
    service = SMSMicroservice(Path(config_path))

    # 设置信号处理
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, _: asyncio.create_task(shutdown_handler(service, s)))

    try:
        # 启动服务
        started = await service.start()
        if not started:
            logger.error("❌ 服务启动失败")
            return 1

        # 运行服务
        await service.run()

    except asyncio.CancelledError:
        logger.info("服务被取消")
    except KeyboardInterrupt:
        logger.info("收到键盘中断")
    except Exception as e:
        logger.error(f"服务运行异常: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # 确保服务被停止
        if not service._shutting_down:
            await service.stop()

    return 0


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS 微服务")
    parser.add_argument(
        "--config", "-c",
        default="config/sms.yaml",
        help="配置文件路径 (默认: config/sms.yaml)"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试模式"
    )

    args = parser.parse_args()

    # 设置日志级别
    if args.debug:
        logger.remove()
        logger.add(
            sys.stdout,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                   "<level>{level: <8}</level> | "
                   "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
                   "<level>{message}</level>",
            level="DEBUG",
            colorize=True
        )

    # 检查配置文件
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"❌ 配置文件不存在: {config_path}")
        print("请创建配置文件或使用 --config 参数指定")
        return 1

    # 运行主程序
    return asyncio.run(main_async(args.config))


if __name__ == "__main__":
    sys.exit(main())
