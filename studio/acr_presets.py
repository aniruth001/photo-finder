"""Load settings: presets made for Adobe Camera Raw and Lightroom, turned into editor presets.

Photographers buy and swap looks as Camera Raw / Lightroom presets: .xmp files (Lightroom Classic 7.3+, Camera Raw,
Lightroom mobile), older .lrtemplate files, or a .zip of either. "Load settings" in the editor reads them and saves
each as a preset of the team (studio/editing.py), made of the editor's own steps:

  White balance        Temperature / Tint (or the Incremental ones presets for JPEGs use)
  Light & colour       Exposure, Contrast, Saturation, Vibrance (and part of Dehaze)
  Light & shadows      Highlights, Shadows, Whites, Blacks, Clarity, Texture (and part of Dehaze)
  Curves               the point curves: ToneCurvePV2012 and its Red, Green and Blue curves
  Colour mixer         HSL: hue, saturation and luminance of the eight colours
  Look                 Split toning and Color grading, worked out as a small colour table
  Black & white        Convert to grayscale
  Sharpen, Vignette, Noise   Sharpening, Post-crop vignetting, Grain

The editor's adjustments are not Adobe's, so a loaded look comes out close to the original, not identical. Settings
with no counterpart (camera profiles and their colour tables, lens corrections, noise reduction, local masks...) are
left out, and the editor says which.
"""
import colorsys
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
MAX_FILE = 2 << 20        # one preset file
MAX_FILES = 200           # presets per load
MAX_UNZIPPED = 40 << 20   # a zip, all of it
EXTS = (".xmp", ".lrtemplate")

# settings that are read; the others that matter are reported as left out
KNOWN = {
    "Exposure2012", "Exposure", "Contrast2012", "Contrast", "Highlights2012", "Shadows2012", "Whites2012", "Blacks2012",
    "Clarity2012", "Clarity", "Texture", "Dehaze", "Vibrance", "Saturation", "Temperature", "Tint", "IncrementalTemperature",
    "IncrementalTint", "WhiteBalance", "ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green", "ToneCurvePV2012Blue",
    "ToneCurve", "ConvertToGrayscale", "Sharpness", "SharpenRadius", "SharpenDetail", "SharpenEdgeMasking",
    "PostCropVignetteAmount", "PostCropVignetteMidpoint", "PostCropVignetteFeather", "PostCropVignetteRoundness",
    "PostCropVignetteStyle", "PostCropVignetteHighlightContrast", "GrainAmount", "GrainSize", "GrainFrequency",
    "SplitToningShadowHue", "SplitToningShadowSaturation", "SplitToningHighlightHue", "SplitToningHighlightSaturation",
    "SplitToningBalance", "ColorGradeBlending", "ColorGradeGlobalHue", "ColorGradeGlobalSat", "ColorGradeGlobalLum",
    "ColorGradeMidtoneHue", "ColorGradeMidtoneSat", "ColorGradeMidtoneLum", "ColorGradeShadowLum", "ColorGradeHighlightLum",
}
BANDS = ["Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta"]
for _b in BANDS:
    KNOWN |= {f"HueAdjustment{_b}", f"SaturationAdjustment{_b}", f"LuminanceAdjustment{_b}"}
LEFT_OUT = {   # setting name prefix -> what to call it
    "LookTable": "camera profile look", "RGBTable": "camera profile look", "Look": "camera profile",
    "LensProfileEnable": "lens corrections", "LensManual": "lens corrections", "AutoLateralCA": "lens corrections", "Defringe": "lens corrections",
    "Perspective": "perspective", "LuminanceSmoothing": "noise reduction", "ColorNoiseReduction": "noise reduction",
    "GrayMixer": "black & white mix", "ParametricShadows": "parametric curve", "ParametricDarks": "parametric curve",
    "ParametricLights": "parametric curve", "ParametricHighlights": "parametric curve", "MaskGroupBasedCorrections": "masks",
    "GradientBasedCorrections": "masks", "CircularGradientBasedCorrections": "masks", "PaintBasedCorrections": "masks",
    "RedHue": "calibration", "RedSaturation": "calibration", "GreenHue": "calibration", "GreenSaturation": "calibration",
    "BlueHue": "calibration", "BlueSaturation": "calibration", "ShadowTint": "calibration", "PointColors": "point color",
}


