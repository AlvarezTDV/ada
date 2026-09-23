import speech_recognition as sr

def check_mics():
    print("\n--- LISTA DE MICRÓFONOS REGISTRADOS EN WINDOWS ---")
    mics = sr.Microphone.list_microphone_names()
    for idx, name in enumerate(mics):
        print(f"Index [{idx}]: {name}")
        
if __name__ == "__main__":
    check_mics()
