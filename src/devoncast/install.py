"""Installs the macOS pieces: the Inbox folder, launchd agents and a Finder Quick Action.

The agents run the `devoncast` installed by `uv tool install`, not the repo checkout, because
background processes can't read ~/Documents without a privacy prompt nobody is there to answer.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import uuid
from pathlib import Path

from .config import Config
from .inbox import inbox_dir

LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
SERVICES = Path("~/Library/Services").expanduser()
INBOX_LABEL = "net.polyculture.devoncast.inbox"
POLL_LABEL = "net.polyculture.devoncast.poll"
QUICK_ACTION = "Add to Devoncast.workflow"
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def installed_executable() -> str:
    exe = Path("~/.local/bin/devoncast").expanduser()
    if not exe.exists():
        raise SystemExit("Run `uv tool install --reinstall .` in the repo first; the agents use that copy.")
    return str(exe)


def install(cfg: Config, poll: bool = True) -> list[str]:
    exe = installed_executable()
    inbox = inbox_dir(cfg)
    for sub in ("", "processing", "done", "failed"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    logs = cfg.home / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    done = []
    # The interval runs retries when they come due, even with nothing new in the Inbox.
    _agent(INBOX_LABEL, [exe, "inbox"], logs / "inbox.log", WatchPaths=[str(inbox)], StartInterval=300)
    done.append(f"launchd agent {INBOX_LABEL} watching {inbox} and retrying failures")
    if poll:
        _agent(POLL_LABEL, [exe, "poll"], logs / "poll.log", StartInterval=60)
        done.append(f"launchd agent {POLL_LABEL} polling the remote queue every minute")
    _quick_action(inbox)
    done.append(f"Finder Quick Action '{QUICK_ACTION.removesuffix('.workflow')}'")
    return done


def uninstall() -> None:
    for label in (INBOX_LABEL, POLL_LABEL):
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        (LAUNCH_AGENTS / f"{label}.plist").unlink(missing_ok=True)
    shutil.rmtree(SERVICES / QUICK_ACTION, ignore_errors=True)


def _agent(label: str, args: list[str], log: Path, **trigger) -> None:
    path = LAUNCH_AGENTS / f"{label}.plist"
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": args,
        "EnvironmentVariables": {"PATH": PATH},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        **trigger,
    }
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    path.write_bytes(plistlib.dumps(plist))
    _launchctl("bootstrap", f"gui/{os.getuid()}", str(path), check=True)


def _launchctl(*args: str, check: bool = False) -> None:
    subprocess.run(["launchctl", *args], capture_output=True, check=check)


def _quick_action(inbox: Path) -> None:
    """An Automator service for Finder that copies the selected files into the Inbox."""
    bundle = SERVICES / QUICK_ACTION / "Contents"
    shutil.rmtree(bundle.parent, ignore_errors=True)
    bundle.mkdir(parents=True)
    command = f'for f in "$@"; do cp -p "$f" "{inbox}/"; done'
    (bundle / "Info.plist").write_bytes(plistlib.dumps({
        "NSServices": [{
            "NSMenuItem": {"default": "Add to Devoncast"},
            "NSMessage": "runWorkflowAsService",
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": ["public.item"],
        }],
    }))
    (bundle / "document.wflow").write_bytes(plistlib.dumps({
        "AMApplicationBuild": "523",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{
            "action": {
                "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
                "AMActionVersion": "2.0.3",
                "AMApplication": ["Automator"],
                "AMParameterProperties": {
                    "COMMAND_STRING": {}, "CheckedForUserDefaultShell": {}, "inputMethod": {},
                    "shell": {}, "source": {},
                },
                "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
                "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
                "ActionName": "Run Shell Script",
                "ActionParameters": {
                    "COMMAND_STRING": command,
                    "CheckedForUserDefaultShell": True,
                    "inputMethod": 1,  # pass input as arguments
                    "shell": "/bin/bash",
                    "source": "",
                },
                "BundleIdentifier": "com.apple.RunShellScript",
                "CFBundleVersion": "2.0.3",
                "CanShowSelectedItemsWhenRun": False,
                "CanShowWhenRun": True,
                "Category": ["AMCategoryUtilities"],
                "Class Name": "RunShellScriptAction",
                "InputUUID": str(uuid.uuid4()).upper(),
                "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
                "OutputUUID": str(uuid.uuid4()).upper(),
                "UUID": str(uuid.uuid4()).upper(),
                "UnlocalizedApplications": ["Automator"],
                "arguments": {},
                "isViewVisible": 1,
                "location": "309.000000:253.000000",
                "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
            },
            "isViewVisible": 1,
        }],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }))
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], capture_output=True)
