# Flathub submission

These two files are what goes into the **Flathub** pull request — nothing else.
Everything else (app code, metainfo, desktop, icon, screenshots) is pulled from
the `Hik-monitor` git repo at the pinned commit by the manifest.

```
io.github.manutdsnake.Hik-monitor.yaml   # the manifest (git source)
python3-modules.yaml                      # pinned Python deps (the include)
```

## Steps

1. **Fork** <https://github.com/flathub/flathub>.

2. In your fork, create a branch named after the app ID and add **both files
   above** to the repo root:
   ```
   io.github.manutdsnake.Hik-monitor.yaml
   python3-modules.yaml
   ```

3. Open a **pull request** against the **`new-pr`** branch of `flathub/flathub`
   (not `master`). Title it with the app ID.

4. Flathub's bot builds it and runs the linter. The screenshot/icon mirroring
   errors you saw locally are resolved automatically here — Flathub downloads
   your screenshots from GitHub and re-hosts them on its own CDN during the build.

5. Address any review feedback. Once merged, Flathub creates
   `flathub/io.github.manutdsnake.Hik-monitor`, builds it, and the app goes live
   on flathub.org and in Discover. You become the maintainer.

## Updating the app later

When you push new commits to `Hik-monitor`, update `commit:` (and `tag:` if you
use one) in the manifest **in your Flathub app repo** and push — that triggers a
rebuild. Optionally add `x-checker-data` so Flathub auto-proposes updates.

## Tip: tag the release

Flathub prefers a tag for releases. Create one and reference it in the manifest:

```bash
git tag v1.0.0 c70a6527cc85efe212082d76edcea4406bff46b0
git push origin v1.0.0
```
Then uncomment the `tag: v1.0.0` line in the manifest.