class PresetError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- reading the files
def _local(tag: str) -> tuple:
    return tuple(tag[1:].split("}", 1)) if tag.startswith("{") else ("", tag)


def parse_xmp(data: bytes):
    """(name, {setting: value}) from a Camera Raw / Lightroom .xmp preset. Values are strings, or lists for curves."""
    if len(data) > MAX_FILE:
        raise PresetError("too large")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:   # a preset never has these; refusing them keeps the parser safe
        raise PresetError("not a preset")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise PresetError("not a preset (the file couldn't be read)")
    settings, name = {}, ""
    rdf = root if _local(root.tag) == (RDF, "RDF") else root.find(f".//{{{RDF}}}RDF")
    for desc in (rdf.findall(f"{{{RDF}}}Description") if rdf is not None else []):   # the preset's own settings, not nested ones
        for k, v in desc.attrib.items():
            ns, key = _local(k)
            if ns == CRS:
                settings[key] = v
        for elem in desc:
            ns, key = _local(elem.tag)
            if ns != CRS:
                continue
            lis = [li.text or "" for li in elem.iter(f"{{{RDF}}}li")]
            if elem.find(f"{{{RDF}}}Seq") is not None:
                settings[key] = lis
            elif elem.find(f"{{{RDF}}}Alt") is not None:
                settings[key] = lis[0] if lis else ""
            elif len(elem) == 0:
                settings[key] = (elem.text or "").strip()
            else:   # a structure: a camera profile look, masks...
                settings[key] = ["structure"]
    name = settings.pop("Name", "") if isinstance(settings.get("Name"), str) else ""
    if not any(k in settings for k in KNOWN) and not any(k.startswith(tuple(LEFT_OUT)) for k in settings):
        raise PresetError("not a Camera Raw or Lightroom preset")
    return name.strip(), settings


def parse_lrtemplate(data: bytes):
    """(name, {setting: value}) from an older Lightroom .lrtemplate preset (a Lua table)."""
    if len(data) > MAX_FILE:
        raise PresetError("too large")
    text = data.decode("utf-8", "replace")
    m = re.search(r"\bsettings\s*=\s*\{", text)
    if not m:
        raise PresetError("not a Lightroom develop preset")
    title = re.search(r'\btitle\s*=\s*"((?:[^"\\]|\\.)*)"', text) or re.search(r'\binternalName\s*=\s*"((?:[^"\\]|\\.)*)"', text)
    body, settings = text[m.end():], {}
    for k, lst in re.findall(r"\b(\w+)\s*=\s*\{([^{}]*)\}", body):
        nums = re.findall(r"-?\d+(?:\.\d+)?", lst)
        if k.startswith("ToneCurve") and len(nums) >= 4:
            settings[k] = [f"{nums[i]}, {nums[i + 1]}" for i in range(0, len(nums) - 1, 2)]
    for k, v in re.findall(r'\b(\w+)\s*=\s*(-?\d+(?:\.\d+)?|true|false|"[^"]*")\s*,', body):
        settings.setdefault(k, v.strip('"'))
    if not any(k in settings for k in KNOWN):
        raise PresetError("not a Lightroom develop preset")
    return (title.group(1) if title else "").strip(), settings


def read_files(files):
    """[(file name, bytes)] -> ([(name, settings, file name)], [(file name, reason)]). Zips are opened."""
    found, bad, total = [], [], 0

    def one(fname, data):
        ext = Path(fname).suffix.lower()
        try:
            name, settings = parse_xmp(data) if ext == ".xmp" else parse_lrtemplate(data)
        except PresetError as e:
            bad.append((fname, str(e)))
            return
        found.append((name or Path(fname).stem, settings, fname))

    for fname, data in files:
        ext = Path(fname).suffix.lower()
        if ext == ".zip":
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                bad.append((fname, "not a zip file"))
                continue
            for info in z.infolist():
                inner = info.filename
                if info.is_dir() or Path(inner).suffix.lower() not in EXTS or "__MACOSX" in inner or Path(inner).name.startswith("._"):
                    continue
                total += info.file_size
                if info.file_size > MAX_FILE or total > MAX_UNZIPPED or len(found) + len(bad) >= MAX_FILES:
                    bad.append((Path(inner).name, "too large"))
                    continue
                one(Path(inner).name, z.read(info))
        elif ext in EXTS:
            one(fname, data)
        else:
            bad.append((fname, "not a preset file (.xmp, .lrtemplate or .zip)"))
        if len(found) >= MAX_FILES:
            break
    return found[:MAX_FILES], bad


