"""
调制解调器测试脚本
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.common.config import ConfigManager
from src.common.modem_manager import ModemManager


async def test_modem_detection():
    """测试调制解调器检测"""
    print("🔍 测试调制解调器检测...")

    config = ConfigManager()
    await config.load(Path("config/sms.yaml"))

    manager = ModemManager(config.get())

    if await manager.initialize():
        status = await manager.get_status()

        print(f"✅ 初始化成功，检测到 {status['total_modems']} 个调制解调器")

        for modem in status["modems"]:
            print(f"  📱 {modem['port']}:")
            print(f"     制造商: {modem['manufacturer']}")
            print(f"     型号: {modem['model']}")
            print(f"     信号: {modem['signal_strength']}")
            print(f"     可用: {modem['is_available']}")
            print(f"     使用中: {modem['in_use']}")

        # 健康检查
        healthy = await manager.health_check()
        print(f"\n🩺 健康检查: {'✅ 通过' if healthy else '❌ 失败'}")

        # 清理
        await manager.cleanup()
        print("🧹 清理完成")
    else:
        print("❌ 初始化失败")


async def test_sms_sending():
    """测试短信发送"""
    print("\n📨 测试短信发送...")

    config = ConfigManager()
    await config.load(Path("config/sms.yaml"))

    manager = ModemManager(config.get())

    if await manager.initialize():
        # 测试电话号码（请替换为实际的测试号码）
        test_number = "+8619834717434"
        test_message = "Hello from SMS微服务测试!"

        try:
            success, message, modem_port = await manager.send_sms(test_number, test_message)

            print(f"📤 发送结果:")
            print(f"   成功: {success}")
            print(f"   消息: {message}")
            print(f"   调制解调器: {modem_port}")

        except Exception as e:
            print(f"❌ 发送失败: {e}")

        await manager.cleanup()
    else:
        print("❌ 初始化失败")


async def main():
    """主测试函数"""
    print("=" * 60)
    print("SMS 微服务 - 调制解调器测试")
    print("=" * 60)

    try:
        await test_modem_detection()
        await test_sms_sending()

        print("\n" + "=" * 60)
        print("🎯 测试完成")
        print("=" * 60)

    except KeyboardInterrupt:
        print("\n⌨️ 用户中断测试")
    except Exception as e:
        print(f"\n💥 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
