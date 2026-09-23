import speech_recognition as sr
import pyttsx3

def test_mic_index(index):
    r = sr.Recognizer()
    r.pause_threshold = 1.0
    r.energy_threshold = 200
    r.dynamic_energy_threshold = True
    
    print(f"\n--- PROBANDO MICRÓFONO INDEX [{index}] ---")
    try:
        mic = sr.Microphone(device_index=index)
        with mic as source:
            print("Calibrando ruido (0.5s)... habla fuerte y claro al micrófono...")
            r.adjust_for_ambient_noise(source, duration=0.5)
            print("🎤 Escuchando... Di una frase completa: 'Hola Aida cómo estás hoy'")
            audio = r.listen(source, timeout=8, phrase_time_limit=12)
            print("Procesando...")
            text = r.recognize_google(audio, language="es-ES")
            print(f"✅ ÉXITO EN INDEX [{index}]: '{text}'")
            return True
    except Exception as e:
        print(f"❌ FALLÓ EN INDEX [{index}]: {e}")
        return False

if __name__ == "__main__":
    # Probamos los índices de micrófono más comunes en Windows
    for i in [None, 0, 1, 5, 6, 12, 16]:
        print(f"\nProbar Index {i}? (Presiona Enter para probar, Ctrl+C para salir)")
        try:
            input()
            test_mic_index(i)
        except KeyboardInterrupt:
            break
