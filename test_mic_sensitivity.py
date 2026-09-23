import speech_recognition as sr
import pyttsx3

def mic_sensitivity_test():
    r = sr.Recognizer()
    mics = sr.Microphone.list_microphone_names()
    print("\n--- PRUEBA DE DISPOSITIVOS Y SENSIBILIDAD ---")
    for i, m in enumerate(mics):
        print(f"Index {i}: {m}")
        
    print("\nSensibilidad actual configurada a 100 (Muy alta sensibilidad)...")
    r.energy_threshold = 100
    r.dynamic_energy_threshold = False
    
    with sr.Microphone() as source:
        print("\n🎤 Habla a una distancia NORMAL de tu micrófono durante 5 segundos...")
        try:
            audio = r.listen(source, timeout=8, phrase_time_limit=8)
            print("🔊 Procesando audio...")
            text = r.recognize_google(audio, language="es-ES")
            print(f"\n✅ RECONOCIDO CON ÉXITO: '{text}'")
            
            engine = pyttsx3.init()
            print("🔊 Probando altavoces...")
            engine.say(f"Prueba de audio correcta. Escuché: {text}")
            engine.runAndWait()
        except sr.WaitTimeoutError:
            print("\n❌ No se detectó audio (El volumen del micrófono en Windows podría estar muy bajo).")
        except sr.UnknownValueError:
            print("\n❌ Audio capturado pero no se entendió (Revisa la ganancia de tu micrófono).")
        except Exception as e:
            print(f"\n❌ Error: {e}")

if __name__ == "__main__":
    mic_sensitivity_test()
