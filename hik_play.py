import os
import sys

def _ensure_ld_path():
    sdk_path = os.environ.get('HIKVISION_SDK_PATH') \
               or os.path.expanduser('~/Desktop/sdk/lib')
    sdk_path = os.path.abspath(sdk_path)
    com_path = os.path.join(sdk_path, 'HCNetSDKCom')
    current = os.environ.get('LD_LIBRARY_PATH', '')
    parts = current.split(':') if current else []
    if sdk_path in parts:
        return
    new_path = ':'.join([sdk_path, com_path] + parts)
    os.environ['LD_LIBRARY_PATH'] = new_path
    os.execv(sys.executable, [sys.executable] + sys.argv)

_ensure_ld_path()

import ctypes
from ctypes import (
    c_char, c_char_p, c_int, c_uint, c_long, c_ulong, c_void_p,
    c_byte, c_ubyte, c_ushort, POINTER, Structure, CFUNCTYPE, byref, cast
)

BYTE  = c_ubyte
PBYTE = POINTER(c_ubyte)
LONG  = c_int
DWORD = c_uint
WORD  = c_ushort
BOOL  = c_int
HWND  = c_void_p

STREAME_REALTIME = 0
STREAME_FILE     = 1

T_YV12  = 3
T_RGB32 = 7

SOURCE_BUF_MAX = 1024 * 100000
DEFAULT_BUF    = 1024 * 1024 * 4


class FRAME_INFO(Structure):
    _fields_ = [
        ('nWidth',      c_int),
        ('nHeight',     c_int),
        ('nStamp',      c_int),
        ('nType',       c_int),
        ('nFrameRate',  c_int),
        ('dwFrameNum',  DWORD),
    ]

DEC_CALLBACK = CFUNCTYPE(None, c_int, POINTER(c_char), c_int,
                         POINTER(FRAME_INFO), c_int, c_int)


