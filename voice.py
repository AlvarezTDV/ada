import pyttsx3
import speech_recognition as sr
import os
import asyncio
import edge_tts
import tempfile
from rich.console import Console

console = Console()

class VoiceEngine:
    def __init__(self, mode="local"):
        self.mode = mode
        self.recognizer = sr.Recognizer()
        
        # AJUSTES DE ALTA SENSIBILIDAD PARA DISTANCIA NORMAL:
        self.recognizer.energy_threshold = 100  # Captura sonidos suaves sin tener que pegarse al micrófono
        self.recognizer.dynamic_energy_threshold = False  # Mantiene la sensibilidad fija para no atenuar
        self.recognizer.pause_threshold = 1.3
        
        self._init_tts()

    def _init_tts(self):
        """Inicializa el motor de voz TTS de Windows."""
        try:
            self.tts_engine = pyttsx3.init()
            voices = self.tts_engine.getProperty('voices')
            female_voice = None
            for voice in voices:
                if "female" in voice.name.lower() or "helena" in voice.name.lower() or "sabina" in voice.name.lower() or "zira" in voice.name.lower():
                    female_voice = voice.id
                    break
            if female_voice:
                self.tts_engine.setProperty('voice', female_voice)
            self.tts_engine.setProperty('rate', 165)
        except Exception as e:
            console.print(f"[yellow]⚠️  No se pudo inicializar pyttsx3: {e}[/yellow]")
            self.tts_engine = None

    def speak(self, text: str):
        """Convierte texto en habla (TTS)."""
        if not text:
            return
            
        if self.mode == "edge":
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Error en Edge-TTS, usando fallback local: {e}[/dim red]")

        # Garantiza que pyttsx3 reinicie el motor si se congela en Windows
        try:
            if not hasattr(self, 'tts_engine') or self.tts_engine is None:
                self._init_tts()
            self.tts_engine.say(text)
            self.tts_engine.runAndWait()
        except Exception as e:
            console.print(f"[red]Re-inicializando motor de audio TTS por error: {e}[/red]")
            try:
                self._init_tts()
                self.tts_engine.say(text)
                self.tts_engine.runAndWait()
            except Exception:
                pass

    async def _speak_edge(self, text: str):
        voice = "es-MX-DaliaNeural"
        communicate = edge_tts.Communicate(text, voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_filename = fp.name
            
        await communicate.save(temp_filename)
        os.system(f'powershell -c "(New-Object Media.SoundPlayer \'{temp_filename}\').PlaySync()" 2>$null || start /min wmplayer "{temp_filename}"')
        
        try:
            os.remove(temp_filename)
        except Exception:
            pass

    def listen(self) -> str:
        """Escucha el micrófono con ultra-sensibilidad fija."""
        with sr.Microphone() as source:
            console.print("[dim cyan]🎤 Escuchando a Dante... (Habla a distancia normal)[/dim cyan]")
            try:
                audio = self.recognizer.listen(source, timeout=10, phrase_time_limit=15)
                console.print("[dim magenta]Procesando voz...[/dim magenta]")
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
            except sr.WaitTimeoutError:
                console.print("[dim yellow]No se detectó sonido suficiente.[/dim yellow]")
                return ""
            except sr.UnknownValueError:
                console.print("[dim red]No logré entender el audio.[/dim red]")
                return ""
            except Exception as e:
                console.print(f"[red]Error al escuchar micrófono: {e}[/red]")
                return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="local")
    v.speak("Prueba de alta sensibilidad completada.")
