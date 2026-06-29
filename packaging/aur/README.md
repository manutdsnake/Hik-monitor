# Publishing to the AUR

One-time, needs an [AUR account](https://aur.archlinux.org) with an SSH key added.

1. Create the v1.0.0 tag and release on GitHub first (so the source tarball exists),
   then pin the checksum:
   ```bash
   cd packaging/aur
   updpkgsums                      # fills sha256sums from the real tarball
   makepkg --printsrcinfo > .SRCINFO
   namcap PKGBUILD                 # optional lint
   ```
2. Test the build locally:
   ```bash
   makepkg -si
   ```
3. Push to the AUR:
   ```bash
   git clone ssh://aur@aur.archlinux.org/hik-monitor.git aur-hik-monitor
   cp PKGBUILD .SRCINFO aur-hik-monitor/
   cd aur-hik-monitor && git add PKGBUILD .SRCINFO
   git commit -m "Initial import: hik-monitor 1.0.0"
   git push
   ```

After this, Arch/Manjaro users install with `yay -S hik-monitor`.
