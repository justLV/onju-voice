import json
import logging
import os
import socket
import time
import webrtcvad

from collections import deque
from pydub import AudioSegment
from logging import Formatter
from rich.logging import RichHandler
from rich.console import Console
from rich import print

class CustomFormatter(Formatter):
    def format(self, record):
        if record.levelno == logging.DEBUG:
            return f"[dim][orange1][bold]{record.name}[/bold][/orange1] {record.msg}[/dim]"
        else:
            return f"[orange1][bold]{record.name}[/bold][/orange1]: {record.msg}"

class Vad:
    def __init__(self, config):
        self.config = config
        self.led_update_time = time.time()
        self.vad = webrtcvad.Vad(3)

        FRAMES_PER_SECOND = int(self.config['mic']['rate'] / self.config['mic']['chunk'])
        WINDOW_FRAMES = int(self.config['vad']['window_length'] * FRAMES_PER_SECOND)
        PREBUFFER_FRAMES = int(self.config['vad']['pre_buffer_length'] * FRAMES_PER_SECOND)
        
        self.window = deque(maxlen=WINDOW_FRAMES)
        self.pre_buffer = deque(maxlen=PREBUFFER_FRAMES)
        self.buffer = []
        self.recording = False
        self.silence_count = 0
        self.frame_count = 0
        self.new_segment = True
        self.led_power = 0
        self.fname = None

    def reset(self):
        self.buffer = []
        self.recording = False
        self.silence_count = 0
        self.frame_count = 0
        self.window.clear()

    def visualization(self):
        return "["+"".join(["*" if x else "-" for x in self.window])+"]"

class Device:
    def __init__(self, hostname, ip_address, config, messages=None, voice=None):
        self.config = config
        self.hostname = hostname
        self.ip_address = ip_address
        self.messages = self.init_messages(messages)
        self.last_beeper_results = {}
        self.last_response = None
        self.vad = Vad(self.config)
        self.log = self.setup_logger()
        self.voice = self.config["elevenlabs_default_voice"] if voice is None else voice

    def construct_init_prompt(self):
        # Give ability for prompts at device level
        init_prompt = self.config['llm']['init_prompt']
        if(self.config['use_notes']):
            init_prompt += self.config['llm']['notes_prompt_append']
        if(self.config['use_home_assistant']):
            init_prompt += self.config['llm']['ha_prompt_append']
        if(self.config['use_maubot']):
            init_prompt += self.config['llm']['maubot_prompt_append']
        init_prompt += self.config['llm']['reminder_prompt_append']
        init_prompt = init_prompt.replace("{USER}", self.config['llm']['users_name'])
        return init_prompt

    def init_messages(self, messages):
        first_message = {"role": "system", "content": self.construct_init_prompt()}
        if(messages is None):
            return [first_message]
        else:
            messages[0] = first_message #update first message in case of change of config
            return messages
        
    def add_message(self, message):
        self.messages.append(message)

    def get_messages(self):
        return self.messages

    def setup_logger(self):
        logger = logging.getLogger(self.hostname)
        logger.setLevel(logging.DEBUG)

        # log to console with rich
        console_handler = RichHandler(console=Console(), rich_tracebacks=True, markup=True, highlighter=None)
        console_handler.setFormatter(CustomFormatter())
        logger.addHandler(console_handler)

        # log to file
        file_handler = logging.FileHandler(os.path.join(self.config['log_dir'], f"{self.hostname}.log"))
        file_format = '%(asctime)s - %(levelname)s - %(message)s'
        file_handler.setFormatter(logging.Formatter(file_format))
        logger.addHandler(file_handler)
        return logger

    def send_audio(self, fname, mic_timeout=5 * 60, volume=13, fade=10):
        # header[0]   0xAA for audio
        # header[1:2] mic timeout in seconds (after audio is done playing)
        # header[3]   volume
        # header[4]   fade rate of LED's VAD visualization
        # header[5]   not used
        header = bytes([0xaa, (mic_timeout & 0xff00) >> 8, mic_timeout & 0xff, volume, fade, 0])
        audio_data = (
            AudioSegment.from_file(os.path.join(self.config['audio_dir'], fname))
            .set_channels(1)
            .set_frame_rate(16000)
            .set_sample_width(2)
            .raw_data
        )
        self.send_TCP(header, audio_data, tcp_timeout=60) # 60 (!!) second tcp_timeout for audio as we currently read bytes from TCP as I2S buffer frees up

    def prune_messages(self):
        while(len(self.messages) > self.config['llm']['max_messages']):
            self.log.debug(f"Pruning message: {self.messages[1]['role']}")
            self.messages.pop(1)

    def update_LEDs(self, is_speech):
        if(is_speech): # accumulate power until ready to update LED's
            self.vad.led_power = min(255, self.vad.led_power + self.config['led']['power'])

        if(time.time() - self.vad.led_update_time > self.config['led']['update_period']):
            self.vad.led_update_time = time.time()
            if(self.vad.led_power > 0):
                # header[0]   0xCC for LED blink command
                # header[1]   starting intensity for rampdown
                # header[2:4] RGB color
                # header[5]   fade rate
                header = bytes([0xcc, self.vad.led_power, 255, 255, 255, self.config['led']['fade']])
                self.send_TCP(header, None, 0.1)
            self.vad.led_power = 0

    def stop_listening(self):
        # header[0]   0xDD for mic timeout command
        # header[1:2] mic timeout in seconds
        # header[3:5] not used
        header = bytes([0xdd, 0, 0, 0, 0, 0])
        self.send_TCP(header, None, 0.2)

    def send_TCP(self, header, data, tcp_timeout):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(tcp_timeout)
        try:
            s.connect((self.ip_address, self.config['tcp_port']))
            s.sendall(header)
            if(data):
                s.sendall(data)
        except socket.timeout:
            self.log.error(f"TCP timeout sending {'header' if data is None else 'data'} ({tcp_timeout} seconds)")
        except Exception as e:
            self.log.error(f"TCP error: {e}")
        finally:
            s.close()


    def to_dict(self):
        return {
            'hostname': self.hostname,
            'ip_address': self.ip_address,
            'messages': self.messages,
            'voice': self.voice,
        }

    @classmethod
    def from_dict(cls, data, config):
        return cls(data['hostname'], data['ip_address'], config, data.get('messages', data['voice']))

    def __repr__(self):
        return f"{self.hostname} {self.ip_address} [{len(self.messages) - 1} messages]"

