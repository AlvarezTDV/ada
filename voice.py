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
        
        # CONFIGURACIÓN DE SENSIBILIDAD CONSTANTE Y ESTABLE (Cero distorsión entre turnos):
        self.recognizer.pause_threshold = 0.8
        self.recognizer.non_speaking_duration = 0.5
        self.recognizer.phrase_threshold = 0.3
        
        # Fuerte umbral fijo (300) sin auto-ajustes dinámicos que alteren la ganancia
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

    def listen(self) -> str:
        """Escucha usando el micrófono con instancia limpia en cada turno."""
        try:
            # Re-instanciamos el objeto Microphone() en cada turno para reiniciar la ganancia del driver en Windows
            with sr.Microphone() as source:
                console.print("[dim cyan]🎤 Escuchando a Dante...[/dim cyan]")
                t0 = time.time()
                audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=15)
                t1 = time.time()
                console.print(f"[dim magenta]Procesando voz... (Capturado en {t1-t0:.1f}s)[/dim magenta]")
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
    v.speak("Prueba de canal continuo.")
