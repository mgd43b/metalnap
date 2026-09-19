"""IPMI power control via ipmitool. The reference PowerBackend."""
import os
import subprocess


class IpmiPower:
    def __init__(self, host_for, user, password, timeout=30):
        #: callable: node name -> BMC hostname. Keeps naming policy out here.
        self.host_for = host_for
        self.user, self.password, self.timeout = user, password, timeout

    def _run(self, name, *args):
        # The password travels in the ENVIRONMENT (-E), never in argv. On the
        # command line it was in every exception subprocess raised -- whose
        # text went on to log lines, Alertmanager and a node annotation any
        # `get nodes` can read -- and in /proc/<pid>/cmdline besides.
        cmd = ["ipmitool", "-I", "lanplus", "-H", self.host_for(name),
               "-U", self.user, "-E", *args]
        env = dict(os.environ, IPMI_PASSWORD=self.password,
                   IPMITOOL_PASSWORD=self.password)
        what = "ipmitool %s on %s" % (" ".join(args), name)
        # Re-raised with text we wrote: nothing below may quote the command.
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, env=env,
                               timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("%s timed out after %ss"
                               % (what, self.timeout)) from None
        if r.returncode != 0:
            raise RuntimeError("%s failed (exit %d): %s"
                               % (what, r.returncode,
                                  (r.stderr or "").strip()[:200]))
        return r.stdout

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

    def cycle(self, name):
        # `cycle`, not `reset`: reset restarts the CPUs with the rails still
        # up, and a machine wedged hard enough to need this one is wedged
        # hard enough that only dropping power clears it -- which is what the
        # operator who recovered the first one by hand actually ran.
        self._run(name, "chassis", "power", "cycle")
