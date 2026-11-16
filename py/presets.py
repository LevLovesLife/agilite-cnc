"""
Agilite CNC presets: the machine, the bits in the rack, the materials and the finish levels.

These replace every number a designer would otherwise type. Lev (or an expert) edits them once;
designers only pick a material and a finish. Tool dicts handed to the engines use the same keys
as G-code Maker's tool library, so the proven 2D engine (gcm_engine.build) takes them unchanged.
"""
import copy
import json
import os

import gcm_engine as gcm

HERE = os.path.dirname(os.path.abspath(__file__))
RACK_FILE = os.path.join(HERE, "Agilite CNC rack.json")
PREFS_FILE = os.path.join(HERE, "Agilite CNC settings.json")

FLAT, BALL, VBIT = "Flat end mill", "Ball nose", gcm.VBIT
SURF = gcm.SURF   # a wide insert cutter that only faces the top (spoilboard / fly cutter): never cuts parts out
TYPES = (FLAT, BALL, VBIT, SURF)

# ---------------------------------------------------------------- the machine
MACHINE = {
    "name": "Stepcraft M700",
    "x_max": 478.0, "y_max": 744.0, "z_travel": 191.0,   # soft limits from the UCCNC profile
    "rpm_min": 10000, "rpm_max": 24000,                  # profile spindle range 10000-25000; Lev runs 24000 max
    "feed_max": 3000.0,
    # HSS end mills run at least this fast (Lev 2026-11-15: 16000, then 20000 when the spindle bogged at 16000)
    "hss_rpm": 20000,
    "safe_z": 5.0,
    # 8 mm since 2026-11-14: Lev has the 8 mm shank XCAN surfacing bit; not yet checked that the collet takes it,
    # so a plan using a shank over 6 mm says it needs an 8 mm collet (plan2d.collet_note)
    "collet_max_shank": 8.0,
    "nut_d": 22.0,               # collet nut diameter, for the reach check
    "probe_d": 32.0, "probe_dx": -20.0, "probe_dy": -20.0,   # Lev's tool length sensor: 32 mm across
    "tc_code": gcm.DEFAULTS["tc_code"],
    # Lev 2026-11-15: the end lifts to the machine's top (G53 Z-3), never a height over the stock that can pass
    # UCCNC's Z soft limit ("Job Z max >= Softlimit Z max") when Z zero is high up
    "end_z_mode": "Machine top (G53 Z-3)", "end_z": 20.0, "end_xy": "X0 Y0",
    "tool_change_min": 2.0,      # what one tool change costs the operator, minutes (used to pick tools)
    # the fixed tool sensor on the bed: the first bit is zeroed on the stock with the puck, every later bit is
    # measured on the bed sensor by Stepcraft's M20201 (measures the bit in, asks for the new one, measures it,
    # shifts Z zero by the difference). Machine coordinates; None until measured at the machine.
    # Lev 2026-11-15: back to the puck on the stock for every end mill ("stock"); the bed mode stays as an option.
    "sensor_mode": "stock", "bed_sensor": None, "bed_macro": "M20201",
}


def machine_with(prefs=None):
    """The machine with the settings kept in the prefs (the bed sensor's position and which sensor to use)."""
    m = dict(MACHINE)
    p = prefs or {}
    if p.get("sensor_mode") in ("bed", "stock"):
        m["sensor_mode"] = p["sensor_mode"]
    bs = p.get("bed_sensor")
    try:
        m["bed_sensor"] = (float(bs[0]), float(bs[1])) if bs and bs[0] is not None and bs[1] is not None else None
    except (TypeError, ValueError, IndexError):
        m["bed_sensor"] = None
    return m


def bed_sensor(machine):
    """The bed sensor's machine X, Y when bit changes are measured there, else None."""
    return machine.get("bed_sensor") if machine.get("sensor_mode") == "bed" else None

