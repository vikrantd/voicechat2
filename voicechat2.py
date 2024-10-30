import asyncio
import aiohttp
import io
import json
import logging
import numpy as np
import os
import re
import soundfile as sf
import tempfile
import time
import traceback
import uuid
import wave

from collections import deque
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from mutagen.oggopus import OggOpus
from openai import OpenAI
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# External endpoints
SRT_ENDPOINT = os.getenv("SRT_ENDPOINT", "http://localhost:8005/inference")
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:8002/v1/chat/completions")
TTS_ENDPOINT = os.getenv("TTS_ENDPOINT", "http://localhost:8003/tts")

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()
app.mount("/ui", StaticFiles(directory="ui"), name="ui")

SYSTEM = {
    "role": "system",
    "content": "You are a helpful AI voice assistant which can answer patient details to the doctor. You can also execute supabase queries to get patient details. Do not make up any information, only answer based on the information you have. Do not entertain any other questions.",
}


class ConversationManager:
    def __init__(self):
        self.sessions = {}
        self.session_timeout = 3600  # 1 hour timeout for sessions

    def create_session(self):
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = {
            "conversation": [SYSTEM],
            "llm_output_sentences": deque(),
            "current_turn": 0,
            "is_processing": False,
            "audio_buffer": b"",  # New: Buffer to accumulate audio data
            "last_activity": time.time(),
            "first_audio_sent": False,
            "latency_metrics": {
                "start_time": 0,
                "srt_start": 0,
                "srt_end": 0,
                "llm_start": 0,
                "llm_first_token": 0,
                "llm_first_sentence": 0,
                "tts_start": 0,
                "tts_end": 0,
                "first_audio_response": 0,
            },
        }
        return session_id

    def reset_latency_metrics(self, session_id):
        self.sessions[session_id]["latency_metrics"] = {
            "start_time": time.time(),
            "srt_start": 0,
            "srt_end": 0,
            "llm_start": 0,
            "llm_first_token": 0,
            "llm_first_sentence": 0,
            "tts_start": 0,
            "tts_end": 0,
            "first_audio_response": 0,
        }

    def update_latency_metric(self, session_id, metric, value):
        self.sessions[session_id]["latency_metrics"][metric] = value

    def calculate_latencies(self, session_id):
        metrics = self.sessions[session_id]["latency_metrics"]
        start_time = metrics["start_time"]

        return {
            "total_voice_to_voice": metrics["first_audio_response"] - start_time,
            "srt_duration": metrics["srt_end"] - metrics["srt_start"],
            "llm_ttft": metrics["llm_first_token"] - metrics["llm_start"],
            "llm_ttfs": metrics["llm_first_sentence"] - metrics["llm_start"],
            "tts_duration": metrics["tts_end"] - metrics["tts_start"],
        }

    def add_user_message(self, session_id, message):
        self.sessions[session_id]["conversation"].append(
            {"role": "user", "content": message}
        )
        self.sessions[session_id]["current_turn"] += 1
        self.sessions[session_id]["last_activity"] = time.time()

    def add_ai_message(self, session_id, message):
        self.sessions[session_id]["conversation"].append(
            {"role": "assistant", "content": message}
        )
        self.sessions[session_id]["current_turn"] += 1
        self.sessions[session_id]["last_activity"] = time.time()

    def get_conversation(self, session_id):
        return self.sessions[session_id]["conversation"]

    def clean_old_sessions(self):
        current_time = time.time()
        sessions_to_remove = [
            session_id
            for session_id, session_data in self.sessions.items()
            if current_time - session_data["last_activity"] > self.session_timeout
        ]
        for session_id in sessions_to_remove:
            del self.sessions[session_id]
        logger.info(f"Cleaned up {len(sessions_to_remove)} old sessions")

    def add_to_audio_buffer(self, session_id, audio_data):
        self.sessions[session_id]["audio_buffer"] += audio_data

    def get_and_clear_audio_buffer(self, session_id):
        audio_data = self.sessions[session_id]["audio_buffer"]
        self.sessions[session_id]["audio_buffer"] = b""
        return audio_data


conversation_manager = ConversationManager()

client = OpenAI()


