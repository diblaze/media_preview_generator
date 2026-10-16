"""Worker device → PCI address → WebGPU EP device."""

import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits import devices


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00000000:02:00.0", "0000:02:00.0"),
        ("0000:0A:1F.3", "0000:0a:1f.3"),
        ("02:00.0", "0000:02:00.0"),
        ("0001:65:00.1", "0001:65:00.1"),
        ("", None),
        (None, None),
        ("pci-0000:02:00.0", None),
        ("0000:02:00.8", None),
    ],
)
def test_normalise_pci_bus_id(value, expected):
    assert devices.normalise_pci_bus_id(value) == expected


class TestWorkerPci:
    def setup_method(self):
        devices.forget_nvidia_pci_bus_ids()

    def teardown_method(self):
        devices.forget_nvidia_pci_bus_ids()

    def _smi(self, stdout, returncode=0):
        return patch.object(
            devices.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout)
        )

    def test_nvidia_index_maps_through_nvidia_smi(self):
        with self._smi("0, 00000000:02:00.0\n1, 00000000:65:00.0\n") as run:
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:1") == "0000:65:00.0"
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") == "0000:02:00.0"
        assert run.call_args.args[0] == ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"]
        # This runs inside the pool's per-device lock, so a wedged nvidia-smi would park that worker thread and
        # queue every other worker on the same GPU behind it.
        assert run.call_args.kwargs["timeout"] == 5
        assert run.call_args.kwargs["capture_output"] is True and run.call_args.kwargs["text"] is True
        assert run.call_count == 1  # cached for the process

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [("0, 00000000:02:00.0\n", "0000:02:00.0"), ("0, 00000000:02:00.0\n1, 00000000:65:00.0\n", None)],
    )
    def test_nvidia_without_an_index_is_known_only_on_a_one_gpu_host(self, stdout, expected):
        with self._smi(stdout):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda") == expected

    def test_nvidia_smi_failing_gives_none(self):
        with patch.object(devices.subprocess, "run", side_effect=OSError("nope")):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") is None

    def test_nvidia_smi_exiting_non_zero_gives_none(self):
        with self._smi("0, 00000000:02:00.0\n", returncode=9):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") is None

    @pytest.mark.parametrize(
        "failure",
        [
            {"side_effect": subprocess.TimeoutExpired("nvidia-smi", 5)},
            {"return_value": SimpleNamespace(returncode=9, stdout="")},
        ],
        ids=["timed-out", "exited-non-zero"],
    )
    def test_a_failed_read_is_not_kept(self, failure):
        # A busy host can time nvidia-smi out once. Kept for the process, that one miss would leave every GPU helper
        # started later without an address (the CPU, on a host whose EP lists several devices) until a restart.
        with patch.object(devices.subprocess, "run", **failure):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:1") is None
        with self._smi("0, 00000000:02:00.0\n1, 00000000:65:00.0\n") as run:
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:1") == "0000:65:00.0"
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") == "0000:02:00.0"
        assert run.call_count == 1  # a good answer is kept

    def test_workers_asking_at_once_share_one_read(self):
        answered = threading.Event()
        calls = []

        def slow_smi(*args, **kwargs):
            calls.append(args)
            answered.wait(5)
            return SimpleNamespace(returncode=0, stdout="0, 00000000:02:00.0\n")

        results = []
        with patch.object(devices.subprocess, "run", side_effect=slow_smi):
            threads = [
                threading.Thread(target=lambda: results.append(devices.worker_pci_bus_id("NVIDIA", "cuda:0")))
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            time.sleep(0.2)  # every thread is inside the read, or waiting for it
            answered.set()
            for thread in threads:
                thread.join(5)
        assert results == ["0000:02:00.0"] * 3
        assert len(calls) == 1

    def test_workers_waiting_on_a_failed_read_share_it_instead_of_each_reading_again(self):
        answered = threading.Event()
        calls = []

        def slow_failure(*args, **kwargs):
            calls.append(args)
            answered.wait(5)
            raise subprocess.TimeoutExpired("nvidia-smi", 5)

        results = []
        with patch.object(devices.subprocess, "run", side_effect=slow_failure):
            threads = [
                threading.Thread(target=lambda: results.append(devices.worker_pci_bus_id("NVIDIA", "cuda:0")))
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            time.sleep(0.2)  # one thread is inside the read, the others wait for it
            answered.set()
            for thread in threads:
                thread.join(5)
        assert results == [None] * 3
        assert len(calls) == 1  # not three 5 s timeouts one after another

    @pytest.mark.parametrize("address", ["0000:00:02.0", "0000:0a:00.0", "0000:c1:00.0"])
    def test_render_node_maps_through_sysfs_with_hex_buses(self, tmp_path, monkeypatch, address):
        device = tmp_path / "devices" / "pci0000:00" / "0000:00:01.1" / address
        device.mkdir(parents=True)
        node = tmp_path / "drm" / "renderD129"
        node.mkdir(parents=True)
        (node / "device").symlink_to(device)
        monkeypatch.setattr(devices, "SYSFS_DRM", str(tmp_path / "drm"))
        assert devices.worker_pci_bus_id("AMD", "/dev/dri/renderD129") == address
        assert devices.worker_pci_bus_id("INTEL", "/dev/dri/renderD128") is None  # no such node in the fake tree

    @pytest.mark.parametrize(("gpu", "path"), [(None, None), ("APPLE", "videotoolbox"), ("WINDOWS_GPU", "d3d11va")])
    def test_others_have_none(self, gpu, path):
        assert devices.worker_pci_bus_id(gpu, path) is None


# An EP list as measured on a host with one NVIDIA card and a board's BMC VGA.
NVIDIA_CARD = {"Discrete": "1", "card_idx": "0", "pci_bus_id": "0000:02:00.0"}
BMC = {"card_idx": "1", "pci_bus_id": "0000:07:00.0"}
IGPU = {"card_idx": "0", "pci_bus_id": "0000:00:02.0"}


@pytest.mark.parametrize(
    ("metadatas", "pci", "expected"),
    [
        ([], "0000:02:00.0", None),
        ([NVIDIA_CARD, BMC], "0000:02:00.0", 0),
        ([BMC, NVIDIA_CARD], "0000:02:00.0", 1),
        ([NVIDIA_CARD, IGPU], "0000:00:02.0", 1),  # the Intel worker's helper
        ([NVIDIA_CARD, BMC], "0000:65:00.0", None),  # no device is this worker's GPU
        ([NVIDIA_CARD, BMC], None, None),  # address unknown, several devices
        ([NVIDIA_CARD], None, 0),  # address unknown, one device
        ([NVIDIA_CARD], "0000:65:00.0", None),  # the only device is another GPU
        ([{}], "0000:02:00.0", 0),  # one device without an address
        ([{}, {}], "0000:02:00.0", None),
        ([{"pci_bus_id": "0000:02:00.0"}, {"pci_bus_id": "0000:02:00.0"}], "0000:02:00.0", None),
    ],
)
def test_choose_ep_device(metadatas, pci, expected):
    assert devices.choose_ep_device(metadatas, pci) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [(NVIDIA_CARD, "0000:02:00.0"), ({}, None), ({"pci_bus_id": "not a bus id"}, None)],
)
def test_ep_device_pci_bus_id(metadata, expected):
    assert devices.ep_device_pci_bus_id(metadata) == expected


def test_a_render_node_whose_sysfs_path_holds_no_address_gives_none(tmp_path, monkeypatch):
    device = tmp_path / "devices" / "platform" / "gpu"  # an SoC GPU: no PCI address anywhere in the path
    device.mkdir(parents=True)
    node = tmp_path / "drm" / "renderD128"
    node.mkdir(parents=True)
    (node / "device").symlink_to(device)
    monkeypatch.setattr(devices, "SYSFS_DRM", str(tmp_path / "drm"))
    assert devices.drm_pci_bus_id("/dev/dri/renderD128") is None


@pytest.mark.parametrize(
    ("pci_bus_id", "expected"),
    [
        ("0000:02:00.0", "pci-0000_02_00_0"),
        ("0001:0a:1f.3", "pci-0001_0a_1f_3"),
        ("02:00.0", "pci-0000_02_00_0"),
        ("00000000:65:00.0", "pci-0000_65_00_0"),
        ("not a bus id", None),
        (None, None),
    ],
)
def test_dri_prime_tag(pci_bus_id, expected):
    assert devices.dri_prime_tag(pci_bus_id) == expected


class TestPinEnvToGpu:
    """The only thing that puts a helper process on one physical GPU."""

    def test_an_address_sets_the_tag_and_the_force_flag(self):
        pinned = devices.pin_env_to_gpu({"PATH": "/usr/bin"}, "0000:65:00.0")
        assert pinned == {
            "PATH": "/usr/bin",
            "DRI_PRIME": "pci-0000_65_00_0",
            "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
        }

    def test_two_addresses_give_two_different_pins(self):
        first = devices.pin_env_to_gpu({}, "0000:02:00.0")
        second = devices.pin_env_to_gpu({}, "0000:65:00.0")
        assert first["DRI_PRIME"] != second["DRI_PRIME"]

    @pytest.mark.parametrize("pci_bus_id", ["0000:02:00.0", None])
    def test_an_inherited_selection_is_always_dropped(self, pci_bus_id):
        inherited = {
            "MESA_VK_DEVICE_SELECT": "10005:0",
            "NODEVICE_SELECT": "1",
            "DRI_PRIME": "pci-0000_99_00_0",
            "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
            "KEEP": "me",
        }
        pinned = devices.pin_env_to_gpu(inherited, pci_bus_id)
        assert "MESA_VK_DEVICE_SELECT" not in pinned and "NODEVICE_SELECT" not in pinned
        assert pinned["KEEP"] == "me"
        assert pinned.get("DRI_PRIME") == (None if pci_bus_id is None else "pci-0000_02_00_0")
        assert pinned.get("MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE") == (None if pci_bus_id is None else "1")

    def test_the_caller_s_environment_is_left_alone(self):
        original = {"MESA_VK_DEVICE_SELECT": "10005:0"}
        devices.pin_env_to_gpu(original, "0000:02:00.0")
        assert original == {"MESA_VK_DEVICE_SELECT": "10005:0"}


class TestI915GucSubmission:
    """Whether i915 runs a GPU's work through GuC, read from sysfs the way a container sees it."""

    @staticmethod
    def _device(tmp_path, *, driver="i915", gt_files=(), with_gt=True):
        drivers = tmp_path / "drivers" / driver
        drivers.mkdir(parents=True)
        device = tmp_path / "devices" / "0000:00:02.0"
        device.mkdir(parents=True)
        (device / "driver").symlink_to(drivers)
        if with_gt:
            gt = device / "drm" / "card1" / "gt" / "gt0"
            gt.mkdir(parents=True)
            for name in ("rps_act_freq_mhz", *gt_files):
                (gt / name).write_text("0")
        return str(tmp_path / "devices")

    @pytest.fixture(autouse=True)
    def _new_kernel(self, monkeypatch):
        monkeypatch.setattr(devices.platform, "release", lambda: "6.18.54-Unraid")

    def test_an_i915_gpu_with_guc_slpc_files_runs_with_guc(self, tmp_path):
        sysfs = self._device(tmp_path, gt_files=("slpc_ignore_eff_freq", "slpc_power_profile"))
        assert devices.i915_guc_submission("0000:00:02.0", sysfs_pci=sysfs) is True

    def test_an_i915_gpu_without_them_runs_on_execlists(self, tmp_path):
        sysfs = self._device(tmp_path)
        assert devices.i915_guc_submission("0000:00:02.0", sysfs_pci=sysfs) is False

    def test_the_xe_driver_is_not_asked_about(self, tmp_path):
        sysfs = self._device(tmp_path, driver="xe")
        assert devices.i915_guc_submission("0000:00:02.0", sysfs_pci=sysfs) is None

    def test_an_i915_gpu_whose_sysfs_has_no_gt_folder_is_unknown(self, tmp_path):
        sysfs = self._device(tmp_path, with_gt=False)
        assert devices.i915_guc_submission("0000:00:02.0", sysfs_pci=sysfs) is None

    @pytest.mark.parametrize(("kernel", "expected"), [("6.4.16", None), ("6.5.0-1-pve", False), ("7.0.0-38", False)])
    def test_a_kernel_older_than_the_slpc_file_is_unknown(self, tmp_path, kernel, expected):
        # Before Linux 6.5 i915 never made the file, so its absence doesn't mean execlists.
        sysfs = self._device(tmp_path)
        assert devices.i915_guc_submission("0000:00:02.0", sysfs_pci=sysfs, kernel=kernel) is expected

    @pytest.mark.parametrize("pci_bus_id", [None, "0000:09:00.0"])
    def test_no_address_or_an_unknown_device_is_unknown(self, tmp_path, pci_bus_id):
        sysfs = self._device(tmp_path)
        assert devices.i915_guc_submission(pci_bus_id, sysfs_pci=sysfs) is None


@pytest.mark.parametrize(
    ("inherited", "expected"),
    [(None, "sync"), ("nocompact", "nocompact,sync"), ("sync", "sync"), ("nocompact,sync", "nocompact,sync")],
)
def test_sync_intel_submissions_adds_sync_once_and_keeps_other_flags(inherited, expected):
    env = {"KEEP": "me"} if inherited is None else {"KEEP": "me", "INTEL_DEBUG": inherited}
    assert devices.sync_intel_submissions(env) == {"KEEP": "me", "INTEL_DEBUG": expected}
