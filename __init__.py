import atexit
import getpass
import hashlib
import os
import sys
import time
import uuid

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty

ADDON_VERSION = "0.9.2"

IDLE_THRESHOLD = 60.0             # seconds with no mouse click before the timer pauses
TICK_INTERVAL = 1.0               # seconds between accounting ticks
DEFAULT_SAVE_INTERVAL_MINUTES = 1  # fallback if preferences aren't available yet

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
    "username": "",
    "session_start": 0.0,
    "session_seconds": 0.0,
    "grand_total": 0.0,       # session_seconds + every other session's seconds, from the log file
    "last_tick": 0.0,
    "last_click": 0.0,
    "last_save": 0.0,
    "paused": True,
    "pause_reason": None,     # "idle" | "unfocused" | None
    "running": False,
    # Bumped on every file load. Loading a file can silently invalidate a
    # running modal operator without Blender ever calling its cancel() -
    # behaviour that has been observed to differ between Blender versions.
    # A stale instance notices its generation no longer matches and cancels
    # itself instead of us having to reliably detect that it already died.
    "generation": 0,
    "kitsu_last_error": None,  # None = last Kitsu sync attempt (if any) succeeded
    "kitsu_task_id": None,     # the Kitsu task this session started against, if any
}


def get_username():
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def is_blender_focused():
    """True if this Blender process currently owns the OS foreground window.

    Windows-only (checked via the Win32 API) - there's no cross-platform way
    to ask this from Python without extra dependencies, and Blender itself
    exposes no focus-changed event. On macOS/Linux this always returns True,
    so tracking there falls back to click-based idle detection alone, same
    as before this existed.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        owner_pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
        return owner_pid.value == kernel32.GetCurrentProcessId()
    except Exception:
        # Never let a platform quirk here break time tracking.
        return True


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
    """Returns {session_id: {"sid", "start", "end", "seconds", "user"}}.

    ``user`` is "" for sessions logged before usernames existed, or logged
    while the "Log Usernames" preference was off.
    """
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
                        "user": fields.get("user", "").replace("_", " "),
                    }
                except (KeyError, ValueError):
                    continue
    except OSError:
        pass
    return sessions


def _group_by_day(ordered):
    """Session list -> [(date_str, day_total_seconds, [sessions]), ...], oldest first."""
    days = {}
    order = []
    for s in ordered:
        date_str = time.strftime("%Y-%m-%d", time.localtime(s["start"]))
        if date_str not in days:
            days[date_str] = []
            order.append(date_str)
        days[date_str].append(s)
    return [(d, sum(s["seconds"] for s in days[d]), days[d]) for d in order]


def _group_by_user(ordered):
    """Session list -> [(username, total_seconds), ...], by first appearance."""
    users = {}
    order = []
    for s in ordered:
        name = s.get("user") or "(unspecified)"
        if name not in users:
            users[name] = 0.0
            order.append(name)
        users[name] += s["seconds"]
    return [(name, users[name]) for name in order]


def write_log(path, sessions, filepath):
    prefs = get_prefs()
    show_days = prefs.show_daily_breakdown if prefs else True
    show_sessions = prefs.show_session_detail if prefs else True
    show_users = prefs.log_usernames if prefs else True

    ordered = sorted(sessions.values(), key=lambda s: s["start"])
    total = sum(s["seconds"] for s in ordered)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("Total time on %s: %s\n\n" % (os.path.basename(filepath), format_hms(total)))

            if show_users:
                f.write("By user:\n")
                for name, secs in _group_by_user(ordered):
                    f.write("  %-20s %s\n" % (name, format_hms(secs)))
                f.write("\n")

            if show_days:
                f.write("By day:\n")
                for date_str, day_total, day_sessions in _group_by_day(ordered):
                    f.write("  %s   %s\n" % (date_str, format_hms(day_total)))
                    if show_users:
                        for name, secs in _group_by_user(day_sessions):
                            f.write("    %-18s %s\n" % (name, format_hms(secs)))
                f.write("\n")

            if show_sessions:
                f.write("Sessions:\n")
                for date_str, _day_total, day_sessions in _group_by_day(ordered):
                    f.write("  %s\n" % date_str)
                    for s in day_sessions:
                        start_str = time.strftime("%H:%M", time.localtime(s["start"]))
                        end_str = time.strftime("%H:%M", time.localtime(s["end"]))
                        who = "   %s" % s["user"] if show_users and s.get("user") else ""
                        f.write("    %s - %s   %s%s\n" % (start_str, end_str, format_hms(s["seconds"]), who))
                f.write("\n")

            # Machine-readable data last - this is what parse_sessions() reads
            # back, but nobody reading the file cares about it, so it doesn't
            # belong at the top pushing the actual numbers off screen.
            f.write("# BB Project Timer data below - do not edit by hand\n")
            f.write("TOTAL=%.1f\n" % total)
            for s in ordered:
                user_field = " user=%s" % s["user"].replace(" ", "_") if s.get("user") else ""
                f.write("SESSION sid=%s start=%.1f end=%.1f seconds=%.1f%s\n" % (
                    s["sid"], s["start"], s["end"], s["seconds"], user_field))
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

    Best-effort: no BB_Kitsu-Pipeline, not logged in, or no task assigned to
    this file are all just "nothing to do", not errors, and any Kitsu/network
    failure is swallowed so this integration can never break the timer
    itself. Kitsu's time-spent endpoint *replaces* the day's logged duration
    rather than adding to it, so this always sends the full total tracked
    for today rather than this session's delta - which also means a manual
    edit made directly in Kitsu for that task and day will be overwritten on
    the next sync.

    Unlike earlier versions, a failed write is not silent to the user: it's
    recorded in _state["kitsu_last_error"] so the panel can show it, since a
    blind "syncing" checkmark that's actually failing every time (e.g. the
    task isn't assigned to this person - a real case that happened) is worse
    than showing nothing.
    """
    try:
        client = _get_kitsu_client()
        if client is None:
            _state["kitsu_last_error"] = None  # not connected - a different, already-visible state
            return
        task_id = _get_kitsu_task_id()
        if not task_id:
            _state["kitsu_last_error"] = None
            return
        person = client.user or {}
        person_id = person.get("id")
        if not person_id:
            _state["kitsu_last_error"] = "no Kitsu user on the client"
            return

        minutes = int(round(_today_seconds(sessions) / 60.0))
        date_str = time.strftime("%Y-%m-%d")
        path = "actions/tasks/%s/time-spents/%s/persons/%s" % (task_id, date_str, person_id)

        try:
            client._request("POST", path, json={"duration": minutes})
            _state["kitsu_last_error"] = None
        except Exception as e:
            prefs = get_prefs()
            auto_assign = prefs.kitsu_auto_assign if prefs else False
            if auto_assign and _looks_like_not_assigned(e) and _try_kitsu_self_assign(client, task_id, person_id):
                try:
                    client._request("POST", path, json={"duration": minutes})
                    _state["kitsu_last_error"] = None
                except Exception as e2:
                    _state["kitsu_last_error"] = str(e2)
                    return
            else:
                _state["kitsu_last_error"] = str(e)
                return  # don't attempt the start-date check off a failed sync

        _ensure_kitsu_real_start_date(client, task_id)
    except Exception as e:
        _state["kitsu_last_error"] = str(e)