async def transcribe_audio(audio_data, session_id, turn_id):
    conversation_manager.update_latency_metric(session_id, "srt_start", time.time())
    try:
        temp_file_path = f"/tmp/{session_id}-{turn_id}.opus"
        with open(temp_file_path, "wb") as temp_file:
            temp_file.write(audio_data)

        # Add a small delay to ensure the file is fully written
        await asyncio.sleep(0.1)

        async with aiohttp.ClientSession() as session:
            data = aiohttp.FormData()
            data.add_field(
                "file",
                open(temp_file_path, "rb"),
                filename=f"/tmp/{session_id}-{turn_id}.opus",
            )
            data.add_field("temperature", "0.0")
            data.add_field("temperature_inc", "0.2")
            data.add_field("response_format", "json")

            async with session.post(SRT_ENDPOINT, data=data) as response:
                result = await response.json()

        # Optionally, you can remove the temporary file here if you don't need it for debugging
        os.remove(temp_file_path)

        # logging
        conversation_manager.update_latency_metric(session_id, "srt_end", time.time())

        logger.debug(result)
        return result["text"]
    except Exception as e:
        logger.error(f"Transcription error: {str(e)}")
        logger.error(traceback.format_exc())
        raise


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_id = conversation_manager.create_session()
    logger.info(f"New WebSocket connection established. Session ID: {session_id}")

    try:
        while True:
            message = await websocket.receive()
            # logger.debug(f"Received message: {message}")

            if "bytes" in message:
                audio_data = message["bytes"]
                logger.debug(f"Received audio data. Size: {len(audio_data)} bytes")
                conversation_manager.sessions[session_id]["audio_buffer"] = audio_data
            elif "text" in message:
                logger.debug(f"Received text message: {message['text']}")
                try:
                    data = json.loads(message["text"])
                    logger.debug(f"Parsed JSON data: {data}")
                    if data.get("type") == "ping":
                        # Immediately send a pong response
                        await websocket.send_json({"type": "pong"})
                    elif data.get("action") == "stop_recording":
                        logger.info(
                            "Stop recording message received. Processing audio..."
                        )
                        conversation_manager.reset_latency_metrics(session_id)
                        if conversation_manager.sessions[session_id]["is_processing"]:
                            logger.warning("Interrupting ongoing processing")
                            conversation_manager.sessions[session_id][
                                "llm_output_sentences"
                            ].clear()
                            conversation_manager.sessions[session_id][
                                "is_processing"
                            ] = False
                            await websocket.send_json({"type": "interrupted"})
                        else:
                            conversation_manager.sessions[session_id][
                                "is_processing"
                            ] = True
                            turn_id = conversation_manager.sessions[session_id][
                                "current_turn"
                            ]
                            try:
                                audio_data = conversation_manager.sessions[session_id][
                                    "audio_buffer"
                                ]
                                logger.info(
                                    f"Processing audio data. Size: {len(audio_data)} bytes"
                                )
                                text = await transcribe_audio(
                                    audio_data, session_id, turn_id
                                )
                                if not text:
                                    raise ValueError(
                                        "Transcription resulted in empty text"
                                    )
                                logger.info(f"Transcription result: {text}")
                                conversation_manager.add_user_message(session_id, text)

                                # Send transcribed text to client
                                await websocket.send_json(
                                    {"type": "transcription", "content": text}
                                )

                                await process_and_stream(websocket, session_id, text)

                                latencies = conversation_manager.calculate_latencies(
                                    session_id
                                )
                                await websocket.send_json(
                                    {"type": "latency_metrics", "metrics": latencies}
                                )
                            except Exception as e:
                                logger.error(f"Error during processing: {str(e)}")
                                logger.error(traceback.format_exc())
                                await websocket.send_json(
                                    {"type": "error", "message": str(e)}
                                )
                            finally:
                                conversation_manager.sessions[session_id][
                                    "is_processing"
                                ] = False
                                await websocket.send_json(
                                    {"type": "processing_complete"}
                                )
                    else:
                        logger.warning(
                            f"Received unexpected action: {data.get('action')}"
                        )
                except json.JSONDecodeError:
                    logger.error(
                        f"Failed to parse JSON from text message: {message['text']}"
                    )
            else:
                logger.warning(f"Received message with unexpected format: {message}")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"Unexpected error in WebSocket endpoint: {str(e)}")
        logger.error(traceback.format_exc())
        await websocket.close(code=1011, reason=str(e))


async def process_and_stream(websocket: WebSocket, session_id, text):
    try:
        # We interleave LLM and TTS output here
        await generate_llm_response(websocket, session_id, text)
    finally:
        conversation_manager.sessions[session_id]["is_processing"] = False
        conversation_manager.sessions[session_id]["first_audio_sent"] = False


tools_payload = [
    {
        "type": "function",
        "function": {
            "name": "get_patient_info",
            "description": "Get patient details from the supabase table called 'transcripts'. The table has these columns: id, created_at, patient_code, transcript, summary. The function should return the supabase query like `supabase.table('transcripts').select('*').eq('patient_code', patient_code).execute()`. Get only summary for most of the questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "supabase_query": {
                        "type": "string",
                        "description": "The supabase query to execute. e.g. supabase.table('transcripts').select('*').eq('patient_code', patient_code).execute()",
                    },
                },
                "required": ["supabase_query"],
                "additionalProperties": False,
            },
        },
    }
]


