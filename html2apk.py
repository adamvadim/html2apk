#!/usr/bin/env python3
"""
html2apk: turn an HTML app into an installable Android APK in seconds.

It takes a ready-made app shell (shell.apk, built once on GitHub) and swaps in
your page, app name, icon and version, then signs it. No Android SDK needed.

  python3 html2apk.py page.html --name "Magic Orb"
  python3 html2apk.py site.zip  --name "My App" --icon logo.png --out MyApp.apk

The same app name (and the same --secret, if you use one) always gives the same
app ID and the same signing key, so a newer APK installs over the older one as
an update and keeps the app's data.
"""
import argparse, hashlib, hmac, io, os, re, struct, sys, time, unicodedata, zipfile, datetime

from PIL import Image, ImageDraw, ImageFont

PH_ID = b'app.htmlapk.placeholder.'          # start of the placeholder application ID
PH_NAME = 'HTMLAPK_APP_NAME_PLACEHOLDER'
PH_VER = 'HTMLAPK_VERSION_NAME_PLACEHOLDER'
ATTR_VERSION_CODE = 0x0101021b

# ---------------------------------------------------------------- binary XML (AndroidManifest.xml)

def _read_pool(buf, off):
    typ, hsz, size = struct.unpack_from('<HHI', buf, off)
    assert typ == 0x0001, 'string pool expected'
    count, styles, flags, sstart, stystart = struct.unpack_from('<IIIII', buf, off + 8)
    utf8 = bool(flags & 0x100)
    offs = struct.unpack_from('<%dI' % count, buf, off + hsz)
    out = []
    for o in offs:
        p = off + sstart + o
        if utf8:
            n = buf[p]; p += 1
            if n & 0x80: p += 1
            n = buf[p]; p += 1
            if n & 0x80: n = ((n & 0x7f) << 8) | buf[p]; p += 1
            out.append(buf[p:p + n].decode('utf-8'))
        else:
            n = struct.unpack_from('<H', buf, p)[0]; p += 2
            if n & 0x8000: n = ((n & 0x7fff) << 16) | struct.unpack_from('<H', buf, p)[0]; p += 2
            out.append(buf[p:p + n * 2].decode('utf-16-le'))
    return out, flags, styles, size


def _write_pool(strings, flags):
    utf8 = bool(flags & 0x100)
    data = bytearray(); offs = []
    for s in strings:
        offs.append(len(data))
        if utf8:
            b = s.encode('utf-8'); cl = len(s)
            for n in (cl, len(b)):
                data += bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xff])
            data += b + b'\0'
        else:
            b = s.encode('utf-16-le'); n = len(b) // 2
            data += struct.pack('<H', n) if n < 0x8000 else struct.pack('<HH', 0x8000 | (n >> 16), n & 0xffff)
            data += b + b'\0\0'
    while len(data) % 4: data += b'\0'
    hsz = 28; sstart = hsz + 4 * len(strings)
    size = sstart + len(data)
    head = struct.pack('<HHIIIIII', 0x0001, hsz, size, len(strings), 0, flags & ~0x0, sstart, 0)
    return head + struct.pack('<%dI' % len(strings), *offs) + bytes(data)


def patch_strings(chunk_bytes, pool_off, repl):
    """Rewrite the string pool at pool_off, applying repl(str)->str. Returns new bytes and size delta."""
    strings, flags, styles, old_size = _read_pool(chunk_bytes, pool_off)
    if styles:
        raise SystemExit('Unexpected styled strings in the shell; rebuild the shell.')
    new = _write_pool([repl(s) for s in strings], flags)
    out = chunk_bytes[:pool_off] + new + chunk_bytes[pool_off + old_size:]
    return out, len(new) - old_size