# ---------------------------------------------------------------- the rack
# reach = how far the bit sticks out below the collet nut and can cut without the nut touching
# owned = False: a standard bit to buy; the program still plans with it and says it must be bought
DEFAULT_RACK = [
    {"name": "6 mm flat 3F aluminium", "tool_type": FLAT, "tool_d": 6.0, "flutes": 3, "flute_len": 20.0,
     "reach": 30.0, "shank_d": 6.0, "tool_mat": "Carbide", "owned": False},
    {"name": "6 mm flat 2F long", "tool_type": FLAT, "tool_d": 6.0, "flutes": 2, "flute_len": 30.0,
     "reach": 50.0, "shank_d": 6.0, "tool_mat": "Carbide", "owned": False},
    {"name": "6 mm ball nose 2F long", "tool_type": BALL, "tool_d": 6.0, "flutes": 2, "flute_len": 25.0,
     "reach": 50.0, "shank_d": 6.0, "tool_mat": "Carbide", "owned": False},
    {"name": "3 mm ball nose 2F long neck", "tool_type": BALL, "tool_d": 3.0, "flutes": 2, "flute_len": 6.0,
     "reach": 25.0, "shank_d": 6.0, "neck_d": 2.8, "tool_mat": "Carbide", "owned": False},
    {"name": "4 mm ball nose 2F carbide", "tool_type": BALL, "tool_d": 4.0, "flutes": 2, "flute_len": 12.0,
     "reach": 20.0, "shank_d": 4.0, "tool_mat": "Carbide", "owned": True},
    {"name": "6 mm flat 2F carbide", "tool_type": FLAT, "tool_d": 6.0, "flutes": 2, "flute_len": 15.0,
     "reach": 22.0, "shank_d": 6.0, "tool_mat": "Carbide", "owned": True},
    {"name": "4 mm flat 2F carbide", "tool_type": FLAT, "tool_d": 4.0, "flutes": 2, "flute_len": 12.0,
     "reach": 20.0, "shank_d": 4.0, "tool_mat": "Carbide", "owned": True},
    {"name": "3 mm flat 2F carbide", "tool_type": FLAT, "tool_d": 3.0, "flutes": 2, "flute_len": 10.0,
     "reach": 18.0, "shank_d": 3.0, "tool_mat": "Carbide", "owned": True},
    {"name": "3 mm flat 1F carbide", "tool_type": FLAT, "tool_d": 3.0, "flutes": 1, "flute_len": 10.0,
     "reach": 18.0, "shank_d": 3.0, "tool_mat": "Carbide", "owned": True},
    {"name": "2 mm flat 2F carbide", "tool_type": FLAT, "tool_d": 2.0, "flutes": 2, "flute_len": 6.0,
     "reach": 15.0, "shank_d": 3.175, "tool_mat": "Carbide", "owned": True},
    {"name": "6 mm 90 deg V-bit carbide", "tool_type": VBIT, "tool_d": 6.0, "flutes": 2, "flute_len": 3.0,
     "reach": 15.0, "shank_d": 6.0, "tool_mat": "Carbide", "tool_angle": 90.0, "owned": True},
    # XCAN spoilboard surfacing bit 8x38.1x60Lx4T: 4 replaceable carbide inserts (AlTiN, R0.1 corners).
    # Wood only (Lev 2026-11-14: "this bit does not cut aluminum")
    {"name": "38 mm surfacing bit XCAN 4 inserts", "tool_type": SURF, "tool_d": 38.1, "flutes": 4, "flute_len": 4.0,
     "reach": 30.0, "shank_d": 8.0, "tool_mat": "Carbide", "owned": True, "cuts": ["Wood / MDF / plywood"]},
]

# a surfacing bit's own cutting data (the end mill table scales with the diameter, which is far too much for a
# 38 mm cutter): depth per pass, chip per insert (mm per tooth) and the stepover, per material family
SURF_DATA = {"Aluminium": {"step": 0.3, "fz": 0.05, "over": 70.0},
             "Wood / MDF / plywood": {"step": 1.0, "fz": 0.12, "over": 70.0}}
