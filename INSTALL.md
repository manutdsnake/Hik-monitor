# Installation Guide

This guide walks you through installing and running Hik Monitor on Ubuntu/Debian Linux.

---

## Step 1 — Install system dependencies


Option A
Open a terminal and run, everything should install automatically:
```bash
curl -fsSL https://raw.githubusercontent.com/manutdsnake/Hik-monitor/main/install.sh | bash
```

ELSE

```bash
sudo apt update
sudo apt install python3 python3-pip ffmpeg git python3.14-venv
```

---

## Step 2 — Clone the repository

```bash
cd Desktop
git clone https://github.com/manutdsnake/hik-monitor.git
cd hik-monitor
```


---

## Step 3 — Install Python packages

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

If you get a permissions error, use:

```bash
pip install --user -r requirements.txt
```

---

## Step 4 — Set up the SDK folder

The repository already includes the Hikvision SDK libraries in the `sdk/` folder.  
You just need to make sure the folder structure is correct.

After cloning, verify it looks like this:

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
            └── (several .so files)
```


```bash
sudo apt install git-lfs
git lfs install
git lfs pull
```

---

## Step 5 — Set the SDK path

The app expects the SDK at `~/Desktop/sdk/lib` by default.

**Option A — Use the default path (easiest)**

Copy the sdk folder to your Desktop:

```bash
cp -r sdk ~/Desktop/sdk
```

**Option B — Use the project folder directly**

Set an environment variable so the app finds the SDK where you cloned it:

```bash
export HIKVISION_SDK_PATH="$(pwd)/sdk/lib"
```

To make this permanent, add it to your `~/.bashrc`:

```bash
echo 'export HIKVISION_SDK_PATH="$HOME/hik-monitor/sdk/lib"' >> ~/.bashrc
source ~/.bashrc
```

---

## Step 6 — Run the application

```bash
python3 hik_monitor2.py
```

A window will open. In the left sidebar:

1. Click **+** next to DEVICES
2. Enter your NVR details:
   - **Device name** — any label you want (e.g. `Home NVR`)
   - **IP address** — your NVR's IP on the local network (e.g. `192.168.1.64`)
   - **Port** — HTTP port, usually `80`
   - **Username / Password** — your NVR admin credentials
3. Click **Test & Save**
4. Click **Connect All**

Cameras will appear under CAMERAS. Click one to view it, or drag it into the grid.

---

## Troubleshooting

**"libhcnetsdk.so: cannot open shared object file"**

The SDK path is wrong. Double-check Step 5 and make sure `libhcnetsdk.so` exists at the path you set.

**"ffmpeg not found"**

Install ffmpeg:
```bash
sudo apt install ffmpeg
```

**"Cannot connect to 192.168.x.x:80"**

- Make sure your PC and NVR are on the same network
- Try opening `http://192.168.x.x` in a browser to confirm the NVR is reachable
- Check that port 80 is not blocked by a firewall

**"Wrong username or password"**

Use the same credentials you use to log in to the NVR web interface.

**Stream shows but video is green or garbled**

This can happen with VAAPI GPU decode on some hardware. It will fall back automatically — if it doesn't, file an issue on GitHub with your GPU model.

**App crashes on startup with a Python error**

Make sure all Python packages are installed:
```bash
pip install PyQt5 requests opencv-python numpy
```

---

## Uninstall

Simply delete the folder:

```bash
cd ..
rm -rf hik-monitor
```

If you set `HIKVISION_SDK_PATH` in `~/.bashrc`, remove that line too.
