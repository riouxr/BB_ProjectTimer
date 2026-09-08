# BB Project Timer

A Blender extension that automatically tracks time spent working on a `.blend` file.

## How it works

- Starts automatically when a file is loaded - there is no manual start/stop control, so it can't be toggled off from the UI.
- Counts time as active only while there is mouse-click activity; if there's no click for 60 seconds, tracking pauses.
- Saves progress to a `<blendfile>_time.txt` sidecar file next to the `.blend`, every 60 seconds and on file switch/quit, so a crash loses at most ~1 minute of tracked time.
- Reopening the same file resumes from the saved total.
- Shows live status and the running total in the 3D Viewport sidebar (N-panel), under the **Tool** tab.

## Requirements

Blender 4.5 or newer.

## Install

Download the latest release zip and install it via **Edit > Preferences > Get Extensions > Install from Disk**, or drag the zip into Blender.

## Limitations

This is not tamper-proof: anyone who can disable add-ons in Preferences or edit the installed files can stop tracking. It removes the easy "just click stop" path but is not hardened against a determined user.
