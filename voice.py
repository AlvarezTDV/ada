import time
import io
import wave
import os
import sys
import asyncio
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

    def speak(self, text: str):
        if not text:
            return
            
        if self.mode == "edge":
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Fallback a motor local por error en Edge-TTS: {e}[/dim red]")

        self._speak_local(text)

    async def _speak_edge(self, text: str):
        """Genera y reproduce el audio MP3 sin dejar archivos temporales ni bloqueos de disco."""
        communicate = edge_tts.Communicate(text, self.edge_voice)
        
        # Creación segura del archivo temporal asegurando el cierre del handle antes de la reproducción
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        temp_filename = temp_file.name
        temp_file.close() # Se cierra el archivo para evitar bloqueos de permisos en Windows
        
        try:
            await communicate.save(temp_filename)
            pygame.mixer.music.load(temp_filename)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
            pygame.mixer.music.unload()
        except Exception as e:
            console.print(f"[dim red]Error reproduciendo con Pygame: {e}[/dim red]")
        finally:
            if os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                except Exception:
                    pass

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
        
        console.print("[bold cyan]🔴 GRABANDO VOZ... Presiona ENTER para DETENER Y ENVIAR AL INSTANTE.[/bold cyan]")
        
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

        console.print("[dim magenta]🛑 Grabación detenida al instante. Transcribiendo...[/dim magenta]")

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
