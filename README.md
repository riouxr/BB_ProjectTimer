# BB Project Timer

A Blender extension that automatically tracks time spent working on a `.blend` file.

## How it works

- Starts automatically when a file is loaded - there is no manual start/stop control, so it can't be toggled off from the UI.
- Counts time as active only while there is mouse-click activity; if there's no click for 60 seconds, tracking pauses.
- Saves progress to a `<blendfile>_time.txt` log file every 60 seconds and on file switch/quit, so a crash loses at most ~1 minute of tracked time.
- Every time you reopen the file, a new **session** starts. The log keeps one line per session (start, end, duration), grouped and totalled per day, plus the running total across all sessions - so you can see today's time, any past day's time, and lifetime project time.
- Shows live status, this session's time, and the file's total time in the 3D Viewport sidebar (N-panel), under the **Tool** tab.
- The log location is configurable in the add-on preferences (**Log Folder**). Leave it empty to save next to each `.blend` file (default), or point it at a shared folder to collect logs from many projects in one place.
- Safe with several Blender instances open at once: each instance only ever writes its own session line, so tracking different files - or even the same file open twice - doesn't clobber another instance's time. The total is simply the sum of every session line in the log.

## Kitsu integration (optional)

If [BB_Kitsu-Pipeline](https://github.com/riouxr/BB_Kitsu-Pipeline) is also installed, logged in, and the current file is stamped with a Kitsu task, Project Timer will automatically push today's tracked time to that task's Kitsu time-spent record every 60 seconds, alongside the local log.

- No separate login or setup - it reuses BB_Kitsu-Pipeline's existing authenticated session and reads the task from the same scene stamp that add-on already writes. If that add-on isn't installed, isn't logged in, or the file has no task stamp, this is a silent no-op.
- Kitsu's time-spent endpoint *replaces* a task's logged duration for a day rather than adding to it, so each push sends the full total tracked for that task **today** (not just the session's delta). This means a manual edit made directly in Kitsu for that task and day will be overwritten by the next automatic push.
- You must be assigned to a task in Kitsu for it to accept time logged against it (a Kitsu/Zou permission rule, not something this add-on controls) - if you see "Kitsu task set, not logged in" or nothing changes in Kitsu, check the assignment there.
- This reaches into BB_Kitsu-Pipeline's internal session object rather than a published API (it doesn't expose one), so a future update to that add-on could change or break this - failures there are caught and simply skip the Kitsu push, never crash the timer or the local log.

## Requirements

Blender 4.5 or newer.

## Install

Download the latest release zip and install it via **Edit > Preferences > Get Extensions > Install from Disk**, or drag the zip into Blender.

## Limitations

This is not tamper-proof: anyone who can disable add-ons in Preferences or edit the installed files can stop tracking. It removes the easy "just click stop" path but is not hardened against a determined user.

If the *same* file is open in two Blender instances at once and both save within the same instant, one instance's on-disk line can be briefly overwritten by the other's - but since each instance rewrites its own line again every 60 seconds, this self-heals within a minute and no time is permanently lost.
