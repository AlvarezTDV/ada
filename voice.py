import os
import re
import asyncio
import tempfile
from typing import NamedTuple
from rich.console import Console

console = Console()


class SpeechResult(NamedTuple):
    """Report confirmed playback progress, not merely successful synthesis."""
    success: bool
    engine: str
    completed_chunks: int = 0
    stage: str | None = None
    error: Exception | None = None


class AudioResult(NamedTuple):
    success: bool
    filename: str | None = None
    error: Exception | None = None


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

    def speak(self, text: str) -> SpeechResult:
        if not text:
            return SpeechResult(True, "none")

        text = self.clean_for_tts(text)
        if not text:
            return SpeechResult(True, "none")

        chunks = [c.strip() for c in re.split(r'(?<=[.!?:])\s+', text)
                  if len(c.strip()) >= 2]
        completed = 0

        if self.mode == "edge":
            try:
                result = asyncio.run(self._speak_edge(chunks))
            except Exception as e:
                result = SpeechResult(False, "edge", stage="setup", error=e)
            if result.success:
                return result
            completed = result.completed_chunks
            console.print(f"[dim red]Fallback a motor local por error en Edge-TTS: {result.error}[/dim red]")
            # Restart only an interrupted chunk; never replay completed chunks.
            if completed:
                text = " ".join(chunks[completed:])

        try:
            self._speak_local(text)
            return SpeechResult(True, "local", len(chunks))
        except Exception as e:
            console.print(f"[red]Salida de voz no disponible; puedes continuar por texto: {e}[/red]")
            return SpeechResult(False, "local", completed, "playback", e)

    async def _speak_edge(self, chunks: list[str]) -> SpeechResult:
        """Prefetch one chunk while playing, retaining confirmed progress on failure."""
        if not chunks:
            return SpeechResult(True, "edge")
        try:
            import edge_tts
            import pygame

            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as e:
            return SpeechResult(False, "edge", stage="setup", error=e)

        def remove_audio(filename):
            if filename:
                try:
                    os.remove(filename)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    console.print(f"[dim red]No se pudo eliminar audio temporal: {e}[/dim red]")

        async def generate_audio(chunk: str) -> AudioResult:
            """Normal completion without a nonempty file is still a failure."""
            filename = None
            try:
                temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
                filename = temp_file.name
                temp_file.close()
                communicate = edge_tts.Communicate(chunk, self.edge_voice)
                await communicate.save(filename)
                if not os.path.getsize(filename):
                    raise RuntimeError("Edge-TTS no generó audio")
                return AudioResult(True, filename)
            except Exception as e:
                remove_audio(filename)
                return AudioResult(False, error=e)
            except BaseException:
                remove_audio(filename)
                raise

        def play_and_cleanup(filename: str) -> AudioResult:
            try:
                pygame.mixer.music.load(filename)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.Clock().tick(10)
                return AudioResult(True)
            except Exception as e:
                return AudioResult(False, error=e)
            finally:
                # Cleanup errors must not turn finished playback into a replay.
                for release in (pygame.mixer.music.stop, pygame.mixer.music.unload):
                    try:
                        release()
                    except Exception as e:
                        console.print(f"[dim red]Error al liberar reproducción de audio: {e}[/dim red]")
                remove_audio(filename)

        current = await generate_audio(chunks[0])
        if not current.success:
            return SpeechResult(False, "edge", 0, "generation", current.error)

        for i in range(len(chunks)):
            prefetch_task = (asyncio.create_task(generate_audio(chunks[i + 1]))
                             if i + 1 < len(chunks) else None)
            playback = asyncio.get_running_loop().run_in_executor(
                None, play_and_cleanup, current.filename
            )
            try:
                # Do not abandon a playback thread if the coroutine is cancelled.
                played = await asyncio.shield(playback)
            except BaseException:
                await asyncio.gather(playback, return_exceptions=True)
                if prefetch_task:
                    pending = (await asyncio.gather(prefetch_task, return_exceptions=True))[0]
                    if isinstance(pending, AudioResult):
                        remove_audio(pending.filename)
                raise

            if not played.success:
                if prefetch_task:
                    pending = await prefetch_task
                    remove_audio(pending.filename)
                return SpeechResult(False, "edge", i, "playback", played.error)

            if prefetch_task:
                current = await prefetch_task
                if not current.success:
                    return SpeechResult(False, "edge", i + 1, "generation", current.error)

        return SpeechResult(True, "edge", len(chunks))

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
