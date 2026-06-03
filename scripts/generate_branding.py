#!/usr/bin/env python3
"""
generate_branding.py — Read config.yml and emit:
  1. include/branding.h   (C #define macros for every string)
  2. Branded copies of the captive-portal HTML files, then regenerate
     lib/wificaptive/src/WifiCaptivePage.h (gzipped byte arrays)
  3. QR code images as G5-compressed C headers
     (src/wifi_connect_qr.h, src/wifi_failed_qr.h)

Can be invoked standalone or as a PlatformIO pre-build script.

Dependencies for QR generation: pip install qrcode pillow
If not installed, QR generation is skipped with a warning.
"""

import io
import gzip
import os
import re
import struct
import sys

# ---------------------------------------------------------------------------
# Minimal YAML parser (no external dependency for CI / PlatformIO)
# ---------------------------------------------------------------------------

def _parse_yaml(path):
    """
    Parse a *simple* YAML file (scalar values, nested dicts, no lists/anchors).
    Returns a nested dict.
    """
    result = {}
    stack = [(result, -1)]  # (dict, indent)

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n\r")
            stripped = line.lstrip()

            # skip blanks / comments
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(stripped)
            # pop stack to correct parent
            while len(stack) > 1 and indent <= stack[-1][1]:
                stack.pop()
            parent = stack[-1][0]

            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)", stripped)
            if not m:
                continue
            key = m.group(1)
            value = m.group(2).strip()

            if value == "" or value.startswith("#"):
                # nested dict
                child = {}
                parent[key] = child
                stack.append((child, indent))
            else:
                # strip quotes
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                parent[key] = value

    return result


# ---------------------------------------------------------------------------
# Template expansion
# ---------------------------------------------------------------------------

def _expand(template, variables):
    """Replace {key} placeholders in *template* with values from *variables*."""
    def _repl(m):
        return variables.get(m.group(1), m.group(0))
    return re.sub(r"\{(\w+)\}", _repl, template)


# ---------------------------------------------------------------------------
# Group5 1-bpp image encoder (pure Python port of Larry Bank's C encoder)
# ---------------------------------------------------------------------------

# Number of consecutive 1-bits from MSB in a byte
_BITCOUNT = [0]*256
for _i in range(256):
    _n = 0
    _v = _i
    while _v & 0x80:
        _n += 1
        _v = (_v << 1) & 0xFF
    _BITCOUNT[_i] = _n

# Vertical code table: (code_value, bit_length) indexed by dx+3
_VTABLE = [
    (0b0000011, 7),  # V(-3)
    (0b000011,  6),  # V(-2)
    (0b011,     3),  # V(-1)
    (0b1,       1),  # V(0)
    (0b010,     3),  # V(1)
    (0b000010,  6),  # V(2)
    (0b0000010, 7),  # V(3)
]

_HORIZ_SS, _HORIZ_SL, _HORIZ_LS, _HORIZ_LL = 0, 1, 2, 3
_MAX_FLIPS = 512


class _BitWriter:
    """Accumulates bits MSB-first into a byte buffer."""
    __slots__ = ("buf", "accum", "bit_off")

    def __init__(self):
        self.buf = bytearray()
        self.accum = 0        # 32-bit accumulator
        self.bit_off = 0

    def put(self, code, length):
        self.accum |= (code << (32 - self.bit_off - length))
        self.bit_off += length
        while self.bit_off >= 8:
            self.buf.append((self.accum >> 24) & 0xFF)
            self.accum = (self.accum << 8) & 0xFFFFFFFF
            self.bit_off -= 8

    def flush(self):
        if self.bit_off:
            self.buf.append((self.accum >> 24) & 0xFF)
            self.accum = 0
            self.bit_off = 0


