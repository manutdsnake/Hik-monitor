import os, sys, time, threading, queue, ctypes, ctypes.util
from ctypes import (
    c_byte, c_ubyte, c_char, c_char_p, c_int, c_uint, c_long, c_ulong,
    c_short, c_ushort, c_void_p, POINTER, Structure, CFUNCTYPE, byref, sizeof
)
from pathlib import Path

BYTE   = c_ubyte
WORD   = c_ushort
DWORD  = c_uint
LONG   = c_int
BOOL   = c_int
HWND   = c_void_p
LPVOID = c_void_p

SERIALNO_LEN     = 48
NET_SDK_INIT_CFG_SDK_PATH = 2

NET_DVR_PLAYBACKSTART       = 1
NET_DVR_PLAYBACKSTOP        = 2
NET_DVR_PLAYBACKPAUSE       = 3
NET_DVR_PLAYBACKRESTART     = 4
NET_DVR_PLAYBACKFAST        = 5
NET_DVR_PLAYBACKSLOW        = 6
NET_DVR_PLAYBACKNORMAL      = 7


class NET_DVR_DEVICEINFO_V30(Structure):
    _fields_ = [
        ('sSerialNumber',        BYTE * SERIALNO_LEN),
        ('byAlarmInPortNum',     BYTE),
        ('byAlarmOutPortNum',    BYTE),
        ('byDiskNum',            BYTE),
        ('byDVRType',            BYTE),
        ('byChanNum',            BYTE),
        ('byStartChan',          BYTE),
        ('byAudioChanNum',       BYTE),
        ('byIPChanNum',          BYTE),
        ('byZeroChanNum',        BYTE),
        ('byMainProto',          BYTE),
        ('bySubProto',           BYTE),
        ('bySupport',            BYTE),
        ('bySupport1',           BYTE),
        ('bySupport2',           BYTE),
        ('wDevType',             WORD),
        ('bySupport3',           BYTE),
        ('byMultiStreamProto',   BYTE),
        ('byStartDChan',         BYTE),
        ('byStartDTalkChan',     BYTE),
        ('byHighDChanNum',       BYTE),
        ('bySupport4',           BYTE),
        ('byLanguageType',       BYTE),
        ('byVoiceInChanNum',     BYTE),
        ('byStartVoiceInChanNo', BYTE),
        ('bySupport5',           BYTE),
        ('bySupport6',           BYTE),
        ('byMirrorChanNum',      BYTE),
        ('wStartMirrorChanNo',   WORD),
        ('bySupport7',           BYTE),
        ('byRes2',               BYTE),
    ]

class NET_DVR_TIME(Structure):
    _fields_ = [
        ('dwYear',   DWORD),
        ('dwMonth',  DWORD),
        ('dwDay',    DWORD),
        ('dwHour',   DWORD),
        ('dwMinute', DWORD),
        ('dwSecond', DWORD),
    ]

class NET_DVR_PREVIEWINFO(Structure):
    _fields_ = [
        ('lChannel',          LONG),
        ('dwStreamType',      DWORD),
        ('dwLinkMode',        DWORD),
        ('hPlayWnd',          HWND),
        ('bBlocked',          BOOL),
        ('bPassbackRecord',   BOOL),
        ('byPreviewMode',     BYTE),
        ('byStreamID',        BYTE * 32),
        ('byProtoType',       BYTE),
        ('byRes1',            BYTE),
        ('byVideoCodingType', BYTE),
        ('dwDisplayBufNum',   DWORD),
        ('byNPQ',             BYTE),
        ('byRes',             BYTE * 215),
    ]