MATERIAL_FAMILIES = ("Aluminium", "Wood / MDF / plywood")   # a bit's "cuts" names these (G-code Maker's rows)


def bit_cuts(bit, material):
    """Whether a rack bit may cut this material: a bit with a "cuts" list only cuts those families."""
    only = bit.get("cuts")
    return not only or MATERIALS[material]["gcm"] in only


def rack_for(rack, material):
    """The bits in the rack that may cut this material."""
    return [b for b in rack if bit_cuts(b, material)]

# ---------------------------------------------------------------- materials
# gcm = the cutting data row in G-code Maker's MATERIALS table
# allowance = what roughing leaves for the finish (3D), mm
# hold = how the part is held: "tabs" (sheet, cut out) or "screws" (block, screwed or clamped at the corners)
MATERIALS = {
    "Aluminium sheet": {"gcm": "Aluminium", "kind": "sheet", "thickness": 3.0, "allowance": 0.3, "hold": "tabs",
                        "max_stepover": 0.4,
                        "about": "Aluminium plate up to about 6 mm. Cut out with tabs that hold the parts."},
    "Aluminium block": {"gcm": "Aluminium", "kind": "block", "thickness": 40.0, "allowance": 0.3, "hold": "screws",
                        "max_stepover": 0.4,
                        "about": "Thick aluminium for molds and 3D parts. Held down at the corners."},
    "MDF / wood": {"gcm": "Wood / MDF / plywood", "kind": "sheet", "thickness": 9.0, "allowance": 0.5, "hold": "tabs",
                   "max_stepover": 0.5,
                   "about": "MDF, plywood and hardwood. Fast and forgiving; good for test parts."},
    "Tooling board": {"gcm": "Wood / MDF / plywood", "kind": "block", "thickness": 50.0, "allowance": 0.4,
                      "hold": "screws", "max_stepover": 0.5,
                      "about": "Polyurethane tooling board for molds and patterns. Cuts like wood, no grain."},
}

# ---------------------------------------------------------------- finish levels
# scallop = the ridge left between ball nose passes, mm
FINISHES = {
    "Fast": {"scallop": 0.05, "finish_pass": False, "about": "Visible lines on curved surfaces. Good for test fits."},
    "Standard": {"scallop": 0.02, "finish_pass": True, "about": "Smooth to the touch. The right choice for most parts."},
    "Fine": {"scallop": 0.008, "finish_pass": True, "about": "Very smooth curves; takes about twice as long."},
}


def load_rack():
    try:
        with open(RACK_FILE, encoding="utf-8") as f:
            rack = json.load(f)
        if isinstance(rack, list) and rack:
            return rack
    except (OSError, ValueError):
        pass
    return copy.deepcopy(DEFAULT_RACK)


def save_rack(rack):
    with open(RACK_FILE, "w", encoding="utf-8") as f:
        json.dump(rack, f, indent=1)


def load_prefs():
    try:
        with open(PREFS_FILE, encoding="utf-8") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(p):
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- designers' profiles
# Lev 2026-11-16: the rack is shared (an edit is for everyone); each designer keeps their own preferences,
# presets and speeds for the end mills. A profile is users/<name>.json; the machine's own facts (which tool sensor,
# where the bed sensor is) stay in the shared settings file, as they describe the one machine.
USERS_DIR = os.path.join(HERE, "users")
SHARED_KEYS = ("sensor_mode", "bed_sensor")
FIRST_USER = "Lev"   # the profile the old single settings file becomes


def clean_user(name):
    name = " ".join(str(name or "").split())[:40]
    return "".join(ch for ch in name if ch not in '\\/:*?"<>|.').strip()


def _user_file(name):
    return os.path.join(USERS_DIR, clean_user(name) + ".json")


