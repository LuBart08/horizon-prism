#!/usr/bin/env python3
"""M0 spike: launch Project Horizon from a Prism Launcher instance.

This is deliberately *not* a fork of Prism. It proves the integration path the
fork will use:

    Prism instance metadata
        -> horizon-prepare (with Prism's version JSON, client jar, libraries, assets)
        -> horizon-client (with the account session on stdin)

What it proves:
  * Prism's instance/meta layout can feed horizon-prepare, so nothing Minecraft
    is downloaded twice;
  * a Prism auth session maps to Horizon's session JSON;
  * Horizon's launch contract (``--launch-api``, exit codes, crash report line)
    can be driven by a plain subprocess, which is what a Prism launch step does.

What it does not do: run Java mods, build native mods, or touch Prism's source.

Exit codes of the spike itself:
  0  success
  1  a step failed (prepare, launch, I/O)
  2  bad invocation or unsupported instance (e.g. not Minecraft 26.3)
  3  the game crashed (Horizon exit 1 or 101)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SPIKE_VERSION = "0.1.0"
TARGET_MC = "26.3"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
# Overridden by the client's own `crash_report_line` when it advertises one.
CRASH_MARKER = "Horizon: crash report saved to "

# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def redact(value: Optional[str]) -> str:
    return "<redacted>" if value else ""


class SpikeError(Exception):
    """A step failed with a message meant for the player."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def parse_ini(path: Path) -> Dict[Tuple[str, str], str]:
    """Parse a MultiMC/Prism instance.cfg (INI with escaped keys)."""
    out: Dict[Tuple[str, str], str] = {}
    section = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().replace("\\ ", " ").replace("\\=", "=")
        out[(section, key)] = value.strip()
    return out


def ini_get(ini: Dict[Tuple[str, str], str], key: str, default: Optional[str] = None) -> Optional[str]:
    for (_section, k), v in ini.items():
        if k == key:
            return v
    return default


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def human_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


# ---------------------------------------------------------------------------
# Prism discovery
# ---------------------------------------------------------------------------


def horizon_launcher_dir() -> Optional[Path]:
    """The Horizon launcher's data folder (it versions its binaries)."""
    home = Path.home()
    env = os.environ.get("HORIZON_LAUNCHER_DIR")
    candidates: List[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            home / "Library/Application Support/horizon-launcher",  # macOS
            home / ".local/share/horizon-launcher",  # Linux
            Path(os.environ.get("APPDATA", "")) / "horizon-launcher"
            if os.environ.get("APPDATA")
            else home / "AppData/Roaming/horizon-launcher",
        ]
    )
    for candidate in candidates:
        if (candidate / "versions").is_dir():
            return candidate
    return None


def find_horizon_binaries() -> Optional[Tuple[Path, Path]]:
    """(horizon-client, horizon-prepare) from the launcher's selected version.

    Falls back to the newest installed version, then to PATH.
    """
    root = horizon_launcher_dir()
    if root:
        versions = root / "versions"
        selected = None
        settings = root / "settings.json"
        if settings.is_file():
            try:
                selected = load_json(settings).get("selected")
            except (OSError, ValueError):
                selected = None
        ordered: List[Path] = []
        if selected and (versions / selected).is_dir():
            ordered.append(versions / selected)
        try:
            ordered.extend(sorted((p for p in versions.iterdir() if p.is_dir()), reverse=True))
        except OSError:
            pass
        for version in ordered:
            client = version / "horizon-client"
            prepare = version / "horizon-prepare"
            if client.is_file() and prepare.is_file():
                return client, prepare
    client = shutil.which("horizon-client")
    prepare = shutil.which("horizon-prepare")
    if client and prepare:
        return Path(client), Path(prepare)
    return None


def default_prism_dirs() -> List[Path]:
    home = Path.home()
    candidates: List[Optional[Path]] = []
    env = os.environ.get("PRISM_DIR") or os.environ.get("PRISMLAUNCHER_DIR")
    if env:
        candidates.append(Path(env).expanduser())
    candidates.extend(
        [
            home / "Library/Application Support/PrismLauncher",  # macOS
            home / ".local/share/PrismLauncher",  # Linux
            home / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher",  # Flatpak
            Path(os.environ.get("APPDATA", "")) / "PrismLauncher" if os.environ.get("APPDATA") else None,
            home / "Library/Application Support/MultiMC",
            home / ".local/share/multimc",
        ]
    )
    seen = set()
    out: List[Path] = []
    for c in candidates:
        if c is None:
            continue
        if c in seen:
            continue
        seen.add(c)
        if c.is_dir():
            out.append(c)
    return out


