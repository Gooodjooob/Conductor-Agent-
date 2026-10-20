"""ClawHive capability probe and fail-closed adapter boundary.

ClawHive embeds an OpenClaw runtime, but the installed desktop bundle does not
expose a documented external task-run API. The probe reports that fact without
reading account secrets or session content. A real adapter must be constructed
with a user-supplied, documented command/API bridge; otherwise submission is
rejected instead of guessed through UI or private databases.
"""
import json
import os
from pathlib import Path
import plistlib
import subprocess

from .dsh import AdapterError


class ClawHiveAdapterError(AdapterError):
    pass


def probe_clawhive(app_path="/Applications/ClawHive.app"):
    app = Path(app_path)
    info_path = app / "Contents" / "Info.plist"
    manifest_path = app / "Contents" / "Resources" / "openclaw-runtime-manifest.json"
    result = {"host":"clawhive", "installed":False, "taskSubmitSupported":False,
              "statusQuerySupported":False, "cancelSupported":False,
              "evidence":"no documented external task API discovered"}
    if not app.is_dir() or app.is_symlink(): return result
    result["installed"] = True
    try:
        with info_path.open("rb") as stream: info = plistlib.load(stream)
        result["bundleId"] = info.get("CFBundleIdentifier")
        result["version"] = info.get("CFBundleShortVersionString")
    except (OSError, ValueError, KeyError):
        result["versionRead"] = False
    try:
        manifest = json.loads(manifest_path.read_text())
        result["runtimeVersion"] = manifest.get("version")
        result["runtimeManifestSha256"] = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    except (OSError, ValueError, TypeError):
        result["runtimeManifestRead"] = False
    return result


class ClawHiveAdapter:
    def __init__(self, command=None, workspace=None, timeout=300):
        self.command = list(command) if command else None
        self.workspace = str(Path(workspace).resolve(strict=True)) if workspace else None
        self.timeout = timeout
        if command and (not isinstance(command[0], str) or not Path(command[0]).is_absolute()):
            raise ValueError("ABSOLUTE_CLAWHIVE_COMMAND_REQUIRED")

    def probe(self):
        result = probe_clawhive()
        if self.command:
            result.update(bridgeConfigured=True, taskSubmitSupported=True,
                          statusQuerySupported=True, cancelSupported=False,
                          evidence="explicit external bridge supplied by operator")
        else:
            result["bridgeConfigured"] = False
        return result

    def start(self):
        if not self.command:
            raise ClawHiveAdapterError("CLAWHIVE_EXTERNAL_TASK_API_UNVERIFIED")
        raise ClawHiveAdapterError("CLAWHIVE_BRIDGE_PROTOCOL_REQUIRED")

    def submit(self, *args, **kwargs):
        raise ClawHiveAdapterError("CLAWHIVE_EXTERNAL_TASK_API_UNVERIFIED")

    def get_status(self, *args, **kwargs):
        raise ClawHiveAdapterError("CLAWHIVE_EXTERNAL_TASK_API_UNVERIFIED")

    def cancel(self, *args, **kwargs):
        raise ClawHiveAdapterError("CLAWHIVE_CANCEL_API_UNVERIFIED")
