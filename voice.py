import os
import re
import asyncio
import tempfile
from rich.console import Console

console = Console()

class VoiceEngine:
    def __init__(self, mode="edge"):
        self.mode = mode
        self.edge_voice = "es-MX-DaliaNeural"
        
        self.whisper_model = None

    def _get_whisper_model(self):
        if self.whisper_model is None:
            from faster_whisper import WhisperModel

            console.print("[dim]Cargando reconocimiento de voz por primera vez...[/dim]")
            self.whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        return self.whisper_model

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

        try:
            self._speak_local(text)
        except Exception as e:
            console.print(f"[red]Salida de voz no disponible; puedes continuar por texto: {e}[/red]")

    async def _speak_edge(self, text: str):
        """
        Chunked TTS with prefetch overlap:
        - Generates next chunk's audio in a background thread while current chunk plays.
        - Dramatically reduces the pause between sentences.
        """
        import edge_tts
        import pygame

        if not pygame.mixer.get_init():
            pygame.mixer.init()

        chunks = re.split(r'(?<=[.!?:])\s+', text)
        chunks = [c.strip() for c in chunks if len(c.strip()) >= 2]

        if not chunks:
            return

        async def generate_audio(chunk: str) -> str:
            """Generate a temporary MP3, removing incomplete files on failure."""
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_filename = temp_file.name
            temp_file.close()
            try:
                communicate = edge_tts.Communicate(chunk, self.edge_voice)
                await communicate.save(temp_filename)
                return temp_filename
            except BaseException:
                try:
                    os.remove(temp_filename)
                except Exception:
                    pass
                raise

        def play_and_cleanup(filename: str):
            """Load, play, wait, unload, delete a temp mp3 file."""
            try:
                pygame.mixer.music.load(filename)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
            finally:
                try:
                    pygame.mixer.music.unload()
                finally:
                    try:
                        os.remove(filename)
                    except OSError:
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

            # Settle prefetch on playback failure so no task or audio file is orphaned.
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, play_and_cleanup, current_file)
            except BaseException:
                await asyncio.gather(prefetch_task, return_exceptions=True)
                if next_file_holder[0]:
                    os.remove(next_file_holder[0])
                raise

            # By the time playback ends, prefetch is already done or nearly so
            await prefetch_task
            current_file = next_file_holder[0]

    def _speak_local(self, text: str):
        import pyttsx3

        engine = pyttsx3.init()
        try:
            voices = engine.getProperty('voices')
            for voice in voices:
                if "female" in voice.name.lower() or "helena" in voice.name.lower() or "sabina" in voice.name.lower():
                    engine.setProperty('voice', voice.id)
                    break
            engine.setProperty('rate', 165)
            engine.say(text)
            engine.runAndWait()
        finally:
            engine.stop()

    def listen_manual_push(self) -> str:
        """Record until Enter, without leaving a background console reader."""
        p = None
        stream = None
        frames = []
        capture_error = None
        try:
            import pyaudio
            import numpy as np

            model = self._get_whisper_model()
            p = pyaudio.PyAudio()

            def record_chunk(data, frame_count, time_info, status):
                nonlocal capture_error
                if status:
                    capture_error = RuntimeError(f"Fallo de captura de audio ({status})")
                    return None, pyaudio.paAbort
                frames.append(data)
                return None, pyaudio.paContinue

            stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000,
                            input=True, frames_per_buffer=1024, stream_callback=record_chunk)
            input("Grabando... Presiona ENTER para detener y volver al chat.\n")
            if capture_error:
                raise capture_error
        except Exception as e:
            console.print(f"[red]Entrada de voz no disponible; puedes continuar por texto: {e}[/red]")
            return ""
        finally:
            if stream is not None:
                for close in (stream.stop_stream, stream.close):
                    try:
                        close()
                    except Exception as e:
                        console.print(f"[red]Error al cerrar micrófono; continúa por texto: {e}[/red]")
            if p is not None:
                try:
                    p.terminate()
                except Exception as e:
                    console.print(f"[red]Error al liberar audio; continúa por texto: {e}[/red]")

        if not frames:
            return ""
        try:
            audio_np = np.frombuffer(b''.join(frames), dtype=np.int16).astype(np.float32) / 32768.0
            segments, info = model.transcribe(audio_np, language="es")
            text = " ".join([segment.text for segment in segments]).strip()
            return text
        except Exception as e:
            console.print(f"[red]Error procesando transcripción; continúa por texto: {e}[/red]")
            return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="edge")
    v.speak("Prueba de refactorización de voz segura.")