def _looks_like_not_assigned(error):
    text = str(error)
    return "403" in text or "not authorised" in text.lower()


def _try_kitsu_self_assign(client, task_id, person_id):
    """Assigns the current Kitsu user to a task, so time can be logged
    against it. Only ever called when the "Auto-assign Me to Task"
    preference is on - this changes shared production data (task
    assignment is visible to the whole team), so it's opt-in, not a
    default behaviour.
    """
    try:
        client._request("PUT", "actions/persons/%s/assign" % person_id, json={"task_ids": [task_id]})
        return True
    except Exception:
        return False


# Task ids already checked/set this Blender session - real_start_date only
# needs setting once per task, ever, so this avoids an extra request on
# every single sync once that's been confirmed.
_kitsu_start_date_checked = set()


def _ensure_kitsu_real_start_date(client, task_id):
    """Sets the task's real_start_date to today the first time this add-on
    logs time against it, if Kitsu hasn't already recorded one. Zou doesn't
    set this automatically just from time being logged - real_start_date
    is meant to mean "when work actually began," which is exactly what
    tracked time represents, so it's a natural thing to fill in rather than
    leave null. Never overwrites an existing value.
    """
    if task_id in _kitsu_start_date_checked:
        return

    # Only cache once we actually know the outcome - caching before this
    # would mean a transient failure here (network hiccup, timing) gets
    # marked "done" and never retried, even though nothing was ever set.
    try:
        task = client.task(task_id)
        if task and not task.get("real_start_date"):
            client._request(
                "PUT", "data/tasks/%s" % task_id,
                json={"real_start_date": time.strftime("%Y-%m-%d")},
            )
        _kitsu_start_date_checked.add(task_id)
    except Exception:
        pass  # leave uncached so the next successful sync retries this


