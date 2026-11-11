"""
=============================================================================
agyIDE-voice-widget - Floating Voice Dictation Widget for Antigravity IDE
=============================================================================
Description:
  Discreet 20px floating widget for dictating voice messages directly to
  Google Antigravity IDE. Records ultra-lightweight 16 kHz mono audio, normalizes
  gain, and pastes into the AI chat using native Windows clipboard (CF_HDROP)
  via simulated Ctrl+V.

Features:
  1. Window & Visibility Synchronization:
     - Automatically hides when Antigravity IDE is minimized or in the background
       (e.g., when browsing in full-screen apps like Google Chrome).
     - Stays permanently visible on top when recording, ensuring complete control.
     - Instantly reappears when Antigravity IDE regains active focus.
  2. Real-Time Telemetry HUD:
     - Displays live minutes, seconds (MM:SS), and accumulated file size (MB).
     - Color-coded safety thresholds before hitting Antigravity's upload limit (~20 MB).
  3. 1-Click Quick Cancellation "✕":
     - Clicking the red '✕' immediately discards the recording without saving
       or pasting anything into the chat.
  4. Clipboard Resilience:
     - The audio file remains ready in the Windows clipboard. If the chat input
       lacked focus when recording stopped, pressing Ctrl+V pastes it manually.
  5. Zero Focus Stealing:
     - Configured with WS_EX_NOACTIVATE to avoid interrupting active typing
       or moving the editor cursor.
  6. Automatic Disk Cleanup:
     - Automatically deletes temporary local recordings older than 24 hours.
=============================================================================
"""

import os
import sys
import time
import json
import wave
import winsound
import ctypes
from ctypes import wintypes
import threading
import tkinter as tk
import sounddevice as sd
import numpy as np
import keyboard
import win32clipboard

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------
# SINGLE INSTANCE MUTEX (Win32)
# ---------------------------------------------------------
# Prevents duplicate widget processes from running concurrently.
_MUTEX_NAME = "Global\\agyIDE_Voice_Widget_SingleInstance_Mutex"
_h_mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
    # Another instance is already active in memory; exit silently
    sys.exit(0)

# ---------------------------------------------------------
# GENERAL CONFIGURATION
# ---------------------------------------------------------
HOTKEY = "f8"                                 # Global keyboard hotkey
SAMPLE_RATE = 16000                           # 16 kHz Mono (Native standard for Gemini / Whisper)
CHANNELS = 1                                  # Mono
MIN_DURATION_SEC = 0.4                        # Discard accidental presses shorter than 0.4s
MAX_RECORD_SECONDS = 300                      # Time limit: 5 minutes
MAX_RECORD_BYTES = int(19.8 * 1024 * 1024)    # File size limit: 19.8 MB (Safe ceiling for Antigravity)

BTN_SIZE = 20                                 # Button dimension: 20x20 px
BADGE_WIDTH = 142                             # HUD badge width
BADGE_HEIGHT = 20                             # HUD badge height

DIR_APP = os.path.dirname(os.path.abspath(__file__))
DIR_RECORDINGS = os.path.join(DIR_APP, "recordings")
CONFIG_FILE = os.path.join(DIR_APP, "config.json")
ICON_PATH = os.path.join(DIR_APP, "app_icon.ico")
MIC_ICON_PATH = os.path.join(DIR_APP, "mic_aurora_mini.png")
STARTUP_DIR = os.path.join(os.environ.get('APPDATA', ''), r'Microsoft\Windows\Start Menu\Programs\Startup')
STARTUP_LNK = os.path.join(STARTUP_DIR, "agyIDE Voice Widget.lnk")
os.makedirs(DIR_RECORDINGS, exist_ok=True)

def ensure_runner_vbs():
    """Ensures the existence of the local silent runner script inside src/."""
    vbs_path = os.path.join(DIR_APP, "runner.vbs")
    if not os.path.isfile(vbs_path):
        content = (
            'Set WshShell = CreateObject("WScript.Shell")\r\n'
            'srcDir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)\r\n'
            'WshShell.CurrentDirectory = srcDir\r\n'
            'WshShell.Run "pythonw """ & srcDir & "\\agyIDE_voice_widget.py""", 0, False\r\n'
        )
        try:
            with open(vbs_path, "w", encoding="latin1") as f:
                f.write(content)
        except Exception:
            pass
    return vbs_path

