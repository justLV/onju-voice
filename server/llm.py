import dateparser
import json
import os
import requests
import time
from datetime import datetime, timedelta
from dateutil import tz

import openai
from rich import print

import devices

openai.api_key = os.getenv("OPENAI_API_KEY")

class OpenAIFunctionCalling:
    def __init__(self, config):
        self.config = config
        self.functions = self.setup_functions()

    def call_gpt_retry(self, device, max_retries=4, include_functions=False):
        wait_time = 0.5
        for attempt in range(max_retries):
            try:
                if(include_functions):
                    response = openai.ChatCompletion.create(
                        model=self.config['llm']['gpt_model'],
                        messages=device.messages,
                        functions=self.functions,
                        max_tokens=300,
                    )
                else:
                    response = openai.ChatCompletion.create(
                        model=self.config['llm']['gpt_model'],
                        messages=device.messages,
                        max_tokens=150,
                    )
                return (True, response)
            except Exception as e:
                device.log.error(f"Attempt {attempt+1} of {max_retries} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)
                    wait_time *= 2  # backoff
                else:
                    return (False, e)

    def askGPT(self, device, question):
        device.messages.append({"role": "user", "content": question})

        success, response = self.call_gpt_retry(device, include_functions=bool(self.functions))
        if not success:
            return f"Error: {response}"

        first_message = response["choices"][0]["message"]
        device.log.info(f"OpenAI Response: \n{first_message}")
        device.messages.append(first_message.to_dict())
        if first_message.get("function_call"):
            available_functions = {}
            if(self.config['use_notes']):
                available_functions["add_note"] = self.add_note
                available_functions["get_notes"] = self.get_notes
            if(self.config['use_maubot']):
                available_functions["get_messages"] = self.get_messages
                available_functions["reply_message"] = self.reply_message
            if(self.config['use_home_assistant']):
                available_functions["control_light"] = self.control_light

            function_name = first_message["function_call"]["name"]
            function_to_call = available_functions[function_name]
            function_args = json.loads(first_message["function_call"]["arguments"])
            function_response = function_to_call(device, **function_args)

            device.messages.append(
                {
                    "role": "function",
                    "name": function_name,
                    "content": function_response,
                }
            )

            success, response = self.call_gpt_retry(device, include_functions=False) # don't include functions to get a response
            if not success:
                return f"Error: {' '.join(response.split(' ')[:4])}"
            
            device.log.info(f"OpenAI second response content: \n{response['choices'][0]['message']['content']}")
            device.messages.append(response["choices"][0]["message"].to_dict())
            return response['choices'][0]['message']['content']
        else:
            return first_message["content"]

    def setup_functions(self):
        self.functions = []
        USERS_NAME = self.config['llm']['users_name']

        if(self.config['use_notes']):
            self.functions += [
            {
                "name": "add_note",
                "description": "Add a note or short memo when asked to remember something",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": "The note text to be added",
                        }
                    },
                    "required": ["note"],
                },
            },
            {
                "name": "get_notes",
                "description": "Get all notes that were added on a recent day",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "The day to search for notes. This is a string that is passed into dateutil.parser.parse(), and describes a recent day such as 'today', 'yesterday', 'the day before yesterday' or 'Friday'",
                        }
                    },
                    "required": ["day"],
                },
            }
        ]

        # this requires a Maubot server running
        if(self.config['use_maubot']):
            self.functions += [
            {
                    "name": "get_messages",
                    "description": "Get an indexed list of messages based on recency, source and sender, to be casually summarized",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "recency": {
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "integer",
                                        "description": "The numerical value of the recency",
                                    },
                                    "unit": {
                                        "type": "string",
                                        "enum": ["minutes", "hours", "days", "weeks"],
                                        "description": "The unit for the recency, can be minutes, hours, days, weeks. Always use plural form.",
                                    },
                                },
                                "required": ["value", "unit"],
                                "description": "How long ago to filter messages by, such as '1 day ago'",
                            },
                            "source": {
                                "type": "string",
                                "description": "The source of the messages, such as Discord, Twitter, Whatsapp, Signal etc., otherwise omit to get messages from all sources",
                            },
                            "sender": {
                                "type": "string",
                                "description": "Substring of a name to filter by, such as 'John'",
                            },
                        },
                        "required": [],
                    },
                },
                {
                    "name": "reply_message",
                    "description": f"Send a reply to a previously fetched message from `get_messages` function referenced by index on behalf of {USERS_NAME} and as if you are {USERS_NAME}. {USERS_NAME} may include comments about tone and and how to best reply that you should follow strictly.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "index": {
                                "type": "string",
                                "description": "The index of the message to reply to, as returned by `get_messages` in square brackets, e.g. `[1]`. Only return the number, not the square brackets.",
                            },
                            "message": {
                                "type": "string",
                                "description": "The message body to send",
                            },
                        },
                        "required": ["index", "message"],
                    },
                },
            ]

        # this requires a Home Assistant server running - see https://www.home-assistant.io/installation/linux#docker-compose
        if(self.config['use_home_assistant']):
            with open('credentials.json') as json_file:
                cred = json.load(json_file)
            HA_URL = cred['home_assistant_url']
            HA_TOKEN = cred['home_assistant_token']

            print(f"\n🏡 Fetching lights from Home Assistant at {HA_URL} to add to function definition for OpenAI")
            
            ha_headers = {
                "Authorization": f"Bearer {HA_TOKEN}",
            }
            device_entity_ids = []
            url = f"{HA_URL}api/states"
            response = requests.get(url, headers=ha_headers)
            states = response.json()

            light_states = [state for state in states if state['entity_id'].startswith('light.')]

            if(len(light_states)==0):
                print("[blink orange] No lights found in Home Assistant, skipping light control function [/]")
            else:
                for light_state in light_states:
                    device_entity_ids.append(light_state['entity_id'])
                    print(f"{'💡' if light_state['state']=='on' else '🌑'}  {light_state['entity_id']}")

                self.functions.append(
                {
                    "name": "control_light",
                    "description": "Control a light or multiple lights in the smart home system",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": device_entity_ids
                                },
                                "description": "The entity IDs of the lights to control",
                            },
                            "brightness": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 255,
                                "description": "The brightness level to set the light(s) to, ranging from 0 (off) to 255 (max brightness)",
                            },
                            "rgb_color": {
                                "type": "array",
                                "items": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 255
                                },
                                "maxItems": 3,
                                "description": "The RGB color to set the light(s) to, represented as an array with three integers ranging from 0 to 255. E.g., red would be [255, 0, 0]",
                            }
                        },
                        "required": ["entity_id"],
                    },
                })

        return self.functions

    def add_note(self, device, note):
        timestamp = datetime.now().isoformat()
        note_obj = {
            'timestamp': timestamp,
            'note': note
        }
        with open(self.config['notes_file'], 'a') as file:
            file.write(json.dumps(note_obj))
            file.write('\n')
        return "Added note"

    def get_notes(self,device,day):
        query_date = dateparser.parse(day)
        if query_date is None:
            device.log.error(f'Could not parse date query: {day}')
            return f"Could not parse date query: {day}"

        notes = []
        if not os.path.exists(self.config['notes_file']):
            return "No notes file found"
        try:
            with open(self.config['notes_file'], 'r') as file:
                for line in file:
                    note_obj = json.loads(line.strip())
                    timestamp = datetime.fromisoformat(note_obj['timestamp'])
                    if timestamp.date() == query_date.date(): # Check for same day
                        notes.append(f"{timestamp.strftime('%I:%M %p')} {note_obj['note']}")
                return f"Found {len(notes)} notes:\n" + '\n'.join(notes)
        except Exception as e:
            device.log.error(f"Error reading notes file: {e}")
            return "Error reading notes file"
        return "No notes found"

    def get_messages(self, device, recency=None, source=None, sender=None):
        params = {}
        if source:
            params['source'] = source
        if sender:
            params['sender'] = sender
        if recency:
            local_tz = tz.tzlocal()
            local_datetime = datetime.now(local_tz) - timedelta(**{recency['unit']: recency['value']})
            params['since'] = int(local_datetime.timestamp()*1000)
        device.log.debug(params)
        try:
            response = requests.request("GET", f"{self.config['maubot']['url']}messages", params=params)
        except Exception as e:
            device.log.error(f"Error fetching messages: {e}")
            return f"Error: {e}"
        messages = response.json()
        if len(messages)>0:
            device.last_beeper_results = {} # allow followup of previous query after unsuccessful query
        result_string = f"Total messages: {len(messages)}\n"
        for i, m in enumerate(messages[:10]):
            device.last_beeper_results[str(i+1)] = m['room_id'] # save this for replies
            result_string += f"[{str(i+1)}] From: {m['from']}\n"
            result_string += f"Source: {m['source']}\n"
            result_string += f"Received: {utc_to_local(m['timestamp'])} ({time_ago(m['timestamp'])})\n"
            if(m['participants']>1):
                result_string += f"Participants: {m['participants']}\n"
            result_string += f"Message: {m['message']}\n\n"
        device.log.info(result_string)
        return result_string

    def reply_message(self, device, index, message):
        if index not in device.last_beeper_results:
            device.log.error(f"Invalid index: {index} datatype: {type(index)}")
            return "Invalid index"
        room_id = device.last_beeper_results[index]
        message+= self.config['maubot']['footer']

        if(self.config['maubot']['send_replies']):
            device.log.info(f"💬 Sending {message} to {room_id}")
            response = requests.request("POST", f"{self.config['maubot']['url']}messages", json={"message":message, "room_id":room_id})
            return response.text
        else:
            device.log.info(f"🏗️ [DUMMY] Sending {message} to {room_id}")
            return "Sent dummy message"
        
    def control_light(self, device, entity_id, rgb_color=None, brightness=None):
        with open("credentials.json", "r") as f:
            cred = json.load(f)
        HA_TOKEN = cred.get("home_assistant_token")
        HA_URL = cred.get("home_assistant_url")

        params={"entity_id": entity_id}
        if(rgb_color):
            params['rgb_color'] = rgb_color
        if(brightness):
            params['brightness'] = brightness

        url = f"{HA_URL}api/services/light/turn_on"
        ha_headers = {
            "Authorization": f"Bearer {HA_TOKEN}",
            "content-type": "application/json"
        }
        device.log.info(f"Light control request:\n{params}")
        response = requests.post(url, headers=ha_headers, json=params)
        if(response.status_code==200):
            device.log.info(f"Light control success")
            return "Success"
        else:
            device.log.error(f"Light control error: {response.status_code} {response.text}")
            return f"Error: {response.status_code} {response.text}"


