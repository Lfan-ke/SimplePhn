import yaml, glob
from dataclasses import dataclass, field, asdict
from gsmmodem.modem import GsmModem
from logger import logger
import time

@dataclass
class PulsarConfig:
    Main: str = "persistent://echo-wing/main"
    Dlq: str = "persistent://echo-wing/dlq/all"
    Url: str = "pulsar://localhost:6650"

    def to_dict(self) -> dict[str, ...]:
        return asdict(self)

    @property
    def main_topic(self) -> str:
        return self.Main

    @property
    def dlq_topic(self) -> str:
        return self.Dlq


@dataclass
class ConsulConfig:
    Host: str = "localhost"
    Port: int = 8500
    Base: str = "echo_wing/"
    Token: str = ""
    Scheme: str = "http"

    @property
    def address(self) -> str:
        return f"{self.Scheme}://{self.Host}:{self.Port}"

    def to_dict(self) -> dict[str, ...]:
        return asdict(self)


@dataclass
class ModemConfig:
    BaudRate: int = 115200
    TimeOut: int = 10.0
    Patterns: list[str] = field(default_factory=lambda: ["COM*"])
    # 默认重置的USB VID/PID
    UsbVPid: list[str] = field(default_factory=lambda: ["0000:0000"])

    def to_dict(self) -> dict[str, ...]:
        return asdict(self)

@dataclass
class AppConfig:
    Name: str
    Mode: str
    Pulsar: PulsarConfig = field(default_factory=PulsarConfig)
    Consul: ConsulConfig = field(default_factory=ConsulConfig)
    Port: ModemConfig = field(default_factory=ModemConfig)

    def to_dict(self) -> dict[str, ...]:
        data = asdict(self)
        data["Mode"] = self.Mode
        data["Pulsar"] = self.Pulsar.to_dict()
        data["Consul"] = self.Consul.to_dict()
        data["Port"] = self.Port.to_dict()
        return data


port_files: dict | None = None
yaml_config: AppConfig | None = None

class ConfigLoader:
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        global yaml_config
        if yaml_config is None:
            yaml_config = self.__load_config()
        self.config = yaml_config

    def main_topic(self, name: str) -> str:
        return self.config.Pulsar.main_topic + "/" + name

    @property
    def dlq_topic(self) -> str:
        return self.config.Pulsar.dlq_topic

    @property
    def port_files(self) -> dict:
        global port_files
        if not port_files:
            all_ports = sorted({port for patt in self.config.Port.Patterns for port in glob.glob(patt)})
            logger.info_sync(f"扫描到了串口: {all_ports}")
            def mapper(p):
                try:
                    modem = GsmModem(p, self.config.Port.BaudRate)
                    modem.connect()
                except Exception as e:
                    return p, None
                return p, modem
            tmp_ports = {
                port: {
                    'modem': modem,
                    'imsi': modem.imsi,
                    'imei': modem.imei if hasattr(modem, 'imei') else "unknown",
                    'signal': modem.signalStrength if hasattr(modem, 'signalStrength') else -1,
                    'model': modem.model if hasattr(modem, 'model') else "Unknown",
                    'status': 'healthy',
                    'last_check': time.time(),
                    'lock': False,
                    'error_count': 0,
                    'last_used': 0,
                    'created_at': time.time()
                } for port, modem in map(mapper, all_ports) if port and hasattr(modem, 'imsi')
            }
            # 按 imsi 去重
            imsi_map: dict = {}
            for port, info in tmp_ports.items():
                imsi = info['imsi']
                signal = info['signal']
                if imsi not in imsi_map:
                    imsi_map[imsi] = []
                imsi_map[imsi].append((port, signal, info))
            ports_to_remove = []
            for imsi, port_list in imsi_map.items():
                if len(port_list) > 1:
                    port_list.sort(key=lambda x: x[1], reverse=True)
                    for port, signal, info in port_list[1:]:
                        try:
                            info['modem'].close()
                            ports_to_remove.append(port)
                        except Exception as _:
                            pass
            for port in ports_to_remove:
                if port in tmp_ports:
                    del tmp_ports[port]
            logger.info_sync(f"可用去重后串口: {tmp_ports.keys()}")
            port_files = tmp_ports
        return port_files

    def get_modem(self):
        for port in self.port_files.keys():
            if not self.port_files[port]['lock']:
                return ModemWrapper(port)
        return None

    def __load_config(self) -> AppConfig:
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                yaml_data = yaml.safe_load(f)

            if not yaml_data:
                raise ValueError("配置文件为空")

            return self.__parse_config(yaml_data)

        except FileNotFoundError:
            raise FileNotFoundError(f"配置文件不存在: {self.config_path}")
        except yaml.YAMLError as e:
            raise ValueError(f"YAML 解析错误: {e}")
        except Exception as e:
            raise RuntimeError(f"加载配置失败: {e}")

    @staticmethod
    def __parse_config(yaml_data: dict[str, ...]) -> AppConfig:
        pulsar_data = yaml_data.get("Pulsar", {})
        pulsar_config = PulsarConfig(
            Main=pulsar_data.get("Main", "persistent://echo-wing/main"),
            Dlq=pulsar_data.get("Dlq", "persistent://echo-wing/dlq/all"),
            Url=pulsar_data.get("Url", "pulsar://localhost:6650")
        )

        consul_data = yaml_data.get("Consul", {})
        consul_config = ConsulConfig(
            Host=consul_data.get("Host", "localhost"),
            Port=consul_data.get("Port", 8500),
            Base=consul_data.get("Base", "echo-wing/"),
            Token=consul_data.get("Token", ""),
            Scheme=consul_data.get("Scheme", "http"),
        )

        port_data = yaml_data.get("Modem", {})
        port_config = ModemConfig(
            BaudRate=port_data.get("BaudRate", 115200),
            TimeOut=port_data.get("TimeOut", 10),
            Patterns=port_data.get("Patterns", ["COM*"]),
            UsbVPid=port_data.get("UsbVPid", ["0000:0000"]),
        )

        configs = AppConfig(
            Name=yaml_data.get("Name", "phn"),
            Mode=yaml_data.get("Mode", "dev"),
            Pulsar=pulsar_config,
            Consul=consul_config,
            Port=port_config,
        )

        return configs

    def init_port(self):
        import os
        for usb in self.config.Port.UsbVPid:
            os.system(f"sudo usbreset {usb}")
        global port_files
        if port_files: port_files.clear()