def sync_log(filepath):
    if not filepath or _state["session_id"] is None:
        return
    path = get_log_path(filepath)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass

    prefs = get_prefs()
    log_usernames = prefs.log_usernames if prefs else True

    sessions = parse_sessions(path)
    sessions[_state["session_id"]] = {
        "sid": _state["session_id"],
        "start": _state["session_start"],
        "end": time.time(),
        "seconds": _state["session_seconds"],
        "user": _state["username"] if log_usernames else "",
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
    _state["username"] = get_username()
    _state["session_start"] = now
    _state["session_seconds"] = 0.0
    _state["last_tick"] = now
    _state["last_click"] = now
    _state["last_save"] = now
    _state["paused"] = False
    _state["kitsu_task_id"] = _get_kitsu_task_id()
    # A stale error from flushing the *previous* file on the way out (via
    # on_load_pre) must not be shown against this file - it hasn't been
    # synced yet, so it has no error of its own until its own attempt runs.
    _state["kitsu_last_error"] = None

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
    if not is_blender_focused():
        # Switched to another window (another Blender instance, or anything
        # else) - stop counting immediately rather than waiting out the idle
        # threshold. Resuming still needs a real click once this instance is
        # focused again if the idle threshold has since passed, same as any
        # other pause.
        _state["paused"] = True
        _state["pause_reason"] = "unfocused"
    elif idle > IDLE_THRESHOLD:
        _state["paused"] = True
        _state["pause_reason"] = "idle"
    else:
        _state["paused"] = False
        _state["pause_reason"] = None

    if not _state["paused"]:
        _state["session_seconds"] += dt
        _state["grand_total"] += dt

    prefs = get_prefs()
    interval_minutes = prefs.save_interval_minutes if prefs else DEFAULT_SAVE_INTERVAL_MINUTES
    if now - _state["last_save"] >= max(interval_minutes, 1) * 60:
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
    _generation = -1

    def modal(self, context, event):
        if self._generation != _state["generation"]:
            # A newer file load has since started a fresh instance - this one
            # is a leftover Blender didn't cleanly cancel, so stop ticking.
            self._remove_timer(context)
            return {'CANCELLED'}
        if not _state["running"]:
            return {'FINISHED'}

        if event.type == 'TIMER':
            tick()
            _redraw_timer_panels(context)
        elif event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE'} and event.value == 'PRESS':
            register_click()

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        self._generation = _state["generation"]
        self._wm_timer = context.window_manager.event_timer_add(TICK_INTERVAL, window=context.window)
        context.window_manager.modal_handler_add(self)
        _state["running"] = True
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        self._remove_timer(context)
        if _state["generation"] == self._generation:
            _state["running"] = False

    def _remove_timer(self, context):
        if self._wm_timer is not None:
            context.window_manager.event_timer_remove(self._wm_timer)
            self._wm_timer = None


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

    # Loading a file can silently invalidate a running modal operator without
    # Blender calling its cancel() - observed to vary between Blender
    # versions. Bumping the generation and clearing the flag here forces a
    # fresh operator on every load rather than trusting a possibly-stale
    # "already running" flag; if the old instance somehow is still alive, its
    # next event will see the generation mismatch and cancel itself, so this
    # never ends up with two instances double-counting time either.
    _state["generation"] += 1
    _state["running"] = False
    ensure_modal_running()
    return None  # one-shot


WATCHDOG_INTERVAL = 5.0  # seconds between liveness checks


def _watchdog():
    """Restarts the modal if it stops ticking for any reason other than a
    file load (which on_load_post already handles) - e.g. some other
    operator taking over the modal stack, or Blender cancelling it and
    correctly clearing "running" but nothing then restarting it. tick()
    runs every TICK_INTERVAL regardless of idle/pause state, so a stall
    here means the operator itself died, not just that the user went idle.

    Deliberately does not require _state["running"] to already be True: a
    modal that was cleanly cancelled (running already False) needs exactly
    the same restart as one that died without telling us - checking only
    the "still marked running but stale" case left that first one stuck
    forever, which is what actually happened in practice.
    """
    _ensure_handlers()

    if _state["session_id"] is None:
        return WATCHDOG_INTERVAL

    if _state["running"] and time.time() - _state["last_tick"] <= TICK_INTERVAL * 5:
        return WATCHDOG_INTERVAL  # ticking normally, nothing to do

    _state["generation"] += 1
    _state["running"] = False
    ensure_modal_running()
    return WATCHDOG_INTERVAL


@bpy.app.handlers.persistent
def on_load_pre(dummy1, dummy2):
    flush_current()


@bpy.app.handlers.persistent
def on_load_post(dummy1, dummy2):
    # bpy.ops is not always safe to call directly from a handler, so defer by one tick.
    bpy.app.timers.register(_start_modal_deferred, first_interval=0.0)


@bpy.app.handlers.persistent
def on_save_post(dummy1, dummy2):
    filepath = bpy.data.filepath
    if not filepath:
        return

    if filepath != _state["log_filepath"]:
        old_filepath = _state["log_filepath"]
        new_task_id = _get_kitsu_task_id()

        # A pipeline tool (e.g. BB_Kitsu-Pipeline's "create file from
        # current") can Save As to a new path for a genuinely different
        # task - e.g. spinning off a Shading file from a Modeling one -
        # without ever going through load_post, so reset_state_for_current_
        # file() never runs and the timer just keeps counting the old
        # task's time under the new path. Kitsu's task stamp is the only
        # reliable signal to tell that apart from an ordinary rename/
        # relocate, where carrying the accumulated time over is correct.
        if new_task_id and new_task_id != _state["kitsu_task_id"]:
            if old_filepath:
                sync_log(old_filepath)  # final write to the file being left behind
            reset_state_for_current_file()
        else:
            # Ordinary "File > Save" on a file that had no path yet, or a
            # plain rename/relocate - same work, just point tracking at the
            # new path and carry over what's accumulated so far.
            _state["log_filepath"] = filepath

    # Every save is a natural checkpoint - flush the log now rather than
    # waiting out the autosave interval, so what's on disk always reflects
    # what was just saved. Reset the autosave clock too, so it doesn't fire
    # again moments later just because the interval had already elapsed.
    sync_log(filepath)
    _state["last_save"] = time.time()


def _ensure_handlers():
    """Re-attaches any of our handlers Blender's own handling silently
    dropped. All three are already @persistent, which is the real fix -
    without it, Blender clears these on every file load and that's exactly
    what caused sessions to stop updating in practice. This is a backstop
    in case something else ever removes them (another add-on touching the
    same lists, a "Reload Scripts", etc.), checked every watchdog cycle.
    """
    if on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(on_load_pre)
    if on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(on_load_post)
    if on_save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(on_save_post)


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

    save_interval_minutes: IntProperty(
        name="Save Every (minutes)",
        description=(
            "How often the log is saved (and synced to Kitsu, if connected). "
            "Lower values lose less time on a crash, at the cost of slightly "
            "more frequent disk/network writes"
        ),
        default=1,
        min=1,
        soft_max=30,
    )

    show_daily_breakdown: BoolProperty(
        name="Show Daily Breakdown",
        description=(
            "Include a readable day-by-day total in the log file. The "
            "underlying per-session record is always kept regardless, so "
            "totals and multi-instance syncing stay correct either way - "
            "this only controls what's shown as readable text"
        ),
        default=True,
    )

    show_session_detail: BoolProperty(
        name="Show Session Times",
        description=(
            "Include each session's exact start/end time in the log file as "
            "readable text. The underlying record is always kept for "
            "accuracy; this only controls whether it's shown as readable text"
        ),
        default=True,
    )

    log_usernames: BoolProperty(
        name="Log Usernames",
        description=(
            "Record who (OS username) logged each session, and show a "
            "per-user total per file. Turn off to keep the log anonymous - "
            "no username is written at all while this is off"
        ),
        default=True,
    )

    kitsu_auto_assign: BoolProperty(
        name="Auto-assign Me to Task",
        description=(
            "If a Kitsu sync fails because you're not assigned to the "
            "current task, automatically assign yourself and retry, rather "
            "than just showing the failure. Requires Kitsu permissions to "
            "assign tasks. Off by default: this changes shared production "
            "data (task assignment is visible to the whole team), so it "
            "should be a deliberate choice, not a silent default"
        ),
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "log_path")
        layout.label(text="Leave empty to save the log next to each .blend file", icon='INFO')

        layout.separator()
        layout.prop(self, "save_interval_minutes")

        layout.separator()
        col = layout.column(heading="Log Detail")
        col.prop(self, "show_daily_breakdown")
        col.prop(self, "show_session_detail")
        col.prop(self, "log_usernames")

        layout.separator()
        col = layout.column(heading="Kitsu")
        col.prop(self, "kitsu_auto_assign")


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

        if _state["paused"]:
            status = "Paused (other window)" if _state["pause_reason"] == "unfocused" else "Paused (idle)"
        else:
            status = "Tracking"
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
            if _get_kitsu_client() is None:
                layout.label(text="Kitsu task set, not logged in", icon='ERROR')
            elif _state["kitsu_last_error"]:
                layout.label(text="Kitsu sync failing", icon='ERROR')
                err_box = layout.box()
                err_text = _state["kitsu_last_error"]
                err_box.label(text=err_text[:60] + ("..." if len(err_text) > 60 else ""))
                if "403" in err_text or "not authorised" in err_text.lower():
                    err_box.label(text="Are you assigned to this task in Kitsu?")
            else:
                layout.label(text="Syncing to Kitsu", icon='CHECKMARK')


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

    # persistent=True: this must keep running across file loads without
    # being re-armed by on_load_post, since it's what catches the modal
    # failing to restart in the first place.
    if not bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.register(_watchdog, first_interval=WATCHDOG_INTERVAL, persistent=True)


def unregister():
    _state["running"] = False
    flush_current()

    if bpy.app.timers.is_registered(_watchdog):
        bpy.app.timers.unregister(_watchdog)

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