async def generate_llm_response(websocket, session_id, text):
    conversation_manager.update_latency_metric(session_id, "llm_start", time.time())
    try:
        conversation = conversation_manager.get_conversation(session_id)

        complete_text = ""
        accumulated_text = ""
        first_token_received = False
        first_sentence_received = False
        tools = []

        for chunk in client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversation + [{"role": "user", "content": text}],
            stream=True,
            tools=tools_payload,
        ):
            content = chunk.choices[0].delta.content
            delta = chunk.choices[0].delta

            if content:
                if not first_token_received:
                    conversation_manager.update_latency_metric(
                        session_id, "llm_first_token", time.time()
                    )
                    first_token_received = True
                complete_text += content
                accumulated_text += content
                await websocket.send_json({"type": "text", "content": content})

                # Check if we have a complete sentence
                if content.endswith((".", "!", "?")):
                    if not first_sentence_received:
                        conversation_manager.update_latency_metric(
                            session_id, "llm_first_sentence", time.time()
                        )
                        first_sentence_received = True
                        conversation_manager.update_latency_metric(
                            session_id, "tts_start", time.time()
                        )
                    await generate_and_send_tts(websocket, accumulated_text)
                    accumulated_text = ""

                    if not conversation_manager.sessions[session_id][
                        "first_audio_sent"
                    ]:
                        logger.debug("first_audio_response")
                        conversation_manager.update_latency_metric(
                            session_id, "first_audio_response", time.time()
                        )
                        await websocket.send_json({"type": "first_audio_response"})
                        conversation_manager.sessions[session_id][
                            "first_audio_sent"
                        ] = True

            if chunk.choices[0].delta.tool_calls:
                tools += chunk.choices[0].delta.tool_calls

        if len(tools) > 0:
            func_call = {"name": None, "arguments": ""}
            # function call here using func_call
            for tool in tools:
                if hasattr(tool.function, "name"):
                    func_call["name"] = tool.function.name
                if hasattr(tool.function, "arguments"):
                    func_call["arguments"] += tool.function.arguments
            logger.debug(f"Function call: {func_call}")
            await generate_and_send_tts(
                websocket, "Please wait while I get the patient details..."
            )
            supabase_query = func_call["arguments"]
            logger.debug(f"Supabase query: {supabase_query}")
            try:
                result = str(eval(json.loads(supabase_query)["supabase_query"]))
            except Exception as e:
                logger.error(f"Error executing supabase query: {str(e)}")
                await generate_and_send_tts(
                    websocket,
                    "I am sorry, I am unable to get the patient details at this time. Please make sure the patient code is correct.",
                )
                accumulated_text = ""
                pass
            logger.debug(f"Supabase query result: {result}")
            await process_and_stream(
                websocket,
                session_id,
                f"Sumarize this result in simple language keep it breif, remove all punctuation: {result}",
            )

        # Send any remaining text
        if accumulated_text:
            logger.debug(f"Remaining text: {accumulated_text}")
            if not first_sentence_received:
                conversation_manager.update_latency_metric(
                    session_id, "llm_first_sentence", time.time()
                )
                first_sentence_received = True
                conversation_manager.update_latency_metric(
                    session_id, "tts_start", time.time()
                )
            await generate_and_send_tts(websocket, accumulated_text)

            if not conversation_manager.sessions[session_id]["first_audio_sent"]:
                logger.debug("first_audio_response")
                conversation_manager.update_latency_metric(
                    session_id, "first_audio_response", time.time()
                )
                await websocket.send_json({"type": "first_audio_response"})
                conversation_manager.sessions[session_id]["first_audio_sent"] = True

        # Finished sending TTS
        conversation_manager.update_latency_metric(session_id, "tts_end", time.time())

        conversation_manager.add_ai_message(session_id, complete_text)
        logger.debug(complete_text)

    except Exception as e:
        logger.error(f"LLM error: {str(e)}")
        logger.error(traceback.format_exc())
        raise


async def generate_and_send_tts(websocket, text):
    async with aiohttp.ClientSession() as session:
        async with session.post(TTS_ENDPOINT, json={"text": text}) as response:
            opus_data = await response.read()
    await websocket.send_bytes(opus_data)


async def process_llm_content(websocket, session_id, content):
    sentences = re.split(r"(?<=[.!?])\s+", content)
    for sentence in sentences:
        if sentence:
            processed_sentence = process_sentence(sentence)
            conversation_manager.sessions[session_id]["llm_output_sentences"].append(
                processed_sentence
            )
            conversation_manager.add_ai_message(session_id, processed_sentence)
            logger.debug(f"Processed sentence: {processed_sentence}")


def process_sentence(sentence):
    sentence = re.sub(r"~+", "!", sentence)
    sentence = re.sub(r"\(.*?\)", "", sentence)
    sentence = re.sub(r"(\*[^*]+\*)|(_[^_]+_)", "", sentence)
    sentence = re.sub(r"[^\x00-\x7F]+", "", sentence)
    return sentence.strip()


@app.get("/")
def read_root():
    return FileResponse("ui/index.html")


# Run session cleanup periodically
"""
@app.on_event("startup")
@app.on_event("shutdown")
async def cleanup_sessions():
    while True:
        conversation_manager.clean_old_sessions()
        await asyncio.sleep(3600)  # Run cleanup every hour
"""

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
