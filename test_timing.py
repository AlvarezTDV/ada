import time
import speech_recognition as sr

def test_timing():
    r = sr.Recognizer()
    r.pause_threshold = 0.4
    r.energy_threshold = 100
    r.dynamic_energy_threshold = False
    
    print("\n--- TEST DE TIEMPOS EXACTOS (STT vs RED) ---")
    with sr.Microphone() as source:
        print("🎤 Presiona enter y habla una frase corta ('Hola Ada')...")
        input("Presiona ENTER...")
        
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] Inicio de captura en micrófono...")
        audio = r.listen(source, timeout=8, phrase_time_limit=10)
        t_captured = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] 🛑 Audio capturado por el micrófono (Duración lectura: {t_captured - t0:.2f}s)")
        
        print(f"[{time.strftime('%H:%M:%S')}] 🌐 Enviando audio a Google STT API...")
        text = r.recognize_google(audio, language="es-ES")
        t_recognized = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] ✅ Transcripción recibida: '{text}' (Tiempo red Google: {t_recognized - t_captured:.2f}s)")

if __name__ == "__main__":
    test_timing()