class PlayM4:
    _lib = None

    @classmethod
    def _load(cls):
        if cls._lib is not None:
            return cls._lib
        sdk_path = os.environ.get('HIKVISION_SDK_PATH') \
                   or os.path.expanduser('~/Desktop/sdk/lib')
        sofile = os.path.join(sdk_path, 'libPlayCtrl.so')
        if not os.path.isfile(sofile):
            raise RuntimeError(f'libPlayCtrl.so not found at {sofile}')

        for dep in ['libAudioRender.so', 'libSuperRender.so']:
            full = os.path.join(sdk_path, dep)
            if os.path.isfile(full):
                try:
                    ctypes.CDLL(full, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass

        lib = ctypes.CDLL(sofile, mode=ctypes.RTLD_GLOBAL)

        lib.PlayM4_GetPort.argtypes          = [POINTER(LONG)]
        lib.PlayM4_GetPort.restype           = BOOL
        lib.PlayM4_FreePort.argtypes         = [LONG]
        lib.PlayM4_FreePort.restype          = BOOL
        lib.PlayM4_SetStreamOpenMode.argtypes= [LONG, DWORD]
        lib.PlayM4_SetStreamOpenMode.restype = BOOL
        lib.PlayM4_OpenStream.argtypes       = [LONG, PBYTE, DWORD, DWORD]
        lib.PlayM4_OpenStream.restype        = BOOL
        lib.PlayM4_CloseStream.argtypes      = [LONG]
        lib.PlayM4_CloseStream.restype       = BOOL
        lib.PlayM4_InputData.argtypes        = [LONG, PBYTE, DWORD]
        lib.PlayM4_InputData.restype         = BOOL
        lib.PlayM4_Play.argtypes             = [LONG, HWND]
        lib.PlayM4_Play.restype              = BOOL
        lib.PlayM4_Stop.argtypes             = [LONG]
        lib.PlayM4_Stop.restype              = BOOL
        lib.PlayM4_Pause.argtypes            = [LONG, DWORD]
        lib.PlayM4_Pause.restype             = BOOL
        lib.PlayM4_Fast.argtypes             = [LONG]
        lib.PlayM4_Fast.restype              = BOOL
        lib.PlayM4_Slow.argtypes             = [LONG]
        lib.PlayM4_Slow.restype              = BOOL
        lib.PlayM4_SetDecCallBackMend.argtypes = [LONG, DEC_CALLBACK, LONG]
        lib.PlayM4_SetDecCallBackMend.restype  = BOOL
        lib.PlayM4_GetPictureSize.argtypes   = [LONG, POINTER(LONG), POINTER(LONG)]
        lib.PlayM4_GetPictureSize.restype    = BOOL
        lib.PlayM4_GetLastError.argtypes     = [LONG]
        lib.PlayM4_GetLastError.restype      = DWORD
        lib.PlayM4_GetSourceBufferRemain.argtypes = [LONG]
        lib.PlayM4_GetSourceBufferRemain.restype  = DWORD
        # Audio output (the same path iVMS uses): PlayM4 decodes the audio track
        # embedded in the live/playback stream and renders it via libAudioRender.
        # Only one port plays sound at a time (global g_bPlaySound in the lib).
        lib.PlayM4_PlaySound.argtypes        = [LONG]
        lib.PlayM4_PlaySound.restype         = BOOL
        lib.PlayM4_StopSound.argtypes        = [LONG]
        lib.PlayM4_StopSound.restype         = BOOL
        lib.PlayM4_SetVolume.argtypes        = [LONG, WORD]
        lib.PlayM4_SetVolume.restype         = BOOL

        cls._lib = lib
        return lib

    def __init__(self):
        self.lib  = self._load()
        self.port = LONG(-1)
        self._cb  = None
        self._on_frame = None
        self._opened = False
        self._stream_open = False
        self._realtime = True
        self._audio_on = False     # desired audio state (applied once playing)
        self._volume   = 1.0       # 0.0–1.0

    def last_error(self):
        return self.lib.PlayM4_GetLastError(self.port)

    # ── Audio (PlayM4 renders the stream's audio track, like iVMS) ───────────
    def set_audio(self, on):
        """Enable/disable sound for this port. Safe to call before the stream is
        open — the state is applied once playback starts."""
        self._audio_on = bool(on)
        if self._stream_open and self.port.value >= 0:
            try:
                if self._audio_on:
                    self.lib.PlayM4_PlaySound(self.port)
                    self._apply_volume()
                else:
                    self.lib.PlayM4_StopSound(self.port)
            except Exception as e:
                print(f'[PlayM4 set_audio] {e}')

    def set_volume(self, v):
        self._volume = max(0.0, min(1.0, float(v)))
        if self._stream_open and self._audio_on:
            self._apply_volume()

    def _apply_volume(self):
        try:
            self.lib.PlayM4_SetVolume(self.port, WORD(int(self._volume * 0xFFFF)))
        except Exception as e:
            print(f'[PlayM4 set_volume] {e}')

    def open(self, on_frame, realtime=True):
        self._on_frame = on_frame
        self._realtime = realtime
        self._stream_open = False

        got = False
        for attempt in range(20):
            if self.lib.PlayM4_GetPort(byref(self.port)):
                got = True
                break
            import time as _t; _t.sleep(0.1)
        if not got:
            raise RuntimeError('PlayM4_GetPort failed (no free ports)')

        def _trampoline(nPort, pBuf, nSize, pInfo, nUser, nRes2):
            try:
                info = pInfo.contents
                if info.nType == T_YV12 and nSize > 0:
                    data = ctypes.string_at(pBuf, nSize)
                    self._on_frame(info.nWidth, info.nHeight, data)
            except Exception as e:
                print(f'[PlayM4 cb] {e}')
        self._cb = DEC_CALLBACK(_trampoline)

        self._opened = True

    def _open_stream(self, header: bytes):
        mode = STREAME_REALTIME if self._realtime else STREAME_FILE
        self.lib.PlayM4_SetStreamOpenMode(self.port, mode)

        hdr = (c_ubyte * len(header)).from_buffer_copy(header)
        if not self.lib.PlayM4_OpenStream(self.port, cast(hdr, PBYTE),
                                          len(header), DEFAULT_BUF):
            err = self.last_error()
            raise RuntimeError(f'PlayM4_OpenStream failed err={err}')

        if not self.lib.PlayM4_SetDecCallBackMend(self.port, self._cb, 0):
            err = self.last_error()
            raise RuntimeError(f'SetDecCallBackMend failed err={err}')

        if not self.lib.PlayM4_Play(self.port, None):
            err = self.last_error()
            raise RuntimeError(f'PlayM4_Play failed err={err}')

        self._stream_open = True
        # Start audio now if it was requested before the stream opened.
        if self._audio_on:
            self.set_audio(True)

    def input(self, data: bytes, is_header: bool = False):
        if not self._opened:
            return False

        if not self._stream_open:
            try:
                self._open_stream(data)
            except Exception as e:
                print(f'[PlayM4] open failed: {e}')
                return False
            return True

        buf = (c_ubyte * len(data)).from_buffer_copy(data)
        return bool(self.lib.PlayM4_InputData(self.port,
                                              cast(buf, PBYTE), len(data)))

    def source_buffer_remain(self):
        if not self._stream_open:
            return 0
        try:
            return int(self.lib.PlayM4_GetSourceBufferRemain(self.port))
        except Exception:
            return 0

    def pause(self, paused: bool):
        if self._opened:
            self.lib.PlayM4_Pause(self.port, 1 if paused else 0)

    def fast(self):
        if self._opened:
            self.lib.PlayM4_Fast(self.port)

    def slow(self):
        if self._opened:
            self.lib.PlayM4_Slow(self.port)

    def close(self):
        if not self._opened:
            return
        try: self.lib.PlayM4_StopSound(self.port)
        except: pass
        try: self.lib.PlayM4_Stop(self.port)
        except: pass
        try: self.lib.PlayM4_CloseStream(self.port)
        except: pass
        try: self.lib.PlayM4_FreePort(self.port)
        except: pass
        self._opened = False


def yv12_to_rgb(width, height, data):
    import numpy as np
    try:
        import cv2
        yuv = np.frombuffer(data, dtype=np.uint8).reshape((height * 3 // 2, width))
        return cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_YV12)
    except Exception:
        ysize = width * height
        csize = (width // 2) * (height // 2)
        yuv = np.frombuffer(data, dtype=np.uint8)
        Y = yuv[0:ysize].reshape((height, width)).astype(np.float32)
        V = yuv[ysize:ysize + csize].reshape((height // 2, width // 2))
        U = yuv[ysize + csize:ysize + 2 * csize].reshape((height // 2, width // 2))
        U = U.repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)
        V = V.repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)
        Uc, Vc = U - 128.0, V - 128.0
        R = Y + 1.402 * Vc
        G = Y - 0.344136 * Uc - 0.714136 * Vc
        B = Y + 1.772 * Uc
        rgb = np.empty((height, width, 3), dtype=np.uint8)
        rgb[..., 0] = np.clip(R, 0, 255)
        rgb[..., 1] = np.clip(G, 0, 255)
        rgb[..., 2] = np.clip(B, 0, 255)
        return rgb
