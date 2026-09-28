import time
import io
import wave
import os
import re
import sys
import asyncio
import threading
import queue
import edge_tts
import tempfile
import pyttsx3
import pygame
import speech_recognition as sr
from rich.console import Console

console = Console()

class VoiceEngine:
    def __init__(self, mode="edge"):
        self.mode = mode
        self.recognizer = sr.Recognizer()
        self.edge_voice = "es-MX-DaliaNeural"
        
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

    def _get_key_hit(self):
        """Abstracción multiplataforma para detectar si se presionó la tecla ENTER."""
        if sys.platform == "win32":
            import msvcrt
            if msvcrt.kbhit():
                key = msvcrt.getch()
                return key in [b'\r', b'\n']
            return False
        else:
            # En Linux/macOS usar la entrada estándar sin bloqueo (select)
            import select
            dr, _, _ = select.select([sys.stdin], [], [], 0)
            if dr:
                sys.stdin.readline()
                return True
            return False

    def listen_manual_push(self) -> str:
        """
        Push-To-Talk multiplataforma con gestión garantizada de recursos de audio (`try...finally`).
        """
        import pyaudio
        
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        
        p = pyaudio.PyAudio()
        stream = None
        frames = []
        
        console.print("[dim magenta]Transcribiendo...[/dim magenta]")
        
        try:
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)
                
                if self._get_key_hit():
                    break
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

        

        wav_buffer = io.BytesIO()
        wf = wave.open(wav_buffer, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))
        wf.close()
        
        wav_buffer.seek(0)
        
        try:
            with sr.AudioFile(wav_buffer) as source:
                audio = self.recognizer.record(source)
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
        except sr.UnknownValueError:
            console.print("[dim red]No logré entender el audio.[/dim red]")
            return ""
        except Exception as e:
            console.print(f"[red]Error procesando transcripción: {e}[/red]")
            return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="edge")
    v.speak("Prueba de refactorización de voz segura.")
