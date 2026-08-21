#!/usr/bin/env python3
"""Watch radiod's control commands and RTP audio on one timeline.

Every silent failure in this project looks the same from one side: no audio.
Seeing only the commands, you cannot tell whether radiod ignored them. Seeing
only the RTP, you cannot tell what was asked for. Watching both together
separates "radiod never sent it" from "we never received it", which is the
first question worth answering and the one that is easy to guess wrong.

It earned its place: after five wrong hypotheses about wideband FM, one
capture showed

    [20.30] CMD  PRESET=wfm ... OUTPUT_SSRC=588328991
    [20.30] RTP  ssrc=588328991 (1 so far)
    [20.66] CMD  RADIO_FREQUENCY=162400000  OUTPUT_SSRC=1073068374

-- the channel started streaming immediately, and the next tune began 0.36 s
later. The app was fine; the test harness was giving up mid-tune.

Usage:
    scripts/sniff_radiod.py [--host HOST] [--group GROUP] [--seconds N]
                           [--all-status]

    --host       radiod status address (default airspyhf-status.local); its
                 commands are decoded
    --group      RTP data group to count (default the app's VFO group)
    --seconds    how long to watch (default 60)
    --all-status show radiod's STATUS packets too; off by default because
                 they drown the commands

Passive: it joins multicast groups and reads. It sends nothing and creates no
channels, so it is safe to run against a receiver someone is listening to.
"""
import argparse
import collections
import os
import select
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ka9q import generate_multicast_ip                       # noqa: E402
from ka9q.types import StatusType                            # noqa: E402
from ka9q.utils import resolve_multicast_address             # noqa: E402

TAG_NAME = {int(v): k for k, v in vars(StatusType).items() if isinstance(v, int)}

# The tags worth reading at a glance. Everything else is noise when the
# question is "what did the client ask for, and did radiod answer?".
INTERESTING = ("RADIO_FREQUENCY", "OUTPUT_SSRC", "PRESET", "DEMOD_TYPE",
               "OUTPUT_ENCODING", "OUTPUT_SAMPRATE", "LIFETIME", "LOW_EDGE",
               "HIGH_EDGE", "SNR_SQUELCH", "BIN_COUNT", "RESOLUTION_BW")


def join(group: str, port: int) -> socket.socket:
    """Join `group` on EVERY local interface.

    Joining with INADDR_ANY lets the kernel pick one, normally the
    default-route interface, which misses a co-located radiod running ttl=0
    (its packets never leave `lo`) and a radiod reached over a secondary
    interface. Both fail identically and silently.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    addrs = subprocess.run(["hostname", "-I"], capture_output=True,
                           text=True).stdout.split()
    for addr in addrs + ["127.0.0.1"]:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            socket.inet_aton(group) + socket.inet_aton(addr))
        except OSError:
            pass
    sock.setblocking(False)
    return sock


def decode(buf: bytes, want_status: bool):
    """Decode one status/command packet into readable tag=value pairs.

    Wire format: first byte 1 = CMD (client to radiod), 0 = STATUS (radiod to
    everyone). Then TLVs -- tag byte, length byte, value -- until tag 0.
    """
    if len(buf) < 2:
        return None
    kind = "CMD " if buf[0] == 1 else "STAT"
    if kind == "STAT" and not want_status:
        return None
    i, out = 1, []
    while i < len(buf):
        tag = buf[i]; i += 1
        if tag == 0:
            break
        if i >= len(buf):
            break
        length = buf[i]; i += 1
        value = buf[i:i + length]; i += length
        name = TAG_NAME.get(tag, f"tag{tag}")
        if name not in INTERESTING:
            continue
        if name == "PRESET":
            try:
                shown = value.decode("ascii").strip("\x00")
            except UnicodeDecodeError:
                shown = value.hex()
        elif length == 8:
            shown = f"{struct.unpack('!d', value)[0]:.0f}"
        elif length == 4 and name in ("RESOLUTION_BW", "LOW_EDGE", "HIGH_EDGE"):
            shown = f"{struct.unpack('!f', value)[0]:.1f}"
        else:
            shown = str(int.from_bytes(value, "big"))
        out.append(f"{name}={shown}")
    return (kind, out) if out else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="airspyhf-status.local")
    ap.add_argument("--group", default=None,
                    help="RTP data group (default: the app's VFO group)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--all-status", action="store_true")
    args = ap.parse_args()

    try:
        ctl_group = resolve_multicast_address(args.host, timeout=5.0)
    except Exception as exc:
        print(f"cannot resolve {args.host}: {exc}", file=sys.stderr)
        return 1
    rtp_group = (args.group or
                 generate_multicast_ip("radiod-monitor-vfo")).split(":")[0]

    ctl = join(ctl_group, 5006)
    rtp = join(rtp_group, 5004)
    print(f"commands on {ctl_group}:5006   RTP on {rtp_group}:5004   "
          f"{args.seconds:.0f}s")
    print("(passive -- nothing is sent and no channels are created)\n")

    counts: collections.Counter = collections.Counter()
    announced: dict = {}
    start = time.time()
    while time.time() - start < args.seconds:
        ready, _, _ = select.select([ctl, rtp], [], [], 0.5)
        now = time.time() - start
        for sock in ready:
            try:
                data, _ = sock.recvfrom(9000)
            except OSError:
                continue
            if sock is ctl:
                decoded = decode(data, args.all_status)
                if decoded:
                    kind, fields = decoded
                    print(f"[{now:7.2f}] {kind}  " + "  ".join(fields))
            else:
                if len(data) < 12:
                    continue
                ssrc = struct.unpack("!I", data[8:12])[0]
                counts[ssrc] += 1
                # One line when a stream starts, then a heartbeat, so a
                # steady stream does not bury the commands.
                if counts[ssrc] == 1 or now - announced.get(ssrc, -99) > 2.0:
                    print(f"[{now:7.2f}] RTP   ssrc={ssrc} "
                          f"({counts[ssrc]} packets so far)")
                    announced[ssrc] = now

    print("\nRTP packets by SSRC:")
    for ssrc, n in counts.most_common():
        print(f"  {ssrc:11d}  {n:6d}  ({n / args.seconds:.1f}/s -- "
              f"continuous audio is ~50/s for 20 ms frames)")
    if not counts:
        print("  (none -- radiod sent no audio on that group)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