def patch_manifest(axml, repl, version_code):
    buf = bytearray(axml)
    typ, hsz, size = struct.unpack_from('<HHI', buf, 0)
    assert typ == 0x0003, 'not a binary XML file'
    buf, delta = patch_strings(bytes(buf), hsz, repl)
    buf = bytearray(buf)
    struct.pack_into('<I', buf, 4, size + delta)
    # walk chunks: find the resource map, then set android:versionCode
    strings = _read_pool(bytes(buf), hsz)[0]
    p = hsz + struct.unpack_from('<I', buf, hsz + 4)[0]
    resmap = []
    while p < len(buf):
        ctype, chsz, csize = struct.unpack_from('<HHI', buf, p)
        if ctype == 0x0180:
            resmap = list(struct.unpack_from('<%dI' % ((csize - chsz) // 4), buf, p + chsz))
        elif ctype == 0x0102:
            ext = p + chsz
            astart, asize, acount = struct.unpack_from('<HHH', buf, ext + 8)
            for i in range(acount):
                a = ext + astart + i * asize
                name = struct.unpack_from('<I', buf, a + 4)[0]
                if name < len(resmap) and resmap[name] == ATTR_VERSION_CODE:
                    struct.pack_into('<I', buf, a + 16, version_code)
        p += csize
    return bytes(buf)


def patch_arsc(arsc, repl):
    buf = bytearray(arsc)
    typ, hsz, size = struct.unpack_from('<HHI', buf, 0)
    assert typ == 0x0002, 'not resources.arsc'
    p = hsz
    out = bytearray(buf[:hsz])
    while p < len(buf):
        ctype, chsz, csize = struct.unpack_from('<HHI', buf, p)
        chunk = bytes(buf[p:p + csize])
        if ctype == 0x0001:  # global value pool (app name could live here)
            chunk, _ = patch_strings(chunk, 0, repl)
        elif ctype == 0x0200:  # package chunk: fixed 128-char name field
            c = bytearray(chunk)
            name = c[12:12 + 256].decode('utf-16-le').split('\0')[0]
            new = repl(name)[:127]
            c[12:12 + 256] = new.encode('utf-16-le').ljust(256, b'\0')
            chunk = bytes(c)
        out += chunk
        p += csize
    struct.pack_into('<I', out, 4, len(out))
    return bytes(out)

# ---------------------------------------------------------------- inputs

def slug(name):
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[^a-z0-9]+', '', s) or 'app'
    if not s[0].isalpha(): s = 'a' + s
    return s[:40]


def read_site(path):
    """Returns {relative path: bytes} for the web app; index.html is the start page."""
    files = {}
    if os.path.isdir(path):
        for d, _, fs in os.walk(path):
            for f in fs:
                full = os.path.join(d, f)
                files[os.path.relpath(full, path).replace(os.sep, '/')] = open(full, 'rb').read()
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if not n.endswith('/') and '__MACOSX' not in n]
            idx = [n for n in names if n.split('/')[-1].lower() == 'index.html']
            htmls = idx or [n for n in names if n.lower().endswith(('.html', '.htm'))]
            if not htmls: raise SystemExit('The zip has no HTML file.')
            start = min(htmls, key=lambda n: n.count('/'))
            base = start.rsplit('/', 1)[0] + '/' if '/' in start else ''
            for n in names:
                if n.startswith(base):
                    files[n[len(base):]] = z.read(n)
            if start[len(base):] != 'index.html':
                files['index.html'] = files.pop(start[len(base):])
    else:
        files['index.html'] = open(path, 'rb').read()
    if 'index.html' not in files:
        h = [k for k in files if k.lower().endswith(('.html', '.htm'))]
        if not h: raise SystemExit('No HTML file found.')
        files['index.html'] = files.pop(sorted(h, key=lambda k: k.count('/'))[0])
    for k in list(files):
        if '..' in k.split('/'): del files[k]
    return files


def page_title(html):
    m = re.search(rb'<title[^>]*>(.*?)</title>', html, re.S | re.I)
    if not m: return None
    t = re.sub(r'\s+', ' ', m.group(1).decode('utf-8', 'ignore')).strip()
    return re.split(r'\s+[|\-–—:·]\s+', t)[0].strip() or None


def theme_color(html):
    m = re.search(rb'<meta[^>]+name=["\']theme-color["\'][^>]*content=["\'](#[0-9a-fA-F]{3,6})', html) or \
        re.search(rb'<meta[^>]+content=["\'](#[0-9a-fA-F]{3,6})["\'][^>]*name=["\']theme-color', html)
    if not m: return None
    c = m.group(1).decode()
    if len(c) == 4: c = '#' + ''.join(ch * 2 for ch in c[1:])
    return c


def html_icon(html, files):
    """A PNG/JPEG/WebP icon referenced by the page (link rel=icon / apple-touch-icon), if any."""
    import base64
    for m in re.finditer(rb'<link[^>]+>', html, re.I):
        tag = m.group(0)
        if not re.search(rb'rel=["\'][^"\']*(icon|apple-touch-icon)', tag, re.I): continue
        h = re.search(rb'href=["\']([^"\']+)', tag)
        if not h: continue
        href = h.group(1).decode('utf-8', 'ignore')
        try:
            if href.startswith('data:image/') and ';base64,' in href and 'svg' not in href[:20]:
                return Image.open(io.BytesIO(base64.b64decode(href.split(',', 1)[1]))).convert('RGBA')
            if href.startswith('data:image/svg'):
                return svg_to_image(href)
            if href in files and not href.endswith('.svg'):
                return Image.open(io.BytesIO(files[href])).convert('RGBA')
        except Exception:
            pass
    return None


def svg_to_image(href):
    try:
        import base64, urllib.parse, cairosvg
        data = href.split(',', 1)[1]
        svg = base64.b64decode(data) if ';base64' in href.split(',', 1)[0] else urllib.parse.unquote(data).encode()
        return Image.open(io.BytesIO(cairosvg.svg2png(bytestring=svg, output_width=512, output_height=512))).convert('RGBA')
    except Exception:
        return None


def letter_icon(name, color):
    """Fallback icon: the app's first letter on a coloured gradient circle."""
    S = 512
    c = tuple(int((color or '#7c3aed')[i:i + 2], 16) for i in (1, 3, 5))
    lum = (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255
    if lum < 0.18:  # very dark theme colour: use a violet accent instead
        c = (124, 58, 237)
    c2 = tuple(max(0, int(v * 0.55)) for v in c)
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    grad = Image.new('RGBA', (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(c[k] * (1 - t) + c2[k] * t) for k in range(3)) + (255,))
    mask = Image.new('L', (S, S), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, S - 1, S - 1), fill=255)
    im.paste(grad, (0, 0), mask)
    letter = (name.strip()[:1] or 'A').upper()
    font = None
    for f in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf']:
        if os.path.exists(f): font = ImageFont.truetype(f, 270); break
    font = font or ImageFont.load_default()
    d = ImageDraw.Draw(im)
    bb = d.textbbox((0, 0), letter, font=font)
    d.text(((S - (bb[2] - bb[0])) / 2 - bb[0], (S - (bb[3] - bb[1])) / 2 - bb[1]), letter, font=font, fill=(255, 255, 255, 255))
    return im


def is_shell_icon(data):
    """The shell's placeholder launcher icons are violet (#8b5cf6) circles; resource paths are shortened, so match by look."""
    try:
        im = Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception:
        return False
    w, h = im.size
    return w == h and w in (48, 72, 96, 144, 192) and im.getpixel((w // 2, h // 2)) == (139, 92, 246, 255)


def fit_icon(src, px, round_=True):
    """Square, centred icon. Opaque square images are cropped to a circle so they look right on any launcher."""
    w, h = src.size; s = min(w, h)
    sq = src.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)).resize((px * 4, px * 4), Image.LANCZOS)
    alpha = sq.getchannel('A')
    corners = [alpha.getpixel(p) for p in [(0, 0), (sq.width - 1, 0), (0, sq.height - 1), (sq.width - 1, sq.height - 1)]]
    if round_ and min(corners) > 200:
        mask = Image.new('L', sq.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, sq.width - 1, sq.height - 1), fill=255)
        sq.putalpha(Image.composite(alpha, Image.new('L', sq.size, 0), mask))
    out = io.BytesIO()
    sq.resize((px, px), Image.LANCZOS).save(out, 'PNG', optimize=True)
    return out.getvalue()

# ---------------------------------------------------------------- signing (APK Signature Scheme v2, RSA-2048)

def _is_prime(n):
    if n < 2: return False
    for sp in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % sp == 0: return n == sp
    d, r = n - 1, 0
    while d % 2 == 0: d //= 2; r += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71):
        x = pow(a, d, n)
        if x in (1, n - 1): continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1: break
        else:
            return False
    return True


def _det_prime(seed, label, bits, e):
    """Deterministic prime from a seed (same seed -> same prime)."""
    ctr = 0
    while True:
        stream = b''.join(hmac.new(seed, label + struct.pack('>II', ctr, i), hashlib.sha512).digest() for i in range(bits // 512 + 1))
        n = int.from_bytes(stream[:bits // 8], 'big') | (3 << (bits - 2)) | 1
        for k in range(0, 20000, 2):
            c = n + k
            if c % e != 1 and _is_prime(c):
                return c
        ctr += 1


def signing_key(package, secret):
    """RSA-2048 key and self-signed certificate derived from the app ID (+ optional secret): same inputs, same key."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    seed = hmac.new(b'html2apk signing key v1', (package + '\n' + (secret or '')).encode(), hashlib.sha512).digest()
    e = 65537
    p = _det_prime(seed, b'p', 1024, e)
    q = _det_prime(seed, b'q', 1024, e)
    if p < q: p, q = q, p
    phi = (p - 1) * (q - 1)
    d = pow(e, -1, phi)
    key = rsa.RSAPrivateNumbers(p, q, d, d % (p - 1), d % (q - 1), pow(q, -1, p), rsa.RSAPublicNumbers(e, p * q)).private_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, package[:64])])
    t0 = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(int.from_bytes(hashlib.sha256(seed).digest()[:16], 'big') >> 1)
            .not_valid_before(t0).not_valid_after(t0 + datetime.timedelta(days=36500))
            .sign(key, hashes.SHA256()))
    return key, cert.public_bytes(serialization.Encoding.DER), \
        key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def lp(b): return struct.pack('<I', len(b)) + b


def sign_v2(apk, key, cert_der, pub_der):
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    eocd = apk.rfind(b'PK\x05\x06')
    cd_size, cd_off = struct.unpack_from('<II', apk, eocd + 12)
    sec1, cd, eo = apk[:cd_off], apk[cd_off:cd_off + cd_size], bytearray(apk[eocd:])

    def chunks(b):
        for i in range(0, len(b), 1 << 20):
            c = b[i:i + (1 << 20)]
            yield hashlib.sha256(b'\xa5' + struct.pack('<I', len(c)) + c).digest()

    def digest(eocd_bytes):
        ds = list(chunks(sec1)) + list(chunks(cd)) + list(chunks(eocd_bytes))
        return hashlib.sha256(b'\x5a' + struct.pack('<I', len(ds)) + b''.join(ds)).digest()

    e2 = bytearray(eo); struct.pack_into('<I', e2, 16, len(sec1))
    dg = digest(bytes(e2))
    ALG = 0x0103  # RSASSA-PKCS1-v1_5 with SHA2-256
    signed = lp(lp(struct.pack('<I', ALG) + lp(dg))) + lp(lp(cert_der)) + lp(b'')
    sig = key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    signer = lp(signed) + lp(lp(struct.pack('<I', ALG) + lp(sig))) + lp(pub_der)
    value = lp(lp(signer))
    pair = struct.pack('<Q', 4 + len(value)) + struct.pack('<I', 0x7109871a) + value
    size = len(pair) + 8 + 16
    block = struct.pack('<Q', size) + pair + struct.pack('<Q', size) + b'APK Sig Block 42'
    struct.pack_into('<I', eo, 16, len(sec1) + len(block))
    return sec1 + block + cd + bytes(eo)

# ---------------------------------------------------------------- build

def build(shell, site, name, out, icon=None, secret=None, app_id=None, version=None, label=None):
    files = read_site(site)
    html = files['index.html']
    name = (name or page_title(html) or 'My App').strip()[:50]
    package = app_id or ('app.h2a.' + slug(name))
    if not re.fullmatch(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+', package):
        raise SystemExit('Invalid app ID: ' + package)
    vcode = int(time.time() // 60) - 29_000_000            # rises every minute, so each build is "newer"
    vname = version or datetime.datetime.now().strftime('%Y.%m.%d-%H%M')

    def repl(s):
        if s.startswith(PH_ID.decode()) or PH_ID.decode() in s:
            s = re.sub(re.escape(PH_ID.decode()) + r'z+', package, s)
        if s.startswith(PH_NAME): s = label or name
        if s.startswith(PH_VER): s = vname
        return s

    src = Image.open(icon).convert('RGBA') if icon else (html_icon(html, files) or letter_icon(name, theme_color(html)))

    zin = zipfile.ZipFile(shell)
    buf = io.BytesIO()
    zout = zipfile.ZipFile(buf, 'w')
    seen_manifest = False

    def add(info_name, data, stored):
        zi = zipfile.ZipInfo(info_name, date_time=(2025, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
        zi.create_system = 0; zi.external_attr = 0
        if stored:  # 4-byte align uncompressed data (needed for resources.arsc on Android 11+)
            here = buf.tell() + 30 + len(info_name.encode())
            pad = (-(here + 4)) % 4
            zi.extra = struct.pack('<HH', 0xd935, pad) + b'\0' * pad
        zout.writestr(zi, data)

    for zi in zin.infolist():
        n = zi.filename
        if n.startswith('META-INF/') and (n.endswith(('.SF', '.RSA', '.EC', '.DSA', '.MF')) or n == 'META-INF/MANIFEST.MF'):
            continue
        if n.startswith('assets/www/'):
            continue
        data = zin.read(n)
        if n == 'AndroidManifest.xml':
            data = patch_manifest(data, repl, vcode); seen_manifest = True
        elif n == 'resources.arsc':
            data = patch_arsc(data, repl)
        elif n.startswith('res/') and n.endswith('.png') and not n.endswith('.9.png') and is_shell_icon(data):
            data = fit_icon(src, Image.open(io.BytesIO(data)).size[0])
        add(n, data, zi.compress_type == zipfile.ZIP_STORED or n == 'resources.arsc')
    for rel, data in sorted(files.items()):
        add('assets/www/' + rel, data, False)
    zout.close()
    assert seen_manifest, 'shell.apk has no manifest'

    key, cert, pub = signing_key(package, secret)
    apk = sign_v2(buf.getvalue(), key, cert, pub)
    with open(out, 'wb') as f: f.write(apk)
    return {'out': out, 'name': label or name, 'id': package, 'version': vname, 'files': len(files), 'size': len(apk)}


def main():
    ap = argparse.ArgumentParser(description='Turn an HTML app into an Android APK.')
    ap.add_argument('site', help='index.html, a .zip of the site, or a folder')
    ap.add_argument('--name', help='app name shown under the icon (default: the page <title>)')
    ap.add_argument('--label', help='text under the icon, e.g. "Magic Orb b4" (default: the name). The app ID still comes from --name, so updates keep working')
    ap.add_argument('--icon', help='PNG/JPG icon (default: the page icon, else a letter icon)')
    ap.add_argument('--out', help='output .apk path')
    ap.add_argument('--secret', help='optional phrase mixed into the signing key; reuse it for updates')
    ap.add_argument('--id', dest='app_id', help='app ID, e.g. com.example.myapp (default: from the name)')
    ap.add_argument('--version', help='version label, e.g. 5.2-build3 (default: date and time); also used in the file name')
    ap.add_argument('--shell', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shell.apk'))
    a = ap.parse_args()
    if not os.path.exists(a.shell):
        raise SystemExit('shell.apk not found next to html2apk.py')
    files = read_site(a.site)
    name = (a.name or page_title(files['index.html']) or 'My App').strip()
    a.version = a.version or datetime.datetime.now().strftime('%Y.%m.%d-%H%M')
    # file name always carries the version, e.g. Magic_Orb_v5.2-build3.apk
    out = a.out or (re.sub(r'[^A-Za-z0-9_-]+', '_', name).strip('_') + '_v' + re.sub(r'[^A-Za-z0-9._-]+', '_', a.version) + '.apk')
    r = build(a.shell, a.site, name, out, a.icon, a.secret, a.app_id, a.version, a.label)
    print('OK  %(out)s  ·  name: %(name)s  ·  app ID: %(id)s  ·  version: %(version)s  ·  %(files)d file(s)  ·  %(size)d bytes' % r)


if __name__ == '__main__':
    main()