class ModemWrapper:
    def __init__(self, port):
        self.port = port
        config = ConfigLoader()
        config.port_files[port]["lock"] = True

    def __del__(self):
        config = ConfigLoader()
        config.port_files[self.port]["lock"] = False
        config.port_files[self.port]["last_used"] = time.time()

    @staticmethod
    def try_new():
        return ConfigLoader().get_modem()

    def get_info(self):
        return ConfigLoader().port_files[self.port] | {"port": self.port}

    def send_sms_sync(self, phone: str, message: str) -> dict:
        """
        同步发送短信

        Args:
            phone: 手机号码（国际格式，已校验）
            message: 短信内容

        Returns:
            dict: 发送结果
        """
        import uuid
        start_time = time.time()
        message_id = str(uuid.uuid4())[:8]

        # 获取当前配置
        config = ConfigLoader()
        port_info = config.port_files[self.port]

        result = {
            'message_id': message_id,
            'success': False,
            'phone': phone,
            'port': self.port,
            'timestamp': start_time,
            'elapsed_time': 0
        }

        try:
            # 检查调制解调器状态
            if port_info.get('status') != 'healthy':
                raise Exception(f"调制解调器状态异常: {port_info.get('status')}")

            if port_info.get('error_count', 0) >= 5:
                raise Exception(f"调制解调器错误过多: {port_info.get('error_count')}")

            # 获取调制解调器对象
            modem = port_info.get('modem')
            if not modem:
                raise Exception("调制解调器对象不存在")

            # 记录发送信息
            logger.info_sync(f"📤 [{message_id}] {self.port} -> {phone}...")
            logger.info_sync(f"    内容长度: {len(message)} 字符")
            logger.info_sync(f"    信号强度: {port_info.get('signal', -1)}")
            logger.info_sync(f"    错误计数: {port_info.get('error_count', 0)}")

            # 发送短信
            modem.sendSms(
                destination=phone,
                text=message,
                waitForDeliveryReport=False,
                deliveryTimeout=30,
                sendFlash=False
            )

            # 更新状态
            elapsed_time = time.time() - start_time
            port_info['last_used'] = start_time
            port_info['error_count'] = max(0, port_info.get('error_count', 0) - 1)

            # 构建成功结果
            result.update({
                'success': True,
                'message': '短信发送成功',
                'elapsed_time': round(elapsed_time, 2),
                'imsi': port_info.get('imsi', 'unknown')[:8] + '...' if isinstance(port_info.get('imsi'), str) and len(
                    port_info.get('imsi', '')) > 8 else port_info.get('imsi', 'unknown'),
                'imei': port_info.get('imei', 'unknown'),
                'signal': port_info.get('signal', -1),
                'model': port_info.get('model', 'Unknown'),
                'network': getattr(modem, 'networkName', 'Unknown') if hasattr(modem, 'networkName') else 'Unknown'
            })

            logger.info_sync(f"✅ [{message_id}] 发送成功 ({elapsed_time:.2f}s)")

        except Exception as e:
            # 错误处理
            elapsed_time = time.time() - start_time
            error_msg = str(e)

            # 更新错误计数
            port_info['error_count'] = port_info.get('error_count', 0) + 1
            port_info['status'] = 'unhealthy' if port_info['error_count'] >= 3 else 'healthy'

            # 记录详细错误
            error_type = type(e).__name__
            result.update({
                'success': False,
                'error': error_msg,
                'error_type': error_type,
                'elapsed_time': round(elapsed_time, 2),
                'retry_count': port_info.get('error_count', 0)
            })

            logger.error_sync(f"❌ [{message_id}] 发送失败: {error_type}: {error_msg}")

        finally:
            # 更新最后使用时间
            port_info['last_used'] = time.time()

            # 如果错误太多，记录警告
            if port_info.get('error_count', 0) >= 5:
                logger.warn_sync(f"⚠️  {self.port} 错误计数达到 {port_info['error_count']}")

        return result

    async def send_sms(self, phone: str, message: str) -> dict:
        """
        异步发送短信

        Args:
            phone: 手机号码（国际格式，已校验）
            message: 短信内容

        Returns:
            dict: 发送结果
        """
        import asyncio

        # 创建一个线程池来执行同步的发送操作
        loop = asyncio.get_event_loop()

        def _sync_send():
            return self.send_sms_sync(phone, message)

        try:
            # 使用线程池执行同步操作
            result = await loop.run_in_executor(None, _sync_send)
            return result

        except asyncio.CancelledError:
            # 如果任务被取消
            logger.warn_sync(f"⏹️  {self.port} -> {phone[:8]}... 发送任务被取消")
            return {
                'success': False,
                'phone': phone,
                'port': self.port,
                'error': '任务被取消',
                'elapsed_time': 0
            }

        except Exception as e:
            # 异步执行过程中的错误
            logger.error_sync(f"💥 {self.port} 异步发送异常: {e}")
            return {
                'success': False,
                'phone': phone,
                'port': self.port,
                'error': f'异步执行异常: {str(e)}',
                'elapsed_time': 0
            }
