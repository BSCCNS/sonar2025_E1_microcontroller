

import sounddevice as sd
import numpy as np
# parece que wave es el emas rapido https://github.com/bastibe/python-soundfile/issues/376
import wave
import soundfile as sf
import threading
import time
import os
import subprocess
import shutil
from pathlib import Path
from pynput import keyboard

from socketudp import (send_wf_point, send_message, send_ls_array)

# TODO FINISH THE REST OF COMMS
try:
    COLUMNS, _ = shutil.get_terminal_size()
except AttributeError:
    COLUMNS = 80

# Configuration
RECORD_SECONDS = 10 # Duration of recording in seconds
SAMPLE_RATE = 44100 # Sample rate in Hz check with microphone
CHANNELS = 1 # Number of audio channels (1 for mono, 2 for stereo)
BLOCKSIZE = 4096 # Block size for audio processing, smaller uses more cpu but gives faster response
SAMPLEWIDTH = 3 # 24 bits per sample, better wavs
GAIN = 200
ROOTFOLDER = Path.absolute(Path("./audio/"))

INPUTFOLDER = ROOTFOLDER / "input"
OUTPUTFOLDER = ROOTFOLDER / "output"
# Ensure input and output directories exist
INPUTFOLDER.mkdir(parents=True, exist_ok=True)
OUTPUTFOLDER.mkdir(parents=True, exist_ok=True)
MAXPITCH = 18
MINPITCH = -18


## Messages to Unreal Engine
CANCEL = "cancel"
CONVERTING = "converting"
PLAY = "play"
READYTOPLAY = "ready_to_play"
RECORDING = "start_waveform"
STOPRECORDING = "end_waveform"


# Global control flags
recording = False
cancel_requested = False
waiting_for_file = False
wait_cancel_event = threading.Event()
play_cancel_event = threading.Event()
last_file_created = None
current_pitch = 0
playing_file = False

# Create a nice output gradient using ANSI escape sequences.
# Stolen from https://gist.github.com/maurisvh/df919538bcef391bc89f
def send_volume_levels(audio_queue, stop_event):    
    while not stop_event.is_set():
        if not audio_queue:
            time.sleep(0.05)
            continue
        chunk = audio_queue.pop(0)
        # rms = librosa.feature.rms(y=indata)
        # vol = np.mean(rms)
        volume = float(np.linalg.norm(chunk) / len(chunk))
        send_wf_point(volume)
        # message = str(volume).encode()
        # sock.sendto(message, (UDP_IP, UDP_PORT))
        col = int(GAIN * volume * (COLUMNS - 1))  # Scale volume to terminal width
        col = min(max(col, 0), COLUMNS - 1)  # Ensure col is within bounds
        line = '█' * col + ' ' * (COLUMNS - col)
        screen_clear(line)

def wait_for_converted_file(converted_filename, wait_cancel_event):
    global waiting_for_file, last_file_created, current_pitch
    send_message(CONVERTING) ## Tell Unreal Engine we are converting
    waiting_for_file = True
    screen_clear(f"[*] Waiting for {converted_filename} to appear... (press ctrl-X to cancel)")
    while not os.path.exists(converted_filename):
        if wait_cancel_event.is_set():
            send_message(CANCEL) ## Tell Unreal Engine we canceled the conversion
            screen_clear("[x] Waiting for converted file canceled by user.")
            waiting_for_file = False
            return
        time.sleep(0.05)
    temp = np.random.rand(500,3)
    temp = [ [float(x) for x in row] for row in temp ]  # Convert to list of lists
    send_message(READYTOPLAY) ## Tell Unreal Engine we are ready to play
    screen_clear(f"[✓] Converted file detected: {converted_filename}")
    last_file_created = converted_filename
    waiting_for_file = False

def play_wav(filename):
    global playing_file
    play_cancel_event.clear()
    data, samplerate = sf.read(filename, dtype='float32')
    blocksize = 1024  # Small block for responsive stop

    def callback(outdata, frames, time, status):
        if play_cancel_event.is_set():
            raise sd.CallbackStop()
        start = callback.pos
        end = start + frames
        if data.ndim == 1:
            outdata[:, 0] = data[start:end]
        else:
            outdata[:] = data[start:end]
        callback.pos = end        
        if end >= len(data):
            playing_file = False
            raise sd.CallbackStop()
    callback.pos = 0

    try:
        playing_file = True
        with sd.OutputStream(samplerate=samplerate, channels=data.shape[1] if data.ndim > 1 else 1,
                             callback=callback, blocksize=blocksize):
            while callback.pos < len(data) and not play_cancel_event.is_set():
                time.sleep(0.05)
        playing_file = False
    except sd.CallbackStop:
        playing_file = False
        pass

