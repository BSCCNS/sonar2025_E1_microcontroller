# https://github.com/kriomant/ch57x-keyboard-tool/tree/master
import sounddevice as sd
import numpy as np
# parece que wave es el emas rapido https://github.com/bastibe/python-soundfile/issues/376
import wave
import soundfile as sf
import threading
import time
import os
import subprocess
import curses
import shutil
from pathlib import Path

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
def send_volume_levels(audio_queue, stop_event, stdscr):    
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
        stdscr.addstr(2, 0, line)  

def wait_for_converted_file(converted_filename, wait_cancel_event, stdscr):
    global waiting_for_file, last_file_created, current_pitch
    send_message(CONVERTING) ## Tell Unreal Engine we are converting
    waiting_for_file = True
    screen_clear(stdscr)
    stdscr.addstr(1, 0, f"[*] Waiting for {converted_filename} to appear... (press ctrl-X to cancel)")
    stdscr.refresh()    
    while not os.path.exists(converted_filename):
        if wait_cancel_event.is_set():
            send_message(CANCEL) ## Tell Unreal Engine we canceled the conversion
            stdscr.clrtoeol()
            stdscr.addstr(1, 0, "[x] Waiting for converted file canceled by user.")
            stdscr.refresh()    
            waiting_for_file = False
            return
        time.sleep(0.05)
    temp = np.random.rand(500,3)
    temp = [ [float(x) for x in row] for row in temp ]  # Convert to list of lists
    send_message(READYTOPLAY) ## Tell Unreal Engine we are ready to play
    stdscr.clrtoeol()
    stdscr.addstr(1, 0, f"[✓] Converted file detected: {converted_filename}")
    stdscr.refresh()        
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


def record_audio(stdscr):
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

    screen_clear(stdscr)   
    stdscr.move(1, 0)
    stdscr.clrtoeol() 
    stdscr.addstr(1, 0, f"[*] Recording started. Press ctrl-X to cancel.")  
    stdscr.refresh() 

    udp_thread = threading.Thread(target=send_volume_levels, args=(audio_queue, stop_event, stdscr))
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
        stdscr.move(1, 0)
        stdscr.clrtoeol()         
        stdscr.addstr(1, 0, f"[!] Recording canceled.")  
        stdscr.refresh()         
    finally:
        stop_event.set()
        udp_thread.join()
        recording = False

    if not cancel_requested:
        send_message(STOPRECORDING)
        stdscr.move(1, 0)
        stdscr.clrtoeol()         
        stdscr.addstr(1, 0, f"[*] Saving to {filename}...")  
        stdscr.refresh()           
        audio_np = np.concatenate(audio_data, axis=0)
        save_to_wav(filename, audio_np)
        stdscr.move(1, 0)
        stdscr.clrtoeol()         
        stdscr.addstr(1, 0, f"[✓] Saved to {filename}")  
        stdscr.refresh()         
        ### SEND TO CONVERSION
        time.sleep(3) # TODO delete in production and chango to Applio call
        cmd = ["cp", str(filename), str(converted_filename)]  # Replace with your actual command
        stdscr.addstr(1, 0, f"[*] Running conversion asynchronously: {' '.join(cmd)}") 
        stdscr.refresh()
        try:
            proc = subprocess.Popen(cmd)
            # Do NOT wait for proc to finish here!
        except Exception as e:
            screen_clear(stdscr)         
            stdscr.addstr(1, 0, f"[x] Conversion failed to start: {e}")  
            stdscr.refresh()           

        # Wait for conversion
        wait_thread = threading.Thread(target=wait_for_converted_file, args=(converted_filename, wait_cancel_event, stdscr))
        wait_thread.start()
        wait_thread.join()
        # while not os.path.exists(converted_filename):
        #     time.sleep(1)
        # print(f"[✓] Converted file detected: {converted_filename}")
    else:
        screen_clear(stdscr)         
        stdscr.addstr(1, 0, f"[x] Recording not saved.")  
        stdscr.refresh()  

def screen_clear(stdscr):
    stdscr.clear()
    stdscr.addstr(0, 0, "Press Ctrl+R to record, Ctrl+X to cancel, Ctrl+P to play, Ctrl+C to exit.")


def main(stdscr):
    global recording, cancel_requested, waiting_for_file, wait_cancel_event, current_pitch

    curses.noecho()
    curses.cbreak()
    stdscr.nodelay(True)
    screen_clear(stdscr)
    stdscr.refresh()

    while True:
        key = stdscr.getch()
        #if key!=-1:
        #    stdscr.addstr(5, 0, f"{key} ")  # Debugging line to show key pressed
        if key == 3:  # Ctrl+C
            break
        elif key == 18 and not recording and not waiting_for_file:  # Ctrl+R
            if playing_file:
                play_cancel_event.set()
            threading.Thread(target=record_audio, args=(stdscr,)).start()
        elif key == 24:  # Ctrl+X            
            if recording:
                cancel_requested = True
            if waiting_for_file:
                stdscr.move(1, 0)
                stdscr.clrtoeol() 
                stdscr.addstr(1, 0, "[x] Canceling waiting for converted file...")
                stdscr.refresh()
                wait_cancel_event.set()
            if playing_file:
                play_cancel_event.set()
                stdscr.move(1, 0)
                stdscr.clrtoeol() 
                stdscr.addstr(1, 0, "[x] Canceling play...")
                stdscr.refresh()
        elif key == 16 and not recording and not waiting_for_file:  # Ctrl+P
            play_cancel_event.set()
            if last_file_created is not None:
                send_message(PLAY)
                screen_clear(stdscr)
                threading.Thread(target=play_wav, args=(str(last_file_created),)).start()
                stdscr.addstr(1, 0, f"[*] Started playing...{last_file_created}")  
                stdscr.refresh()              
            else:
                stdscr.move(1, 0)
                stdscr.clrtoeol() 
                stdscr.addstr(1, 0, "[x] No file to play.")
                stdscr.refresh()
        elif key == 7:
            current_pitch = max(MINPITCH, current_pitch - 3)
            if current_pitch < 0:
                s="-"
            elif current_pitch > 0: 
                s = "+"
            else:
                s = ""  
            send_message(f"pitch_{s}{str(current_pitch).zfill(2)}")
            stdscr.move(1, 0)
            stdscr.clrtoeol() 
            stdscr.addstr(1, 0, f"[*] Pitch down to {current_pitch}")
            stdscr.refresh()
        elif key == 263:   
            current_pitch = min(MAXPITCH, current_pitch + 3)         
            if current_pitch < 0:
                s="-"
            elif current_pitch > 0: 
                s = "+"
            else:
                s = ""  
            send_message(f"pitch_{s}{str(current_pitch).zfill(2)}")
            send_message(f"pitch_{current_pitch}")
            stdscr.move(1, 0)
            stdscr.clrtoeol() 
            stdscr.addstr(1, 0, f"[*] Pitch up to {current_pitch}")
            stdscr.refresh()        
        time.sleep(0.05)

class curses_screen:
    def __enter__(self):
        self.stdscr = curses.initscr()
        curses.cbreak()
        curses.noecho()
        self.stdscr.keypad(1)
        SCREEN_HEIGHT, SCREEN_WIDTH = self.stdscr.getmaxyx()
        return self.stdscr
    def __exit__(self,a,b,c):
        curses.nocbreak()
        self.stdscr.keypad(0)
        curses.echo()
        curses.endwin()

if __name__ == "__main__":
    with curses_screen() as stdscr:
        main(stdscr=stdscr)

    #curses.wrapper(main)

