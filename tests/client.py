"""
SMS 微服务测试客户端 - 通过 Consul 发现服务
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional, Tuple
import consul as consul_lib
import grpc
from loguru import logger

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sms import sms_pb2, sms_pb2_grpc


class ConsulServiceDiscovery:
    """Consul 服务发现"""

    def __init__(self, consul_host: str = "localhost:8500"):
        # 解析主机和端口
        if ":" in consul_host:
            host_str, port_str = consul_host.split(":", 1)
            port = int(port_str)
        else:
            host_str = consul_host
            port = 8500

        self.client = consul_lib.Consul(
            host=host_str,
            port=port,
            scheme="http",
            verify=False
        )
        self.consul_host = consul_host

    async def discover_service(self, service_name: str) -> Optional[Tuple[str, int]]:
        """
        从 Consul 发现服务

        Args:
            service_name: 服务名称

        Returns:
            (host, port) 元组，如果未找到则返回 None
        """
        try:
            logger.info(f"🔍 在 Consul {self.consul_host} 中查找服务: {service_name}")

            # 获取健康的服务实例
            index, nodes = self.client.health.service(
                service=service_name,
                passing=True
            )

            if not nodes:
                logger.warning(f"⚠️  未找到健康的服务实例: {service_name}")
                return None

            # 选择第一个健康的实例（简单的负载均衡）
            node = nodes[0]
            service_info = node.get('Service', {})

            address = service_info.get('Address', '')
            port = service_info.get('Port', 0)

            # 如果服务地址是空字符串，使用节点的地址
            if not address:
                address = node.get('Node', {}).get('Address', '')

            logger.info(f"✅ 发现服务: {service_name} -> {address}:{port}")

            # 获取服务的元数据
            meta = service_info.get('Meta', {})
            if meta:
                logger.info(f"   元数据: {meta}")

            return address, port

        except Exception as e:
            logger.error(f"❌ 服务发现失败: {e}")
            return None

    async def get_all_services(self) -> list:
        """获取所有注册的服务"""
        try:
            services = self.client.agent.services()
            logger.info(f"📋 Consul 中的服务列表:")

            service_list = []
            for service_id, service_info in services.items():
                name = service_info.get('Service', 'Unknown')
                address = service_info.get('Address', '')
                port = service_info.get('Port', 0)

                logger.info(f"  - {name} ({service_id}): {address}:{port}")
                service_list.append({
                    'id': service_id,
                    'name': name,
                    'address': address,
                    'port': port,
                    'tags': service_info.get('Tags', [])
                })

            return service_list

        except Exception as e:
            logger.error(f"❌ 获取服务列表失败: {e}")
            return []

    async def get_service_health(self, service_name: str) -> dict:
        """获取服务健康状态"""
        try:
            index, nodes = self.client.health.service(
                service=service_name,
                passing=True
            )

            healthy_count = len(nodes)
            total_index, all_nodes = self.client.health.service(
                service=service_name
            )
            total_count = len(all_nodes)

            health_info = {
                'service_name': service_name,
                'healthy_instances': healthy_count,
                'total_instances': total_count,
                'health_status': 'healthy' if healthy_count > 0 else 'unhealthy',
                'instances': []
            }

            for node in nodes[:3]:  # 只显示前3个实例
                service_info = node.get('Service', {})
                health_info['instances'].append({
                    'address': service_info.get('Address', ''),
                    'port': service_info.get('Port', 0),
                    'id': service_info.get('ID', ''),
                    'tags': service_info.get('Tags', []),
                    'meta': service_info.get('Meta', {})
                })

            logger.info(f"📊 服务健康状态: {service_name} - {healthy_count}/{total_count} 个健康实例")
            return health_info

        except Exception as e:
            logger.error(f"❌ 获取服务健康状态失败: {e}")
            return {}


class SMSConsulClient:
    """基于 Consul 发现的 SMS 客户端"""

    def __init__(self, consul_host: str = "localhost:8500", service_name: str = "sms.rpc"):
        self.consul_host = consul_host
        self.service_name = service_name
        self.service_discovery = ConsulServiceDiscovery(consul_host)
        self.channel = None
        self.stub = None
        self.service_address = None
        self.service_port = None

    async def connect_via_consul(self) -> bool:
        """
        通过 Consul 发现并连接到服务

        Returns:
            是否连接成功
        """
        try:
            # 1. 从 Consul 发现服务
            service_info = await self.service_discovery.discover_service(self.service_name)

            if not service_info:
                logger.error(f"❌ 无法在 Consul 中找到服务: {self.service_name}")
                return False

            address, port = service_info
            self.service_address = address
            self.service_port = port

            # 2. 连接到 gRPC 服务
            target = f"{address}:{port}"
            logger.info(f"🔗 连接到 gRPC 服务: {target}")

            self.channel = grpc.aio.insecure_channel(target)
            self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)

            # 3. 测试连接
            try:
                response = await asyncio.wait_for(
                    self.stub.HealthCheck(sms_pb2.HealthCheckRequest()),
                    timeout=5.0
                )

                if response.status == 200:
                    logger.info(f"✅ 连接成功: {response.message}")

                    # 解析健康数据
                    try:
                        health_data = json.loads(response.data)
                        logger.info(f"📊 服务状态: {health_data.get('health_status', 'unknown')}")
                        logger.info(f"📡 调制解调器: {health_data.get('details', {}).get('available_modems', 0)} 个可用")
                    except:
                        pass

                    return True
                else:
                    logger.error(f"❌ 服务不健康: {response.message}")
                    return False

            except asyncio.TimeoutError:
                logger.error(f"⏰ 连接超时: {target}")
                return False
            except Exception as e:
                logger.error(f"❌ 连接测试失败: {e}")
                return False

        except Exception as e:
            logger.error(f"💥 通过 Consul 连接失败: {e}")
            return False

    async def connect_direct(self, target: str = "localhost:50052") -> bool:
        """
        直接连接到指定的地址

        Args:
            target: gRPC 服务器地址

        Returns:
            是否连接成功
        """
        try:
            logger.info(f"🔗 直接连接到: {target}")

            self.channel = grpc.aio.insecure_channel(target)
            self.stub = sms_pb2_grpc.SMSServiceStub(self.channel)

            # 测试连接
            try:
                response = await asyncio.wait_for(
                    self.stub.HealthCheck(sms_pb2.HealthCheckRequest()),
                    timeout=5.0
                )

                logger.info(f"✅ 直接连接成功: {response.message}")
                return True

            except asyncio.TimeoutError:
                logger.error(f"⏰ 连接超时: {target}")
                return False
            except Exception as e:
                logger.error(f"❌ 连接测试失败: {e}")
                return False

        except Exception as e:
            logger.error(f"💥 直接连接失败: {e}")
            return False

    async def close(self):
        """关闭连接"""
        if self.channel:
            await self.channel.close()
            logger.info("🔌 连接已关闭")

    async def send_sms(self, phone_number: str, content: str, sender_id: str = "consul_client") -> Optional[dict]:
        """
        发送短信

        Args:
            phone_number: 手机号码
            content: 短信内容
            sender_id: 发送者ID

        Returns:
            发送结果字典，如果失败返回 None
        """
        if not self.stub:
            logger.error("❌ 未连接到服务")
            return None

        logger.info(f"📤 发送短信到: {phone_number}")
        logger.info(f"📄 内容长度: {len(content)} 字符")

        request = sms_pb2.SendSMSRequest(
            phone_number=phone_number,
            content=content,
            sender_id=sender_id,
            delivery_report=True,
            metadata={
                "client": "consul_client",
                "consul_host": self.consul_host,
                "service_name": self.service_name
            }
        )

        try:
            start_time = asyncio.get_event_loop().time()
            response = await self.stub.SendSMS(request)
            elapsed_time = asyncio.get_event_loop().time() - start_time

            logger.info(f"📨 响应: {response.message}")
            logger.info(f"📊 状态码: {response.status}")
            logger.info(f"⏱️  耗时: {elapsed_time:.2f}秒")

            result = {
                "success": response.status == 200,
                "status_code": response.status,
                "message": response.message,
                "elapsed_time": elapsed_time
            }

            if response.data:
                try:
                    data = json.loads(response.data)
                    result.update(data)

                    # 打印详细结果
                    if result['success']:
                        logger.info(f"✅ 短信发送成功!")
                        logger.info(f"   消息ID: {data.get('message_id', 'N/A')}")
                        logger.info(f"   调制解调器: {data.get('modem_port', 'N/A')}")
                        logger.info(f"   参考号: {data.get('reference', 'N/A')}")
                    else:
                        logger.error(f"❌ 短信发送失败!")
                        logger.error(f"   错误: {data.get('message', 'N/A')}")

                except Exception as e:
                    logger.warning(f"⚠️  解析响应数据失败: {e}")
                    result['raw_data'] = response.data

            return result

        except Exception as e:
            logger.error(f"💥 发送短信失败: {e}")
            return None

    async def health_check(self) -> Optional[dict]:
        """健康检查"""
        if not self.stub:
            logger.error("❌ 未连接到服务")
            return None

        try:
            response = await self.stub.HealthCheck(sms_pb2.HealthCheckRequest())

            result = {
                "status_code": response.status,
                "message": response.message
            }

            if response.data:
                try:
                    data = json.loads(response.data)
                    result.update(data)

                    # 打印健康状态
                    logger.info(f"📊 健康状态: {response.message}")
                    logger.info(f"📈 状态码: {response.status}")

                    if 'details' in data:
                        details = data['details']
                        logger.info(f"📡 调制解调器: {details.get('available_modems', 0)}/{details.get('total_modems', 0)} 可用")
                        logger.info(f"🔄 服务就绪: {data.get('service_ready', False)}")

                except:
                    logger.info(f"📋 原始数据: {response.data[:200]}...")

            return result

        except Exception as e:
            logger.error(f"❌ 健康检查失败: {e}")
            return None

    async def get_modem_status(self) -> Optional[dict]:
        """获取调制解调器状态"""
        if not self.stub:
            logger.error("❌ 未连接到服务")
            return None

        try:
            response = await self.stub.GetModemStatus(sms_pb2.ModemStatusRequest())

            result = {
                "status_code": response.status,
                "message": response.message
            }

            if response.data:
                try:
                    data = json.loads(response.data)
                    result.update(data)

                    # 打印调制解调器状态
                    logger.info(f"📡 调制解调器状态: {response.message}")

                    if 'modems' in data:
                        modems = data['modems']
                        logger.info(f"📱 总调制解调器: {data.get('total_modems', 0)}")
                        logger.info(f"✅ 可用调制解调器: {data.get('available_modems', 0)}")
                        logger.info(f"🔒 使用中调制解调器: {data.get('in_use_modems', 0)}")

                        # 显示前3个调制解调器
                        for i, modem in enumerate(modems[:3]):
                            status = "✅ 可用" if modem.get('is_available') else "❌ 不可用"
                            in_use = " (使用中)" if modem.get('in_use') else ""
                            logger.info(f"   {i+1}. {modem.get('port', 'N/A')}: {modem.get('model', 'Unknown')} - 信号: {modem.get('signal_strength', 0)} {status}{in_use}")

                except Exception as e:
                    logger.warning(f"⚠️  解析调制解调器状态失败: {e}")
                    result['raw_data'] = response.data

            return result

        except Exception as e:
            logger.error(f"❌ 获取调制解调器状态失败: {e}")
            return None

    async def explore_consul(self):
        """探索 Consul 中的服务"""
        logger.info(f"🔍 探索 Consul: {self.consul_host}")

        # 1. 获取所有服务
        services = await self.service_discovery.get_all_services()

        # 2. 获取 SMS 服务健康状态
        sms_health = await self.service_discovery.get_service_health(self.service_name)

        return {
            "all_services": services,
            "sms_service_health": sms_health
        }


async def main():
    """主测试函数"""
    import argparse

    parser = argparse.ArgumentParser(description="SMS 微服务测试客户端 (通过 Consul)")
    parser.add_argument("--consul", default="localhost:8500", help="Consul 服务器地址")
    parser.add_argument("--service", default="sms.rpc", help="服务名称")
    parser.add_argument("--phone", default="+8619834717434", help="测试手机号码")
    parser.add_argument("--direct", help="直接连接地址 (跳过 Consul 发现)")
    parser.add_argument("--explore", action="store_true", help="探索 Consul 中的服务")
    parser.add_argument("--test-long", default=True, action="store_true", help="测试长短信")

    args = parser.parse_args()

    # 配置日志
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
        colorize=True
    )

    client = SMSConsulClient(
        consul_host=args.consul,
        service_name=args.service
    )

    try:
        if args.explore:
            # 探索模式：只查看 Consul 中的服务
            logger.info("🔍 探索 Consul 服务...")
            consul_info = await client.explore_consul()

            print("\n" + "="*60)
            print("Consul 服务发现报告")
            print("="*60)

            # 打印所有服务
            services = consul_info.get('all_services', [])
            print(f"\n📋 总服务数: {len(services)}")
            for svc in services:
                print(f"  - {svc['name']}: {svc['address']}:{svc['port']} (ID: {svc['id']})")

            # 打印 SMS 服务健康状态
            sms_health = consul_info.get('sms_service_health', {})
            print(f"\n📊 SMS 服务健康状态: {sms_health.get('service_name', 'N/A')}")
            print(f"   健康实例: {sms_health.get('healthy_instances', 0)}/{sms_health.get('total_instances', 0)}")

            for i, instance in enumerate(sms_health.get('instances', [])):
                print(f"   实例 {i+1}: {instance.get('address')}:{instance.get('port')}")

            return

        # 连接模式
        connected = False

        if args.direct:
            # 直接连接模式
            connected = await client.connect_direct(args.direct)
        else:
            # Consul 发现模式
            logger.info(f"🚀 通过 Consul 发现服务: {args.service}")
            connected = await client.connect_via_consul()

        if not connected:
            logger.error("❌ 无法连接到服务，测试终止")
            return

        print("\n" + "="*60)
        print("SMS 微服务测试")
        print("="*60)

        # 1. 健康检查
        logger.info("\n1. 🩺 健康检查...")
        health_result = await client.health_check()

        if health_result and health_result.get('status_code') == 200:
            logger.info("✅ 服务健康")
        else:
            logger.warning("⚠️  服务可能不健康")

        # 2. 获取调制解调器状态
        logger.info("\n2. 📡 获取调制解调器状态...")
        await client.get_modem_status()

        # 3. 发送测试短信
        logger.info(f"\n3. 📨 发送测试短信到: {args.phone}")

        test_content = f"【SMS微服务测试】\n时间: {asyncio.get_event_loop().time():.2f}\n这是一条通过 Consul 发现的测试短信。\n✅ 中文和英文混合测试\n服务发现: {args.service} via {args.consul}"

        send_result = await client.send_sms(
            phone_number=args.phone,
            content=test_content,
            sender_id="consul_test_client"
        )

        if send_result and send_result.get('success'):
            logger.info("🎉 测试短信发送成功!")
        else:
            logger.error("❌ 测试短信发送失败")

        if args.test_long:
            logger.info("\n4. 📨 测试长短信...")
            long_content = "这是一个长短信测试，" * 125  # 约 300 字符

            long_result = await client.send_sms(
                phone_number=args.phone,
                content=long_content,
                sender_id="consul_test_long"
            )

            if long_result and long_result.get('success'):
                logger.info("🎉 长短信测试完成!")
                if 'total_segments' in long_result:
                    logger.info(f"   共 {long_result['total_segments']} 段")
            else:
                logger.error("❌ 长短信测试失败")

        print("\n" + "="*60)
        print("🎯 测试完成!")
        print("="*60)

        # 打印总结
        if send_result:
            print(f"\n📊 测试总结:")
            print(f"  手机号码: {args.phone}")
            print(f"  发送结果: {'✅ 成功' if send_result.get('success') else '❌ 失败'}")
            print(f"  响应消息: {send_result.get('message', 'N/A')}")
            print(f"  耗时: {send_result.get('elapsed_time', 0):.2f}秒")

            if args.direct:
                print(f"  连接方式: 直接连接 ({args.direct})")
            else:
                print(f"  连接方式: Consul 发现 ({args.consul} -> {args.service})")
                if client.service_address:
                    print(f"  服务地址: {client.service_address}:{client.service_port}")

    except KeyboardInterrupt:
        logger.info("\n⌨️ 用户中断测试")
    except Exception as e:
        logger.error(f"💥 测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
