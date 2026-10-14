#!/usr/bin/env python3
"""
plasma-panel-sync — keep matching Plasma 6 panels in sync across screens.

Plasma has no concept of "the same panel on every monitor": each screen gets an
independent containment, and they drift the moment you pin an app on one of them.
This mirrors them, with a per-property policy so screen-specific settings survive.

Model
-----
  groups          panels are grouped by edge (top / bottom / left / right)
  master          "last edited wins" - the panel whose state changed since the
                  last snapshot is treated as the source of truth for its group
  syncs           widget set, widget order, per-widget configuration
  never syncs     screen, hiding (auto-hide), height  -- see PER_PANEL below

The "never syncs" list is the important part. An OLED wants auto-hide to avoid
burn-in while cheap IPS panels don't care, and a fractionally-scaled display
wants a different panel thickness. A blanket copy would flatten both.

Everything is applied live through plasmashell's scripting D-Bus interface, so
no plasmashell restart and no config-file surgery.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

APPLETSRC = Path.home() / ".config" / "plasma-org.kde.plasma.desktop-appletsrc"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "plasma-panel-sync"
SNAPSHOT = STATE_DIR / "snapshot.json"
USERCONF = Path.home() / ".config" / "plasma-panel-sync.json"

# Panel properties that are deliberately allowed to differ between screens.
PER_PANEL = {"id", "screen", "hiding", "height"}

# Applet config groups that hold transient UI state rather than settings.
SKIP_GROUPS = {"ConfigDialog"}
SKIP_KEYS = {"migrated", "popupHeight", "popupWidth"}

LOCATIONS = {0: "floating", 1: "desktop", 2: "fullscreen",
             3: "top", 4: "bottom", 5: "left", 6: "right"}


# ---------------------------------------------------------------- plasmashell

def _qdbus() -> str:
    for cand in ("qdbus6", "qdbus-qt6", "qdbus"):
        path = subprocess.run(["which", cand], capture_output=True, text=True)
        if path.returncode == 0:
            return path.stdout.strip()
    raise RuntimeError("no qdbus binary found (need qdbus6)")


def evaluate(js: str) -> str:
    """Run a script inside plasmashell and return whatever it print()s."""
    proc = subprocess.run(
        [_qdbus(), "org.kde.plasmashell", "/PlasmaShell",
         "org.kde.PlasmaShell.evaluateScript", js],
        capture_output=True, text=True,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"plasmashell rejected the script: {proc.stderr.strip() or out}")
    # plasmashell reports script errors in-band rather than via exit status
    if out.startswith("Error:") or "ReferenceError" in out or "SyntaxError" in out:
        raise RuntimeError(f"script error: {out}")
    return out


JS_PANELS = """
var out = [];
panels().forEach(function (p) {
    var ws = [];
    p.widgets().forEach(function (w) { ws.push(w.type); });
    out.push({ id: p.id, screen: p.screen, location: p.location,
               height: p.height, hiding: p.hiding, alignment: p.alignment,
               lengthMode: p.lengthMode, widgets: ws });
});
print(JSON.stringify(out));
"""


def live_panels() -> list[dict]:
    return json.loads(evaluate(JS_PANELS))


# ------------------------------------------------------------------ appletsrc

def parse_kconfig(path: Path) -> dict[tuple, dict]:
    """Parse a KConfig file into {group_tuple: {key: value}}."""
    data: dict[tuple, dict] = {}
    current: tuple | None = None
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        if line.startswith("["):
            current = tuple(re.findall(r"\[([^\]]*)\]", line))
            data.setdefault(current, {})
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            data[current][key] = value
    return data


def applet_config(cfg: dict, cid: str, aid: str) -> dict[str, dict]:
    """Collect an applet's settings groups, minus transient state.

    Ordinary applets keep settings under [Applets][N][Configuration][...].
    Container applets (the system tray) are containments themselves, so theirs
    sit directly under [Applets][N][General] - that is where the tray's
    hidden/shown icon lists live. The scripting API addresses both the same way
    (currentConfigGroup = ["General"]), so they share one namespace here.

    Empty values are dropped: Plasma deletes a list key when it is emptied, and
    we can only blank keys, never delete them, so "" and absent must compare equal.
    """
    base = ("Containments", cid, "Applets", aid)
    out: dict[str, dict] = {}
    for group, kv in cfg.items():
        if group[:4] != base or len(group) < 5:
            continue
        if group[4] == "Configuration":
            parts = group[5:]
        elif group[4] == "Applets":
            continue  # nested applets are tracked by nested_plugins()
        else:
            parts = group[4:]
        name = "/".join(parts)
        if not name or name in SKIP_GROUPS:
            continue
        cleaned = {k: v for k, v in kv.items() if k not in SKIP_KEYS and v != ""}
        if cleaned:
            out.setdefault(name, {}).update(cleaned)
    return out


def clearable(value: str) -> bool:
    """Whether blanking a key is a safe stand-in for deleting it.

    A blank list or string reads the same as an unset one. A blank boolean or
    number does not (it may not be the default), so those are left alone.
    """
    return value not in ("true", "false") and not re.fullmatch(r"-?\d+(\.\d+)?", value)


def nested_plugins(cfg: dict, cid: str, aid: str) -> list[str]:
    """Plugins nested inside a container applet, e.g. the system tray."""
    prefix = ("Containments", cid, "Applets", aid, "Applets")
    found = []
    for group, kv in cfg.items():
        if group[:5] == prefix and len(group) == 6 and "plugin" in kv:
            found.append(kv["plugin"])
    return sorted(found)


def panel_state(cfg: dict, cid: str) -> dict:
    """The syncable state of one panel containment."""
    order = cfg.get(("Containments", cid, "General"), {}).get("AppletOrder", "")
    aids = [a for a in order.split(";") if a]
    applets, ids = [], []
    for aid in aids:
        base = cfg.get(("Containments", cid, "Applets", aid), {})
        plugin = base.get("plugin")
        if not plugin:
            continue
        applets.append({
            "plugin": plugin,
            "config": applet_config(cfg, cid, aid),
            "nested": nested_plugins(cfg, cid, aid),
        })
        ids.append(int(aid))
    # ids are per-panel, so they stay out of the comparison (see signature())
    return {"applets": applets, "ids": ids}


# --------------------------------------------------------------------- policy

def load_userconf() -> dict:
    if USERCONF.exists():
        try:
            return json.loads(USERCONF.read_text())
        except json.JSONDecodeError as exc:
            print(f"warning: {USERCONF} is not valid JSON ({exc}); ignoring", file=sys.stderr)
    return {}


def build_groups(panels: list[dict], only: str | None) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for p in panels:
        name = LOCATIONS.get(p["location"], str(p["location"]))
        groups.setdefault(name, []).append(p)
    if only:
        groups = {k: v for k, v in groups.items() if k == only}
    # a group of one has nothing to sync
    return {k: v for k, v in groups.items() if len(v) > 1}


def signature(state: dict) -> str:
    return json.dumps(state["applets"], sort_keys=True)


# ----------------------------------------------------------------------- push

def js_string(value: str) -> str:
    return json.dumps(value)


def stale_keys(master: dict, target: dict | None) -> list[tuple[int, str, str, str]]:
    """Keys a target applet has that its master counterpart does not.

    Returns (applet index, group, key, value). Only meaningful when the widget
    lists match; after a rebuild the target's old config is gone anyway.
    """
    if not target:
        return []
    stale = []
    for index, (m, t) in enumerate(zip(master["applets"], target["applets"])):
        if m["plugin"] != t["plugin"]:
            continue
        for group, kv in t["config"].items():
            for key, value in kv.items():
                if key not in m["config"].get(group, {}):
                    stale.append((index, group, key, value))
    return stale


def build_push_js(target_id: int, master: dict, rebuild: bool,
                  target_ids: list[int] = (),
                  clear: list[tuple[int, str, str, str]] = ()) -> str:
    # p.widgets() lists applets in creation order, NOT on-screen order, so an
    # untouched panel's widgets are looked up by applet id. Only freshly added
    # widgets are in a known order (the order we added them).
    lines = [f"var p = panelById({target_id});"]
    if rebuild:
        lines.append("p.widgets().forEach(function (w) { w.remove(); });")
        for applet in master["applets"]:
            lines.append(f"p.addWidget({js_string(applet['plugin'])});")
        lines.append("var ws = p.widgets();")
    else:
        lines.append("var byId = {};")
        lines.append("p.widgets().forEach(function (w) { byId[w.id] = w; });")
        lines.append(f"var ws = {json.dumps(list(target_ids))}.map(function (i) {{ return byId[i]; }});")
    for index, applet in enumerate(master["applets"]):
        writes = {g: dict(kv) for g, kv in applet["config"].items()}
        for c_index, group, key, _ in clear:
            if c_index == index:
                writes.setdefault(group, {})[key] = ""
        if not writes:
            continue
        lines.append(f"if (ws[{index}] && ws[{index}].type === {js_string(applet['plugin'])}) {{")
        for group, kv in writes.items():
            parts = ", ".join(js_string(g) for g in group.split("/"))
            lines.append(f"  ws[{index}].currentConfigGroup = [{parts}];")
            for key, value in kv.items():
                lines.append(f"  ws[{index}].writeConfig({js_string(key)}, {js_string(value)});")
        lines.append(f"  ws[{index}].reloadConfig();")
        lines.append("}")
    lines.append('print("ok");')
    return "\n".join(lines)


def push(master_state: dict, master_id: int, targets: list[dict],
         states: dict, dry_run: bool) -> list[str]:
    actions = []
    master_plugins = [a["plugin"] for a in master_state["applets"]]
    for target in targets:
        target_state = states[target["id"]]
        rebuild = [a["plugin"] for a in target_state["applets"]] != master_plugins
        verb = "rebuild + config" if rebuild else "config only"
        actions.append(f"panel {target['id']} (screen {target['screen']}): {verb}")
        clear = []
        if not rebuild:
            for item in stale_keys(master_state, target_state):
                index, group, key, value = item
                widget = master_plugins[index].split(".")[-1]
                if clearable(value):
                    clear.append(item)
                    actions.append(f"  clear {widget} {group}/{key}")
                else:
                    actions.append(f"  can't reset {widget} {group}/{key}={value} "
                                   f"live - toggle it once on panel {target['id']}")
        if dry_run:
            continue
        evaluate(build_push_js(target["id"], master_state, rebuild,
                               target_state["ids"], clear))
    return actions


# ------------------------------------------------------------------- commands

def gather() -> tuple[list[dict], dict]:
    if not APPLETSRC.exists():
        raise RuntimeError(f"missing {APPLETSRC}")
    return live_panels(), parse_kconfig(APPLETSRC)


def cmd_status(args) -> int:
    panels, cfg = gather()
    groups = build_groups(panels, args.group)
    if not groups:
        print("No panel groups with more than one member — nothing to sync.")
        return 0
    for name, members in sorted(groups.items()):
        print(f"\n{name} panels ({len(members)}):")
        sigs = {}
        for p in members:
            state = panel_state(cfg, str(p["id"]))
            sig = signature(state)
            sigs.setdefault(sig, []).append(p)
            widgets = ", ".join(a["plugin"].split(".")[-1] for a in state["applets"])
            print(f"  panel {p['id']:>3}  screen {p['screen']}  "
                  f"h={p['height']:<4} hiding={p['hiding']:<10} [{widgets}]")
        if len(sigs) == 1:
            print("  -> in sync")
        else:
            print(f"  -> DRIFTED: {len(sigs)} distinct states")
    return 0


def cmd_push(args) -> int:
    panels, cfg = gather()
    groups = build_groups(panels, args.group)
    by_id = {p["id"]: p for p in panels}
    if args.master is not None and args.master not in by_id:
        print(f"error: no panel with id {args.master}", file=sys.stderr)
        return 1

    snapshot = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    new_snapshot = {}
    did_work = False
    reported = False

    for name, members in sorted(groups.items()):
        states = {p["id"]: panel_state(cfg, str(p["id"])) for p in members}
        for pid, state in states.items():
            new_snapshot[str(pid)] = signature(state)

        if args.master is not None:
            if args.master not in states:
                continue
            master_id = args.master
            reason = "explicit --master"
        else:
            changed = [pid for pid, st in states.items()
                       if snapshot.get(str(pid)) != signature(st)]
            distinct = {signature(st) for st in states.values()}
            if len(distinct) == 1:
                continue  # already in sync
            if not changed:
                print(f"{name}: drifted, but nothing changed since the last "
                      f"snapshot — pass --master <id> to pick a winner")
                reported = True
                continue
            if len(changed) > 1:
                print(f"{name}: {len(changed)} panels changed at once "
                      f"({changed}); pass --master <id> to disambiguate")
                reported = True
                continue
            master_id = changed[0]
            reason = "last edited"

        targets = [p for p in members if p["id"] != master_id]
        if not targets:
            continue
        print(f"{name}: master = panel {master_id} ({reason})")
        for action in push(states[master_id], master_id, targets, states, args.dry_run):
            print(f"  {'would ' if args.dry_run else ''}{action}")
        did_work = True

    if not did_work and not reported:
        print("Nothing to do — all groups in sync.")

    if not args.dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # re-read after pushing so the snapshot reflects the settled state
        time.sleep(1.5)
        _, cfg_after = gather()
        settled = {}
        for members in groups.values():
            for p in members:
                settled[str(p["id"])] = signature(panel_state(cfg_after, str(p["id"])))
        SNAPSHOT.write_text(json.dumps({**new_snapshot, **settled}, indent=2))
    return 0


def cmd_snapshot(args) -> int:
    panels, cfg = gather()
    groups = build_groups(panels, args.group)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    snap = {}
    for members in groups.values():
        for p in members:
            snap[str(p["id"])] = signature(panel_state(cfg, str(p["id"])))
    SNAPSHOT.write_text(json.dumps(snap, indent=2))
    print(f"Baseline written for {len(snap)} panels -> {SNAPSHOT}")
    return 0


def cmd_watch(args) -> int:
    """Poll the config file's mtime; on quiescence, diff and push."""
    print(f"Watching {APPLETSRC} (debounce {args.debounce}s). Ctrl-C to stop.")
    if not SNAPSHOT.exists():
        cmd_snapshot(args)
    last_mtime = APPLETSRC.stat().st_mtime
    suppress_until = 0.0
    while True:
        time.sleep(1.0)
        try:
            mtime = APPLETSRC.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime == last_mtime:
            continue
        # wait for plasmashell to stop writing before acting
        while True:
            time.sleep(args.debounce)
            newer = APPLETSRC.stat().st_mtime
            if newer == mtime:
                break
            mtime = newer
        last_mtime = mtime
        if time.time() < suppress_until:
            continue  # this write was our own push echoing back
        try:
            cmd_push(args)
        except Exception as exc:  # keep the watcher alive
            print(f"sync failed: {exc}", file=sys.stderr)
        suppress_until = time.time() + args.debounce * 3
        last_mtime = APPLETSRC.stat().st_mtime


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="panelsync", description="Keep matching Plasma 6 panels in sync across screens.")
    parser.add_argument("--group", help="limit to one group (top/bottom/left/right)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show panel groups and any drift")
    p_status.set_defaults(func=cmd_status)

    p_push = sub.add_parser("push", help="sync each group from its master")
    p_push.add_argument("--master", type=int, help="force this panel id to be the master")
    p_push.add_argument("--dry-run", action="store_true", help="print actions without applying")
    p_push.set_defaults(func=cmd_push)

    p_snap = sub.add_parser("snapshot", help="record current state as the baseline")
    p_snap.set_defaults(func=cmd_snapshot)

    p_watch = sub.add_parser("watch", help="auto-sync when a panel changes")
    p_watch.add_argument("--debounce", type=float, default=2.0)
    p_watch.add_argument("--master", type=int, default=None)
    p_watch.add_argument("--dry-run", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