def _migrate():
    """Once: the single settings file's personal part becomes Lev's profile."""
    if os.path.isdir(USERS_DIR):
        return
    os.makedirs(USERS_DIR, exist_ok=True)
    old = load_prefs()
    with open(_user_file(FIRST_USER), "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in old.items() if k not in SHARED_KEYS}, f, indent=1)


def list_users():
    """The designers' profile names, the first (migrated) one first."""
    _migrate()
    try:
        names = [os.path.splitext(f)[0] for f in os.listdir(USERS_DIR) if f.lower().endswith(".json")]
    except OSError:
        names = []
    return sorted(names, key=lambda n: (n != FIRST_USER, n.lower()))


def load_user(name):
    """A designer's preferences with the machine's shared settings in them."""
    _migrate()
    try:
        with open(_user_file(name), encoding="utf-8") as f:
            p = json.load(f)
        p = p if isinstance(p, dict) else {}
    except (OSError, ValueError):
        p = {}
    shared = load_prefs()
    for k in SHARED_KEYS:
        if k in shared:
            p[k] = shared[k]
    return p


def save_user(name, prefs):
    """Saves a designer's own preferences; the machine's settings go to the shared file."""
    if not clean_user(name):
        return
    _migrate()
    shared = load_prefs()
    changed = False
    for k in SHARED_KEYS:
        if k in prefs and shared.get(k) != prefs[k]:
            shared[k] = prefs[k]
            changed = True
    if changed:
        save_prefs(shared)
    try:
        with open(_user_file(name), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in prefs.items() if k not in SHARED_KEYS}, f, indent=1)
    except OSError:
        pass


def rack_with_speeds(rack, speeds):
    """The shared rack as one designer sees it: their own speeds (prefs["speeds"]: {end mill: {family: {...}}})
    over the rack's. An empty family entry means the designer chose the program's numbers."""
    if not speeds:
        return rack
    out = copy.deepcopy(rack)
    for bit in out:
        mine = speeds.get(bit.get("name")) or {}
        if not mine:
            continue
        sp = dict(bit.get("speeds") or {})
        for fam, v in mine.items():
            if v:
                sp[fam] = v
            else:
                sp.pop(fam, None)
        if sp:
            bit["speeds"] = sp
        else:
            bit.pop("speeds", None)
    return out


def set_user_speeds(prefs, rack, name, material, speeds):
    """Keeps a designer's own speeds for one end mill in the material's family (None/{} = the program's numbers).
    Returns False when the end mill isn't in the rack."""
    if not any(b.get("name") == name for b in rack):
        return False
    fam = MATERIALS[material]["gcm"]
    clean = {k: float(v) for k, v in (speeds or {}).items()
             if k in gcm.SPEED_KEYS and v not in (None, "") and float(v) > 0}
    allsp = dict(prefs.get("speeds") or {})
    bitsp = dict(allsp.get(name) or {})
    bitsp[fam] = clean
    allsp[name] = bitsp
    prefs["speeds"] = allsp
    return True


def rack_for_saving(new_rack, old_rack):
    """A rack the page sends back carries the designer's own speeds in it: the shared file keeps its own."""
    old = {b.get("name"): b.get("speeds") for b in old_rack}
    out = []
    for b in new_rack:
        b = dict(b)
        b.pop("speeds", None)
        if old.get(b.get("name")):
            b["speeds"] = old[b["name"]]
        out.append(b)
    return out


def machine_folder(prefs=None):
    """Where 'Send to machine' saves: the machine PC's folder, else Desktop\\G-code."""
    p = (prefs or {}).get("machine_folder")
    if p and os.path.isdir(p):
        return p
    if os.path.isdir("D:\\G-code"):
        return "D:\\G-code"
    return gcm.DEFAULT_OUT