def is_startup_enabled():
    """Checks whether the automatic startup shortcut exists in Windows."""
    return os.path.isfile(STARTUP_LNK)

def set_startup_enabled(enable):
    """Creates or removes the startup shortcut in the Windows Startup directory."""
    try:
        if enable:
            vbs_target = ensure_runner_vbs()
            import win32com.client
            ws = win32com.client.Dispatch("WScript.Shell")
            s = ws.CreateShortcut(STARTUP_LNK)
            s.TargetPath = vbs_target
            s.WorkingDirectory = DIR_APP
            if os.path.isfile(ICON_PATH):
                s.IconLocation = f"{ICON_PATH},0"
            s.Save()
        else:
            if os.path.isfile(STARTUP_LNK):
                os.remove(STARTUP_LNK)
    except Exception:
        pass

# ---------------------------------------------------------
# WIN32 STRUCTURES
# ---------------------------------------------------------
class DROPFILES(ctypes.Structure):
    _fields_ = [
        ('pFiles', wintypes.DWORD),
        ('pt', wintypes.POINT),
        ('fNC', wintypes.BOOL),
        ('fWide', wintypes.BOOL),
    ]

TH32CS_SNAPPROCESS = 0x00000002
class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_size_t),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_char * 260)
    ]

def get_antigravity_pids():
    """Scans running processes for Antigravity IDE."""
    hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    pids = []
    if kernel32.Process32First(hSnap, ctypes.byref(pe)):
        while True:
            name = pe.szExeFile.decode('utf-8', errors='ignore').lower()
            if name in ("antigravity ide.exe", "antigravity.exe"):
                pids.append(pe.th32ProcessID)
            if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(hSnap)
    return pids

def get_antigravity_state():
    """
    Returns Antigravity IDE state:
    - running: bool (True if the process exists)
    - is_minimized: bool (True if its windows are minimized)
    - is_active: bool (True if Antigravity or the widget holds foreground focus)
    """
    pids = get_antigravity_pids()
    if not pids:
        return False, False, False

    antigravity_hwnds = []
    def enum_cb(hwnd, lParam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            parent = user32.GetParent(hwnd)
            if not parent:
                style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
                WS_VISIBLE = 0x10000000
                is_iconic = bool(user32.IsIconic(hwnd))
                if (style & WS_VISIBLE) or is_iconic:
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        antigravity_hwnds.append(hwnd)
        return True

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(EnumWindowsProc(enum_cb), 0)

    if not antigravity_hwnds:
        return True, False, False

    all_minimized = all(user32.IsIconic(h) for h in antigravity_hwnds)

    # Active foreground window detection
    hwnd_fg = user32.GetForegroundWindow()
    fg_pid = wintypes.DWORD()
    if hwnd_fg:
        user32.GetWindowThreadProcessId(hwnd_fg, ctypes.byref(fg_pid))

    is_active = (fg_pid.value in pids) or (fg_pid.value == os.getpid())
    return True, all_minimized, is_active

def copy_file_to_clipboard(file_path):
    """Copies the file path to clipboard as native CF_HDROP structure."""
    df = DROPFILES()
    df.pFiles = ctypes.sizeof(DROPFILES)
    df.pt.x = 0
    df.pt.y = 0
    df.fNC = False
    df.fWide = True
    files = (os.path.abspath(file_path) + '\0\0').encode('utf-16le')
    payload = bytes(df) + files

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, payload)
    finally:
        win32clipboard.CloseClipboard()

def send_paste():
    """Simulates Ctrl+V keystroke to paste into the active input."""
    user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
    user32.keybd_event(0x56, 0, 0, 0)  # V down
    time.sleep(0.04)
    user32.keybd_event(0x56, 0, 2, 0)  # V up
    user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up