class NET_DVR_VOD_PARA(Structure):
    _fields_ = [
        ('dwSize',         DWORD),
        ('struIDInfo',     NET_DVR_PREVIEWINFO),
        ('struBeginTime',  NET_DVR_TIME),
        ('struEndTime',    NET_DVR_TIME),
        ('hWnd',           HWND),
        ('byDrawFrame',    BYTE),
        ('byVolumeNum',    BYTE),
        ('byStreamType',   BYTE),
        ('byProtoType',    BYTE),
        ('sFileName',      c_char * 128),
        ('byLocatorType',  BYTE),
        ('byTransProtocol',BYTE),
        ('byRes',          BYTE * 126),
    ]

STREAM_ID_LEN = 32

class NET_DVR_PLAYCOND(Structure):
    _fields_ = [
        ('dwChannel',           DWORD),
        ('struStartTime',       NET_DVR_TIME),
        ('struStopTime',        NET_DVR_TIME),
        ('byDrawFrame',         BYTE),
        ('byStreamType',        BYTE),
        ('byStreamID',          BYTE * STREAM_ID_LEN),
        ('byCourseFile',        BYTE),
        ('byDownload',          BYTE),
        ('byOptimalStreamType', BYTE),
        ('byVODFileType',       BYTE),
        ('byRes',               BYTE * 26),
    ]

class NET_DVR_LOCAL_SDK_PATH(Structure):
    _fields_ = [
        ('sPath', c_char * 256),
        ('byRes', BYTE * 128),
    ]

PLAYBACK_CALLBACK = CFUNCTYPE(None, LONG, DWORD, POINTER(c_ubyte), DWORD, c_void_p)


