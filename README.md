# plasma-panel-sync

Keep matching Plasma 6 panels in sync across multiple screens.

Plasma gives every monitor its own independent panel containment. There is no
"same panel on every screen" concept, so the moment you pin an app to one dock
the others fall behind. This mirrors them.

## Why not just copy the panel

Because some settings are legitimately per-screen. On this machine:

- the middle monitor is an OLED, so its panels auto-hide to avoid burn-in
- the left and right monitors are IPS/TN and keep their top bars visible
- the OLED runs at 1.5x scale, so a shared pixel thickness reads differently

A blanket duplicate would flatten all of that. So the tool syncs *content* and
leaves *placement and behaviour* alone:

| Synced | Never synced |
| --- | --- |
| widget set | `screen` |
| widget order | `hiding` (auto-hide) |
| per-widget configuration | `height` |

The never-sync list lives in `PER_PANEL` at the top of `panelsync.py`.

## How it decides who wins

"Last edited wins." The tool keeps a snapshot of each panel's state. When a
group has drifted, the panel whose state changed since the snapshot becomes the
master and is pushed to its siblings.

If it can't tell — no snapshot yet, or several panels changed at once — it
refuses to guess and asks you to pass `--master <id>`. It never picks arbitrarily.

## Installing

On Arch, build and install the package from the `packaging/` folder:

```bash
cd packaging
makepkg -si
systemctl --user enable --now plasma-panel-sync.service
```

The PKGBUILD builds from the repository's committed state, so commit before
rebuilding. Watch what the background sync does with:

```bash
journalctl --user -u plasma-panel-sync -f
```

## Usage

```bash
plasma-panel-sync status                    # show groups and any drift
plasma-panel-sync push --dry-run            # what would happen
plasma-panel-sync push --master 99          # force a winner (first reconcile)
plasma-panel-sync snapshot                  # record current state as baseline
plasma-panel-sync watch                     # auto-sync on change
plasma-panel-sync --group bottom status     # limit to one edge
```

Panels are grouped by screen edge, so `bottom` is the docks and `top` is the
menu bars. Groups with only one member are ignored. Panel ids come from
`status`.

The watcher polls the config file's mtime, waits for writes to settle, then
diffs and pushes. After each push it suppresses itself briefly so its own
writes don't retrigger a sync.

## How it applies changes

Through plasmashell's scripting interface:

```bash
qdbus6 org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript '...'
```

This means changes apply live — no plasmashell restart, no editing
`plasma-org.kde.plasma.desktop-appletsrc` behind plasmashell's back (which gets
clobbered, since plasmashell holds that file in memory and rewrites it).

The config file is still *read*, because the scripting API can only read config
keys by name and gives no way to enumerate them. So: discover keys from the
file, apply them over D-Bus.

When the widget sets already match, only configuration is written and nothing
visibly changes. A differing widget set triggers a rebuild of the target panel,
which does flicker briefly.

## Known limits

- **System tray.** The tray is a container, and its settings live under the
  applet itself rather than under `[Configuration]`. Its `hiddenItems` /
  `shownItems` / `disabledStatusNotifiers` lists sync. Settings of the widgets
  *inside* the tray (volume, network, ...) are not walked.
- **Removing a setting.** The scripting API can write a key but never delete
  it. When the master has dropped a key (Plasma does this when a list is
  emptied or a value goes back to its default), lists and text are blanked on
  the other panels, which reads the same. On/off and number settings are left
  alone and reported, since a blank one may not mean "default": toggle those
  once by hand on the panel named in the log.
- **Rebuild ordering.** Rebuilds remove and re-add widgets to force order, since
  the scripting API offers no direct reorder. Untouched panels are addressed
  by applet id, because `panel.widgets()` lists widgets in creation order, not
  on-screen order.
- **Config-file reads.** If plasmashell hasn't flushed a change yet, the watcher
  may act on slightly stale keys. The debounce is there to make that unlikely.

## Reverting

Config backups from the initial setup pass:

```
~/.config/plasma-theme-backup-20260808/
```
