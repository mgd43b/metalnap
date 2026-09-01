"""IPMI power control via ipmitool. The reference PowerBackend."""
import subprocess


class IpmiPower:
    def __init__(self, host_for, user, password, timeout=30):
        #: callable: node name -> BMC hostname. Keeps naming policy out here.
        self.host_for = host_for
        self.user, self.password, self.timeout = user, password, timeout

    def _run(self, name, *args):
        cmd = ["ipmitool", "-I", "lanplus", "-H", self.host_for(name),
               "-U", self.user, "-P", self.password, *args]
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=self.timeout, check=True).stdout

    def state(self, name):
        out = self._run(name, "chassis", "power", "status").lower()
        return "on" if "is on" in out else "off"

    def on(self, name):
        self._run(name, "chassis", "power", "on")

    def soft_off(self, name):
        # `soft` asks the OS to shut down, so filesystems flush and the kubelet
        # deregisters. Never `power off`, which cuts the rail underneath a
        # running machine.
        self._run(name, "chassis", "power", "soft")