# ---------------------------------------------------------------------------------------------- the editor's steps
def _num(s: dict, key: str, default: float = 0.0) -> float:
    v = s.get(key)
    if isinstance(v, str):
        try:
            return float(v.replace("+", ""))
        except ValueError:
            return default
    return default


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _curve(v):
    pts = []
    for p in v if isinstance(v, list) else []:
        try:
            x, y = (float(t) for t in str(p).split(","))
            pts.append([round(_clamp(x, 0, 255)), round(_clamp(y, 0, 255))])
        except ValueError:
            continue
    pts.sort()
    return pts if len(pts) >= 2 else None


def _identity(pts) -> bool:
    return not pts or all(abs(x - y) <= 1 for x, y in pts)


def _grade_lut(s: dict, n: int = 17):
    """Split toning / Color grading as a colour table (the editor's Look step, red changing fastest), or None."""
    def tint(hue, sat):   # the colour to push towards, as an offset from grey of the same lightness
        r, g, b = colorsys.hsv_to_rgb((hue % 360) / 360, 1, 1)
        y = .2126 * r + .7152 * g + .0722 * b
        k = _clamp(sat, 0, 100) / 100
        return ((r - y) * k, (g - y) * k, (b - y) * k)

    sh = tint(_num(s, "SplitToningShadowHue"), _num(s, "SplitToningShadowSaturation"))
    hi = tint(_num(s, "SplitToningHighlightHue"), _num(s, "SplitToningHighlightSaturation"))
    mid = tint(_num(s, "ColorGradeMidtoneHue"), _num(s, "ColorGradeMidtoneSat"))
    glo = tint(_num(s, "ColorGradeGlobalHue"), _num(s, "ColorGradeGlobalSat"))
    lum = {"sh": _num(s, "ColorGradeShadowLum"), "mid": _num(s, "ColorGradeMidtoneLum"), "hi": _num(s, "ColorGradeHighlightLum"),
           "glo": _num(s, "ColorGradeGlobalLum")}
    if not any(any(t) for t in (sh, hi, mid, glo)) and not any(lum.values()):
        return None
    bal = _clamp(_num(s, "SplitToningBalance"), -100, 100) / 100   # + favours the highlights
    data = []
    for bi in range(n):
        for gi in range(n):
            for ri in range(n):
                r, g, b = ri / (n - 1), gi / (n - 1), bi / (n - 1)
                y = .2126 * r + .7152 * g + .0722 * b
                p = _clamp(y - bal * 0.3, 0, 1)
                ws, wh, wm = (1 - p) ** 2, p ** 2, max(0.0, 1 - abs(2 * y - 1))
                k = 0.45
                dr = k * (sh[0] * ws + hi[0] * wh + mid[0] * wm + glo[0])
                dg = k * (sh[1] * ws + hi[1] * wh + mid[1] * wm + glo[1])
                db = k * (sh[2] * ws + hi[2] * wh + mid[2] * wm + glo[2])
                dl = 0.2 * (lum["sh"] * ws + lum["hi"] * wh + lum["mid"] * wm + lum["glo"]) / 100
                data += [round(_clamp(c + d + dl, 0, 1) * 255) for c, d in ((r, dr), (g, dg), (b, db))]
    return {"name": "Color grading", "size": n, "data": data, "strength": 100}