@dataclass
class Instance:
    root: Path
    name: str
    mc_version: Optional[str]
    instance_type: str
    managed_pack: Optional[str]
    settings: Dict[Tuple[str, str], str] = field(default_factory=dict)

    @property
    def dir_name(self) -> str:
        return self.root.name

    def game_dir(self) -> Path:
        for name in ("minecraft", ".minecraft"):
            candidate = self.root / name
            if candidate.is_dir():
                return candidate
        return self.root / "minecraft"

    def resources_dir(self) -> Path:
        return self.root / "resources"

    def home_dir(self) -> Path:
        return self.root / "horizon-home"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "dir": self.dir_name,
            "name": self.name,
            "mc_version": self.mc_version,
            "instance_type": self.instance_type,
            "managed_pack": self.managed_pack,
            "game_dir": str(self.game_dir()),
        }


def mc_version_of(root: Path) -> Optional[str]:
    pack = root / "mmc-pack.json"
    if pack.is_file():
        try:
            data = load_json(pack)
        except (OSError, ValueError):
            data = {}
        for component in data.get("components", []):
            if component.get("uid") == "net.minecraft":
                return str(component.get("version"))
    cfg = root / "instance.cfg"
    if cfg.is_file():
        ini = parse_ini(cfg)
        for key in ("IntendedVersion", "MinecraftVersion"):
            value = ini_get(ini, key)
            if value:
                return value
    return None


def load_instance(root: Path) -> Instance:
    if not (root / "instance.cfg").is_file():
        raise SpikeError("%s is not a Prism instance (no instance.cfg)" % root, 2)
    ini = parse_ini(root / "instance.cfg")
    return Instance(
        root=root,
        name=ini_get(ini, "name") or root.name,
        mc_version=mc_version_of(root),
        instance_type=ini_get(ini, "InstanceType") or "OneSix",
        managed_pack=ini_get(ini, "ManagedPackName"),
        settings=ini,
    )


def list_instances(prism_dir: Path) -> List[Instance]:
    instances_dir = prism_dir / "instances"
    if not instances_dir.is_dir():
        return []
    out = []
    for child in sorted(instances_dir.iterdir()):
        if child.is_dir() and (child / "instance.cfg").is_file():
            try:
                out.append(load_instance(child))
            except SpikeError:
                continue
    return out


def resolve_instance(arg: Optional[str], prism_dir: Optional[Path]) -> Instance:
    if arg:
        candidate = Path(arg).expanduser()
        if candidate.is_dir() and (candidate / "instance.cfg").is_file():
            return load_instance(candidate)
        # An instance id or name, under a Prism folder.
        dirs = [prism_dir] if prism_dir else default_prism_dirs()
        for d in dirs:
            if d is None:
                continue
            by_dir = d / "instances" / arg
            if (by_dir / "instance.cfg").is_file():
                return load_instance(by_dir)
            for inst in list_instances(d):
                if inst.name.lower() == arg.lower():
                    return inst
        raise SpikeError("no Prism instance found for %r" % arg, 2)

    dirs = [prism_dir] if prism_dir else default_prism_dirs()
    if not dirs:
        raise SpikeError("no Prism Launcher folder found; pass --prism-dir or --instance", 2)
    instances = list_instances(dirs[0])
    if not instances:
        raise SpikeError("no instances in %s" % (dirs[0] / "instances"), 2)
    if len(instances) == 1:
        return instances[0]
    raise SpikeError(
        "several instances in %s; pass --instance <name|path>: %s"
        % (dirs[0], ", ".join(i.name for i in instances)),
        2,
    )


# ---------------------------------------------------------------------------
# Prism meta -> horizon-prepare inputs
# ---------------------------------------------------------------------------


@dataclass
class McFiles:
    mc_version: str
    version_json: Optional[Path]
    client_jar: Optional[Path]
    libraries_dir: Path
    assets_dir: Path
    version_data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mc_version": self.mc_version,
            "version_json": str(self.version_json) if self.version_json else None,
            "client_jar": str(self.client_jar) if self.client_jar else None,
            "libraries_dir": str(self.libraries_dir),
            "assets_dir": str(self.assets_dir),
        }


def find_mc_files(prism_dir: Path, mc_version: str) -> McFiles:
    meta = prism_dir / "meta" / "net.minecraft"
    version_json = None
    for candidate in (
        meta / ("%s.json" % mc_version),
        meta / mc_version / ("%s.json" % mc_version),
        meta / mc_version / "version.json",
    ):
        if candidate.is_file():
            version_json = candidate
            break

    client_jar = None
    for candidate in (
        prism_dir / "libraries" / "com" / "mojang" / "minecraft" / mc_version / ("minecraft-%s-client.jar" % mc_version),
        meta / mc_version / ("%s.jar" % mc_version),
        meta / ("%s.jar" % mc_version),
    ):
        if candidate.is_file():
            client_jar = candidate
            break

    data: Dict[str, Any] = {}
    if version_json:
        try:
            data = load_json(version_json)
        except (OSError, ValueError) as exc:
            eprint("warning: cannot read %s: %s" % (version_json, exc))

    return McFiles(
        mc_version=mc_version,
        version_json=version_json,
        client_jar=client_jar,
        libraries_dir=prism_dir / "libraries",
        assets_dir=prism_dir / "assets",
        version_data=data,
    )


