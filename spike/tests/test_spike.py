#!/usr/bin/env python3
"""End-to-end tests for the M0 spike, using the stub Horizon binaries.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPIKE = HERE.parent / "horizon_prism_spike.py"
STUBS = HERE.parent / "stubs"
CLIENT = STUBS / "horizon-client"
PREPARE = STUBS / "horizon-prepare"
SPIKE_CMD = [sys.executable, str(SPIKE)]


class Fixture:
    """A Prism-shaped data folder with one instance and its meta files."""

    def __init__(self, root: Path, mc_version: str = "26.3") -> None:
        self.root = root
        self.mc_version = mc_version
        self.prism = root / "prism"
        self.instance = self.prism / "instances" / "Test"
        self.record = root / "record"
        self.report = root / "report.json"
        self.game = self.instance / "minecraft"

        self.game.mkdir(parents=True)
        (self.instance / "instance.cfg").write_text(
            "[General]\n"
            "InstanceType=OneSix\n"
            "name=Test\n"
            "ManagedPackName=Fabulously Optimized\n"
            "JavaPath=/nonexistent/bin/java\n"
        )
        (self.instance / "mmc-pack.json").write_text(
            json.dumps(
                {
                    "formatVersion": 1,
                    "components": [
                        {"uid": "net.minecraft", "version": mc_version, "important": True},
                        {"uid": "net.fabricmc.fabric-loader", "version": "0.20.1"},
                    ],
                }
            )
        )

        meta = self.prism / "meta" / "net.minecraft"
        meta.mkdir(parents=True)
        (meta / ("%s.json" % mc_version)).write_text(
            json.dumps(
                {
                    "id": mc_version,
                    "name": mc_version,
                    "assetIndex": {"id": "34", "sha1": "0", "size": 1},
                    "libraries": [],
                    "mainJar": {
                        "name": "com.mojang:minecraft:%s:client" % mc_version,
                        "downloads": {"artifact": {"url": "https://example.invalid/client.jar"}},
                    },
                }
            )
        )

        jar = (
            self.prism
            / "libraries"
            / "com"
            / "mojang"
            / "minecraft"
            / mc_version
            / ("minecraft-%s-client.jar" % mc_version)
        )
        jar.parent.mkdir(parents=True)
        jar.write_bytes(b"stub client jar")
        (self.prism / "libraries" / "com" / "example").mkdir(parents=True)

        assets = self.prism / "assets" / "indexes"
        assets.mkdir(parents=True)
        (assets / "34.json").write_text(json.dumps({"objects": {}}))

        self.server_jar = root / "server.jar"
        self.server_jar.write_bytes(b"stub server jar")

        self.session = root / "session.json"
        self.session.write_text(
            json.dumps(
                {
                    "name": "TestPlayer",
                    "uuid": "069A79F4-44E9-4726-A5BE-FCA90E38AAF5",
                    "access_token": "secret-token",
                    "user_type": "msa",
                }
            )
        )

    def run(self, *extra: str, env=None, session=True):
        cmd = SPIKE_CMD + [
            "--prism-dir",
            str(self.prism),
            "--instance",
            "Test",
            "--client",
            str(CLIENT),
            "--prepare",
            str(PREPARE),
            "--server-jar",
            str(self.server_jar),
            "--json-out",
            str(self.report),
        ]
        if session:
            cmd += ["--session-json", str(self.session)]
        cmd += list(extra)
        full_env = dict(os.environ)
        full_env["STUB_CLIENT_RECORD"] = str(self.record)
        full_env.pop("STUB_CLIENT_EXIT", None)
        if env:
            full_env.update(env)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=full_env)

    def report_data(self):
        return json.loads(self.report.read_text())


class SpikeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="horizon-spike-"))
        self.fx = Fixture(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_run_prepares_with_prism_files_and_launches(self):
        result = self.fx.run("--stage", "all")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.fx.report_data()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["instance"]["mc_version"], "26.3")

        prepared = json.loads((self.fx.instance / "resources" / "prepared.json").read_text())
        # The preparer reads Prism's layout directly: no individual paths, no
        # server jar, no re-download of anything.
        self.assertEqual(prepared["prism_dir"], str(self.fx.prism))
        for absent in ("version_json", "client_jar", "server_jar"):
            self.assertIsNone(prepared[absent], absent)
        self.assertTrue(prepared["complete"])
        self.assertEqual(report["prepare"]["prism_dir"], str(self.fx.prism))

        launch = json.loads((self.fx.record / "launch.json").read_text())
        self.assertEqual(launch["session"]["name"], "TestPlayer")
        self.assertEqual(launch["session"]["uuid"], "069a79f444e94726a5befca90e38aaf5")
        self.assertTrue(launch["session"]["has_token"])
        for flag in ("--resources", "--game-dir", "--home", "--session-stdin"):
            self.assertIn(flag, launch["argv"])
        self.assertEqual(report["launch"]["exit"], 0)

    def test_old_preparer_falls_back_to_explicit_paths(self):
        result = self.fx.run(env={"STUB_PREPARE_NO_PRISM_DIR": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads((self.fx.instance / "resources" / "prepared.json").read_text())
        self.assertIsNone(prepared["prism_dir"])
        self.assertEqual(prepared["version_json"], str(self.fx.prism / "meta/net.minecraft/26.3.json"))
        self.assertEqual(
            prepared["client_jar"],
            str(
                self.fx.prism
                / "libraries/com/mojang/minecraft/26.3/minecraft-26.3-client.jar"
            ),
        )
        self.assertEqual(prepared["libraries_dir"], str(self.fx.prism / "libraries"))
        self.assertEqual(prepared["assets_dir"], str(self.fx.prism / "assets"))
        self.assertEqual(prepared["server_jar"], str(self.fx.server_jar.resolve()))

    def test_player_dir_is_passed_when_asked(self):
        player = self.tmp / "player"
        result = self.fx.run("--skip-prepare", "--player-dir", str(player))
        self.assertEqual(result.returncode, 0, result.stderr)
        launch = json.loads((self.fx.record / "launch.json").read_text())
        self.assertIn("--player-dir", launch["argv"])
        self.assertIn(str(player.resolve()), launch["argv"])
        self.assertTrue(player.is_dir())

    def test_optional_gaps_do_not_force_reprepare_without_complete(self):
        env = {"STUB_PREPARE_OPTIONAL_GAPS": "1"}
        self.assertEqual(self.fx.run("--no-complete", env=env).returncode, 0)
        result = self.fx.run("--no-complete", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.fx.report_data()
        self.assertFalse(report["prepare"]["needed"])
        self.assertFalse(report["prepare"]["ran"])
        self.assertEqual(sorted(report["prepare"]["optional_gaps"]), ["packs", "sounds"])
        self.assertIn("optional extras missing", result.stderr)

    def test_complete_run_closes_optional_gaps(self):
        env = {"STUB_PREPARE_OPTIONAL_GAPS": "1"}
        result = self.fx.run(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.fx.report_data()
        self.assertEqual(report["prepare"]["optional_gaps"], [])
        self.assertTrue(report["prepare"]["ran"])

    def test_second_run_skips_preparation(self):
        self.assertEqual(self.fx.run().returncode, 0)
        result = self.fx.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.fx.report_data()
        self.assertFalse(report["prepare"]["needed"])
        self.assertFalse(report["prepare"]["ran"])

    def test_crash_exit_is_mapped_to_spike_exit_3(self):
        result = self.fx.run(env={"STUB_CLIENT_EXIT": "101"})
        self.assertEqual(result.returncode, 3, result.stderr)
        report = self.fx.report_data()
        self.assertFalse(report["ok"])
        self.assertEqual(report["launch"]["exit"], 101)
        self.assertIsNotNone(report["launch"]["crash_report"])
        self.assertTrue(report["launch"]["crash_report"].endswith("crash-report.txt"))

    def test_wrong_minecraft_version_is_refused(self):
        shutil.rmtree(self.tmp)
        self.tmp = Path(tempfile.mkdtemp(prefix="horizon-spike-"))
        self.fx = Fixture(self.tmp, mc_version="26.2")
        result = self.fx.run("--stage", "inspect")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Horizon plays Minecraft 26.3", result.stderr)

    def test_missing_session_is_refused_before_launch(self):
        result = self.fx.run(session=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no session", result.stderr)
        report = self.fx.report_data()
        self.assertFalse(report["ok"])

    def test_dry_run_changes_nothing(self):
        result = self.fx.run("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.fx.instance / "resources" / ".stub-prepared").exists())
        report = self.fx.report_data()
        self.assertTrue(report["ok"])
        self.assertIn("would run", result.stderr)
        self.assertNotIn("secret-token", result.stderr)
        self.assertTrue(report["launch"]["dry_run"])

    def test_dry_run_without_session_still_shows_the_launch(self):
        result = self.fx.run("--dry-run", session=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("no session", result.stderr)
        self.assertIn("(dry run)", result.stderr)
        report = self.fx.report_data()
        self.assertTrue(report["launch"]["dry_run"])

    def test_json_mode_keeps_stdout_clean(self):
        result = self.fx.run("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        # stdout is the report only; the stub's log line moved to stderr.
        report = json.loads(result.stdout)
        self.assertTrue(report["ok"])
        self.assertIn("stub: hello TestPlayer", result.stderr)


class RealInstallTests(unittest.TestCase):
    def test_list_instances_on_this_machine(self):
        result = subprocess.run(SPIKE_CMD + ["--list-instances"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            self.skipTest("no Prism Launcher folder on this machine")
        self.assertIn("26.3", result.stdout)

    def _horizon_version_dir(self):
        root = Path.home() / "Library/Application Support/horizon-launcher"
        settings = root / "settings.json"
        if not settings.is_file():
            self.skipTest("no Horizon launcher installed")
        selected = json.loads(settings.read_text()).get("selected")
        if not selected:
            self.skipTest("Horizon has no selected version")
        return root / "versions" / selected

    def test_real_binaries_announce_the_contract_the_spike_expects(self):
        version_dir = self._horizon_version_dir()
        client = version_dir / "horizon-client"
        prepare = version_dir / "horizon-prepare"
        if not client.is_file() or not prepare.is_file():
            self.skipTest("Horizon binaries not installed")

        client_api = json.loads(
            subprocess.run([str(client), "--launch-api"], capture_output=True, text=True, timeout=60).stdout
        )
        self.assertEqual(client_api["minecraft"], "26.3")
        self.assertIn("--session-stdin", [o["name"] for o in client_api["options"]])
        self.assertTrue(client_api["crash_report_line"].startswith("Horizon: crash report saved to"))

        prepare_api = json.loads(
            subprocess.run([str(prepare), "--launch-api"], capture_output=True, text=True, timeout=60).stdout
        )
        options = [o["name"] for o in prepare_api["options"]]
        self.assertIn("--prism-dir", options)
        self.assertIn("--check", options)
        self.assertEqual(prepare_api["java_major"], 25)

    def test_spike_finds_the_installed_horizon_client(self):
        listing = subprocess.run(SPIKE_CMD + ["--list-instances"], capture_output=True, text=True, timeout=60)
        if listing.returncode != 0 or not listing.stdout.strip():
            self.skipTest("no Prism instances")
        line = listing.stdout.splitlines()[0]
        name = line[:24].strip()
        result = subprocess.run(
            SPIKE_CMD + ["--instance", name, "--stage", "inspect"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if "Horizon plays Minecraft 26.3" in result.stderr:
            self.skipTest("first instance is not Minecraft 26.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("launch API 0.1.0", result.stderr)

    def test_real_check_on_prepared_resources(self):
        version_dir = self._horizon_version_dir()
        prepare = version_dir / "horizon-prepare"
        resources = version_dir / ".local/horizon-client/26.3"
        if not prepare.is_file() or not resources.is_dir():
            self.skipTest("no prepared Horizon resources")
        result = subprocess.run(
            [str(prepare), "--check", "--output", str(resources)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # 0 = complete and current; 1 = gaps, each named on stdout (a nightly
        # that changed its output_version makes the launcher re-prepare). 2 is
        # a refused command line and must never happen.
        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        if result.returncode == 1:
            self.assertTrue(result.stdout.strip(), "gaps must be explained on stdout")
            self.assertNotIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
