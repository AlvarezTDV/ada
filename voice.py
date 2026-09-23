import speech_recognition as sr
import os
import asyncio
import edge_tts
import tempfile
import pyttsx3
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
        
        # 1. TIEMPO DE RESPUESTA RÁPIDO:
        # Reducimos la espera tras silencio a 0.7 segundos para corte inmediato al terminar de hablar.
        self.recognizer.pause_threshold = 0.7
        self.recognizer.energy_threshold = 100
        self.recognizer.dynamic_energy_threshold = False
        
        # Voz por defecto en Edge-TTS (Dalia: Femenina, mexicana, muy natural y expresiva)
        # Opciones disponibles: 'es-MX-DaliaNeural', 'es-ES-ElviraNeural', 'es-CL-CatalinaNeural'
        self.edge_voice = "es-MX-DaliaNeural"

    def speak(self, text: str):
        """Convierte texto en habla (TTS) de alta definición humana."""
        if not text:
            return
            
        if self.mode == "edge":
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Fallback a motor local por error en Edge-TTS: {e}[/dim red]")

        # Fallback a pyttsx3 nativo de Windows si falla la red
        self._speak_local(text)

    async def _speak_edge(self, text: str):
        """Genera y reproduce la voz neuronal humana usando Edge-TTS en tiempo real."""
        communicate = edge_tts.Communicate(text, self.edge_voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_filename = fp.name
            
        await communicate.save(temp_filename)
        
        # Reproducir audio MP3 usando reproductor síncrono ligero en PowerShell
        cmd = f'powershell -c "(New-Object Media.SoundPlayer \'{temp_filename}\').PlaySync()" 2>$null || powershell -c "(New-Object -com WMPlayer.OCX.7).openPlayer(\'{temp_filename}\')"'
        os.system(cmd)
        
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
    v.speak("Hola joven Dante. Esta es mi voz neuronal realista.")
