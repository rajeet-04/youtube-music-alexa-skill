"""Check browser services without opening a VNC client on every probe."""

import urllib.request


def vnc_listening():
    # The container shares the VPN network namespace; 0A is TCP LISTEN.
    with open('/proc/net/tcp', encoding='ascii') as connections:
        return any(
            fields[1].endswith(':170C') and fields[3] == '0A'
            for line in connections
            if len(fields := line.split()) >= 4
        )


if __name__ == '__main__':
    with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=2):
        pass
    if not vnc_listening():
        raise SystemExit('VNC is not listening on port 5900')
    with urllib.request.urlopen('http://127.0.0.1:6080/vnc.html', timeout=2):
        pass
