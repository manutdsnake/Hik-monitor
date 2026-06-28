# Hik Monitor
Added support for Safire device (playback only, live view doesnt work)
Added support for ONVIF devices

My personal project of desktop application for monitoring Hikvision NVR systems on Linux.  
Supports live view, multi-camera grid layouts, recording playback, and timeline navigation.

If something is broken I most likely won't fix it but you are free to. 
It works for my NVRs on my system (Ubuntu 26.04, AMD GPU 5070XT, Ryzen 5 7600x) and Lenovo L340 (nvidia gpu)


![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- Live view with 1×1, 2×2, 3×3, 4×4, 1+3, and 1+7 grid layouts
- Recording search and playback with timeline scrubbing
- Playback speed control (0.25× to 8×)
- Multi-NVR support
- Native Hikvision SDK decode via PlayCtrl (no ffmpeg required for live view)
- GPU-accelerated playback via VAAPI (if available)
- RTSP fallback for environments without the SDK
- Drag-and-drop cameras into grid cells
- Dark theme UI

---

## Requirements

Copy this to your terminal, it should install automatically: 
in case of any issues, follow install.md
```bash
curl -fsSL https://raw.githubusercontent.com/manutdsnake/Hik-monitor/main/install.sh | bash
```

### System

- Linux (Ubuntu 20.04 / 22.04 or newer recommended), x86_64
- Python 3.8 or newer
- ffmpeg (for playback)

```bash
sudo apt install ffmpeg python3 python3-pip python3.14-venv
```

### Python packages

```bash
pip install PyQt5 requests opencv-python numpy
```

### Hikvision SDK

The application uses the Hikvision HCNetSDK and PlayCtrl SDK libraries.  
These are included in the repository or you can get them from Hikvision.

---

## Folder Structure

Your project folder **must** look exactly like this before running:

```
hik-monitor/
├── hik_monitor2.py
├── hik_sdk.py
├── hik_play.py
├── requirements.txt
└── sdk/
    └── lib/
        ├── libhcnetsdk.so
        ├── libPlayCtrl.so
        ├── libHCCore.so
        ├── libcrypto.so.1.1
        ├── libssl.so.1.1
        ├── libz.so
        ├── libAudioRender.so
        ├── libSuperRender.so
        └── HCNetSDKCom/
            ├── libHCNetSDKComAnalyzeData.so
            ├── libHCNetSDKComNPQ.so
            └── (other HCNetSDKCom .so files)
```

The application looks for the SDK at `~/Desktop/sdk/lib` by default, or at the path set in the `HIKVISION_SDK_PATH` environment variable.  
If you clone into your home folder, the default path works automatically.

---

## Installation

See [INSTALL.md](INSTALL.md) for step-by-step instructions.

---

## Usage

```bash
cd hik-monitor
python3 hik_monitor2.py
```

On first launch, click **+** in the sidebar to add your NVR:

- **IP address** — your NVR's local IP (e.g. `192.168.1.64`)
- **Port** — HTTP port, usually `80`
- **Username / Password** — your NVR login credentials

Click **Test & Save**, then **Connect All**.  
Cameras will appear in the sidebar. Click or drag them into the grid to start streaming.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HIKVISION_SDK_PATH` | `~/Desktop/sdk/lib` | Path to the SDK lib folder |

---

## Tested On

| NVR Model | Firmware | Status |
|---|---|---|
| DS-7604NI-K1/4P | V4.x | ✅ Working |
| DS-7608NI-Q1 | V? | ✅ Working |
Ubuntu 26.04, AMD GPU 5070XT, Ryzen 5 7600x
---

## License

MIT — see [LICENSE](LICENSE).  
Hikvision SDK libraries are owned by Hikvision and remain subject to their own terms.