def _pixels_to_flips(row_bytes, width):
    """Convert a 1-bpp row (MSB first, white=1) into run-end positions."""
    flips = []
    x = 0
    bit_pos = 0
    byte_idx = 0
    nbytes = len(row_bytes)
    c = row_bytes[0] if nbytes > 0 else 0xFF
    cbits = 8

    while x < width:
        # count white (1-bits)
        run = 0
        while True:
            i = _BITCOUNT[c]
            run += i
            c = (c << i) & 0xFF
            cbits -= i
            if cbits <= 0:
                run += cbits  # adjust
                byte_idx += 1
                if byte_idx >= nbytes:
                    cbits = 0
                    break
                c = row_bytes[byte_idx]
                cbits = 8
            else:
                break

        x += run
        if x >= width:
            break
        flips.append(min(x, width))

        # count black (0-bits) — invert to count via bitcount
        c = (~c) & 0xFF
        run = 0
        while True:
            i = _BITCOUNT[c]
            run += i
            c = (c << i) & 0xFF
            cbits -= i
            if cbits <= 0:
                run += cbits
                byte_idx += 1
                if byte_idx >= nbytes:
                    cbits = 0
                    break
                c = row_bytes[byte_idx]
                c = (~c) & 0xFF
                cbits = 8
            else:
                c = (~c) & 0xFF
                break

        x += run
        if x >= width:
            break
        flips.append(min(x, width))

    # pad with xsize sentinels
    flips.extend([width] * 4)
    return flips


def g5_encode(pixels_1bpp, width, height):
    """
    Encode a 1-bpp image (list of *height* byte-rows, MSB-first, white=1/black=0)
    into Group5 compressed data. Returns bytes.
    """
    hlen = (width - 1).bit_length()  # bits for long horizontal codes
    bw = _BitWriter()
    ref = [width] * (_MAX_FLIPS + 4)

    for y in range(height):
        cur = _pixels_to_flips(pixels_1bpp[y], width)
        a0 = 0
        icur = 0
        iref = 0

        while a0 < width:
            b1 = ref[iref]
            b2 = ref[iref + 1] if (iref + 1) < len(ref) else width
            a1 = cur[icur] if icur < len(cur) else width

            if b2 < a1:
                # pass mode
                bw.put(0b0001, 4)
                a0 = b2
                iref += 2
            else:
                dx = b1 - a1
                if dx > 3 or dx < -3:
                    # horizontal mode
                    bw.put(0b001, 3)
                    run1 = (cur[icur] if icur < len(cur) else width) - a0
                    run2 = (cur[icur + 1] if (icur + 1) < len(cur) else width) - \
                           (cur[icur] if icur < len(cur) else width)
                    if run1 < 8:
                        if run2 < 8:
                            bw.put(_HORIZ_SS, 2); bw.put(run1, 3); bw.put(run2, 3)
                        else:
                            bw.put(_HORIZ_SL, 2); bw.put(run1, 3); bw.put(run2, hlen)
                    else:
                        if run2 < 8:
                            bw.put(_HORIZ_LS, 2); bw.put(run1, hlen); bw.put(run2, 3)
                        else:
                            bw.put(_HORIZ_LL, 2); bw.put(run1, hlen); bw.put(run2, hlen)
                    a0 = cur[icur + 1] if (icur + 1) < len(cur) else width
                    if a0 != width:
                        icur += 2
                        while iref < len(ref) and ref[iref] != width and ref[iref] <= a0:
                            iref += 2
                else:
                    # vertical mode
                    vcode, vlen = _VTABLE[dx + 3]
                    bw.put(vcode, vlen)
                    a0 = a1
                    if a0 != width:
                        if iref != 0:
                            iref -= 2
                        iref += 1
                        icur += 1
                        while iref < len(ref) and ref[iref] <= a0 and ref[iref] != width:
                            iref += 2

        ref = cur

    bw.flush()
    return bytes(bw.buf)


def make_bb_bitmap(g5_data, width, height):
    """Wrap G5 compressed data in a BB_BITMAP header (little-endian)."""
    marker = 0xBBBF
    hdr = struct.pack("<HHH H", marker, width, height, len(g5_data))
    return hdr + g5_data