def save_to_wav(filename, audio_np):
    if SAMPLEWIDTH == 3:
        # difficult to save 24-bit directly, use a library
        sf.write(filename, audio_np, SAMPLE_RATE, subtype='PCM_24')
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

    timestamp = f"{int(time.time())}"
    filename = INPUTFOLDER / f"recording_{timestamp}.wav"
    converted_filename = OUTPUTFOLDER / f"recording_{timestamp}_converted.wav"
    audio_data = []
    audio_queue = []
    stop_event = threading.Event()
    cancel_requested = False
    recording = True
    wait_cancel_event.clear()
    waiting_for_file = False

    screen_clear(f"[*] Recording started. Press ctrl-X to cancel.")  

    udp_thread = threading.Thread(target=send_volume_levels, args=(audio_queue, stop_event))
    udp_thread.start()

    def callback(indata, frames, time_info, status):
        if cancel_requested:
            raise sd.CallbackStop
        audio_data.append(indata.copy())
        audio_queue.append(indata.copy())

    try:
        send_message(RECORDING)
        with sd.InputStream(callback=callback, 
                            channels=CHANNELS, 
                            samplerate=SAMPLE_RATE,
                            blocksize=BLOCKSIZE):
            start_time = time.time()
            while (time.time() - start_time) < RECORD_SECONDS:
                if cancel_requested:
                    break
                time.sleep(0.1)  # Check every 50ms for cancellation
    except sd.CallbackStop:
        screen_clear(f"[!] Recording canceled.")  
    finally:
        stop_event.set()
        udp_thread.join()
        recording = False

    if not cancel_requested:
        send_message(STOPRECORDING)
        screen_clear(f"[*] Saving to {filename}...")          
        audio_np = np.concatenate(audio_data, axis=0)
        save_to_wav(filename, audio_np)
        screen_clear(f"[✓] Saved to {filename}")          
        ### SEND TO CONVERSION
        time.sleep(3) # TODO delete in production and chango to Applio call
        cmd = ["cp", str(filename), str(converted_filename)]  # Replace with your actual command
        screen_clear(f"[*] Running conversion asynchronously: {' '.join(cmd)}") 
        try:
            proc = subprocess.Popen(cmd)
            # Do NOT wait for proc to finish here!
        except Exception as e:
            screen_clear(f"[x] Conversion failed to start: {e}")  

        # Wait for conversion
        wait_thread = threading.Thread(target=wait_for_converted_file, args=(converted_filename, wait_cancel_event))
        wait_thread.start()
        wait_thread.join()
        # while not os.path.exists(converted_filename):
        #     time.sleep(1)
        # print(f"[✓] Converted file detected: {converted_filename}")
    else:
        screen_clear(f"[x] Recording not saved.")  

def screen_clear(text=None):
    os.system("clear")
    print("Press Ctrl+R to record, Ctrl+X to cancel, Ctrl+P to play, Ctrl+C to exit.")
    if text is not None:
        print(text)

def on_record():
    global recording, waiting_for_file
    if not recording and not waiting_for_file:
        threading.Thread(target=record_audio).start()

def on_cancel():
    global cancel_requested, play_cancel_event
    if recording:
        cancel_requested = True
    if waiting_for_file:
        wait_cancel_event.set()
    if playing_file:
                play_cancel_event.set()
    print("[x] Cancel requested.")

def on_play():
    global last_file_created
    if last_file_created is not None:
        play_cancel_event.set()
        send_message(PLAY)                
        threading.Thread(target=play_wav, args=(str(last_file_created),)).start()
        print(f"[*] Playing {last_file_created}")
    else:
        print("[x] No file to play.")
        
def main():
    print("Global Hotkeys:")
    print("  Ctrl+R: Record")
    print("  Ctrl+P: Play last file")
    print("  Ctrl+X: Cancel recording/playback")
    print("  Ctrl+C: Exit")
    with keyboard.GlobalHotKeys({
        '<ctrl>+r': on_record,
        '<ctrl>+p': on_play,
        '<ctrl>+x': on_cancel,
    }) as h:
        try:
            h.join()
        except KeyboardInterrupt:
            print("Exiting...")

if __name__ == "__main__":
    main()