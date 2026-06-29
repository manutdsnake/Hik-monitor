# Flatpak packaging

Packages Hik Monitor as a Flatpak for Flathub (which is what KDE **Discover**
shows). App ID: `io.github.manutdsnake.Hik-monitor`.

The proprietary Hikvision SDK is **not** bundled — the app downloads it on
demand at runtime (see `sdk_installer.py`).

## Files

| File | Purpose |
|------|---------|
| `io.github.manutdsnake.Hik-monitor.yaml` | Flatpak manifest |
| `io.github.manutdsnake.Hik-monitor.metainfo.xml` | AppStream metadata (store listing) |
| `io.github.manutdsnake.Hik-monitor.desktop` | Desktop entry |
| `io.github.manutdsnake.Hik-monitor.svg` | Icon (placeholder — replace with a real one) |
| `hik-monitor.sh` | In-sandbox launcher |
| `python3-modules.yaml` | Pinned Python deps (opencv-headless, numpy, requests) — **already generated and committed** |

PyQt5 + Qt5 are **not** pip-installed — they come from the official
`com.riverbankcomputing.PyQt.BaseApp` (set as `base:` in the manifest), which is
the Flathub-recommended way to ship PyQt apps.

## 1. One-time tooling

```bash
sudo apt install flatpak-builder
flatpak install -y flathub org.kde.Platform//5.15-24.08 org.kde.Sdk//5.15-24.08
flatpak install -y flathub com.riverbankcomputing.PyQt.BaseApp//5.15-24.08
flatpak install -y flathub org.freedesktop.Platform.ffmpeg-full//24.08
```

## 2. Regenerating the Python dependency list (only if deps change)

`python3-modules.yaml` is already generated. Regenerate only if you add/upgrade a
dependency (Flathub builds offline, so deps must be pre-pinned with sha256):

```bash
wget https://raw.githubusercontent.com/flatpak/flatpak-builder-tools/master/pip/flatpak-pip-generator.py
pip install requirements-parser PyYAML       # generator's own deps (use a venv)
python3 flatpak-pip-generator.py --runtime=org.kde.Sdk//5.15-24.08 --yaml \
    --output python3-modules \
    --prefer-wheels=opencv-python-headless,numpy \
    opencv-python-headless numpy requests
```

> Notes: do **not** add PyQt5 here (the BaseApp provides it). Use
> opencv-python-**headless** (the GUI build clashes with Qt). `--prefer-wheels`
> is required because opencv/numpy ship only as platform wheels.

## 3. Build & run locally

```bash
cd ..                                  # repo root (manifest source path is '..')
flatpak-builder --user --install --force-clean build-dir \
    flatpak/io.github.manutdsnake.Hik-monitor.yaml
flatpak run io.github.manutdsnake.Hik-monitor
```

## 4. Validate before submitting

```bash
appstreamcli validate flatpak/io.github.manutdsnake.Hik-monitor.metainfo.xml
desktop-file-validate flatpak/io.github.manutdsnake.Hik-monitor.desktop
flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest \
    flatpak/io.github.manutdsnake.Hik-monitor.yaml
```

## 5. Publish to Flathub

1. Add **at least one screenshot** to the metainfo (Flathub requires it).
2. In the manifest, swap the `type: dir` source for a pinned `type: git`
   source (url + tag + full commit sha).
3. Fork <https://github.com/flathub/flathub>, add this manifest, and open a PR
   against the **`new-pr`** branch.
4. After review + merge, Flathub creates `flathub/io.github.manutdsnake.Hik-monitor`
   and builds it. The app then appears on flathub.org and in Discover.

See <https://docs.flathub.org/docs/for-app-authors/submission> for details.
