import time
import io
import wave
import pyaudio
import speech_recognition as sr
import os
import asyncio
import edge_tts
import tempfile
import pyttsx3
import pygame
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
        communicate = edge_tts.Communicate(text, self.edge_voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_filename = fp.name
            
        await communicate.save(temp_filename)
        
        try:
            pygame.mixer.music.load(temp_filename)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
            pygame.mixer.music.unload()
        except Exception as e:
            console.print(f"[dim red]Error reproduciendo con Pygame: {e}[/dim red]")
        finally:
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

    def listen_manual_push(self) -> str:
        """
        Push-To-Talk REAL por teclado:
        - Abre el stream del micrófono al instante en cuanto le das ENTER.
        - En cuanto presionas ENTER por segunda vez, CORTA EL AUDIO DE INMEDIATO (0.01 segundos).
        """
        CHUNK = 1024
        FORMAT = pyaudio.paInt16
        CHANNELS = 1
        RATE = 16000
        
        p = pyaudio.PyAudio()
        try:
            stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
        except Exception as e:
            console.print(f"[red]Error abriendo micrófono: {e}[/red]")
            return ""

        console.print("[bold cyan]🔴 GRABANDO VOZ... Presiona ENTER en cualquier momento para DETENER Y ENVIAR AL INSTANTE.[/bold cyan]")
        
        frames = []
        import sys
        import msvcrt

        # Grabación manual continua en búfer de memoria hasta presionar ENTER
        while True:
            data = stream.read(CHUNK, exception_on_overflow=False)
            frames.append(data)
            
            # Detectar si el usuario presionó la tecla ENTER en la consola Windows
            if msvcrt.kbhit():
                key = msvcrt.getch()
                if key in [b'\r', b'\n']: # ENTER detectado
                    break

        console.print("[dim magenta]🛑 Grabación detenida al instante. Transcribiendo...[/dim magenta]")
        
        stream.stop_stream()
        stream.close()
        p.terminate()

        # Convertir búfer de audio en objeto AudioData de SpeechRecognition
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
    v.speak("Prueba de Push-to-talk real instantáneo.")
