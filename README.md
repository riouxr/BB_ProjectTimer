# BB Project Timer

A Blender extension that automatically tracks time spent working on a `.blend` file.

## How it works

- Starts automatically when a file is loaded - there is no manual start/stop control, so it can't be toggled off from the UI.
- Counts time as active only while there is mouse-click activity; if there's no click for 60 seconds, tracking pauses. On Windows, switching to another window (another Blender instance included) pauses tracking immediately instead of waiting out that 60 seconds - it's not a cross-platform check, so macOS/Linux fall back to click-based idle detection alone.
- Saves progress to a `<blendfile>_time.txt` log file (every minute by default - see below), on every save of the .blend itself, and on file switch/quit, so a crash loses at most one autosave interval of tracked time.
- Every time you reopen (or re-load) the file, a new **session** starts. A session covers one continuous file-open period - taking a break without closing the file doesn't start a new one, it's still the same session, just with the idle time not counted toward its duration. The log keeps one line per session (start, end, duration), grouped and totalled per day, per user, and across the whole file - so you can see today's time, any past day's time, who worked on it, and lifetime project time.
- Also starts a fresh session (timer back to 0) on a plain "Save As" if BB_Kitsu-Pipeline's task stamp changes as part of that save - e.g. creating a Shading file from a Modeling one. That's a genuinely new task's file even though no full file-load event fires for it, so without this it would keep counting the old task's time under the new file. An ordinary rename/relocate (same task) still correctly carries the accumulated time over, as does any Save As without Kitsu installed - there's no other reliable signal to tell those apart.
- Shows live status, this session's time, and the file's total time in the 3D Viewport sidebar (N-panel), under the **Tool** tab.
- Safe with several Blender instances open at once: each instance only ever writes its own session line, so tracking different files - or even the same file open twice - doesn't clobber another instance's time. The total is simply the sum of every session line in the log.

## Preferences

- **Log Folder** - where the log is saved. Leave empty (default) to save next to each `.blend` file, or point it at a shared folder to collect logs from many projects in one place.
- **Save Every (minutes)** - how often the log is saved and synced to Kitsu. Default is 1 minute; raise it for fewer writes, lower it (minimum 1) for tighter crash protection.
- **Show Daily Breakdown** / **Show Session Times** - hide the per-day or per-session readable sections in the log if that's more detail than you want visible. The underlying per-session record is always kept either way - this only affects the readable text, not what's tracked, since the totals and multi-instance syncing depend on it staying complete.
- **Log Usernames** - tag each session with the OS username that logged it, and show a per-user total. Turn off to keep the log anonymous; no username is written while it's off.
- **Auto-assign Me to Task** - if a Kitsu sync fails because you're not assigned to the current task, automatically assign yourself and retry, instead of just showing the failure. Requires Kitsu permission to assign tasks. **Off by default** - this changes shared production data (task assignment is visible to the whole team), so it's a deliberate opt-in rather than a silent default.
- **Set Kitsu Start Date** - also set the task's Start Date (not just Real Start Date) to today on the first successful sync, if Kitsu doesn't have one. Kitsu's own convention is that Start Date is a planned/scheduled date set by production management - **off by default** since another user of this add-on might rely on it staying empty until a producer sets it. Turn this on only if you want to repurpose it as your own actual-start date. Never overwrites an existing value.

## Kitsu integration (optional)

If [BB_Kitsu-Pipeline](https://github.com/riouxr/BB_Kitsu-Pipeline) is also installed, logged in, and the current file is stamped with a Kitsu task, Project Timer will automatically push today's tracked time to that task's Kitsu time-spent record every 60 seconds, alongside the local log.

- No separate login or setup - it reuses BB_Kitsu-Pipeline's existing authenticated session and reads the task from the same scene stamp that add-on already writes. If that add-on isn't installed, isn't logged in, or the file has no task stamp, this is a silent no-op.
- Kitsu's time-spent endpoint *replaces* a task's logged duration for a day rather than adding to it, so each push sends the full total tracked for that task **today** (not just the session's delta). This means a manual edit made directly in Kitsu for that task and day will be overwritten by the next automatic push.
- You must be assigned to a task in Kitsu for it to accept time logged against it (a Kitsu/Zou permission rule, not something this add-on controls). The panel reflects this honestly: it only shows "Syncing to Kitsu" when the last push actually succeeded - if a sync is failing (e.g. not assigned to the task), it shows "Kitsu sync failing" with the underlying error instead of a misleading checkmark.
- This reaches into BB_Kitsu-Pipeline's internal session object rather than a published API (it doesn't expose one), so a future update to that add-on could change or break this - failures there are caught and simply skip the Kitsu push, never crash the timer or the local log.
- Kitsu/Zou rejects a duration of 0 minutes with an error, so before a full minute has accumulated for today (e.g. the first tens of seconds of a session), syncing is skipped rather than attempted and failing.
- Every Kitsu sync attempt (skipped, succeeded, or failed, including the specific error) is logged to Blender's System Console (`Window > Toggle System Console`) prefixed with `[BB Project Timer] Kitsu:`, for diagnosing sync issues without guessing.
- On the first *successful* sync for a task, if Kitsu has no `real_start_date` recorded for it yet, this sets it to today - Zou doesn't fill that in automatically just from time being logged, and it's meant to mean "when work actually began," which is exactly what tracked time represents. Never overwrites an existing value. A sync that's failing (e.g. not assigned) never reaches this step, since claiming work started on a task that couldn't actually log time would be misleading - once the underlying failure is fixed (assignment, permissions, etc.) and a sync succeeds, this fills in on that next successful attempt.
- Kitsu commonly displays duration in hours or days rather than minutes, so a few minutes of tracked time can round down to a visibly unchanged "0" in the UI even though the underlying record is correct - this isn't a sync failure, just a display rounding effect for small amounts of time.

## Requirements

Blender 4.5 or newer.

## Install

Download the latest release zip and install it via **Edit > Preferences > Get Extensions > Install from Disk**, or drag the zip into Blender.

## Limitations

This is not tamper-proof: anyone who can disable add-ons in Preferences or edit the installed files can stop tracking. It removes the easy "just click stop" path but is not hardened against a determined user.

If the *same* file is open in two Blender instances at once and both save within the same instant, one instance's on-disk line can be briefly overwritten by the other's - but since each instance rewrites its own line again every save interval, this self-heals and no time is permanently lost.

Loading or opening a file can silently invalidate a running modal operator, and this has been observed to behave differently across Blender versions - without a fix, that can leave the timer stuck showing a stale, frozen value after a file switch. A generation-counter mechanism plus a background watchdog (checked every 5 seconds) detects this and restarts tracking automatically, without double-counting time from the old instance.