# ---------------------------------------------------------------------------
# QR code generation
# ---------------------------------------------------------------------------

def _try_import_qrcode():
    try:
        import qrcode
        return qrcode
    except ImportError:
        return None


def generate_qr_header(url, var_name, target_px, out_path):
    """
    Generate a QR code for *url*, render at exactly *target_px* × *target_px*,
    G5-compress it, and write a C header to *out_path*.
    Returns True on success, False if qrcode lib is missing.
    """
    qrcode = _try_import_qrcode()
    if qrcode is None:
        return False

    # Generate QR matrix
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=0,  # we add our own border
    )
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()  # list of list of bool (True=black)
    modules = len(matrix)

    # Scale as large as possible while fitting inside target_px
    scale = max(1, target_px // modules)
    qr_px = modules * scale

    # Center QR inside exactly target_px × target_px (pad with white)
    pad_left = (target_px - qr_px) // 2
    pad_top = (target_px - qr_px) // 2

    row_bytes_width = (target_px + 7) // 8
    rows = []
    for y in range(target_px):
        row = bytearray(b'\xff' * row_bytes_width)  # all white (1-bits)
        qr_y = y - pad_top
        if 0 <= qr_y < qr_px:
            mod_y = qr_y // scale
            for mx in range(modules):
                if matrix[mod_y][mx]:  # black module
                    for sx in range(scale):
                        px = pad_left + mx * scale + sx
                        if 0 <= px < target_px:
                            row[px >> 3] &= ~(0x80 >> (px & 7))
        # Mask final partial byte so bits beyond target_px are white
        tail_bits = target_px & 7
        if tail_bits:
            row[-1] |= (0xFF >> tail_bits)
        rows.append(bytes(row))

    # Compress
    g5_data = g5_encode(rows, target_px, target_px)
    bb_data = make_bb_bitmap(g5_data, target_px, target_px)

    # Format as C header
    hex_vals = ",".join(f"0x{b:02x}" for b in bb_data)
    content = (
        f"//\n"
        f"// Auto-generated QR code for: {url}\n"
        f"// {target_px} x {target_px} x 1-bit per pixel\n"
        f"// compressed image data size = {len(g5_data)} bytes\n"
        f"//\n"
        f"const uint8_t {var_name}[] = {{\n"
        f"    {hex_vals}}};\n"
    )

    # Re-wrap lines at ~80 chars for readability
    lines = content.split("\n")
    out_lines = []
    for line in lines:
        if line.startswith("    0x"):
            # split the hex data line
            chunks = []
            data = line.strip().rstrip(",")
            vals = [v.strip() for v in data.split(",") if v.strip()]
            for i in range(0, len(vals), 16):
                chunks.append("    " + ",".join(vals[i:i+16]) + ",")
            # remove trailing comma on last chunk
            chunks[-1] = chunks[-1].rstrip(",")
            # add closing brace+semicolon
            out_lines.extend(chunks)
        else:
            out_lines.append(line)

    content = "\n".join(out_lines) + "\n"

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            if f.read() == content:
                print(f"[branding] {out_path} is up-to-date")
                return True
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[branding] wrote {out_path} ({target_px}x{target_px}, {len(g5_data)} bytes compressed)")
    return True


# ---------------------------------------------------------------------------
# PNG → G5 image conversion
# ---------------------------------------------------------------------------

def _try_import_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def generate_image_header(png_path, var_name, expected_w, expected_h, out_path):
    """
    Load a 1-bit PNG, G5-compress it, and write a C header.
    The PNG must match expected_w × expected_h.
    Returns True on success, False if PIL is missing, raises on bad input.
    """
    Image = _try_import_pil()
    if Image is None:
        return False

    img = Image.open(png_path).convert("1")  # force 1-bit
    if img.size != (expected_w, expected_h):
        raise ValueError(
            f"{png_path}: expected {expected_w}×{expected_h} but got {img.size[0]}×{img.size[1]}"
        )

    # Convert to row bytes (MSB first, white=1, black=0 — PIL '1' mode: 255=white, 0=black)
    row_byte_width = (expected_w + 7) // 8
    rows = []
    for y in range(expected_h):
        row = bytearray(row_byte_width)
        for x in range(expected_w):
            if img.getpixel((x, y)):  # white pixel → set bit
                row[x >> 3] |= 0x80 >> (x & 7)
        # Mask trailing bits beyond width to white
        tail = expected_w & 7
        if tail:
            row[-1] |= (0xFF >> tail)
        rows.append(bytes(row))

    g5_data = g5_encode(rows, expected_w, expected_h)
    bb_data = make_bb_bitmap(g5_data, expected_w, expected_h)

    # Format as C header
    hex_lines = []
    vals = [f"0x{b:02x}" for b in bb_data]
    for i in range(0, len(vals), 16):
        hex_lines.append(",".join(vals[i:i+16]))
    hex_body = ",\n    ".join(hex_lines)
    content = (
        f"//\n"
        f"// Auto-generated from {os.path.basename(png_path)}\n"
        f"// {expected_w} x {expected_h} x 1-bit per pixel\n"
        f"// compressed image data size = {len(g5_data)} bytes\n"
        f"//\n"
        f"const uint8_t {var_name}[] = {{\n"
        f"    {hex_body}}};\n"
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            if f.read() == content:
                print(f"[branding] {out_path} is up-to-date")
                return True
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[branding] wrote {out_path} ({expected_w}×{expected_h}, {len(g5_data)} bytes compressed)")
    return True


# ---------------------------------------------------------------------------
# Branding header generation
# ---------------------------------------------------------------------------

_C_IDENT = re.compile(r"[^A-Za-z0-9_]")

def _c_escape(s):
    """Escape a string for use inside a C string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')

def generate_branding_h(cfg, out_path):
    """Write include/branding.h from the parsed config."""
    branding = cfg.get("branding", {})
    wifi = cfg.get("wifi", {})
    urls = cfg.get("urls", {})
    strings = cfg.get("strings", {})

    device_name = branding.get("device_name", "TRMNL")
    ap_ssid = wifi.get("ap_ssid_prefix", device_name)
    api_base = urls.get("api_base_url", "https://trmnl.app")
    setup_url = urls.get("setup_url", "trmnl.com/start")
    wifi_connect_qr_url = urls.get("wifi_connect_qr_url", "")
    wifi_failed_qr_url = urls.get("wifi_failed_qr_url", "")

    variables = {
        "device_name": device_name,
        "setup_url": setup_url,
        "api_base_url": api_base,
    }

    lines = [
        "// ============================================================",
        "// AUTO-GENERATED by scripts/generate_branding.py — DO NOT EDIT",
        "// Edit config.yml and re-run the generator instead.",
        "// ============================================================",
        "#ifndef BRANDING_H",
        "#define BRANDING_H",
        "",
        "// --- Core identity ---",
        f'#define BRAND_DEVICE_NAME       "{_c_escape(device_name)}"',
        f'#define BRAND_WIFI_AP_SSID      "{_c_escape(ap_ssid)}"',
        "",
        "// --- URLs ---",
        f'#define BRAND_API_BASE_URL      "{_c_escape(api_base)}"',
        f'#define BRAND_SETUP_URL         "{_c_escape(setup_url)}"',
        f'#define BRAND_WIFI_CONNECT_QR   "{_c_escape(wifi_connect_qr_url)}"',
        f'#define BRAND_WIFI_FAILED_QR    "{_c_escape(wifi_failed_qr_url)}"',
        "",
        "// --- User-facing strings ---",
    ]

    for key, template in strings.items():
        macro = "BRAND_STR_" + key.upper()
        expanded = _expand(template, variables)
        lines.append(f'#define {macro:<40s} "{_c_escape(expanded)}"')

    lines += ["", "#endif  // BRANDING_H", ""]
    content = "\n".join(lines)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # Only write if changed (avoid unnecessary rebuilds)
    if os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            if f.read() == content:
                print(f"[branding] {out_path} is up-to-date")
                return
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[branding] wrote {out_path}")


# ---------------------------------------------------------------------------
# Captive-portal HTML branding + WifiCaptivePage.h regeneration
# ---------------------------------------------------------------------------

def _brand_portal_colors(text, cfg):
    """Replace {{accent_color}} placeholders in portal HTML source."""
    branding = cfg.get("branding", {})
    accent = branding.get("accent_color", "#F86527")
    return text.replace("{{accent_color}}", accent)


def _brand_html(html_text, cfg):
    """Apply branding substitutions to HTML source."""
    branding = cfg.get("branding", {})
    urls = cfg.get("urls", {})
    strings = cfg.get("strings", {})

    device_name = branding.get("device_name", "TRMNL")
    api_base = urls.get("api_base_url", "https://trmnl.app")

    variables = {
        "device_name": device_name,
        "api_base_url": api_base,
    }

    portal_title = _expand(strings.get("portal_title", "{device_name} Wi-Fi Configuration"), variables)
    portal_advanced_title = _expand(strings.get("portal_advanced_title", "{device_name} Advanced Configuration"), variables)
    portal_reset_warning = _expand(strings.get("portal_reset_warning", ""), variables)
    portal_server_note = _expand(strings.get("portal_server_note", ""), variables)
    portal_api_placeholder = _expand(strings.get("portal_api_placeholder", ""), variables)

    # Title replacement
    html_text = html_text.replace(
        "<title>TRMNL Wi-Fi Configuration</title>",
        f"<title>{portal_title}</title>",
    )
    html_text = html_text.replace(
        "<title>TRMNL Advanced Configuration</title>",
        f"<title>{portal_advanced_title}</title>",
    )

    # Reset warning modal
    html_text = re.sub(
        r"Are you sure\? Soft resetting your TRMNL device will delete all WiFi credentials,\s*\n?\s*clear your Device's ID, and reset your API key\.",
        portal_reset_warning,
        html_text,
    )

    # Custom server warning modal
    html_text = re.sub(
        r"This button allows you to specify a custom API server.*?Are you sure you want to have a custom server specified\?",
        portal_server_note,
        html_text,
        flags=re.DOTALL,
    )

    # API placeholder
    html_text = html_text.replace(
        'Enter your custom server without a trailing / (e.g. https://trmnl.app)',
        portal_api_placeholder if portal_api_placeholder else f'Enter your custom server without a trailing / (e.g. {api_base})',
    )

    html_text = _brand_portal_colors(html_text, cfg)

    return html_text


def _gzip_bytes(data):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(data)
    return buf.getvalue()


def generate_captive_portal(cfg, portal_dir, output_path):
    """
    Read HTML/SVG from portal_dir, apply branding, gzip, and write the
    WifiCaptivePage.h byte-array header.
    """
    allowed = (".html", ".svg")
    entries = []

    for fname in sorted(os.listdir(portal_dir)):
        if not fname.endswith(allowed):
            continue
        fpath = os.path.join(portal_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            raw = f.read()

        if fname.endswith(".html"):
            raw = _brand_html(raw, cfg)

        compressed = _gzip_bytes(raw.encode("utf-8"))
        array_name = fname.replace(".", "_").upper()
        hex_vals = ", ".join(hex(b) for b in compressed)
        entries.append(
            f"const uint8_t {array_name}[] PROGMEM = {{ {hex_vals} }};\n"
            f"const int {array_name}_LEN = sizeof({array_name});\n"
        )

    content = (
        "#ifndef WifiCaptivePage_h\n"
        "#define WifiCaptivePage_h\n\n"
        "#include <pgmspace.h>\n\n"
        + "\n".join(entries)
        + "\n#endif\n"
    )

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            if f.read() == content:
                print(f"[branding] {output_path} is up-to-date")
                return
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[branding] wrote {output_path}")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------

def main(project_dir=None):
    if project_dir is None:
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    config_path = os.path.join(project_dir, "config.yml")
    branding_h_path = os.path.join(project_dir, "include", "branding.h")
    portal_dir = os.path.join(project_dir, "lib", "wificaptive", "portal")
    captive_h_path = os.path.join(project_dir, "lib", "wificaptive", "src", "WifiCaptivePage.h")

    if not os.path.isfile(config_path):
        print(f"[branding] WARNING: {config_path} not found — using defaults")
        cfg = {}
    else:
        cfg = _parse_yaml(config_path)

    generate_branding_h(cfg, branding_h_path)
    generate_captive_portal(cfg, portal_dir, captive_h_path)

    # --- QR code generation ---
    urls = cfg.get("urls", {})
    src_dir = os.path.join(project_dir, "src")
    qr_size = 66  # match original 66x66 pixel QR codes

    wifi_connect_url = urls.get("wifi_connect_qr_url", "")
    wifi_failed_url = urls.get("wifi_failed_qr_url", "")

    if wifi_connect_url:
        ok = generate_qr_header(
            wifi_connect_url, "wifi_connect_qr", qr_size,
            os.path.join(src_dir, "wifi_connect_qr.h"),
        )
        if not ok:
            print("[branding] WARNING: install 'qrcode' and 'pillow' packages to regenerate QR codes")
            print("[branding]   pip install qrcode pillow")
    if wifi_failed_url:
        ok = generate_qr_header(
            wifi_failed_url, "wifi_failed_qr", qr_size,
            os.path.join(src_dir, "wifi_failed_qr.h"),
        )

    # --- Custom image generation ---
    images = cfg.get("images", {})
    _IMAGE_SPECS = {
        # key in config.yml → (C variable name, width, height, output .h file)
        "logo_small":  ("logo_small",  86,  86,  "logo_small.h"),
        "logo_medium": ("logo_medium", 240, 240, "logo_medium.h"),
        "loading":     ("loading",     800, 480, "loading.h"),
    }

    pil_warned = False
    for cfg_key, (var_name, w, h, out_name) in _IMAGE_SPECS.items():
        png_path = images.get(cfg_key, "")
        if not png_path:
            continue
        abs_png = os.path.join(project_dir, png_path)
        if not os.path.isfile(abs_png):
            print(f"[branding] WARNING: {png_path} not found — skipping {cfg_key}")
            continue
        ok = generate_image_header(abs_png, var_name, w, h, os.path.join(src_dir, out_name))
        if not ok and not pil_warned:
            print("[branding] WARNING: install 'pillow' package to convert custom images")
            print("[branding]   pip install pillow")
            pil_warned = True

    # --- Custom portal logo (SVG override) ---
    portal_logo_path = images.get("portal_logo", "")
    if portal_logo_path:
        abs_svg = os.path.join(project_dir, portal_logo_path)
        if os.path.isfile(abs_svg):
            dest = os.path.join(portal_dir, "logo.svg")
            with open(abs_svg, "r", encoding="utf-8") as f:
                svg_content = f.read()
            # Only copy if different from what's already in portal dir
            write_it = True
            if os.path.isfile(dest):
                with open(dest, "r", encoding="utf-8") as f:
                    if f.read() == svg_content:
                        write_it = False
            if write_it:
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(svg_content)
                print(f"[branding] copied portal logo from {portal_logo_path}")
                # Regenerate WifiCaptivePage.h since SVG changed
                generate_captive_portal(cfg, portal_dir, captive_h_path)
        else:
            print(f"[branding] WARNING: {portal_logo_path} not found — skipping portal logo")


# PlatformIO pre-script hook -----------------------------------------------
try:
    Import("env")
    _proj_dir = env.subst("$PROJECT_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main(_proj_dir)
except NameError:
    pass  # not running inside PlatformIO — Import() is undefined


if __name__ == "__main__":
    main()
