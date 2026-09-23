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
        """
        Inicializa el motor de Voz de A.D.A.
        mode: 'edge' (Voz Neuronal HD ultra-realista) o 'local' (pyttsx3 nativo).
        """
        self.mode = mode
        self.recognizer = sr.Recognizer()
        
        # TIEMPO DE RESPUESTA RÁPIDO:
        self.recognizer.pause_threshold = 0.7
        self.recognizer.energy_threshold = 100
        self.recognizer.dynamic_energy_threshold = False
        
        self.edge_voice = "es-MX-DaliaNeural"
        
        # Inicializar Pygame Mixer de forma silenciosa para audio invisible
        try:
            pygame.mixer.init()
        except Exception:
            pass

    def speak(self, text: str):
        """Convierte texto en habla (TTS) de alta definición de forma 100% invisible."""
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
        """Genera y reproduce el audio MP3 en memoria/segundo plano de forma totalmente invisible."""
        communicate = edge_tts.Communicate(text, self.edge_voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_filename = fp.name
            
        await communicate.save(temp_filename)
        
        # Reproducir mediante Pygame Mixer en segundo plano (0 ventanas)
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
        """Fallback local con re-inicialización limpia."""
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
        """Escucha el micrófono y corta rápido al hacer pausa (0.7s)."""
        with sr.Microphone() as source:
            console.print("[dim cyan]🎤 Escuchando a Dante...[/dim cyan]")
            try:
                audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=15)
                console.print("[dim magenta]Procesando voz...[/dim magenta]")
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
            except sr.WaitTimeoutError:
                console.print("[dim yellow]No se detectó habla.[/dim yellow]")
                return ""
            except sr.UnknownValueError:
                console.print("[dim red]No entendí el audio.[/dim red]")
                return ""
            except Exception as e:
                console.print(f"[red]Error micrófono: {e}[/red]")
                return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="edge")
    v.speak("Hola joven Dante. Esta es mi voz neuronal invisible.")
