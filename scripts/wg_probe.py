#!/usr/bin/env python3
"""Pick a WireGuard server that actually accepts our peer.

Surfshark's hostnames rotate across many server addresses and only some answer a given key.
This sends one handshake initiation to every resolved address and prints the responders,
fastest first (one IP per line). Diagnostics go to stderr; key material is never printed.

    wg_probe.py <wg-conf> [--skip IP]...      exit 0 = at least one responder, 3 = none

Needs `cryptography` (already in the ytmusic image).
"""
import base64, configparser, hashlib, hmac, os, socket, struct, sys, time
from concurrent.futures import ThreadPoolExecutor
from cryptography.hazmat.primitives import serialization as S
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

TRIES, WAIT = 3, 2.0


def _h(*parts): return hashlib.blake2s(b"".join(parts)).digest()
def _hmac(key, data): return hmac.new(key, data, hashlib.blake2s).digest()
def _raw(pub): return pub.public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)
def _dh(priv, pub): return priv.exchange(X25519PublicKey.from_public_bytes(pub))
def _seal(key, ad, plain): return ChaCha20Poly1305(key).encrypt(b"\0" * 12, plain, ad)


def _kdf(chain, data, n):
    t0, prev, out = _hmac(chain, data), b"", []
    for i in range(1, n + 1):
        prev = _hmac(t0, prev + bytes([i]))
        out.append(prev)
    return out


def _initiation(spriv, spub, rpub):
    c = _h(b"Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s")
    h = _h(_h(c, b"WireGuard v1 zx2c4 Jason@zx2c4.com"), rpub)
    eph = X25519PrivateKey.generate()
    epub = _raw(eph.public_key())
    c = _kdf(c, epub, 1)[0]
    h = _h(h, epub)
    c, k = _kdf(c, _dh(eph, rpub), 2)
    static = _seal(k, h, spub)
    h = _h(h, static)
    c, k = _kdf(c, _dh(spriv, rpub), 2)
    stamp = _seal(k, h, struct.pack(">QI", int(time.time()) + (1 << 62) + 10, 0))
    msg = struct.pack("<I", 1) + os.urandom(4) + epub + static + stamp
    mac1 = hashlib.blake2s(msg, digest_size=16, key=_h(b"mac1----", rpub)).digest()
    return msg + mac1 + b"\0" * 16


def _probe(ip, port, spriv, spub, rpub):
    for _ in range(TRIES):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(WAIT)
        start = time.monotonic()
        try:
            sock.sendto(_initiation(spriv, spub, rpub), (ip, port))
            data, _ = sock.recvfrom(2048)
            if len(data) == 92 and data[0] == 2:
                return time.monotonic() - start
        except OSError:
            pass
        finally:
            sock.close()
    return None


RESOLVERS = ("1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222")


def _dns_a(host, server, timeout=2.0):
    """Minimal UDP DNS A lookup against one resolver (bypasses the local cache)."""
    qname = b"".join(bytes([len(part)]) + part.encode() for part in host.split(".")) + b"\0"
    query = os.urandom(2) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + qname + b"\x00\x01\x00\x01"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(query, (server, 53))
        data, _ = sock.recvfrom(4096)
    except OSError:
        return []
    finally:
        sock.close()

    def skip_name(i):
        while i < len(data):
            n = data[i]
            if n & 0xC0 == 0xC0:
                return i + 2
            if n == 0:
                return i + 1
            i += 1 + n
        return i

    out, i = [], skip_name(12) + 4
    for _ in range(struct.unpack(">H", data[6:8])[0]):
        i = skip_name(i)
        rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if rtype == 1 and rdlen == 4:
            out.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return out


def _resolve(host, port, rounds=4):
    found = set()
    try:
        found.update(a[4][0] for a in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM))
    except OSError:
        pass
    for _ in range(rounds):  # the name rotates; several resolvers x rounds cover most of the pool
        for server in RESOLVERS:
            found.update(_dns_a(host, server))
        time.sleep(0.2)
    return sorted(found)


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    skip = {argv[i + 1] for i, a in enumerate(argv) if a == "--skip" and i + 1 < len(argv)}
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(argv[0])
    sk = base64.b64decode(cp["Interface"]["PrivateKey"].strip())
    rpub = base64.b64decode(cp["Peer"]["PublicKey"].strip())
    host, port = cp["Peer"]["Endpoint"].strip().rsplit(":", 1)
    port = int(port)
    spriv = X25519PrivateKey.from_private_bytes(sk)
    spub = _raw(spriv.public_key())
    ips = [host] if host.replace(".", "").isdigit() else _resolve(host, port)
    ips = [ip for ip in ips if ip not in skip]
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(ips)))) as pool:
        times = list(pool.map(lambda ip: _probe(ip, port, spriv, spub, rpub), ips))
    good = sorted((t, ip) for ip, t in zip(ips, times) if t is not None)
    print(f"wg_probe: {len(good)}/{len(ips)} addresses answered", file=sys.stderr)
    for _, ip in good:
        print(ip)
    return 0 if good else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