# help out the LLM by describing the recency
def time_ago(unix_timestamp):
    timestamp = datetime.utcfromtimestamp(unix_timestamp/1000)
    now = datetime.utcnow()
    diff = now - timestamp

    seconds_in_day = 60 * 60 * 24
    seconds_in_hour = 60 * 60
    seconds_in_minute = 60

    if diff.total_seconds() < seconds_in_minute:
        return "just now"
    elif diff.total_seconds() < seconds_in_hour:
        minutes = round(diff.total_seconds() / seconds_in_minute)
        return f"{minutes} minutes ago" if minutes > 1 else "a minute ago"
    elif diff.total_seconds() < seconds_in_day:
        hours = round(diff.total_seconds() / seconds_in_hour)
        return f"{hours} hours ago" if hours > 1 else "an hour ago"
    else:
        days = round(diff.total_seconds() / seconds_in_day)
        return f"{days} days ago" if days > 1 else "yesterday"

def utc_to_local(utc_timestamp_millis):
    utc_timestamp = utc_timestamp_millis / 1000  # convert to seconds
    utc_dt = datetime.utcfromtimestamp(utc_timestamp).replace(tzinfo=tz.tzutc())
    local_dt = utc_dt.astimezone(tz.tzlocal())
    return local_dt.strftime("%I:%M %p")




