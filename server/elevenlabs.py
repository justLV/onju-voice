import json
import os
import requests
from datetime import datetime
from pydub import AudioSegment
from rich import print

class ElevenLabs:
    def __init__(self, config):
        with open("credentials.json", "r") as f:
            cred = json.load(f)
        token = cred.get("elevenlabs_token")
        self.headers = {
            'Content-Type': 'application/json',
            'xi-api-key': token
        }
        self.default_voice = config["elevenlabs_default_voice"]
        self.URL = "https://api.elevenlabs.io/v1/"
        self.jsonfile = config['voices_file']
        self.voices = self.get_voices()
        self.temp_wav_fname = config['temp_wav_fname']
        for k,v in self.voices.items():
            print(f"{v['name']} \t[dim]({v['voice_id']})[/dim]")

    def get_voices(self):
        if(os.path.exists(self.jsonfile)):
            with open(self.jsonfile, "r") as f:
                voices = json.load(f)

            print(f"\n🗣️  Loaded {len(voices)} voices from [bold]{self.jsonfile}[/]")
            return voices
        else:
            response = requests.request("GET", f"{self.URL}voices", headers=self.headers)
            voices_dict={}
            for elevenvoice in response.json().get('voices'):
                if(elevenvoice['category'] == "cloned"):
                    voices_dict[elevenvoice['name']] = {
                        "voice_id": elevenvoice['voice_id'],
                        "name": elevenvoice['name'],
                    }
            print(f"Fetched voices from Elevenlabs and saved to [bold]{self.jsonfile}[/]. [blink red] Remember to modify the friendly name if your LLM uses these! [/]")

            with open(self.jsonfile, 'w') as fp:
                json.dump(voices_dict, fp,indent=4)
            return voices_dict

    def get_voice_id(self, device):
        if device.voice in self.voices:
            return self.voices[device.voice]['voice_id']
        else:
            device.log.warning(f"Voice '{device.voice}' not found, using default {self.default_voice}")
            return self.voices[self.default_voice]['voice_id']

    def text_to_speech(self, device, text, path_name="data"):
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d_%H-%M-%S")

        payload = json.dumps({
            "text": text
        })
        voice_id = self.get_voice_id(device)
        response = requests.request("POST", f"{self.URL}text-to-speech/{voice_id}", headers=self.headers, data=payload)
        if response.status_code != 200:
            device.log.error(f"Error: {response.status_code}\n{response.text}")
            return None
        fname = os.path.join(path_name, f'{voice_id}_{now_str}.mp3')
        device.log.debug(f"Saving audio response to {fname}", extra={"highlighter": None})
        with open(fname, 'wb') as f:
            f.write(response.content)
        
        audio = AudioSegment.from_mp3(fname)
        audio.export(os.path.join(path_name, self.temp_wav_fname), format="wav")

        return self.temp_wav_fname

