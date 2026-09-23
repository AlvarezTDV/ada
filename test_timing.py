import time
import speech_recognition as sr

def test_timing():
    r = sr.Recognizer()
    r.pause_threshold = 0.6
    r.non_speaking_duration = 0.5
    r.energy_threshold = 250
    r.dynamic_energy_threshold = True
    
    print("\n--- TEST DE TIEMPOS Y CAPTURA REALTEK ---")
    mic = sr.Microphone(device_index=1)
    with mic as source:
        print("🎤 Presiona ENTER y habla una frase al micrófono Realtek...")
        input("Presiona ENTER para empezar...")
        
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] Escuchando...")
        audio = r.listen(source, timeout=7, phrase_time_limit=10)
        t_captured = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] 🛑 Audio capturado (Tiempo lectura: {t_captured - t0:.2f}s)")
        
        print(f"[{time.strftime('%H:%M:%S')}] 🌐 Enviando a Google STT...")
        text = r.recognize_google(audio, language="es-ES")
        t_recognized = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] ✅ TRANSCRIPCIÓN RECIBIDA: '{text}' (Tiempo red: {t_recognized - t_captured:.2f}s)")

if __name__ == "__main__":
    test_timing()
