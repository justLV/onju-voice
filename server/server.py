import atexit
import json
import os
import socket
import sys
import subprocess
import threading
import time
import traceback
import warnings
import yaml
from datetime import datetime
from queue import Queue

import numpy as np
import fire
import whisper
from scipy.io.wavfile import write
from rich import print
from rich.traceback import install

install(show_locals=False)

from devices import DeviceManager
from elevenlabs import ElevenLabs
from llm import OpenAIFunctionCalling

# listen to UDP packets from devices & use Voice Activity Detection (VAD) to add spoken segments to transcribe queue
def listen_detect(queue, manager, config):

    UDP_ADDR_PORT = (config['udp']['ip'], config['udp']['port'])
    CHUNK_BYTES = config['mic']['chunk'] * np.dtype(config['mic']['format']).itemsize
    RATE = config['mic']['rate']
    FRAMES_PER_SECOND = int(RATE / config['mic']['chunk'])
    MIC_FORMAT = np.dtype(config['mic']['format'])
    
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(UDP_ADDR_PORT)
                while True:
                    data, addr = s.recvfrom(CHUNK_BYTES)
                    device = manager.get_device_from_ip(
                        addr[0]
                    )  # what device sent this packet? (Needs to be added from multicast_listen)
                    
                    if device:
                        frame = np.frombuffer(data, dtype=MIC_FORMAT)
                        is_speech = device.vad.vad.is_speech(data, RATE)

                        device.update_LEDs(is_speech)  # Visualize speaking (and server listening) on LED's
                        device.vad.window.append(is_speech)  # Running window to calculate ratio of frames that are classified as speech

                        if (len(device.vad.window) == device.vad.window.maxlen):  # wait till full
                            ratio = sum(device.vad.window) / len(device.vad.window)

                            if not device.vad.recording:
                                # Keep pre-buffering until VAD ratio is enough to indicate speech
                                device.vad.pre_buffer.append(frame)
                                if ratio > config['vad']['start_ratio']:
                                    device.vad.fname = f"output_{device.hostname}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                                    device.log.debug(
                                        f"🔴 Started recording. VAD window: {device.vad.visualization()}"
                                    )
                                    device.vad.recording = True
                                    device.vad.buffer.extend(device.vad.pre_buffer)
                                    device.vad.pre_buffer.clear()
                            else:
                                device.vad.buffer.extend(frame)
                                device.vad.frame_count += 1
                                # This is used to transcribe every TRANSCRIBE_PERIOD seconds, in applications where you want to see transcription updating realtime, say on a screen
                                if (
                                    device.vad.frame_count
                                    % int(FRAMES_PER_SECOND * config['transcribe']['period'])
                                    == 0
                                ):
                                    audio_data = np.frombuffer(
                                        b"".join(list(device.vad.buffer)),
                                        dtype=MIC_FORMAT,
                                    )
                                    device.log.debug(
                                        f"Adding incomplete phrase to transcribe queue"
                                    )
                                    queue.put([audio_data, device, False])

                                # Speech has stopped
                                if ratio < config['vad']['silence_stopping_ratio']:
                                    device.vad.silence_count += 1
                                    if (
                                        device.vad.silence_count
                                        > config['vad']['silence_stopping_time'] * FRAMES_PER_SECOND
                                    ):
                                        audio_data = np.frombuffer(
                                            b"".join(list(device.vad.buffer)),
                                            dtype=MIC_FORMAT,
                                        )
                                        queue.put([audio_data, device, True])
                                        audio_data = (
                                            audio_data - np.mean(audio_data)
                                        ).astype(np.int16)
                                        write(
                                            os.path.join(
                                                config['audio_dir'], f"{device.vad.fname}.wav"
                                            ),
                                            RATE,
                                            audio_data.astype(MIC_FORMAT),
                                        )
                                        device.log.debug(
                                            f"⏹ Added to transcribe queue. Saved to {device.vad.fname}.wav",
                                            extra={"highlighter": None},
                                        )
                                        device.vad.reset()
                                else:
                                    device.vad.silence_count = 0
        except Exception:
            print(traceback.format_exc())
        finally:
            if s:
                s.close()