def find_prism_java(prism_dir: Path) -> Optional[Path]:
    """A Java 25 from Prism's managed runtimes, newest first."""
    root = prism_dir / "java"
    if not root.is_dir():
        return None
    found: List[Tuple[int, Path]] = []
    for runtime in sorted(root.iterdir()):
        binary = runtime / "bin" / "java"
        if not binary.is_file():
            continue
        major = 0
        release = runtime / "release"
        if release.is_file():
            text = release.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'JAVA_VERSION="(\d+)', text)
            if match:
                major = int(match.group(1))
        found.append((major, binary))
    if not found:
        return None
    found.sort(reverse=True)
    return found[0][1]


def find_java(explicit: Optional[str], prism_dir: Path) -> Optional[Path]:
    if explicit:
        p = Path(explicit).expanduser()
        return p / "bin" / "java" if (p / "bin" / "java").is_file() else p
    env = os.environ.get("JAVA_HOME")
    if env and (Path(env) / "bin" / "java").is_file():
        return Path(env) / "bin" / "java"
    if platform.system() == "Darwin" and Path("/usr/libexec/java_home").is_file():
        try:
            out = subprocess.run(
                ["/usr/libexec/java_home", "-v", "25"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                candidate = Path(out.stdout.strip()) / "bin" / "java"
                if candidate.is_file():
                    return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    return find_prism_java(prism_dir)


# ---------------------------------------------------------------------------
# subprocesses
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    code: int
    stdout: List[str]
    stderr: List[str]
    seconds: float
    timed_out: bool = False

    @property
    def stderr_text(self) -> str:
        return "\n".join(self.stderr)

    def find_crash_report(self, marker: str = CRASH_MARKER) -> Optional[str]:
        for line in reversed(self.stderr):
            idx = line.find(marker)
            if idx >= 0:
                return line[idx + len(marker) :].strip()
        return None


def run_stream(
    argv: List[str],
    stdin_bytes: Optional[bytes] = None,
    timeout: Optional[float] = None,
    on_line: Optional[Callable[[str, str], None]] = None,
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> RunResult:
    """Run a process, stream both pipes line by line, return the outcome."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except OSError as exc:
        raise SpikeError("cannot run %s: %s" % (argv[0], exc), 1)

    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    def pump(stream: Any, sink: List[str], kind: str) -> None:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n")
            sink.append(line)
            if on_line:
                on_line(kind, line)
        stream.close()

    threads = [
        threading.Thread(target=pump, args=(proc.stdout, stdout_lines, "stdout"), daemon=True),
        threading.Thread(target=pump, args=(proc.stderr, stderr_lines, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()

    if stdin_bytes is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin_bytes.decode("utf-8", "replace"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    for thread in threads:
        thread.join(timeout=5)
    return RunResult(
        code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_lines,
        stderr=stderr_lines,
        seconds=time.monotonic() - started,
        timed_out=timed_out,
    )


# ---------------------------------------------------------------------------
# Horizon launch API
# ---------------------------------------------------------------------------


@dataclass
class LaunchApi:
    binary: Path
    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.data)

    def option_names(self) -> set:
        names = set()
        for item in self.data.get("options", []) or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(item["name"])
        return names

    def version(self) -> Optional[str]:
        return self.data.get("launch_api") or self.data.get("version")

    def minecraft(self) -> Optional[str]:
        return self.data.get("minecraft")

    def java_major(self) -> Optional[int]:
        value = self.data.get("java_major")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def crash_line(self) -> str:
        value = self.data.get("crash_report_line")
        return value if isinstance(value, str) and value else CRASH_MARKER


def probe_launch_api(binary: Path, kind: str, timeout: float = 20.0) -> LaunchApi:
    result = run_stream([str(binary), "--launch-api"], timeout=timeout)
    if result.code != 0:
        return LaunchApi(binary=binary, kind=kind, error="--launch-api exited %d" % result.code)
    text = "\n".join(result.stdout).strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        return LaunchApi(binary=binary, kind=kind, error="--launch-api printed no JSON: %s" % exc)
    if not isinstance(data, dict):
        return LaunchApi(binary=binary, kind=kind, error="--launch-api printed a non-object")
    return LaunchApi(binary=binary, kind=kind, data=data)


def pass_option(args: List[str], name: str, value: Optional[str], supported: set) -> None:
    """Append an option only when the build advertises it (or when it said nothing)."""
    if supported and name not in supported:
        return
    args.append(name)
    if value is not None:
        args.append(value)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def prepare_argv(
    prepare: Path,
    resources: Path,
    files: McFiles,
    prism_dir: Optional[Path],
    server_jar: Optional[Path],
    java: Optional[Path],
    supported: set,
    complete: bool,
) -> List[str]:
    argv = [str(prepare)]
    pass_option(argv, "--prepare", None, supported)
    pass_option(argv, "--output", str(resources), supported)
    # Preferred: let the preparer read Prism's own layout (version JSON, Maven
    # libraries, assets, components). It then uses the client jar as the server
    # side, so no server jar is needed at all.
    use_prism_dir = bool(prism_dir) and (not supported or "--prism-dir" in supported)
    if use_prism_dir:
        pass_option(argv, "--prism-dir", str(prism_dir), supported)
    else:
        if files.version_json:
            pass_option(argv, "--version-json", str(files.version_json), supported)
        if files.client_jar:
            pass_option(argv, "--client-jar", str(files.client_jar), supported)
        pass_option(argv, "--libraries-dir", str(files.libraries_dir), supported)
        pass_option(argv, "--assets-dir", str(files.assets_dir), supported)
        if server_jar:
            pass_option(argv, "--server-jar", str(server_jar), supported)
    if java:
        pass_option(argv, "--java-home", str(java.parent.parent), supported)
    if complete:
        pass_option(argv, "--complete", None, supported)
    pass_option(argv, "--progress", "json", supported)
    return argv


def prism_dir_for(instance: Instance) -> Path:
    """The Prism root that owns the instance, else the instance itself.

    ``horizon-prepare --prism-dir`` accepts either; a root is better because it
    carries ``meta/`` for the rewritten version JSON.
    """
    instances = instance.root.parent
    if instances.name == "instances":
        return instances.parent
    return instance.root


# Gaps that `horizon-prepare --complete` fills (sounds, built-in packs, title
# panorama, Unifont glyphs, bundled languages). They are optional: the game
# runs without them (silent, with the classic panorama, etc.).
OPTIONAL_GAPS = {"packs", "panorama", "fonts", "languages", "sounds"}


def classify_gaps(text: str) -> Tuple[List[str], List[str]]:
    """Split `--check` output into (optional, required) gap names.

    A gap line looks like ``the sounds missing: horizon-prepare sounds``.
    """
    optional: List[str] = []
    required: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("error:"):
            continue
        name = None
        if "missing:" in line:
            command = line.split("missing:", 1)[1].strip().split()
            if command:
                name = command[-1]
        elif "missing" in line:
            name = line.rsplit(None, 1)[-1].strip(":")
        if name in OPTIONAL_GAPS:
            optional.append(name)
        else:
            required.append(line)
    return optional, required


def prepare_needed(
    prepare: Path, resources: Path, supported: set, require_complete: bool
) -> Tuple[bool, Optional[str], List[str]]:
    """(needed, reason, optional gaps).

    With ``require_complete`` false, a folder missing only optional extras is
    considered usable: no re-preparation on every run.
    """
    if not resources.is_dir():
        return True, "resources folder missing", []
    if supported and "--check" not in supported:
        return False, None, []
    result = run_stream([str(prepare), "--check", "--output", str(resources)], timeout=120)
    if result.code == 0:
        return False, None, []
    text = "\n".join(result.stdout) or result.stderr_text
    optional, required = classify_gaps(text)
    detail = "\n".join(required[-5:]) or text.strip()[-500:] or "check failed"
    if not required and optional and not require_complete:
        return False, None, optional
    return True, detail.strip() or "check failed", optional


def run_prepare(
    prepare: Path,
    resources: Path,
    files: McFiles,
    prism_dir: Optional[Path],
    server_jar: Optional[Path],
    java: Optional[Path],
    supported: set,
    timeout: float,
    verbose: bool,
    complete: bool,
) -> RunResult:
    argv = prepare_argv(
        prepare, resources, files, prism_dir, server_jar, java, supported, complete=complete
    )
    if verbose:
        eprint("+ " + shlex.join(argv))
    last_fraction = -1.0

    def on_line(kind: str, line: str) -> None:
        nonlocal last_fraction
        if kind != "stdout" or not line.strip():
            return
        try:
            event = json.loads(line)
        except ValueError:
            eprint("  " + line)
            return
        if not isinstance(event, dict):
            return
        if "error" in event:
            eprint("  error: %s" % event["error"])
            return
        fraction = event.get("fraction")
        message = event.get("message")
        if isinstance(fraction, (int, float)):
            pct = max(0.0, min(1.0, float(fraction))) * 100.0
            if pct - last_fraction >= 5.0 or pct >= 100.0:
                last_fraction = pct
                eprint("  [%5.1f%%] %s" % (pct, message or ""))
        elif message:
            eprint("  " + str(message))

    return run_stream(argv, timeout=timeout, on_line=on_line, cwd=resources.parent)


def download_server_jar(mc_version: str, dest: Path, verbose: bool) -> Path:
    """Fetch downloads.server for mc_version, verifying Mojang's SHA-1."""
    eprint("  resolving Minecraft %s from Mojang..." % mc_version)
    try:
        with urllib.request.urlopen(MOJANG_MANIFEST, timeout=60) as response:
            manifest = json.load(response)
        entry = next((v for v in manifest.get("versions", []) if v.get("id") == mc_version), None)
        if not entry:
            raise SpikeError("Minecraft %s is not in Mojang's version manifest" % mc_version, 2)
        with urllib.request.urlopen(entry["url"], timeout=60) as response:
            version = json.load(response)
        server = version.get("downloads", {}).get("server")
        if not server:
            raise SpikeError("Minecraft %s has no server download" % mc_version, 2)
    except SpikeError:
        raise
    except Exception as exc:  # urllib raises a wide range
        raise SpikeError("cannot read Mojang's metadata: %s" % exc, 1)

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    digest = hashlib.sha1()
    eprint("  downloading %s (%s)..." % (server["url"], human_bytes(int(server.get("size", 0)))))
    try:
        with urllib.request.urlopen(server["url"], timeout=120) as response, part.open("wb") as fh:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                fh.write(chunk)
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise SpikeError("server jar download failed: %s" % exc, 1)
    actual = digest.hexdigest()
    expected = server.get("sha1")
    if expected and actual != expected:
        part.unlink(missing_ok=True)
        raise SpikeError("server jar checksum mismatch (expected %s, got %s)" % (expected, actual), 1)
    part.replace(dest)
    return dest


# ---------------------------------------------------------------------------
# session + launch
# ---------------------------------------------------------------------------


def load_session(path: Optional[Path], offline: bool) -> Dict[str, Any]:
    if offline:
        return {
            "name": "Spike",
            "uuid": "0" * 32,
            "access_token": "0",
            "user_type": "msa",
        }
    if not path:
        raise SpikeError(
            "no session: pass --session-json <file> (Prism account export) or --offline-session for a stub",
            2,
        )
    try:
        data = load_json(path)
    except (OSError, ValueError) as exc:
        raise SpikeError("cannot read session JSON %s: %s" % (path, exc), 2)
    if not isinstance(data, dict):
        raise SpikeError("session JSON must be an object", 2)
    missing = [k for k in ("name", "uuid", "access_token") if not data.get(k)]
    if missing:
        raise SpikeError("session JSON is missing %s" % ", ".join(missing), 2)
    data["uuid"] = str(data["uuid"]).replace("-", "").lower()
    data.setdefault("user_type", "msa")
    return data


def launch_argv(
    client: Path,
    resources: Path,
    game_dir: Path,
    home: Path,
    player_dir: Optional[Path],
    supported: set,
    width: int,
    height: int,
    fullscreen: bool,
    quick_world: Optional[str],
    join: Optional[str],
    frames: Optional[int],
    smoke_report: Optional[Path],
) -> List[str]:
    argv = [str(client)]
    pass_option(argv, "--resources", str(resources), supported)
    pass_option(argv, "--game-dir", str(game_dir), supported)
    pass_option(argv, "--home", str(home), supported)
    if player_dir:
        pass_option(argv, "--player-dir", str(player_dir), supported)
    pass_option(argv, "--width", str(width), supported)
    pass_option(argv, "--height", str(height), supported)
    if fullscreen:
        pass_option(argv, "--fullscreen", None, supported)
    if quick_world:
        pass_option(argv, "--quickPlaySingleplayer", quick_world, supported)
    if join:
        pass_option(argv, "--quickPlayMultiplayer", join, supported)
    if frames:
        # Testing flags are not advertised by --launch-api: pass them as asked.
        argv += ["--frames", str(frames)]
    if smoke_report:
        argv += ["--smoke-report", str(smoke_report)]
    pass_option(argv, "--session-stdin", None, supported)
    return argv


def describe_exit(code: int) -> str:
    return {
        0: "player quit",
        1: "the game stopped on an error",
        2: "the command line was refused",
        101: "the game panicked",
        -9: "the game was killed",
        -15: "the game was terminated",
    }.get(code, "unknown exit code")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def make_report(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "spike": SPIKE_VERSION,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "arguments": {
            "instance": args.instance,
            "prism_dir": str(args.prism_dir) if args.prism_dir else None,
            "stage": args.stage,
            "dry_run": args.dry_run,
        },
        "instance": None,
        "prism": None,
        "mc_files": None,
        "client": None,
        "prepare": None,
        "launch": None,
        "warnings": [],
        "ok": False,
    }


# ---------------------------------------------------------------------------
# main flow
# ---------------------------------------------------------------------------


def cmd_inspect(args: argparse.Namespace, report: Dict[str, Any]) -> Tuple[Instance, McFiles, LaunchApi]:
    prism_dir = args.prism_dir
    instance = resolve_instance(args.instance, prism_dir)
    report["instance"] = instance.as_dict()

    if prism_dir is None:
        # The instance may have been given as a path; keep the Prism folder that owns it.
        prism_dir = instance.root.parent.parent
    report["prism"] = {"dir": str(prism_dir)}

    eprint("instance : %s (%s)" % (instance.name, instance.root))
    eprint("minecraft: %s" % (instance.mc_version or "unknown"))
    eprint("game dir : %s" % instance.game_dir())

    if instance.mc_version != TARGET_MC:
        raise SpikeError(
            "Horizon plays Minecraft %s; this instance is %s. Create or point at a %s instance."
            % (TARGET_MC, instance.mc_version or "unknown", TARGET_MC),
            2,
        )

    files = find_mc_files(prism_dir, instance.mc_version)
    report["mc_files"] = files.as_dict()
    if files.version_json:
        eprint("version  : %s" % files.version_json)
    else:
        report["warnings"].append("no version JSON under %s" % (prism_dir / "meta/net.minecraft"))
    if files.client_jar:
        eprint("client jar: %s" % files.client_jar)
    else:
        report["warnings"].append("no client jar under %s" % (prism_dir / "libraries/com/mojang/minecraft"))
    if (files.assets_dir / "indexes").is_dir():
        eprint("assets   : %s" % files.assets_dir)
    else:
        report["warnings"].append("no assets/indexes under %s" % files.assets_dir)

    client = Path(args.client).expanduser().resolve() if args.client else None
    if client is None:
        env = os.environ.get("HORIZON_CLIENT")
        client = Path(env).expanduser().resolve() if env else None
    if client is None:
        found = find_horizon_binaries()
        client = found[0] if found else None
    api = LaunchApi(binary=client or Path("horizon-client"), kind="client")
    if client:
        api = probe_launch_api(client, "client")
        if api.ok:
            eprint("client   : %s (launch API %s, Minecraft %s)" % (client, api.version(), api.minecraft()))
            if api.minecraft() and api.minecraft() != TARGET_MC:
                report["warnings"].append(
                    "client plays Minecraft %s, instance is %s" % (api.minecraft(), TARGET_MC)
                )
        else:
            report["warnings"].append("client --launch-api failed: %s" % api.error)
    else:
        report["warnings"].append("no horizon-client found (pass --client)")
    for warning in report["warnings"]:
        eprint("warning  : " + warning)
    report["client"] = {
        "path": str(client) if client else None,
        "launch_api": api.version(),
        "minecraft": api.minecraft(),
        "options": sorted(api.option_names()),
        "error": api.error,
    }
    return instance, files, api


def cmd_prepare(
    args: argparse.Namespace,
    report: Dict[str, Any],
    instance: Instance,
    files: McFiles,
    api: LaunchApi,
) -> None:
    prepare = Path(args.prepare).expanduser().resolve() if args.prepare else None
    if prepare is None:
        env = os.environ.get("HORIZON_PREPARE")
        prepare = Path(env).expanduser().resolve() if env else None
    if prepare is None:
        found = find_horizon_binaries()
        prepare = found[1] if found else None
    if not prepare:
        raise SpikeError("no horizon-prepare found; pass --prepare PATH", 2)
    if not prepare.is_file():
        raise SpikeError("horizon-prepare not found at %s" % prepare, 2)

    prepare_api = probe_launch_api(prepare, "prepare")
    supported = prepare_api.option_names() if prepare_api.ok else set()
    if not prepare_api.ok:
        report["warnings"].append("prepare --launch-api failed: %s" % prepare_api.error)
    java = find_java(args.java_home, instance.root.parent.parent)
    if prepare_api.java_major() and java:
        # Best effort: the release file is the only thing that says the major.
        release = java.parent.parent / "release"
        major = None
        if release.is_file():
            match = re.search(r'JAVA_VERSION="(\d+)', release.read_text(errors="replace"))
            if match:
                major = int(match.group(1))
        if major and major != prepare_api.java_major():
            report["warnings"].append(
                "Java %s found, preparation pins Java %s" % (major, prepare_api.java_major())
            )

    resources = Path(args.resources).expanduser().resolve() if args.resources else instance.resources_dir()
    report["instance"]["resources"] = str(resources)  # type: ignore[index]

    # Newer preparers read Prism's layout directly and use its client jar as the
    # server side, so no server jar is needed. Older ones take the four paths.
    prism_dir: Optional[Path] = None
    if not supported or "--prism-dir" in supported:
        prism_dir = prism_dir_for(instance)
        eprint("prism    : %s (read by horizon-prepare)" % prism_dir)

    server_jar: Optional[Path] = None
    if args.server_jar:
        server_jar = Path(args.server_jar).expanduser().resolve()
        if not server_jar.is_file():
            raise SpikeError("server jar not found: %s" % server_jar, 2)
    elif args.download_server_jar and not prism_dir:
        server_jar = instance.root / "horizon" / ("server-%s.jar" % instance.mc_version)
        if not server_jar.is_file():
            if args.dry_run:
                eprint("would download the %s server jar to %s" % (instance.mc_version, server_jar))
            else:
                server_jar = download_server_jar(instance.mc_version, server_jar, args.verbose)
        else:
            eprint("server jar: %s" % server_jar)

    if args.skip_prepare:
        report["prepare"] = {
            "binary": str(prepare),
            "resources": str(resources),
            "prism_dir": str(prism_dir) if prism_dir else None,
            "needed": None,
            "reason": "skipped",
            "optional_gaps": [],
            "java": str(java) if java else None,
            "server_jar": str(server_jar) if server_jar else None,
            "ran": False,
            "exit": None,
            "seconds": None,
        }
        eprint("prepare  : skipped (--skip-prepare)")
        return

    complete = not args.no_complete
    needed, why, optional_gaps = prepare_needed(prepare, resources, supported, complete)
    entry: Dict[str, Any] = {
        "binary": str(prepare),
        "resources": str(resources),
        "prism_dir": str(prism_dir) if prism_dir else None,
        "needed": needed,
        "reason": why,
        "optional_gaps": optional_gaps,
        "java": str(java) if java else None,
        "server_jar": str(server_jar) if server_jar else None,
        "ran": False,
        "exit": None,
        "seconds": None,
    }
    report["prepare"] = entry

    if needed and not server_jar and not prism_dir:
        raise SpikeError(
            "preparation needs the Minecraft %s server jar once: pass --server-jar PATH "
            "or --download-server-jar" % instance.mc_version,
            2,
        )

    if not needed:
        eprint("prepare  : up to date at %s" % resources)
        if optional_gaps:
            eprint("prepare  : optional extras missing (%s); --complete adds them" % ", ".join(optional_gaps))
        return

    argv = prepare_argv(
        prepare,
        resources,
        files,
        prism_dir,
        server_jar,
        java,
        supported,
        complete=not args.no_complete,
    )
    if args.dry_run:
        eprint("would run: " + shlex.join(argv))
        entry["argv"] = [redact(a) for a in argv]
        return

    eprint("prepare  : running horizon-prepare (first run can take minutes)")
    result = run_prepare(
        prepare,
        resources,
        files,
        prism_dir,
        server_jar,
        java,
        supported,
        timeout=args.prepare_timeout,
        verbose=args.verbose,
        complete=not args.no_complete,
    )
    entry.update(
        {
            "ran": True,
            "exit": result.code,
            "seconds": round(result.seconds, 2),
            "argv": [redact(a) for a in argv],
        }
    )
    if result.timed_out:
        raise SpikeError("horizon-prepare timed out after %.0fs" % args.prepare_timeout, 1)
    if result.code != 0:
        tail = "\n".join(result.stderr[-10:]) or "\n".join(result.stdout[-10:])
        raise SpikeError("horizon-prepare failed (exit %d):\n%s" % (result.code, tail), 1)

    still_needed, why, optional_gaps = prepare_needed(prepare, resources, supported, complete)
    entry["optional_gaps"] = optional_gaps
    if still_needed:
        raise SpikeError("preparation finished but --check still fails: %s" % why, 1)
    if optional_gaps:
        eprint("prepare  : ready in %.1fs (optional extras missing: %s)" % (result.seconds, ", ".join(optional_gaps)))
    else:
        eprint("prepare  : ready in %.1fs" % result.seconds)


def cmd_launch(
    args: argparse.Namespace,
    report: Dict[str, Any],
    instance: Instance,
    api: LaunchApi,
) -> None:
    client = api.binary
    if not client or not Path(client).is_file():
        raise SpikeError("no horizon-client found; pass --client PATH", 2)
    if not api.ok:
        raise SpikeError(
            "cannot read the client's launch contract (--launch-api): %s" % (api.error or "no JSON"),
            1,
        )
    supported = api.option_names()
    if supported and "--session-stdin" not in supported:
        raise SpikeError("this client does not support --session-stdin", 2)

    resources = Path(args.resources).expanduser().resolve() if args.resources else instance.resources_dir()
    game_dir = Path(args.game_dir).expanduser().resolve() if args.game_dir else instance.game_dir()
    home = Path(args.home).expanduser().resolve() if args.home else instance.home_dir()
    player_dir = Path(args.player_dir).expanduser().resolve() if args.player_dir else None
    smoke_report = Path(args.smoke_report).expanduser().resolve() if args.smoke_report else None
    if not args.dry_run:
        game_dir.mkdir(parents=True, exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        if player_dir:
            player_dir.mkdir(parents=True, exist_ok=True)

    try:
        session = load_session(
            Path(args.session_json).expanduser() if args.session_json else None, args.offline_session
        )
    except SpikeError:
        if not args.dry_run:
            raise
        session = {"name": "(dry run)", "uuid": "0" * 32, "access_token": "", "user_type": "msa"}
    payload = json.dumps(session, separators=(",", ":")).encode("utf-8")

    argv = launch_argv(
        client,
        resources,
        game_dir,
        home,
        player_dir,
        supported,
        args.width,
        args.height,
        args.fullscreen,
        args.quick_play_world,
        args.join,
        args.frames,
        smoke_report,
    )
    entry: Dict[str, Any] = {
        "argv": [redact(a) for a in argv],
        "cwd": str(instance.root),
        "session_name": session.get("name"),
        "session_uuid": session.get("uuid"),
        "dry_run": args.dry_run,
    }
    report["launch"] = entry

    if args.dry_run:
        eprint("would run: " + shlex.join(argv))
        eprint("session  : %s (%s), passed on stdin" % (session.get("name"), session.get("uuid")))
        return

    eprint("launch   : %s" % shlex.join(argv))
    eprint("session  : %s (%s), on stdin" % (session.get("name"), session.get("uuid")))

    def on_line(kind: str, line: str) -> None:
        # With --json, stdout carries only the report: game logs go to stderr.
        stream = sys.stderr if args.json or kind == "stderr" else sys.stdout
        print(line, file=stream)
        sys.stdout.flush()
        sys.stderr.flush()

    result = run_stream(
        argv,
        stdin_bytes=payload,
        timeout=args.launch_timeout or None,
        on_line=on_line,
        cwd=instance.root,
    )
    entry.update(
        {
            "exit": result.code,
            "seconds": round(result.seconds, 2),
            "timed_out": result.timed_out,
            "crash_report": result.find_crash_report(api.crash_line()),
        }
    )
    if smoke_report and smoke_report.is_file():
        try:
            entry["smoke"] = load_json(smoke_report)
        except (OSError, ValueError) as exc:
            entry["smoke_error"] = str(exc)
    report["launch"] = entry
    eprint(
        "launch   : exit %d (%s) after %.1fs"
        % (result.code, describe_exit(result.code), result.seconds)
    )
    if result.timed_out:
        raise SpikeError("the game did not quit within %.0fs and was killed" % args.launch_timeout, 3)
    if result.code != 0:
        if result.code in (1, 101):
            if entry["crash_report"]:
                eprint("crash    : %s" % entry["crash_report"])
            raise SpikeError("the game crashed (exit %d)" % result.code, 3)
        raise SpikeError("the game refused the command line (exit %d)" % result.code, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="horizon-prism-spike",
        description="M0 spike: run Horizon from a Prism Launcher instance (no fork).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--instance", help="Prism instance: folder, folder name, or display name")
    parser.add_argument("--prism-dir", type=Path, help="Prism Launcher data folder (auto-detected)")
    parser.add_argument("--client", help="horizon-client binary (default: installed Horizon launcher, $HORIZON_CLIENT, or PATH)")
    parser.add_argument("--prepare", help="horizon-prepare binary (default: installed Horizon launcher, $HORIZON_PREPARE, or PATH)")
    parser.add_argument("--resources", help="prepared resources folder (default: <instance>/resources)")
    parser.add_argument("--game-dir", help="game folder (default: <instance>/minecraft)")
    parser.add_argument("--home", help="logs and crash reports (default: <instance>/horizon-home)")
    parser.add_argument("--player-dir", help="per-player files (options, screenshots) for a shared game dir")
    parser.add_argument("--session-json", help="Horizon session JSON file (name, uuid, access_token, ...)")
    parser.add_argument("--offline-session", action="store_true", help="stub-only session; Horizon needs a real one")
    parser.add_argument("--server-jar", help="server jar for preparers without --prism-dir (not needed with it)")
    parser.add_argument("--download-server-jar", action="store_true", help="download it from Mojang (owner account)")
    parser.add_argument("--java-home", help="JDK 25 home for horizon-prepare (auto-detected)")
    parser.add_argument("--stage", choices=["inspect", "prepare", "launch", "all"], default="all")
    parser.add_argument("--skip-prepare", action="store_true", help="do not prepare, only launch")
    parser.add_argument("--no-complete", action="store_true", help="do not pass --complete to the preparer")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fullscreen", action="store_true")
    parser.add_argument("--quick-play-world", help="open this world of saves/ at start")
    parser.add_argument("--join", help="join HOST[:PORT] at start")
    parser.add_argument("--frames", type=int, help="exit after N frames (real client smoke test)")
    parser.add_argument("--smoke-report", help="ask the client for a JSON smoke report at this path")
    parser.add_argument("--prepare-timeout", type=float, default=3600.0)
    parser.add_argument("--launch-timeout", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="print the report as JSON on stdout")
    parser.add_argument("--json-out", help="also write the report to this file")
    parser.add_argument("--dry-run", action="store_true", help="print the commands, change nothing")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version="%(prog)s " + SPIKE_VERSION)
    parser.add_argument("--list-instances", action="store_true", help="list instances and exit")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_instances:
        dirs = [args.prism_dir] if args.prism_dir else default_prism_dirs()
        if not dirs:
            eprint("no Prism Launcher folder found")
            return 2
        for inst in list_instances(dirs[0]):
            print("%-24s %-8s %s" % (inst.name, inst.mc_version or "?", inst.root))
        return 0

    report = make_report(args)
    try:
        instance, files, api = cmd_inspect(args, report)

        if args.stage in ("prepare", "all"):
            cmd_prepare(args, report, instance, files, api)

        if args.stage in ("launch", "all"):
            cmd_launch(args, report, instance, api)

        report["ok"] = True
        eprint("done     : %s" % ("dry run" if args.dry_run else "success"))
        code = 0
    except SpikeError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        eprint("error    : %s" % exc)
        code = exc.code
    except KeyboardInterrupt:
        report["ok"] = False
        report["error"] = "interrupted"
        eprint("interrupted")
        code = 1

    if args.json_out:
        write_json(Path(args.json_out).expanduser(), report)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