def normalize_audio(audio_data):
    """Applies peak normalization for clear gain and optimal speech audibility."""
    samples = audio_data.astype(np.float32)
    max_val = np.max(np.abs(samples))
    if max_val > 150:
        target = 32767.0 * 0.85
        gain = min(target / max_val, 35.0)
        samples = samples * gain
        return np.clip(samples, -32768, 32767).astype(np.int16)
    return audio_data

def cleanup_old_recordings():
    """Deletes temporary recordings older than 24 hours to conserve disk space."""
    try:
        now = time.time()
        for f in os.listdir(DIR_RECORDINGS):
            path = os.path.join(DIR_RECORDINGS, f)
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > 86400:
                os.remove(path)
    except Exception:
        pass

def draw_round_rect(canvas, x1, y1, x2, y2, radius=6, **kwargs):
    """Draws a smooth rounded rectangle on a Tkinter Canvas."""
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)

def setup_no_activate(widget):
    """Applies WS_EX_NOACTIVATE style so window never steals cursor focus."""
    try:
        hwnd = user32.GetParent(widget.winfo_id()) or widget.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
    except Exception:
        pass


class VoiceWidget:
    def __init__(self):
        cleanup_old_recordings()

        self.root = tk.Tk()
        self.root.title("agyIDE-voice-widget")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        if os.path.isfile(ICON_PATH):
            try:
                self.root.iconbitmap(ICON_PATH)
            except Exception:
                pass

        self.trans_color = "#000001"
        self.root.wm_attributes("-transparentcolor", self.trans_color)
        self.root.config(bg=self.trans_color)

        # Load saved position and HUD layout
        saved_x, saved_y, saved_pos = self.load_position()
        self.badge_pos = saved_pos if saved_pos in ("left", "top") else "left"

        if saved_x is not None and saved_y is not None:
            pos_x, pos_y = saved_x, saved_y
        else:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            pos_x = screen_w - 55
            pos_y = screen_h - 140

        self.root.geometry(f"{BTN_SIZE}x{BTN_SIZE}+{pos_x}+{pos_y}")
        self.root.update_idletasks()
        setup_no_activate(self.root)

        # Main Circular Button (20x20 px)
        self.canvas = tk.Canvas(
            self.root, width=BTN_SIZE, height=BTN_SIZE,
            bg=self.trans_color, highlightthickness=0
        )
        self.canvas.pack()

        self.circle = self.canvas.create_oval(
            1, 1, BTN_SIZE - 2, BTN_SIZE - 2,
            fill="#FFFFFF", outline="#A0AAB5", width=1
        )
        self.mic_img = None
        if os.path.isfile(MIC_ICON_PATH):
            try:
                self.mic_img = tk.PhotoImage(file=MIC_ICON_PATH)
            except Exception:
                pass

        if self.mic_img:
            self.icon = self.canvas.create_image(10, 10, image=self.mic_img)
        else:
            self.icon = self.canvas.create_text(
                10, 10,
                text="🎙️", font=("Segoe UI Emoji", 7)
            )

        # Telemetry HUD Badge Window (Timer + Megabytes + Cancel ✕)
        self.badge_win = tk.Toplevel(self.root)
        self.badge_win.overrideredirect(True)
        self.badge_win.attributes("-topmost", True)
        self.badge_win.wm_attributes("-transparentcolor", self.trans_color)
        self.badge_win.config(bg=self.trans_color)
        self.badge_win.withdraw()
        setup_no_activate(self.badge_win)

        self.badge_canvas = tk.Canvas(
            self.badge_win, width=BADGE_WIDTH, height=BADGE_HEIGHT,
            bg=self.trans_color, highlightthickness=0
        )
        self.badge_canvas.pack()

        self.badge_bg = draw_round_rect(
            self.badge_canvas, 1, 1, BADGE_WIDTH - 1, BADGE_HEIGHT - 1,
            radius=6, fill="#18181B", outline="#3F3F46", width=1
        )
        self.badge_dot = self.badge_canvas.create_oval(
            7, 6, 14, 13, fill="#EF4444", outline=""
        )
        self.badge_text = self.badge_canvas.create_text(
            62, 10, text="00:00 / 0.0 MB", fill="#F4F4F5",
            font=("Consolas", 8, "bold")
        )
        # Vertical divider & Cancel button
        self.badge_sep = self.badge_canvas.create_line(
            116, 4, 116, 16, fill="#3F3F46", width=1
        )
        self.badge_cancel = self.badge_canvas.create_text(
            129, 10, text="✕", fill="#A1A1AA",
            font=("Segoe UI", 8, "bold")
        )

        # State tracking
        self.is_recording = False
        self.stream = None
        self.frames = []
        self.start_time = 0
        self.recorded_bytes = 0

        self.drag_start_x = 0
        self.drag_start_y = 0
        self.win_start_x = 0
        self.win_start_y = 0
        self.is_dragging = False

        self.is_visible_on_screen = True

        # Button event bindings
        self.canvas.bind("<Button-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.show_native_menu)

        # HUD event bindings
        self.badge_canvas.bind("<Button-1>", self.on_press)
        self.badge_canvas.bind("<B1-Motion>", self.on_drag)
        self.badge_canvas.bind("<ButtonRelease-1>", self.on_release)
        self.badge_canvas.bind("<Motion>", self.on_badge_motion)
        self.badge_canvas.bind("<Leave>", self.on_badge_leave)
        self.badge_canvas.bind("<Button-3>", self.show_native_menu)

        self.running = True
        threading.Thread(target=self.background_supervisor, daemon=True).start()

    def load_position(self):
        try:
            if os.path.isfile(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("x"), data.get("y"), data.get("badge_pos", "left")
        except Exception:
            pass
        return None, None, "left"

    def save_position(self, x, y):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": x, "y": y, "badge_pos": self.badge_pos}, f)
        except Exception:
            pass

    def update_badge_geometry(self, cur_x=None, cur_y=None):
        """Positions the HUD badge relative to the floating button."""
        if cur_x is None:
            cur_x = self.root.winfo_x()
            cur_y = self.root.winfo_y()

        if self.badge_pos == "left":
            bx = cur_x - BADGE_WIDTH - 5
            by = cur_y
        else:  # "top"
            bx = cur_x - (BADGE_WIDTH - BTN_SIZE) // 2
            by = cur_y - BADGE_HEIGHT - 4

        self.badge_win.geometry(f"{BADGE_WIDTH}x{BADGE_HEIGHT}+{bx}+{by}")

    def toggle_badge_position(self):
        """Toggles HUD position between left and top."""
        self.badge_pos = "top" if self.badge_pos == "left" else "left"
        self.save_position(self.root.winfo_x(), self.root.winfo_y())
        if self.is_recording:
            self.update_badge_geometry()

    def on_badge_motion(self, event):
        """Hover effect over the '✕' cancel glyph."""
        if event.x >= 116:
            self.badge_canvas.itemconfig(self.badge_cancel, fill="#EF4444")
            self.badge_canvas.config(cursor="hand2")
        else:
            self.badge_canvas.itemconfig(self.badge_cancel, fill="#A1A1AA")
            self.badge_canvas.config(cursor="")

    def on_badge_leave(self, event):
        self.badge_canvas.itemconfig(self.badge_cancel, fill="#A1A1AA")
        self.badge_canvas.config(cursor="")

    def on_press(self, event):
        self.drag_start_x = event.x_root
        self.drag_start_y = event.y_root
        self.win_start_x = self.root.winfo_x()
        self.win_start_y = self.root.winfo_y()
        self.is_dragging = False

    def on_drag(self, event):
        dx = event.x_root - self.drag_start_x
        dy = event.y_root - self.drag_start_y
        if abs(dx) > 2 or abs(dy) > 2:
            self.is_dragging = True
            new_x = self.win_start_x + dx
            new_y = self.win_start_y + dy
            self.root.geometry(f"+{new_x}+{new_y}")
            if self.is_recording:
                self.update_badge_geometry(new_x, new_y)

    def on_release(self, event):
        if self.is_dragging:
            self.save_position(self.root.winfo_x(), self.root.winfo_y())
        else:
            if event.widget == self.badge_canvas and event.x >= 116:
                self.cancel_record()
            else:
                self.toggle_recording()

    def show_native_menu(self, event):
        """Native Win32 right-click popup context menu."""
        try:
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            MF_STRING = 0x00000000
            TPM_RETURNCMD = 0x0100
            TPM_RIGHTBUTTON = 0x0002

            hmenu = user32.CreatePopupMenu()
            pos_label = "Top / Arriba" if self.badge_pos == "left" else "Left / Izquierda"
            startup_label = "✓ Start with Windows" if is_startup_enabled() else "Start with Windows"

            user32.AppendMenuW(hmenu, MF_STRING, 2, f"Move HUD to: {pos_label}")
            user32.AppendMenuW(hmenu, MF_STRING, 3, startup_label)
            user32.AppendMenuW(hmenu, MF_STRING, 1, "Exit Voice Widget")

            user32.SetForegroundWindow(hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                event.x_root,
                event.y_root,
                0,
                hwnd,
                None
            )
            user32.PostMessageW(hwnd, 0, 0, 0)
            user32.DestroyMenu(hmenu)

            if cmd == 1:
                self.close_app()
            elif cmd == 2:
                self.toggle_badge_position()
            elif cmd == 3:
                set_startup_enabled(not is_startup_enabled())
        except Exception:
            pass

    def toggle_recording(self):
        if not self.is_recording:
            self.start_record()
        else:
            self.stop_record()

    def start_record(self):
        self.is_recording = True
        self.frames = []
        self.start_time = time.time()
        self.recorded_bytes = 0

        self.canvas.itemconfig(self.circle, fill="#FF4D4F", outline="#CF1322", width=1.5)
        self.canvas.delete(self.icon)
        self.icon = self.canvas.create_text(10, 10, text="⏹️", font=("Segoe UI Emoji", 6))

        winsound.Beep(1200, 70)

        def audio_callback(indata, frame_count, time_info, status):
            self.frames.append(indata.copy())
            self.recorded_bytes += indata.nbytes

        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            callback=audio_callback
        )
        self.stream.start()

        self.is_visible_on_screen = True
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)

        self.update_badge_geometry()
        self.badge_win.deiconify()
        self.badge_win.lift()
        self.badge_win.attributes("-topmost", True)
        self.update_timer_loop()

    def stop_record(self):
        if not self.is_recording:
            return
        self.is_recording = False
        duration = time.time() - self.start_time

        self.canvas.itemconfig(self.circle, fill="#FFFFFF", outline="#A0AAB5", width=1)
        self.canvas.delete(self.icon)
        if self.mic_img:
            self.icon = self.canvas.create_image(10, 10, image=self.mic_img)
        else:
            self.icon = self.canvas.create_text(10, 10, text="🎙️", font=("Segoe UI Emoji", 7))
        self.badge_win.withdraw()

        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass

        if duration < MIN_DURATION_SEC:
            winsound.Beep(400, 100)
            return

        winsound.Beep(800, 80)
        threading.Thread(target=self.process_and_paste, args=(self.frames, duration), daemon=True).start()

    def cancel_record(self):
        """Immediately discards active recording without saving or pasting."""
        if not self.is_recording:
            return
        self.is_recording = False

        self.canvas.itemconfig(self.circle, fill="#FFFFFF", outline="#A0AAB5", width=1)
        self.canvas.delete(self.icon)
        if self.mic_img:
            self.icon = self.canvas.create_image(10, 10, image=self.mic_img)
        else:
            self.icon = self.canvas.create_text(10, 10, text="🎙️", font=("Segoe UI Emoji", 7))
        self.badge_win.withdraw()

        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass

        self.frames = []
        self.recorded_bytes = 0

        # Gentle descending cancellation tone
        winsound.Beep(500, 70)
        winsound.Beep(350, 90)
        print("[Cancelled] Recording discarded by user.")

    def update_timer_loop(self):
        """Refreshes HUD timer and size telemetry in real time (10 Hz)."""
        if not self.is_recording:
            return

        elapsed = time.time() - self.start_time
        mins = int(elapsed) // 60
        secs = int(elapsed) % 60
        total_bytes = self.recorded_bytes + 44  # +44 bytes standard WAV header
        mb = total_bytes / (1024 * 1024)

        # Warning threshold colors (~20 MB / 5 min ceiling)
        if mb >= 18.5 or elapsed >= 270:
            text_color = "#EF4444"   # Critical red
            border_color = "#DC2626"
        elif mb >= 15.0 or elapsed >= 240:
            text_color = "#F59E0B"   # Caution amber
            border_color = "#D97706"
        else:
            text_color = "#F4F4F5"   # Standard slate
            border_color = "#3F3F46"

        time_str = f"{mins:02d}:{secs:02d} / {mb:.1f} MB"
        self.badge_canvas.itemconfig(self.badge_text, text=time_str, fill=text_color)
        self.badge_canvas.itemconfig(self.badge_bg, outline=border_color)

        if int(elapsed * 2) % 2 == 0:
            self.badge_canvas.itemconfig(self.badge_dot, fill="#EF4444")
        else:
            self.badge_canvas.itemconfig(self.badge_dot, fill="#7F1D1D")

        self.root.after(100, self.update_timer_loop)

    def process_and_paste(self, frames, duration):
        try:
            raw_audio = np.concatenate(frames, axis=0)
            processed_audio = normalize_audio(raw_audio)

            timestamp = int(time.time() * 1000)
            wav_path = os.path.join(DIR_RECORDINGS, f"audio_{timestamp}.wav")

            with wave.open(wav_path, 'wb') as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(processed_audio.tobytes())

            copy_file_to_clipboard(wav_path)
            time.sleep(0.05)
            send_paste()
            mb = os.path.getsize(wav_path) / (1024 * 1024)
            print(f"[OK] Audio {duration:.1f}s ({mb:.2f} MB) attached. (Clipboard ready)")
        except Exception as e:
            print("[Audio error]:", e)

    def update_visibility_state(self, is_minimized, is_active):
        """
        Manages visibility and Z-order based on Antigravity IDE focus:
        - When recording: stays PERMANENTLY visible and topmost, even if the user
          switches windows to consult documents.
        - When idle and Antigravity is minimized or in background (e.g., browsing in Chrome):
          smoothly hides (withdraw) to keep screen completely clear.
        - When idle and Antigravity regains active focus:
          instantly restores to front (deiconify, lift).
        """
        if self.is_recording:
            if not self.is_visible_on_screen:
                self.root.deiconify()
                self.is_visible_on_screen = True
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.badge_win.lift()
            self.badge_win.attributes("-topmost", True)
            return

        # Idle / Standby
        if is_minimized or not is_active:
            if self.is_visible_on_screen:
                self.root.withdraw()
                self.is_visible_on_screen = False
        else:
            if not self.is_visible_on_screen:
                self.root.deiconify()
                self.is_visible_on_screen = True
            self.root.lift()
            self.root.attributes("-topmost", True)

    def background_supervisor(self):
        """Supervises safety limits, global F8 hotkey, and Antigravity IDE lifecycle."""
        last_check_state = time.time()

        while self.running:
            now = time.time()

            # 1. Safety limit check
            if self.is_recording:
                elapsed = now - self.start_time
                total_bytes = self.recorded_bytes + 44
                if elapsed >= MAX_RECORD_SECONDS or total_bytes >= MAX_RECORD_BYTES:
                    winsound.Beep(600, 150)
                    winsound.Beep(600, 150)
                    self.root.after(0, self.stop_record)

            # 2. Global F8 hotkey
            try:
                if keyboard.is_pressed(HOTKEY):
                    self.root.after(0, self.toggle_recording)
                    time.sleep(0.4)
            except Exception:
                pass

            # 3. Antigravity IDE lifecycle check (every 0.25s)
            if now - last_check_state > 0.25:
                last_check_state = now
                running, is_minimized, is_active = get_antigravity_state()
                if not running:
                    print("[Auto-Close] Antigravity IDE closed. Exiting voice widget...")
                    self.root.after(0, self.close_app)
                    break
                self.root.after(0, lambda m=is_minimized, a=is_active: self.update_visibility_state(m, a))

            time.sleep(0.04)

    def close_app(self):
        self.running = False
        try:
            self.save_position(self.root.winfo_x(), self.root.winfo_y())
        except Exception:
            pass
        try:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
        except Exception:
            pass
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = VoiceWidget()
    app.run()