def convert(s: dict):
    """Camera Raw / Lightroom settings -> (the editor's steps, [settings left out])."""
    steps = []
    # white balance: presets for JPEGs use the incremental sliders; RAW presets name a temperature in kelvin
    temp = tint = 0.0
    if "IncrementalTemperature" in s or "IncrementalTint" in s:
        temp, tint = _num(s, "IncrementalTemperature"), _num(s, "IncrementalTint")
    elif "Temperature" in s and s.get("WhiteBalance", "Custom") not in ("As Shot", "Auto"):
        temp, tint = (_num(s, "Temperature", 5500) - 5500) / 45, _num(s, "Tint") / 1.5
    if abs(temp) >= 1 or abs(tint) >= 1:
        steps.append({"op": "wb", "v": {"temperature": round(_clamp(temp, -100, 100)), "tint": round(_clamp(tint, -100, 100))}})

    dehaze = _num(s, "Dehaze")
    exposure = _num(s, "Exposure2012", _num(s, "Exposure")) * 0.45   # Adobe's EV are in linear light; the editor's aren't
    contrast = _num(s, "Contrast2012", _num(s, "Contrast")) * 0.5 + dehaze * 0.25
    saturation = _num(s, "Saturation") + _num(s, "Vibrance") * 0.4 + dehaze * 0.1
    if abs(exposure) >= 0.01 or abs(contrast) >= 1 or abs(saturation) >= 1:
        steps.append({"op": "adjust", "v": {"exposure": round(_clamp(exposure, -2, 2), 2), "brightness": 0,
                                            "contrast": round(_clamp(contrast, -100, 100)), "saturation": round(_clamp(saturation, -100, 100)),
                                            "hue": 0, "temperature": 0}})

    tone = {"highlights": _num(s, "Highlights2012"), "shadows": _num(s, "Shadows2012"), "whites": _num(s, "Whites2012"),
            "blacks": _num(s, "Blacks2012") - dehaze * 0.3,
            "clarity": _num(s, "Clarity2012", _num(s, "Clarity")) + _num(s, "Texture") * 0.3 + dehaze * 0.2}
    if any(abs(v) >= 1 for v in tone.values()):
        steps.append({"op": "tone", "v": {k: round(_clamp(v, -100, 100)) for k, v in tone.items()}})

    curves = {k: _curve(s.get(key)) for k, key in (("rgb", "ToneCurvePV2012"), ("r", "ToneCurvePV2012Red"),
                                                     ("g", "ToneCurvePV2012Green"), ("b", "ToneCurvePV2012Blue"))}
    if curves["rgb"] is None and _curve(s.get("ToneCurve")) is not None:   # the process version before 2012
        curves["rgb"] = _curve(s.get("ToneCurve"))
    if not all(_identity(c) for c in curves.values()):
        steps.append({"op": "curves", "v": {k: c or [[0, 0], [255, 255]] for k, c in curves.items()}})

    mix = {}
    for b in BANDS:
        h, sa, lu = _num(s, f"HueAdjustment{b}"), _num(s, f"SaturationAdjustment{b}"), _num(s, f"LuminanceAdjustment{b}")
        if h or sa or lu:
            mix[b.lower()] = {"h": round(_clamp(h * 0.3, -30, 30)), "s": round(_clamp(sa, -100, 100)), "l": round(_clamp(lu, -100, 100))}
    if mix:
        steps.append({"op": "hsl", "v": mix})

    grade = _grade_lut(s)
    if grade:
        steps.append({"op": "lut", "v": grade})
    if str(s.get("ConvertToGrayscale", "")).lower() == "true":
        steps.append({"op": "grayscale", "v": {}})

    sharp = _num(s, "Sharpness")
    if sharp > 0:
        steps.append({"op": "sharpen", "v": {"amount": round(_clamp(sharp * 1.2, 0, 300)),
                                             "radius": round(_clamp(_num(s, "SharpenRadius", 1.0), 0.5, 6), 1)}})
    vig = _num(s, "PostCropVignetteAmount")
    if vig < 0:
        steps.append({"op": "vignette", "v": {"amount": round(_clamp(-vig * 0.8, 0, 100)),
                                              "size": round(_clamp(_num(s, "PostCropVignetteMidpoint", 50) * 0.7, 0, 90))}})
    grain = _num(s, "GrainAmount")
    if grain > 0:
        steps.append({"op": "noise", "v": {"amount": round(_clamp(grain * 0.25, 0, 60)), "mono": True, "seed": 1}})

    left = set()
    for k, v in s.items():
        for prefix, what in LEFT_OUT.items():
            if k.startswith(prefix) and v not in ("", "0", "+0", "0.0", "false", "False", [], None):
                left.add(what)
    if s.get("CameraProfile") not in (None, "", "Adobe Standard", "Embedded", "Adobe Color"):
        left.add("camera profile")
    if _num(s, "PostCropVignetteAmount") > 0:
        left.add("white vignette")
    return steps, sorted(left)
