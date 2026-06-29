#!/bin/sh
# Flatpak entry point. The app's modules (hik_sdk, hik_play, onvif_client,
# sdk_installer) are imported relative to the script dir, so run from there.
cd /app/share/hik-monitor || exit 1
exec python3 hik_monitor2.py "$@"
