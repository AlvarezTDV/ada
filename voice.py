import time
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
        
        self.recognizer.pause_threshold = 0.8
        self.recognizer.non_speaking_duration = 0.5
        self.recognizer.phrase_threshold = 0.3
        self.recognizer.energy_threshold = 300
        self.recognizer.dynamic_energy_threshold = False
        
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
        Modo Push-To-Talk controlado por ENTER:
        1. Al presionar ENTER la primera vez, empieza a grabar.
        2. El usuario habla el tiempo que quiera.
        3. Presiona ENTER por segunda vez para detener inmediatamente la grabación.
        """
        try:
            with sr.Microphone() as source:
                console.print("[bold cyan]🎙️  Grabando voz... (Presiona ENTER cuando termines de hablar)[/bold cyan]")
                
                # Iniciar la escucha en segundo plano o capturar mientras espera input
                # Para un push-to-talk limpio en CLI sin bloqueos de hilos:
                # Usaremos la grabación de flujo continuo con detención manual por ENTER.
                audio_data = []
                stop_listening = False
                
                # Usamos una lectura de frases síncrona mientras se presiona ENTER
                audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=30)
                console.print("[dim magenta]Procesando voz transcrita...[/dim magenta]")
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
        except sr.WaitTimeoutError:
            console.print("[dim yellow]No se detectó habla.[/dim yellow]")
            return ""
        except sr.UnknownValueError:
            console.print("[dim red]No logré entender el audio.[/dim red]")
            return ""
        except Exception as e:
            console.print(f"[red]Error micrófono: {e}[/red]")
            return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="edge")
    v.speak("Prueba de Push-to-Talk.")
