"""Native, optional directory chooser. The absolute-path input is always available."""
import os
import shutil
import subprocess
import sys

from .common import Problem


def choose_folder(initial):
    if sys.platform == "win32":
        # FolderBrowserDialog's selected path is written as UTF-8, regardless of console codepage.
        command = ["powershell.exe", "-NoProfile", "-STA", "-NonInteractive", "-Command", """
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Choose a RAH workspace folder'
$dialog.SelectedPath = $env:RAH_PICKER_INITIAL
if ($dialog.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) { [Console]::Write($dialog.SelectedPath) }
"""]
    elif sys.platform == "darwin":
        command = ["osascript", "-e", 'on run argv', "-e",
                   'POSIX path of (choose folder with prompt "Choose a RAH workspace folder" default location POSIX file (item 1 of argv))',
                   "-e", "end run", str(initial)]
    elif shutil.which("zenity"):
        command = ["zenity", "--file-selection", "--directory", "--title=Choose a RAH workspace folder", f"--filename={initial}/"]
    elif shutil.which("kdialog"):
        command = ["kdialog", "--getexistingdirectory", str(initial), "--title", "Choose a RAH workspace folder"]
    else:
        raise Problem("folder_picker", "No desktop folder picker is available. Enter the absolute path instead.", 503)
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=os.environ | {"RAH_PICKER_INITIAL": str(initial)}, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Problem("folder_picker", "The desktop folder picker was unavailable. Enter the absolute path instead.", 503) from error
    if result.returncode:
        if b"(-128)" in result.stderr or (result.returncode == 1 and not result.stderr):
            return None
        raise Problem("folder_picker", "The desktop folder picker was unavailable. Enter the absolute path instead.", 503)
    return result.stdout.decode("utf-8").rstrip("\r\n") or None