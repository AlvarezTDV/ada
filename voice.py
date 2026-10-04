import time
import io
import wave
import os
import re
import sys
import asyncio
import threading
import edge_tts
import tempfile
import pyttsx3
import pygame
from faster_whisper import WhisperModel
from rich.console import Console

console = Console()

class VoiceEngine:
    def __init__(self, mode="edge"):
        self.mode = mode
        self.edge_voice = "es-MX-DaliaNeural"
        
        # Cargar el modelo local de Whisper (quantizado a int8 para CPU) para máxima velocidad y privacidad
        self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        
        try:
            pygame.mixer.init()
        except Exception:
            pass

    @staticmethod
    def clean_for_tts(text: str) -> str:
        """Strip markdown symbols so TTS reads clean natural text."""
        # Remove bold/italic markers while keeping inner text
        text = re.sub(r'\*\*?(.*?)\*\*?', r'\1', text)
        # Remove heading markers
        text = re.sub(r'#{1,6}\s?', '', text)
        # Remove remaining inline symbols
        text = re.sub(r'[*_`~|]', '', text)
        # Remove markdown links [label](url)
        text = re.sub(r'\[.*?\]\(.*?\)', '', text)
        # Remove list bullets at the start of lines
        text = re.sub(r'(?m)^\s*[-•]\s?', '', text)
        # Replace newlines with spoken pauses
        text = re.sub(r'\n+', '. ', text)
        # Collapse extra whitespace
        text = re.sub(r'\s{2,}', ' ', text)
        return text.strip()

    def speak(self, text: str):
        if not text:
            return

        text = self.clean_for_tts(text)

        if self.mode == "edge":
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Fallback a motor local por error en Edge-TTS: {e}[/dim red]")

        self._speak_local(text)

    async def _speak_edge(self, text: str):
        """
        Chunked TTS with prefetch overlap:
        - Generates next chunk's audio in a background thread while current chunk plays.
        - Dramatically reduces the pause between sentences.
        """
        chunks = re.split(r'(?<=[.!?:])\s+', text)
        chunks = [c.strip() for c in chunks if len(c.strip()) >= 2]

        if not chunks:
            return

        async def generate_audio(chunk: str) -> str | None:
            """Generate a temp mp3 for a chunk and return its path, or None on failure."""
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_filename = temp_file.name
            temp_file.close()
            try:
                communicate = edge_tts.Communicate(chunk, self.edge_voice)
                await communicate.save(temp_filename)
                return temp_filename
            except Exception as e:
                console.print(f"[dim red]Error generando audio: {e}[/dim red]")
                try:
                    os.remove(temp_filename)
                except Exception:
                    pass
                return None

        def play_and_cleanup(filename: str):
            """Load, play, wait, unload, delete a temp mp3 file."""
            try:
                pygame.mixer.music.load(filename)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                pygame.mixer.music.unload()
            except Exception as e:
                console.print(f"[dim red]Error reproduciendo chunk: {e}[/dim red]")
            finally:
                try:
                    os.remove(filename)
                except Exception:
                    pass

        # Pre-generate the first chunk before entering the loop
        current_file = await generate_audio(chunks[0])

        for i, chunk in enumerate(chunks):
            # Kick off generation of the NEXT chunk concurrently.
            # run_in_executor offloads pygame playback to a thread so the event
            # loop stays alive and can actually drive the prefetch coroutine.
            next_file_holder = [None]

            async def prefetch_next():
                if i + 1 < len(chunks):
                    next_file_holder[0] = await generate_audio(chunks[i + 1])

            prefetch_task = asyncio.create_task(prefetch_next())

            # Play in a thread — event loop stays free for prefetch_task
            if current_file:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, play_and_cleanup, current_file)

            # By the time playback ends, prefetch is already done or nearly so
            await prefetch_task
            current_file = next_file_holder[0]

    def _speak_local(self, text: str):
        try:
            engine = pyttsx3.init()
            voices = engine.getProperty('voices')
            for voice in voices:
                if "female" in voice.name.lower() or "helena" in voice.name.lower() or "sabina" in voice.name.lower():
                    engine.setProperty('voice', voice.id)
                    break
            engine.setProperty('rate', 165)
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        except Exception:
            pass

    def listen_manual_push(self) -> str:
        """
        Push-To-Talk con gestión garantizada y eventos de consola puros (Sin msvcrt).
        """
        import pyaudio
        
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        
        p = pyaudio.PyAudio()
        stream = None
        frames = []
        
        stop_event = threading.Event()
        
        def wait_for_enter():
            input()
            stop_event.set()
            
        # Hilo en segundo plano que espera el próximo ENTER de forma nativa multiplataforma
        threading.Thread(target=wait_for_enter, daemon=True).start()
        
        try:
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
            while not stop_event.is_set():
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)
                
        except Exception as e:
            console.print(f"[red]Error abriendo/leyendo micrófono: {e}[/red]")
            return ""
        finally:
            # Garantizamos la liberación segura del hardware de audio sin importar excepciones
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            p.terminate()

        import numpy as np
        audio_data = b''.join(frames)
        
        # Convertir el audio crudo PCM de 16-bit a matriz Numpy Float32 (Requisito nativo de Whisper)
        # ¡Esto evita escribir archivos en el disco duro y soluciona bloqueos en Windows!
        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        
        try:
            # Transcripción offline 100% privada usando faster-whisper directamente desde la memoria RAM
            segments, info = self.whisper_model.transcribe(audio_np, language="es")
            text = " ".join([segment.text for segment in segments]).strip()
            return text
        except Exception as e:
            console.print(f"[red]Error procesando transcripción: {e}[/red]")
            return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="edge")
    v.speak("Prueba de refactorización de voz segura.")
