import atexit
import hashlib
import os
import sys
import time
import uuid

import bpy
from bpy.props import StringProperty

ADDON_VERSION = "0.3.0"

IDLE_THRESHOLD = 60.0    # seconds with no mouse click before the timer pauses
TICK_INTERVAL = 1.0      # seconds between accounting ticks
SAVE_INTERVAL = 60.0     # seconds between autosaves to the log file

# Module-level state - not stored as bpy properties since it needs to keep
# ticking via a modal operator/timer regardless of which object/scene is active.
#
# Each time a file is loaded a new session_id is generated. Every Blender
# instance only ever writes its own session's line in the log file, so
# opening several files (or the same file) in several Blender instances at
# once does not clobber another instance's tracked time - each instance's
# entry is independent and the total is the sum of all entries.
_state = {
    "session_id": None,
    "log_filepath": None,     # the .blend path the current session is logged against
    "session_start": 0.0,
    "session_seconds": 0.0,
    "grand_total": 0.0,       # session_seconds + every other session's seconds, from the log file
    "last_tick": 0.0,
    "last_click": 0.0,
    "last_save": 0.0,
    "paused": True,
    "running": False,
}


def get_prefs():
    addon = bpy.context.preferences.addons.get(__name__)
    return addon.preferences if addon else None


def get_log_dir(filepath):
    prefs = get_prefs()
    custom = prefs.log_path.strip() if prefs else ""
    if custom:
        return bpy.path.abspath(custom)
    return os.path.dirname(filepath)


def get_log_path(filepath):
    prefs = get_prefs()
    custom = prefs.log_path.strip() if prefs else ""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    if custom:
        # Several projects may share one custom log folder. The parent folder
        # name alone isn't unique enough (e.g. ProjectA/Shots/Shot010 and
        # ProjectB/Shots/Shot010), so tag the name with a short hash of the
        # full source directory to guarantee no two different files collide.
        parent = os.path.basename(os.path.dirname(filepath)).strip()
        src_dir = os.path.normcase(os.path.abspath(os.path.dirname(filepath)))
        tag = hashlib.sha1(src_dir.encode("utf-8")).hexdigest()[:8]
        name = "%s_%s_%s_time.txt" % (parent, stem, tag) if parent else "%s_%s_time.txt" % (stem, tag)
    else:
        name = "%s_time.txt" % stem
    return os.path.join(get_log_dir(filepath), name)


def format_hms(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s)


def parse_sessions(path):
    """Returns {session_id: {"sid", "start", "end", "seconds"}}."""
    sessions = {}
    if not os.path.isfile(path):
        return sessions
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("SESSION "):
                    continue
                fields = {}
                for part in line.strip().split()[1:]:
                    if "=" in part:
                        k, v = part.split("=", 1)
                        fields[k] = v
                try:
                    sessions[fields["sid"]] = {
                        "sid": fields["sid"],
                        "start": float(fields["start"]),
                        "end": float(fields["end"]),
                        "seconds": float(fields["seconds"]),
                    }
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return sessions


def write_log(path, sessions, filepath):
    ordered = sorted(sessions.values(), key=lambda s: s["start"])
    total = sum(s["seconds"] for s in ordered)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# BB Project Timer log - machine-readable lines below, do not edit by hand\n")
            f.write("TOTAL=%.1f\n" % total)
            for s in ordered:
                f.write("SESSION sid=%s start=%.1f end=%.1f seconds=%.1f\n" % (
                    s["sid"], s["start"], s["end"], s["seconds"]))
            f.write("\n")
            f.write("Total time on %s: %s\n\n" % (os.path.basename(filepath), format_hms(total)))
            f.write("Sessions:\n")
            for s in ordered:
                start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["start"]))
                end_str = time.strftime("%H:%M", time.localtime(s["end"]))
                f.write("  %s - %s   %s\n" % (start_str, end_str, format_hms(s["seconds"])))
    except OSError:
        pass
    return total


