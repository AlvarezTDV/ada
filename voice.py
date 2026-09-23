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
        
        # AJUSTES DE SENSIBILIDAD DEL MICRÓFONO:
        # 1. pause_threshold: Tiempo de silencio (en segundos) antes de considerar que terminaste de hablar.
        self.recognizer.pause_threshold = 1.8
        # 2. non_speaking_duration: Margen de pausa entre palabras sin cortar la grabación.
        self.recognizer.non_speaking_duration = 0.8
        # 3. energy_threshold: Umbral de volumen mínimo (si el micro graba bajo, ajustamos el piso).
        self.recognizer.dynamic_energy_threshold = True
        
        # Inicializar pyttsx3 nativo de Windows (SAPI5)
        try:
            self.tts_engine = pyttsx3.init()
            voices = self.tts_engine.getProperty('voices')
            female_voice = None
            for voice in voices:
                if "female" in voice.name.lower() or "helena" in voice.name.lower() or "sabina" in voice.name.lower() or "zira" in voice.name.lower() or "hilda" in voice.name.lower():
                    female_voice = voice.id
                    break
            if female_voice:
                self.tts_engine.setProperty('voice', female_voice)
            self.tts_engine.setProperty('rate', 165) # Velocidad ligeramente más pausada y clara
        except Exception as e:
            console.print(f"[yellow]⚠️  No se pudo inicializar pyttsx3: {e}[/yellow]")
            self.tts_engine = None

    def speak(self, text: str):
        """Convierte texto en habla (TTS) de forma síncrona y segura."""
        if not text:
            return
            
        if self.mode == "edge":
            try:
                asyncio.run(self._speak_edge(text))
                return
            except Exception as e:
                console.print(f"[dim red]Error en Edge-TTS, usando fallback local: {e}[/dim red]")

        # Fallback a pyttsx3 local asegurando que el loop finalice
        if self.tts_engine:
            try:
                self.tts_engine.say(text)
                self.tts_engine.runAndWait()
            except Exception as e:
                console.print(f"[red]Error reproduciendo audio: {e}[/red]")

    async def _speak_edge(self, text: str):
        """Genera y reproduce audio HD usando Edge-TTS."""
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
        """Escucha el micrófono del usuario con tolerancia extendida de tiempo."""
        with sr.Microphone() as source:
            console.print("[dim cyan]🎤 Escuchando a Dante... (Habla tranquilo, no te apresures)[/dim cyan]")
            # Reducimos la calibración de ruido inicial a 0.3s para que no consuma tu tiempo de inicio
            self.recognizer.adjust_for_ambient_noise(source, duration=0.3)
            try:
                # timeout: tiempo máximo esperando a que empiece a hablar (8 segundos)
                # phrase_time_limit: duración máxima de la frase hablada (15 segundos)
                audio = self.recognizer.listen(source, timeout=8, phrase_time_limit=15)
                console.print("[dim magenta]Procesando voz...[/dim magenta]")
                text = self.recognizer.recognize_google(audio, language="es-ES")
                return text
            except sr.WaitTimeoutError:
                console.print("[dim yellow]No se detectó inicio de voz a tiempo.[/dim yellow]")
                return ""
            except sr.UnknownValueError:
                console.print("[dim red]No logré entender el audio (intenta hablar un poco más fuerte).[/dim red]")
                return ""
            except Exception as e:
                console.print(f"[red]Error al escuchar micrófono: {e}[/red]")
                return ""

if __name__ == "__main__":
    v = VoiceEngine(mode="local")
    v.speak("Hola joven Dante, calibración de micrófono completada.")