class HCNetSDK:
    def __init__(self, sdk_path=None):
        sdk_path = sdk_path or os.environ.get('HIKVISION_SDK_PATH') \
                   or os.path.expanduser('~/Desktop/sdk/lib')
        sdk_path = os.path.abspath(sdk_path)

        if not os.path.isdir(sdk_path):
            raise RuntimeError(f'SDK directory not found: {sdk_path}')
        if not os.path.isfile(os.path.join(sdk_path, 'libhcnetsdk.so')):
            raise RuntimeError(f'libhcnetsdk.so not in {sdk_path}')

        self.sdk_path = sdk_path

        com_path = os.path.join(sdk_path, 'HCNetSDKCom')
        ld = os.environ.get('LD_LIBRARY_PATH', '')
        os.environ['LD_LIBRARY_PATH'] = f'{sdk_path}:{com_path}:{ld}'

        for dep in ['libcrypto.so.1.1', 'libssl.so.1.1', 'libz.so', 'libHCCore.so']:
            full = os.path.join(sdk_path, dep)
            if os.path.isfile(full):
                try:
                    ctypes.CDLL(full, mode=ctypes.RTLD_GLOBAL)
                except OSError as e:
                    print(f'[SDK] Warning: failed to preload {dep}: {e}')

        self.lib = ctypes.CDLL(os.path.join(sdk_path, 'libhcnetsdk.so'),
                                mode=ctypes.RTLD_GLOBAL)

        self._setup_prototypes()

        path_cfg = NET_DVR_LOCAL_SDK_PATH()
        path_cfg.sPath = sdk_path.encode() + b'/'
        ok = self.lib.NET_DVR_SetSDKInitCfg(NET_SDK_INIT_CFG_SDK_PATH, byref(path_cfg))
        if not ok:
            print(f'[SDK] Warning: SetSDKInitCfg failed ({self.last_error()})')

        if not self.lib.NET_DVR_Init():
            raise RuntimeError(f'NET_DVR_Init failed: {self.last_error()}')

        self.lib.NET_DVR_SetConnectTime(5000, 3)
        self.lib.NET_DVR_SetReconnect(10000, 1)

        self._cb_refs = {}

    def _setup_prototypes(self):
        L = self.lib

        L.NET_DVR_Init.restype = BOOL
        L.NET_DVR_Cleanup.restype = BOOL
        L.NET_DVR_GetLastError.restype = DWORD
        L.NET_DVR_GetErrorMsg.argtypes = [POINTER(LONG)]
        L.NET_DVR_GetErrorMsg.restype = c_char_p
        L.NET_DVR_SetConnectTime.argtypes = [DWORD, DWORD]
        L.NET_DVR_SetReconnect.argtypes = [DWORD, BOOL]

        L.NET_DVR_SetSDKInitCfg.argtypes = [DWORD, c_void_p]
        L.NET_DVR_SetSDKInitCfg.restype = BOOL

        L.NET_DVR_Login_V30.argtypes = [c_char_p, WORD, c_char_p, c_char_p,
                                          POINTER(NET_DVR_DEVICEINFO_V30)]
        L.NET_DVR_Login_V30.restype = LONG

        L.NET_DVR_Logout.argtypes = [LONG]
        L.NET_DVR_Logout.restype = BOOL

        L.NET_DVR_PlayBackByTime_V40.argtypes = [LONG, POINTER(NET_DVR_VOD_PARA)]
        L.NET_DVR_PlayBackByTime_V40.restype = LONG

        L.NET_DVR_PlayBackByTime.argtypes = [LONG, LONG,
                                              POINTER(NET_DVR_TIME),
                                              POINTER(NET_DVR_TIME),
                                              HWND]
        L.NET_DVR_PlayBackByTime.restype = LONG

        L.NET_DVR_RealPlay_V40.argtypes = [LONG, POINTER(NET_DVR_PREVIEWINFO),
                                            PLAYBACK_CALLBACK, c_void_p]
        L.NET_DVR_RealPlay_V40.restype = LONG

        L.NET_DVR_StopRealPlay.argtypes = [LONG]
        L.NET_DVR_StopRealPlay.restype = BOOL

        L.NET_DVR_SetPlayDataCallBack.argtypes = [LONG, PLAYBACK_CALLBACK, c_void_p]
        L.NET_DVR_SetPlayDataCallBack.restype = BOOL

        L.NET_DVR_PlayBackControl.argtypes = [LONG, DWORD, DWORD, c_void_p]
        L.NET_DVR_PlayBackControl.restype = BOOL

        L.NET_DVR_StopPlayBack.argtypes = [LONG]
        L.NET_DVR_StopPlayBack.restype = BOOL

        L.NET_DVR_GetFileByTime_V40.argtypes = [LONG, c_char_p, POINTER(NET_DVR_PLAYCOND)]
        L.NET_DVR_GetFileByTime_V40.restype  = LONG
        L.NET_DVR_GetDownloadPos.argtypes    = [LONG]
        L.NET_DVR_GetDownloadPos.restype     = LONG
        L.NET_DVR_StopGetFile.argtypes       = [LONG]
        L.NET_DVR_StopGetFile.restype        = BOOL
        L.NET_DVR_PlayBackControl.argtypes   = [LONG, DWORD, DWORD, c_void_p]

    def last_error(self):
        code = self.lib.NET_DVR_GetLastError()
        msg_p = self.lib.NET_DVR_GetErrorMsg(byref(LONG(code)))
        msg = msg_p.decode(errors='replace') if msg_p else '?'
        return f'code={code} ({msg})'

    def login(self, host, port=8000, username='admin', password=''):
        info = NET_DVR_DEVICEINFO_V30()
        user_id = self.lib.NET_DVR_Login_V30(
            host.encode('utf-8'), WORD(port),
            username.encode('utf-8'), password.encode('utf-8'),
            byref(info)
        )
        if user_id < 0:
            raise RuntimeError(f'Login to {host}:{port} failed: {self.last_error()}')
        return user_id, info

    def logout(self, user_id):
        if user_id >= 0:
            self.lib.NET_DVR_Logout(user_id)

    def playback_by_time(self, user_id, channel, start_dt, end_dt, data_callback):
        start_t = self._dt_to_nvr(start_dt)
        end_t   = self._dt_to_nvr(end_dt)

        handle = self.lib.NET_DVR_PlayBackByTime(
            user_id, channel, byref(start_t), byref(end_t), None
        )
        if handle < 0:
            raise RuntimeError(f'PlayBackByTime failed: {self.last_error()}')

        if not self.lib.NET_DVR_PlayBackControl(handle, NET_DVR_PLAYBACKSTART, 0, None):
            err = self.last_error()
            self.lib.NET_DVR_StopPlayBack(handle)
            raise RuntimeError(f'PlayBackControl(START) failed: {err}')

        def _trampoline(h, dtype, buf, dlen, _user):
            if dlen > 0 and buf:
                data = ctypes.string_at(buf, dlen)
                try:
                    data_callback(int(dtype), data)
                except Exception as e:
                    print(f'[SDK callback] {e}')

        cb = PLAYBACK_CALLBACK(_trampoline)
        self._cb_refs[handle] = cb

        if not self.lib.NET_DVR_SetPlayDataCallBack(handle, cb, None):
            err = self.last_error()
            self.lib.NET_DVR_StopPlayBack(handle)
            self._cb_refs.pop(handle, None)
            raise RuntimeError(f'SetPlayDataCallBack failed: {err}')

        return handle

    def stop_playback(self, handle):
        if handle >= 0:
            self.lib.NET_DVR_StopPlayBack(handle)

    def download_start(self, user_id, channel, start_dt, end_dt, save_path, sub=False):
        cond = NET_DVR_PLAYCOND()
        cond.dwChannel      = channel
        cond.struStartTime  = self._dt_to_nvr(start_dt)
        cond.struStopTime   = self._dt_to_nvr(end_dt)
        cond.byDrawFrame    = 0
        cond.byStreamType   = 1 if sub else 0
        cond.byCourseFile   = 0
        cond.byDownload     = 1
        cond.byVODFileType  = 0

        h = self.lib.NET_DVR_GetFileByTime_V40(
            user_id, save_path.encode('utf-8'), byref(cond))
        if h < 0:
            raise RuntimeError(f'GetFileByTime failed: {self.last_error()}')

        self.lib.NET_DVR_PlayBackControl(h, NET_DVR_PLAYBACKSTART, 0, None)
        return h

    def download_progress(self, handle):
        if handle < 0:
            return -1
        return int(self.lib.NET_DVR_GetDownloadPos(handle))

    def download_stop(self, handle):
        if handle >= 0:
            try: self.lib.NET_DVR_StopGetFile(handle)
            except: pass

    def realplay(self, user_id, channel, data_callback, sub=False):
        info = NET_DVR_PREVIEWINFO()
        info.lChannel      = channel
        info.dwStreamType  = 1 if sub else 0
        info.dwLinkMode    = 0
        info.hPlayWnd      = None
        info.bBlocked      = 0
        info.byPreviewMode = 0

        def _trampoline(h, dtype, buf, dlen, _user):
            if dlen > 0 and buf:
                data = ctypes.string_at(buf, dlen)
                try:
                    data_callback(int(dtype), data)
                except Exception as e:
                    print(f'[SDK realplay callback] {e}')

        cb = PLAYBACK_CALLBACK(_trampoline)

        handle = self.lib.NET_DVR_RealPlay_V40(user_id, byref(info), cb, None)
        if handle < 0:
            raise RuntimeError(f'RealPlay_V40 failed: {self.last_error()}')

        self._cb_refs[('rp', handle)] = cb
        return handle

    def stop_realplay(self, handle):
        if handle >= 0:
            self.lib.NET_DVR_StopRealPlay(handle)

    @staticmethod
    def _dt_to_nvr(dt):
        t = NET_DVR_TIME()
        t.dwYear, t.dwMonth, t.dwDay = dt.year, dt.month, dt.day
        t.dwHour, t.dwMinute, t.dwSecond = dt.hour, dt.minute, dt.second
        return t

    def cleanup(self):
        try:
            self.lib.NET_DVR_Cleanup()
        except: pass
