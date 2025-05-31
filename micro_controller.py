import sounddevice as sd
import numpy as np
# parece que wave es el emas rapido https://github.com/bastibe/python-soundfile/issues/376
import wave
from pynput import keyboard
import threading
import time
import os
import subprocess

from socketudp import SocketUDP, format_wf_point

# Configuration
RECORD_SECONDS = 10
SAMPLE_RATE = 44100
CHANNELS = 1

SAMPLEWIDTH = 3 # 24 bits per sample

# Global control flags
recording = False
cancel_requested = False
waiting_for_file = False

wait_cancel_event = threading.Event()

def send_volume_levels(audio_queue, stop_event):
    socket = SocketUDP("localhost", debug= None)
    while not stop_event.is_set():
        if not audio_queue:
            time.sleep(0.05)
            continue
        chunk = audio_queue.pop(0)
        # rms = librosa.feature.rms(y=indata)
        # vol = np.mean(rms)
        volume = float(np.linalg.norm(chunk) / len(chunk))
        socket.send(format_wf_point(volume))
        # message = str(volume).encode()
        # sock.sendto(message, (UDP_IP, UDP_PORT))

def wait_for_converted_file(converted_filename, wait_cancel_event):
    global waiting_for_file
    waiting_for_file = True
    print(f"[*] Waiting for {converted_filename} to appear... (press 'c' to cancel)")
    while not os.path.exists(converted_filename):
        if wait_cancel_event.is_set():
            print("[x] Waiting for converted file canceled by user.")
            waiting_for_file = False
            return
        time.sleep(0.1)
    print(f"[✓] Converted file detected: {converted_filename}")
    waiting_for_file = False

def save_to_wav(filename, audio_np):
    if SAMPLEWIDTH == 3:
        # Convert float32 [-1, 1] to int32, then to 24-bit PCM bytes
        audio_int32 = np.clip(audio_np * 2147483647, -2147483648, 2147483647).astype(np.int32)
        # Convert int32 to 24-bit PCM bytes (little endian)
        audio_bytes = audio_int32.astype('<i4').tobytes()
        # Remove the highest byte to get 3 bytes per sample
        audio_bytes_24 = b''.join([audio_bytes[i:i+3] for i in range(0, len(audio_bytes), 4)])
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(3)  # 24 bits = 3 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_bytes_24)
    elif SAMPLEWIDTH == 2:
        audio_np = (audio_np * 32767).astype(np.int16)  # Convert to 16-bit
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_np.tobytes())
    if SAMPLEWIDTH == 4:
        # Convert float32 [-1, 1] to int32
        audio_int32 = np.clip(audio_np * 2147483647, -2147483648, 2147483647).astype(np.int32)
        with wave.open(filename, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(4)  # 32 bits = 4 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_int32.tobytes())            



def record_audio():
    global recording, cancel_requested, wait_cancel_event, waiting_for_file

    filename = f"recording_{int(time.time())}.wav"
    audio_data = []
    audio_queue = []
    stop_event = threading.Event()
    cancel_requested = False
    recording = True
    wait_cancel_event.clear()
    waiting_for_file = False

    print("[*] Recording started. Press 'c' to cancel.")

    udp_thread = threading.Thread(target=send_volume_levels, args=(audio_queue, stop_event))
    udp_thread.start()

    def callback(indata, frames, time_info, status):
        if cancel_requested:
            raise sd.CallbackStop
        audio_data.append(indata.copy())
        audio_queue.append(indata.copy())

    try:
        with sd.InputStream(callback=callback, channels=CHANNELS, samplerate=SAMPLE_RATE):
            sd.sleep(RECORD_SECONDS * 1000)
    except sd.CallbackStop:
        print("[!] Recording canceled.")
    finally:
        stop_event.set()
        udp_thread.join()
        recording = False

    if not cancel_requested:
        print(f"[*] Saving to {filename}...")
        audio_np = np.concatenate(audio_data, axis=0)
        save_to_wav(filename, audio_np)
        print(f"[✓] Saved to {filename}")
        ### SEND TO CONVERSION
        converted_filename = filename.replace('.wav', '_converted.wav')

        cmd = ["sleep 5; cp", filename, converted_filename]  # Replace with your actual command
        print(f"[*] Running conversion asynchronously: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(cmd)
            # Do NOT wait for proc to finish here!
        except Exception as e:
            print(f"[x] Conversion failed to start: {e}")

        # Wait for conversion
        print(f"[*] Waiting for {converted_filename} to appear...")
        wait_thread = threading.Thread(target=wait_for_converted_file, args=(converted_filename, wait_cancel_event))
        wait_thread.start()
        wait_thread.join()
        # while not os.path.exists(converted_filename):
        #     time.sleep(1)
        # print(f"[✓] Converted file detected: {converted_filename}")
    else:
        print("[x] Recording not saved.")

def on_press(key):
    global recording, cancel_requested, wait_cancel_event, waiting_for_file

    try:
        if key.char == 'r' and not recording:
            threading.Thread(target=record_audio).start()
        elif key.char == 'c':
            if recording:               
                cancel_requested = True
            if waiting_for_file:
                print("[x] Canceling waiting for converted file...")
                wait_cancel_event.set()
    except AttributeError:
        pass

def main():
    print("=== Audio Recorder ===")
    print("Press 'r' to record 10 seconds of audio.")
    print("Press 'c' during recording to cancel.")
    print("Press Ctrl+C to exit.")
    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

if __name__ == "__main__":
    main()