def tool_for(bit, material, machine=MACHINE):
    """A G-code Maker tool dict for one rack bit cutting one material, with suggested speeds and feeds."""
    m = MATERIALS[material]
    s = {"material": m["gcm"], "tool_d": bit["tool_d"], "flutes": bit["flutes"], "tool_mat": bit["tool_mat"],
         "tool_type": bit["tool_type"], "rpm_min": machine["rpm_min"], "rpm_max": machine["rpm_max"],
         "feed_max": machine["feed_max"]}
    sug = gcm.suggest(s)
    t = {k: bit[k] for k in ("name", "tool_d", "tool_type", "flutes", "tool_mat")}
    t["tool_angle"] = bit.get("tool_angle", 90.0)
    t.update(rpm=sug["rpm"], feed=sug["feed"], ramp_feed=sug["ramp_feed"], plunge_feed=sug["plunge_feed"],
             step=sug["step"], ramp_angle=10.0 if m["gcm"] == "Aluminium" else 20.0)
    if bit["tool_type"] == SURF:
        # a wide insert cutter: the spindle's lowest speed keeps the rim speed sane, the feed follows the chip per
        # insert (inside the machine's limit), shallow passes, and it ramps in gently
        sd = SURF_DATA.get(m["gcm"], SURF_DATA["Wood / MDF / plywood"])
        rpm = machine["rpm_min"]
        feed = min(machine["feed_max"], rpm * max(1, int(bit["flutes"])) * sd["fz"])
        t.update(rpm=rpm, feed=round(feed, -1), ramp_feed=round(feed / 2, -1), plunge_feed=round(feed / 5, -1),
                 step=sd["step"], ramp_angle=3.0, surf_over=sd["over"])
    elif bit["tool_mat"] == "HSS" and sug["rpm"] < machine.get("hss_rpm", 0):
        # Lev 2026-11-15: "16000 rpm is fine for HSS" (the cutting-speed table would put HSS at the spindle's
        # lowest speed, where it may not start); the feed follows the same chip per tooth
        rpm = machine["hss_rpm"]
        feed = max(10.0, round(min(machine["feed_max"], rpm * max(1, int(bit["flutes"])) * sug["fz_target"]), -1))
        t.update(rpm=rpm, feed=feed, ramp_feed=max(10.0, round(feed / 2, -1)),
                 plunge_feed=max(10.0, round(feed / 5, -1)))
    # Lev 2026-11-15: his own tuned numbers for this end mill in this material win over the program's
    t["sug"] = {k: t[k] for k in gcm.SPEED_KEYS}
    t["mat_family"] = m["gcm"]
    own = own_speeds(bit, material)
    for k, v in own.items():
        t[k] = v
    t["own"] = sorted(own)
    for k in ("flute_len", "reach", "shank_d", "neck_d", "owned"):
        if k in bit:
            t[k] = bit[k]
    return t


def own_speeds(bit, material):
    """The speeds Lev set for this end mill in this material's family ({rpm, feed, ...}, only the ones he set)."""
    fam = MATERIALS[material]["gcm"]
    sp = (bit.get("speeds") or {}).get(fam) or {}
    out = {}
    for k in gcm.SPEED_KEYS:
        try:
            v = float(sp[k])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0:
            out[k] = int(round(v)) if k == "rpm" else v
    return out


def set_own_speeds(rack, name, material, speeds):
    """Keep (or with None / {} forget) this end mill's own speeds for the material's family. Returns the bit."""
    fam = MATERIALS[material]["gcm"]
    for bit in rack:
        if bit.get("name") == name:
            sp = dict(bit.get("speeds") or {})
            clean = {k: float(v) for k, v in (speeds or {}).items()
                     if k in gcm.SPEED_KEYS and v not in (None, "") and float(v) > 0}
            if clean:
                sp[fam] = clean
            else:
                sp.pop(fam, None)
            if sp:
                bit["speeds"] = sp
            else:
                bit.pop("speeds", None)
            return bit
    return None