class DeviceManager:
    def __init__(self, config):
        self.devices = {}
        self.config = config
        self.load_from_json()

    def create_device(self, hostname, ip_address):
        device = self.devices.get(hostname)
        if device is None:
            device = Device(hostname, ip_address, self.config)
            self.devices[hostname] = device
            device.log.info(f'Created new device with IP {ip_address}')
        elif device.ip_address != ip_address:
            device.ip_address = ip_address
            device.log.info(f'Updated IP address to {ip_address}')
        else:
            device.log.info(f'Device already exists with IP {ip_address}')
        return device

    def get_device_from_ip(self, ip_address):
        for device in self.devices.values():
            if device.ip_address == ip_address:
                return device
        return None
    
    def save_to_json(self):
        print(f"Saving devices to {self.config['devices_file']}")
        with open(self.config['devices_file'], 'w') as f:
            json_devices = {k: v.to_dict() for k, v in self.devices.items()}
            json.dump(json_devices, f, indent=4)

    def load_from_json(self):
        if os.path.exists(self.config['devices_file']):
            with open(self.config['devices_file'], 'r') as f:
                try:
                    json_devices = json.load(f)
                    if(len(json_devices) > 0):
                        self.devices = {k: Device.from_dict(v, self.config) for k, v in json_devices.items()}
                        print(f"\n🍐 Loaded {len(self.devices)} devices from [bold]{self.config['devices_file']}[/]:")
                        for device in self.devices.values():
                            print(f"{device.hostname} \t [dim]{device.ip_address}[/] \tMessages: {len(device.messages)}")
                    else:
                        print(f"File {self.config['devices_file']} is empty, using empty device manager")
                except Exception as e:
                    print(f"Error loading {self.config['devices_file']}, using empty device manager\n{e}")
        else:
            print(f"File {self.config['devices_file']} does not exist, using empty device manager")

    def __repr__(self):
        return '\n'.join(str(device) for device in self.devices)


