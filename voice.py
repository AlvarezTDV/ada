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
        """
        Inicializa los motores de Voz (TTS y STT).
        mode: 'local' (pyttsx3 nativo) o 'edge' (voz HD femenina en español).
        """
        self.mode = mode
        self.recognizer = sr.Recognizer()
        
        # Inicializar pyttsx3 nativo de Windows (SAPI5)
        try:
            self.tts_engine = pyttsx3.init()
            # Buscar una voz femenina instalada en Windows
            voices = self.tts_engine.getProperty('voices')
            female_voice = None
            for voice in voices:
                if "female" in voice.name.lower() or "helena" in voice.name.lower() or "sabina" in voice.name.lower() or "zira" in voice.name.lower():
                    female_voice = voice.id
                    break
            if female_voice:
                self.tts_engine.setProperty('voice', female_voice)
            self.tts_engine.setProperty('rate', 175) # Velocidad de habla natural
        except Exception as e:
            console.print(f"[yellow]⚠️  No se pudo inicializar pyttsx3: {e}[/yellow]")
            self.tts_engine = None

    def speak(self, text: str):
        """Convierte texto en habla (TTS)."""
        if not text:
            return
            
        if self.mode == "edge":
            # Usar voz HD de Microsoft Edge (Voz femenina natural en español: es-ES-ElviraNeural o es-MX-DaliaNeural)
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Error en Edge-TTS, usando fallback local: {e}[/dim red]")

        # Fallback a pyttsx3 local
        if self.tts_engine:
            self.tts_engine.say(text)
            self.tts_engine.runAndWait()

    async def _speak_edge(self, text: str):
        """Genera y reproduce audio HD usando Edge-TTS."""
        voice = "es-MX-DaliaNeural" # Voz femenina mexicana natural
        communicate = edge_tts.Communicate(text, voice)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_filename = fp.name
            
        await communicate.save(temp_filename)
        
        # Reproducir audio temporal usando powershell/system player
        os.system(f'powershell -c "(New-Object Media.SoundPlayer \'{temp_filename}\').PlaySync()" 2>$null || start /min wmplayer "{temp_filename}"')
        
        # Limpieza diferida
        try:
            os.remove(temp_filename)
        except Exception:
            pass

    def listen(self) -> str:
        """Escucha el micrófono del usuario y convierte voz a texto (STT)."""
        with sr.Microphone() as source:
            console.print("[dim cyan]🎤 Escuchando a Dante... (Habla ahora)[/dim cyan]")
            self.recognizer.adjust_for_ambient_noise(source, duration=0.8)
            try:
                audio = self.recognizer.listen(source, timeout=6, phrase_time_limit=10)
                console.print("[dim magenta]Procesando voz...[/dim magenta]")
                # Usa el reconocedor de Google Speech en español (gratuito)
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
            except sr.WaitTimeoutError:
                return ""
            except sr.UnknownValueError:
                console.print("[dim red]No logré entender el audio.[/dim red]")
                return ""
            except Exception as e:
                console.print(f"[red]Error al escuchar micrófono: {e}[/red]")
                return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="local")
    v.speak("Hola joven Dante, el módulo de voz de A.D.A está completamente funcional.")
    print("Prueba de voz completada.")