# transcribe audio segments from queue, get LLM response, and send TTS to device
def transcribe_respond(queue, tts, llm, config):
    tic = time.time()
    audio_model = whisper.load_model(config['transcribe']['whisper_model'])
    print(
        f"\n🎤 Loaded Whisper model [bold]{config['transcribe']['whisper_model']}[/] in {time.time()-tic:.3f} seconds\n"
    )

    while True:
        while queue.empty():
            time.sleep(0.01)

        data, device, last_one = queue.get()
        tic = time.time()

        with warnings.catch_warnings():  # stop repeated warnings from Whisper
            warnings.simplefilter("ignore")
            res = audio_model.transcribe(
                data.astype(np.float32) / 32768.0, initial_prompt=device.last_response
            )

        if "text" in res:
            if res["segments"]:
                device.log.debug(f"Transcription time: {time.time()-tic:.3f}")
                if res["segments"][0]["no_speech_prob"] < config['transcribe']['no_speech_prob']:
                    new_res = res["text"].strip()
                    device.log.info(
                        f"[dim]Transcribed:[/] {new_res} ({res['segments'][0]['no_speech_prob']:.2f})"
                        + ("" if last_one else "[INCOMPLETE]")
                    )
                    if last_one:
                        device.stop_listening()  # while server is "thinking"
                        text_response = llm.askGPT(device, new_res)
                        device.last_response = text_response  # use this as prompt for next Whisper transcription
                        wav_fname = tts.text_to_speech(
                            device, text_response, path_name=config['audio_dir']
                        )
                        if wav_fname:
                            device.send_audio(wav_fname, mic_timeout=10)
                        else:
                            # TODO: send placeholder response saying there's an issue
                            device.log.warning(f"No audio sent")
                        device.prune_messages()
                else:
                    device.log.debug(
                        f"[NO SPEECH] {res['text'].strip()} ({res['segments'][0]['no_speech_prob']:.2f})"
                    )
            else:
                device.log.debug(f"No result")
        else:
            device.log.warning("No text")
        queue.task_done()


# Listen to new devices joining the network and send greeting, which prevents the need to manually program in the server IP
def multicast_listen(manager, config):
    mcast_sock = None
    try:
        mcast_sock = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
        )
        mcast_sock.bind(("", config['multicast']['port']))

        group = socket.inet_aton(config['multicast']['group'])
        mcast_sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            group + socket.inet_aton("0.0.0.0"),
        )

        while True:
            data, address = mcast_sock.recvfrom(1024)
            greet_msg = data.decode("utf-8")
            print(
                f"[blink]👋[/] Received [bold]{greet_msg}[/] from {address[0]}:{address[1]}"
            )
            host_name = greet_msg.split(" ")[0]
            device = manager.create_device(host_name, address[0])
            device.send_audio(config['greeting_wav'], volume=14, fade=10, mic_timeout=30)

    except Exception:
        print(traceback.format_exc())
    finally:
        if mcast_sock:
            print("Closing multicast socket")
            mcast_sock.close()

class ConfigUpdater:
    def __init__(self, config):
        self.config = config

    def update(self, **kwargs):
        if kwargs:
            print(f"\n🔥 Updating config with params: {kwargs}")
        for key, value in kwargs.items():
            if value is not None:
                if key == 'mb':
                    self.config['use_maubot'] = value
                elif key == 'ha':
                    self.config['use_home_assistant'] = value
                elif key == 'n':
                    self.config['use_notes'] = value
                elif key == 'whisper':
                    self.config['transcribe']['whisper_model'] = value
                elif key == 'max_messages':
                    self.config['llm']['max_messages'] = int(value)
                elif key == 'voice':
                    self.config['elevenlabs_default_voice'] = value
                elif key == 'send':
                    self.config['maubot']['send_replies'] = value
                else:
                    print(f"[blink red] Unknown config key:[/] {key} - see examples in {__file__}:{sys._getframe().f_lineno}")

def show_git_hash():
    try:
        git_hash = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).strip().decode('utf-8')
        print(f"\n🚀 Git hash: [green]{git_hash}[/]")
    except subprocess.CalledProcessError:
        print("An error occurred while retrieving the Git hash.")

def load_and_validate_config(filename):
    if not os.path.exists("credentials.json"):
        raise FileNotFoundError(f"credentials.json not found. See credentials.json.example for an example")
    
    with open(filename, 'r') as f:
        config = yaml.safe_load(f)
    
    for dir in [config['audio_dir'], config['log_dir']]:
        if not os.path.exists(dir):
            print(f"📂 [gold1]Creating [bold]{dir}[/]")
            os.makedirs(dir)
    
    if not os.path.exists(os.path.join(config['audio_dir'], config['greeting_wav'])):
        raise FileNotFoundError(f"File {config['greeting_wav']} does not exist in {config['audio_dir']}")
    
    assert config['mic']['chunk'] * np.dtype(config['mic']['format']).itemsize < 1400, "UDP packets should probably be less than 1400 bytes to avoid fragmentation!"
    
    return config

def main(**kwargs):
    config = load_and_validate_config('config.yaml')

    ConfigUpdater(config).update(**kwargs)

    if(config['use_maubot']):
        print(f"\n🤖 Maubot is enabled, expecting API at {config['maubot']['url']}")
        if config['maubot']['send_replies']:
            print(f"💬 [blink red bold]Maubot will send replies to messages!![/]")

    if(config['use_notes']):
        print(f"\n📝 Notes are enabled, using {config['notes_file']}")

    show_git_hash()

    queue = Queue()
    manager = DeviceManager(config)
    tts = ElevenLabs(config)
    llm = OpenAIFunctionCalling(config)

    atexit.register(manager.save_to_json)

    threads = [
        threading.Thread(target=listen_detect, args=(queue, manager, config), daemon=True),
        threading.Thread(target=transcribe_respond, args=(queue, tts, llm, config), daemon=True),
        threading.Thread(target=multicast_listen, args=(manager,config), daemon=True),
    ]

    for thread in threads:
        thread.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    fire.Fire(main)