def _find_kitsu_module():
    """The loaded BB_Kitsu-Pipeline module, if any.

    Its Blender package is installed as an extension, so its module name is
    namespaced (e.g. ``bl_ext.user_default.BB_pipeline``) rather than the
    bare ``BB_pipeline`` - match on the suffix instead of hardcoding a key
    into ``bpy.context.preferences.addons``.
    """
    for name, mod in list(sys.modules.items()):
        if (name == "BB_pipeline" or name.endswith(".BB_pipeline")) and hasattr(mod, "session"):
            return mod
    return None


def _get_kitsu_client():
    """The pipeline add-on's already-authenticated Kitsu client, or None.

    This is not a published API - it's reaching into another add-on's
    internal session object. Any failure here (module missing, not logged
    in, or the pipeline add-on restructured this on an update) just means
    "nothing to sync", handled by the try/except in sync_kitsu().
    """
    mod = _find_kitsu_module()
    if mod is None:
        return None
    client = mod.session.state.client
    if not client or not client.logged_in:
        return None
    return client


def _get_kitsu_task_id():
    """The Kitsu task this file is stamped for, read from the scene.

    BB_Kitsu-Pipeline stores this as a plain custom property on the Scene
    (``scene["BB_pipeline"]``), independent of whether that add-on's own
    Python module is currently loaded - the most stable way to read it.
    """
    scene = bpy.context.scene
    ctx = scene.get("BB_pipeline") if scene else None
    return ctx.get("task_id") if ctx else None


def _today_seconds(sessions):
    today = time.strftime("%Y-%m-%d")
    total = 0.0
    for s in sessions.values():
        if time.strftime("%Y-%m-%d", time.localtime(s["start"])) == today:
            total += s["seconds"]
    return total


def sync_kitsu(sessions):
    """Push today's tracked total for the current task to Kitsu.

    Best-effort and silent: no BB_Kitsu-Pipeline, not logged in, or no task
    assigned to this file are all just "nothing to do", not errors - and
    any Kitsu/network failure is swallowed so this integration can never
    break the timer itself. Kitsu's time-spent endpoint *replaces* the
    day's logged duration rather than adding to it, so this always sends
    the full total tracked for today rather than this session's delta -
    which also means a manual edit made directly in Kitsu for that task
    and day will be overwritten on the next sync.
    """
    try:
        client = _get_kitsu_client()
        if client is None:
            return
        task_id = _get_kitsu_task_id()
        if not task_id:
            return
        person = client.user or {}
        person_id = person.get("id")
        if not person_id:
            return

        minutes = int(round(_today_seconds(sessions) / 60.0))
        date_str = time.strftime("%Y-%m-%d")
        client._request(
            "POST",
            "actions/tasks/%s/time-spents/%s/persons/%s" % (task_id, date_str, person_id),
            json={"duration": minutes},
        )
    except Exception:
        pass


def sync_log(filepath):
    if not filepath or _state["session_id"] is None:
        return
    path = get_log_path(filepath)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass

    sessions = parse_sessions(path)
    sessions[_state["session_id"]] = {
        "sid": _state["session_id"],
        "start": _state["session_start"],
        "end": time.time(),
        "seconds": _state["session_seconds"],
    }
    _state["grand_total"] = write_log(path, sessions, filepath)
    sync_kitsu(sessions)


def flush_current():
    sync_log(_state["log_filepath"])


def reset_state_for_current_file():
    filepath = bpy.data.filepath
    now = time.time()

    _state["session_id"] = uuid.uuid4().hex[:8]
    _state["log_filepath"] = filepath
    _state["session_start"] = now
    _state["session_seconds"] = 0.0
    _state["last_tick"] = now
    _state["last_click"] = now
    _state["last_save"] = now
    _state["paused"] = False

    if filepath:
        sessions = parse_sessions(get_log_path(filepath))
        _state["grand_total"] = sum(s["seconds"] for s in sessions.values())
    else:
        _state["grand_total"] = 0.0


def tick():
    now = time.time()
    dt = now - _state["last_tick"]
    _state["last_tick"] = now

    idle = now - _state["last_click"]
    _state["paused"] = idle > IDLE_THRESHOLD

    if not _state["paused"]:
        _state["session_seconds"] += dt
        _state["grand_total"] += dt

    if now - _state["last_save"] >= SAVE_INTERVAL:
        _state["last_save"] = now
        flush_current()


def register_click():
    _state["last_click"] = time.time()


class BBPT_OT_modal_timer(bpy.types.Operator):
    """Runs for the lifetime of the Blender session, tracking mouse clicks
    and accumulating active time. Reinstated automatically after every file
    load - never exposed as a manual start/stop control."""
    bl_idname = "bb_project_timer.modal_timer"
    bl_label = "BB Project Timer (internal)"
    bl_options = {'INTERNAL'}

    _wm_timer = None

    def modal(self, context, event):
        if not _state["running"]:
            return {'FINISHED'}

        if event.type == 'TIMER':
            tick()
            _redraw_timer_panels(context)
        elif event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'} and event.value == 'PRESS':
            register_click()

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        self._wm_timer = context.window_manager.event_timer_add(TICK_INTERVAL, window=context.window)
        context.window_manager.modal_handler_add(self)
        _state["running"] = True
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        if self._wm_timer is not None:
            context.window_manager.event_timer_remove(self._wm_timer)
            self._wm_timer = None
        _state["running"] = False


def _redraw_timer_panels(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def ensure_modal_running():
    if _state["running"]:
        return
    try:
        bpy.ops.bb_project_timer.modal_timer('INVOKE_DEFAULT')
    except RuntimeError:
        pass


def _start_modal_deferred():
    reset_state_for_current_file()
    ensure_modal_running()
    return None  # one-shot


def on_load_pre(dummy1, dummy2):
    flush_current()


def on_load_post(dummy1, dummy2):
    # bpy.ops is not always safe to call directly from a handler, so defer by one tick.
    bpy.app.timers.register(_start_modal_deferred, first_interval=0.0)


def on_save_post(dummy1, dummy2):
    # Covers "File > Save" on a file that had no path yet, and "Save As" -
    # both change bpy.data.filepath without going through load_post. The
    # time already accumulated this session carries over to the new path.
    filepath = bpy.data.filepath
    if filepath and filepath != _state["log_filepath"]:
        _state["log_filepath"] = filepath
        sync_log(filepath)


class BBPT_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    log_path: StringProperty(
        name="Log Folder",
        description=(
            "Folder where the time-tracking log is saved. "
            "Leave empty to save it next to the .blend file"
        ),
        subtype='DIR_PATH',
        default="",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "log_path")
        layout.label(text="Leave empty to save the log next to each .blend file", icon='INFO')


class BBPT_PT_panel(bpy.types.Panel):
    bl_label = "Project Timer"
    bl_idname = "BBPT_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Tool"

    def draw(self, context):
        layout = self.layout
        filepath = bpy.data.filepath

        if not filepath:
            layout.label(text="Save the file to start tracking", icon='ERROR')
            return

        status = "Paused (idle)" if _state["paused"] else "Tracking"
        icon = 'PAUSE' if _state["paused"] else 'REC'
        layout.label(text=status, icon=icon)

        col = layout.column()
        col.label(text="This session:")
        col.label(text=format_hms(_state["session_seconds"]))
        col.separator()
        col.label(text="Total on this file:")
        col.label(text=format_hms(_state["grand_total"]))

        layout.separator()
        layout.label(text=os.path.basename(get_log_path(filepath)), icon='FILE_TEXT')

        task_id = _get_kitsu_task_id()
        if task_id:
            if _get_kitsu_client() is not None:
                layout.label(text="Syncing to Kitsu", icon='CHECKMARK')
            else:
                layout.label(text="Kitsu task set, not logged in", icon='ERROR')


classes = (
    BBPT_OT_modal_timer,
    BBPT_AddonPreferences,
    BBPT_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.app.handlers.load_pre.append(on_load_pre)
    bpy.app.handlers.load_post.append(on_load_post)
    bpy.app.handlers.save_post.append(on_save_post)
    atexit.register(flush_current)

    # Blender may already have a file loaded when the add-on is enabled.
    bpy.app.timers.register(_start_modal_deferred, first_interval=0.0)


def unregister():
    _state["running"] = False
    flush_current()

    try:
        atexit.unregister(flush_current)
    except ValueError:
        pass

    if on_load_pre in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.remove(on_load_pre)
    if on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(on_load_post)
    if on_save_post in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.remove(on_save_post)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
