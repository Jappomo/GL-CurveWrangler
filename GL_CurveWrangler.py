"""
GL - Curve Wrangler v1
======================

A grab / comb brush for NURBS curves in Maya, in the spirit of XGen's
groom brushes.

    import GL_CurveWrangler
    GL_CurveWrangler.show()

Features
--------
* Grab and Comb modes with screen-space falloff.
* Live brush circle painted over the viewport, following the cursor even
  when no button is held, with a readout of what is under it.
* Auto-Mask: with several curves selected, only the curve under the
  cursor is affected by the stroke.
* Length preservation and a pinned root so strands comb instead of stretch.
* PySide interface, no plugin required.

Controls
--------
    LMB drag            brush
    MMB drag L/R        brush radius
    Ctrl + LMB          temporarily flip Grab <-> Comb

Notes
-----
* Periodic (closed) curves skip the length solver.
* Undo is recorded per stroke, on mouse release.
* Overlay coordinates assume a 1.0 device pixel ratio; on a scaled
  display the circle may need the DPI_SCALE constant adjusted.
"""

import math

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.api.OpenMayaUI as omui
import maya.OpenMayaUI as omui1

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance
except ImportError:
    from PySide2 import QtCore, QtGui, QtWidgets
    from shiboken2 import wrapInstance


TOOL_NAME = "GL - Curve Wrangler"
VERSION = "v1"
CTX = "glCurveWranglerCtx"
WIN_OBJ = "glCurveWranglerWin"

DPI_SCALE = 1.0

# ------------------------------------------------------------------ palette --
#
# Sampled directly from Maya's shelf icons:
#
#   #2E2E2E  border / recess      #373737  background      #444444  panel
#   #5B5B5B  raised               #8F8F8F  dim text        #BDBDBD  text
#   #FFAA64  icon orange, bright  #DB9456  icon orange body
#   #5285A6  selection blue
#
# Only the two accents are user-swappable; the grey ladder is fixed.

C_BG        = "#373737"
C_PANEL     = "#444444"
C_PANEL_HI  = "#4F4F4F"
C_LINE      = "#2E2E2E"
C_EDGE      = "#5B5B5B"
C_TEXT      = "#BDBDBD"
C_TEXT_DIM  = "#8F8F8F"
GRAY_LIGHT  = "#EDEDED"

DEF_ACCENT = "#FFAA64"      # Maya icon orange
DEF_SECOND = "#5285A6"      # Maya selection blue

OPTVAR_ACCENT = "glCurveWranglerAccent"
OPTVAR_SECOND = "glCurveWranglerSecond"

ACCENT_PRESETS = [
    ("Maya Orange", "#FFAA64"),
    ("Icon Body", "#DB9456"),
    ("Maya Blue", "#5285A6"),
    ("Salmon", "#FD9054"),
    ("Amber", "#E8A33D"),
    ("Teal", "#2FA9A0"),
    ("Violet", "#8B6BD9"),
    ("Green", "#6FBF73"),
]

# derived accents, filled in by _apply_theme()
C_ACCENT = C_ACCENT_HI = C_SECOND = C_SECOND_HI = "#000000"

QC_ACCENT = QtGui.QColor(DEF_ACCENT)
QC_LIVE = QtGui.QColor(DEF_SECOND)
QC_WHITE = QtGui.QColor(GRAY_LIGHT)


def _lift(hex_c, amount):
    """Brighten a colour. Once value clips at 1.0 it tints toward white."""
    c = QtGui.QColor(hex_c)
    h, s, v, a = c.getHsvF()
    raw = v * amount + 0.02
    over = max(0.0, raw - 1.0)
    return QtGui.QColor.fromHsvF(
        max(h, 0.0), max(0.0, s - over * 0.55), min(1.0, raw), a).name().upper()


def current_accents():
    a = (cmds.optionVar(query=OPTVAR_ACCENT)
         if cmds.optionVar(exists=OPTVAR_ACCENT) else DEF_ACCENT)
    b = (cmds.optionVar(query=OPTVAR_SECOND)
         if cmds.optionVar(exists=OPTVAR_SECOND) else DEF_SECOND)
    return a or DEF_ACCENT, b or DEF_SECOND


def _apply_theme(accent=None, second=None):
    """Rebuild the accent globals. Safe to call repeatedly."""
    global C_ACCENT, C_ACCENT_HI, C_SECOND, C_SECOND_HI
    global QC_ACCENT, QC_LIVE, QC_WHITE

    if accent is None or second is None:
        saved_a, saved_b = current_accents()
        accent = accent or saved_a
        second = second or saved_b

    C_ACCENT = accent
    C_ACCENT_HI = _lift(accent, 1.15)
    C_SECOND = second
    C_SECOND_HI = _lift(second, 1.45)

    QC_ACCENT = QtGui.QColor(C_ACCENT)
    # the viewport ring has to read against a dark 3D scene
    QC_LIVE = QtGui.QColor(_lift(second, 1.9))
    QC_WHITE = QtGui.QColor(GRAY_LIGHT)


def save_accents(accent, second):
    cmds.optionVar(stringValue=(OPTVAR_ACCENT, accent))
    cmds.optionVar(stringValue=(OPTVAR_SECOND, second))
    _apply_theme(accent, second)


_apply_theme()


# ------------------------------------------------------------------ options --

OPTS = {
    "mode": "comb",        # "grab" | "comb"
    "radius": 60.0,        # pixels
    "strength": 1.0,       # 0..1
    "root_lock": 1,        # CVs pinned at the root
    "tip_bias": 1.0,       # comb: >1 pushes motion toward the tip
    "automask": True,      # restrict a stroke to one curve
    "preserve": True,      # keep segment lengths in comb mode
}


# -------------------------------------------------------------------- state --

_S = {
    "fns": [], "names": [], "orig": [], "weights": [],
    "seglen": [], "periodic": [], "targets": None,
    "anchor": None, "normal": None, "mode": "grab",
    "radius_start": 60.0, "active": False,
}

_HOVER = {"cache": [], "dirty": True}
_OVERLAYS = {}
_JOBS = []
_WIN = None


# -------------------------------------------------------------------- maths --

def _smoothstep(t):
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _view_ray(view, px, py):
    """(origin, direction) in world space for a viewport pixel.

    M3dView is a thin port of the old API: viewToWorld fills output
    arguments instead of returning them on most builds.
    """
    x, y = int(px), int(py)
    try:
        res = view.viewToWorld(x, y)
        if isinstance(res, (tuple, list)) and len(res) >= 2:
            return om.MPoint(res[0]), om.MVector(res[1])
    except TypeError:
        pass
    origin = om.MPoint()
    direction = om.MVector()
    view.viewToWorld(x, y, origin, direction)
    return origin, direction


def _ray_plane(view, px, py, plane_pt, plane_n):
    origin, direction = _view_ray(view, px, py)
    denom = direction * plane_n
    if abs(denom) < 1e-9:
        return om.MPoint(plane_pt)
    t = ((om.MVector(plane_pt) - om.MVector(origin)) * plane_n) / denom
    return om.MPoint(om.MVector(origin) + direction * t)


def _camera_view_dir(view):
    return om.MFnCamera(view.getCamera()).viewDirection(om.MSpace.kWorld)


def _selected_curve_fns():
    out = []
    sel = om.MGlobal.getActiveSelectionList()
    for i in range(sel.length()):
        try:
            dag = sel.getDagPath(i)
        except Exception:
            continue
        if dag.hasFn(om.MFn.kTransform):
            try:
                dag.extendToShape()
            except Exception:
                continue
        if not dag.hasFn(om.MFn.kNurbsCurve):
            continue
        out.append((om.MFnNurbsCurve(dag), dag.fullPathName()))
    return out


# ------------------------------------------------------------------- hover --

HOVER_MAX_PTS = 1500


def _hover_rebuild():
    """Cache a subsampled set of world CV positions for hover testing."""
    data = []
    pairs = _selected_curve_fns()
    total = 0
    for fn, name in pairs:
        try:
            pts = fn.cvPositions(om.MSpace.kWorld)
        except Exception:
            continue
        total += len(pts)
        data.append((name.split("|")[-1], pts))

    stride = max(1, int(math.ceil(total / float(HOVER_MAX_PTS)))) if total else 1
    _HOVER["cache"] = [(nm, [pts[i] for i in range(0, len(pts), stride)])
                       for nm, pts in data]
    _HOVER["dirty"] = False


def _hover_probe(view, vx, vy):
    """What sits under the brush at viewport point (vx, vy)?

    Returns (curve_count, cv_count, nearest_curve_name).
    """
    if _HOVER["dirty"]:
        _hover_rebuild()

    r = OPTS["radius"]
    curves, cvs, best, best_d = 0, 0, None, 1e18
    for name, pts in _HOVER["cache"]:
        hit = 0
        for p in pts:
            try:
                sx, sy, vis = view.worldToView(p)
            except Exception:
                continue
            if not vis:
                continue
            d = math.hypot(sx - vx, sy - vy)
            if d < r:
                hit += 1
                if d < best_d:
                    best_d, best = d, name
        if hit:
            curves += 1
            cvs += hit
    return curves, cvs, best


# ----------------------------------------------------------------- overlay --

class _BrushOverlay(QtWidgets.QWidget):
    """Frameless, click-through, translucent window drawn over the viewport.

    This MUST be a top level window. A child widget stacked on Maya's
    native OpenGL viewport is never composited by Qt, so its background is
    never erased: every frame paints over the last one and the circles
    pile up while the un-erased surface reads as flat grey.

    The window is sized to a box around the cursor rather than the whole
    panel, so it does not float over the rest of the Maya interface.
    """

    PAD_X = 250
    PAD_Y = 90

    def __init__(self):
        super(_BrushOverlay, self).__init__(None)
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.Tool
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.WindowTransparentForInput
            | QtCore.Qt.NoDropShadowWindowHint)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_NoSystemBackground, True)
        self.setFocusPolicy(QtCore.Qt.NoFocus)

        self.center = QtCore.QPointF(0, 0)
        self.label = ""
        self.sub = ""
        self.live = False
        self.hide()

    def show_at(self, global_pos):
        """Move the window so the cursor sits at its centre, then repaint."""
        r = OPTS["radius"] * DPI_SCALE
        w = int(2 * (r + self.PAD_X))
        h = int(2 * (r + self.PAD_Y))
        self.setGeometry(int(global_pos.x() - w * 0.5),
                         int(global_pos.y() - h * 0.5), w, h)
        self.center = QtCore.QPointF(w * 0.5, h * 0.5)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, _event):
        p = QtGui.QPainter(self)

        # wipe the previous frame; without this the strokes accumulate
        p.setCompositionMode(QtGui.QPainter.CompositionMode_Source)
        p.fillRect(self.rect(), QtCore.Qt.transparent)
        p.setCompositionMode(QtGui.QPainter.CompositionMode_SourceOver)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        x, y = self.center.x(), self.center.y()
        r = OPTS["radius"] * DPI_SCALE
        col = QC_LIVE if self.live else QC_ACCENT

        p.setPen(QtGui.QPen(col, 1.6))
        p.drawEllipse(QtCore.QPointF(x, y), r, r)

        soft = QtGui.QColor(col)
        soft.setAlpha(90)
        p.setPen(QtGui.QPen(soft, 1.0, QtCore.Qt.DashLine))
        p.drawEllipse(QtCore.QPointF(x, y), r * 0.5, r * 0.5)

        p.setPen(QtGui.QPen(col, 1.2))
        p.drawLine(QtCore.QPointF(x - 4, y), QtCore.QPointF(x + 4, y))
        p.drawLine(QtCore.QPointF(x, y - 4), QtCore.QPointF(x, y + 4))

        if not self.label:
            p.end()
            return

        f = QtGui.QFont("Segoe UI", 8)
        f.setBold(True)
        fm = QtGui.QFontMetrics(f)
        f2 = QtGui.QFont("Segoe UI", 8)
        fm2 = QtGui.QFontMetrics(f2)

        try:
            w1 = fm.horizontalAdvance(self.label)
            w2 = fm2.horizontalAdvance(self.sub) if self.sub else 0
        except AttributeError:
            w1 = fm.width(self.label)
            w2 = fm2.width(self.sub) if self.sub else 0

        bw = min(max(w1, w2) + 16, self.PAD_X - 12)
        bh = 32 if self.sub else 20
        bx = x + r * 0.72 + 6
        by = y + r * 0.72 + 6

        p.setPen(QtGui.QPen(
            QtGui.QColor(col.red(), col.green(), col.blue(), 120), 1.0))
        p.setBrush(QtGui.QColor(12, 13, 16, 215))
        p.drawRoundedRect(QtCore.QRectF(bx, by, bw, bh), 4, 4)

        p.setPen(col)
        p.setFont(f)
        p.drawText(QtCore.QRectF(bx + 8, by + 3, bw, 14),
                   QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.label)
        if self.sub:
            p.setPen(QC_WHITE)
            p.setFont(f2)
            p.drawText(QtCore.QRectF(bx + 8, by + 16, bw, 14),
                       QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self.sub)
        p.end()


class _ViewportFilter(QtCore.QObject):
    """Tracks the cursor over a model panel and drives its overlay."""

    def __init__(self, overlay, panel_widget, parent=None):
        super(_ViewportFilter, self).__init__(parent)
        self.overlay = overlay
        self.panel = panel_widget

    def eventFilter(self, obj, event):
        et = event.type()

        if et == QtCore.QEvent.MouseMove:
            if _ctx_is_active():
                try:
                    self._update(event)
                except Exception:
                    self.overlay.hide()
            elif self.overlay.isVisible():
                self.overlay.hide()

        elif et in (QtCore.QEvent.Leave, QtCore.QEvent.FocusOut,
                    QtCore.QEvent.WindowDeactivate):
            self.overlay.hide()

        return False

    def _update(self, event):
        ov = self.overlay
        par = self.panel
        if par is None or not par.isVisible():
            ov.hide()
            return

        try:
            gp = event.globalPosition().toPoint()
        except AttributeError:
            gp = event.globalPos()

        local = par.mapFromGlobal(gp)
        if not par.rect().contains(local):
            ov.hide()
            return

        ov.live = bool(_S["active"])
        ov.label = "{0}  {1:.0f}px".format(
            OPTS["mode"].upper(), OPTS["radius"])

        if _S["active"]:
            ov.sub = "{0} curves affected".format(len(_S["fns"]))
        else:
            # viewport y runs from the bottom, Qt from the top
            view = omui.M3dView.active3dView()
            curves, cvs, near = _hover_probe(
                view, local.x(), par.height() - local.y())
            if curves == 0:
                ov.sub = "no curve under brush"
            elif OPTS["automask"] and near:
                ov.sub = "MASK -> {0}".format(near)
            else:
                ov.sub = "{0} curves / {1} CVs".format(curves, cvs)

        ov.show_at(gp)


def attach_overlays():
    """Create an overlay + cursor filter on every model panel."""
    detach_overlays()
    for panel in cmds.getPanel(type="modelPanel") or []:
        try:
            ptr = omui1.MQtUtil.findControl(panel)
            if not ptr:
                continue
            widget = wrapInstance(int(ptr), QtWidgets.QWidget)

            overlay = _BrushOverlay()          # top level, no parent
            filt = _ViewportFilter(overlay, widget, widget)

            widget.installEventFilter(filt)
            widget.setMouseTracking(True)
            for child in widget.findChildren(QtWidgets.QWidget):
                child.setMouseTracking(True)
                child.installEventFilter(filt)

            _OVERLAYS[panel] = (overlay, filt, widget)
        except Exception:
            continue


def detach_overlays():
    for panel, (overlay, filt, widget) in list(_OVERLAYS.items()):
        try:
            widget.removeEventFilter(filt)
            for child in widget.findChildren(QtWidgets.QWidget):
                child.removeEventFilter(filt)
        except Exception:
            pass
        try:
            overlay.hide()
            overlay.setParent(None)
            overlay.deleteLater()
        except Exception:
            pass
    _OVERLAYS.clear()


def _overlays_refresh():
    for overlay, _f, _w in _OVERLAYS.values():
        try:
            if overlay.isVisible():
                overlay.update()
        except Exception:
            pass


CLUMP_SAMPLES = 64      # resolution of the guide polyline
CLUMP_TRAVEL = 150.0    # pixels of drag for a full clump


def _sample_curve(fn, count=CLUMP_SAMPLES):
    """Evenly spaced world points along a curve by arc length."""
    out = []
    try:
        total = fn.length()
    except Exception:
        return out
    if total <= 1e-9:
        return out
    for k in range(count):
        u = k / float(count - 1)
        try:
            param = fn.findParamFromLength(min(u, 0.9999) * total)
            out.append(om.MPoint(fn.getPointAtParam(param, om.MSpace.kWorld)))
        except Exception:
            if out:
                out.append(om.MPoint(out[-1]))
            else:
                out.append(om.MPoint())
    return out


def _average_samples(sample_sets):
    """Mean polyline of several equal-length sample lists."""
    if not sample_sets:
        return []
    n = len(sample_sets[0])
    out = []
    for k in range(n):
        acc = om.MVector(0.0, 0.0, 0.0)
        used = 0
        for s in sample_sets:
            if len(s) == n:
                acc += om.MVector(s[k])
                used += 1
        out.append(om.MPoint(acc / float(used)) if used else om.MPoint())
    return out


def _at_normalised(samples, u):
    """Linearly interpolate a sample polyline at normalised position u."""
    if not samples:
        return om.MPoint()
    n = len(samples)
    x = max(0.0, min(1.0, u)) * (n - 1)
    i = int(math.floor(x))
    if i >= n - 1:
        return om.MPoint(samples[-1])
    f = x - i
    a = om.MVector(samples[i])
    b = om.MVector(samples[i + 1])
    return om.MPoint(a + (b - a) * f)


def _chord_params(pts):
    """Normalised cumulative chord length for each CV."""
    n = len(pts)
    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + (pts[i] - pts[i - 1]).length())
    total = cum[-1]
    if total <= 1e-9:
        return [i / float(max(1, n - 1)) for i in range(n)]
    return [c / total for c in cum]


# ------------------------------------------------------------------ context --

def _ctx_is_active():
    try:
        return cmds.currentCtx() == CTX
    except Exception:
        return False


def _press():
    button = cmds.draggerContext(CTX, query=True, button=True)
    _S["active"] = (button == 1)
    _S["radius_start"] = OPTS["radius"]
    if button != 1:
        return

    mod = cmds.draggerContext(CTX, query=True, modifier=True)
    mode = OPTS["mode"]
    if mod == "ctrl":
        mode = "comb" if mode == "grab" else "grab"
    _S["mode"] = mode

    pairs = _selected_curve_fns()
    if not pairs:
        om.MGlobal.displayWarning(
            "{0}: select one or more NURBS curves.".format(TOOL_NAME))
        _S["active"] = False
        return

    view = omui.M3dView.active3dView()
    ax, ay, _z = cmds.draggerContext(CTX, query=True, anchorPoint=True)
    radius = OPTS["radius"]

    rows = []
    for fn, name in pairs:
        pts = fn.cvPositions(om.MSpace.kWorld)
        n = len(pts)
        if n < 2:
            continue

        is_periodic = (fn.form == om.MFnNurbsCurve.kPeriodic)

        w, closest = [], 1e18
        for i in range(n):
            sx, sy, vis = view.worldToView(pts[i])
            if not vis:
                w.append(0.0)
                continue
            d = math.hypot(sx - ax, sy - ay)
            closest = min(closest, d)
            val = _smoothstep(1.0 - d / radius) if d < radius else 0.0

            if not is_periodic:
                if i < OPTS["root_lock"]:
                    val = 0.0
                elif mode == "comb":
                    u = float(i) / float(n - 1)
                    val *= u ** OPTS["tip_bias"]
            w.append(val * OPTS["strength"])

        if not any(v > 0.0 for v in w):
            continue

        lens = [(pts[i + 1] - pts[i]).length() for i in range(n - 1)]
        rows.append({"fn": fn, "name": name, "pts": pts, "w": w,
                     "lens": lens, "periodic": is_periodic, "near": closest})

    # Auto-Mask means different things per mode:
    #   grab / comb -> restrict the stroke to the nearest curve
    #   clump       -> choose the nearest curve as the clump guide,
    #                  otherwise clump toward the average of the group
    targets = None
    if mode == "clump":
        if len(rows) < 2:
            _S["active"] = False
            _overlays_refresh()
            if _WIN:
                _WIN.set_status("clump needs 2+ curves under the brush")
            return

        sample_sets = [_sample_curve(r["fn"]) for r in rows]

        if OPTS["automask"]:
            gi = min(range(len(rows)), key=lambda i: rows[i]["near"])
            guide = sample_sets[gi]
            rows = [r for i, r in enumerate(rows) if i != gi]
            sample_sets = [s for i, s in enumerate(sample_sets) if i != gi]
        else:
            guide = _average_samples([s for s in sample_sets if s])

        if not guide or not rows:
            _S["active"] = False
            _overlays_refresh()
            return

        targets = []
        for r in rows:
            us = _chord_params(r["pts"])
            targets.append([_at_normalised(guide, u) for u in us])

    elif OPTS["automask"] and len(rows) > 1:
        rows = [min(rows, key=lambda r: r["near"])]

    if not rows:
        _S["active"] = False
        _overlays_refresh()
        return

    hit_pts, hit_wts = [], []
    for r in rows:
        for i, wv in enumerate(r["w"]):
            if wv > 0.0:
                hit_pts.append(r["pts"][i])
                hit_wts.append(wv)

    total = sum(hit_wts)
    acc = om.MVector(0.0, 0.0, 0.0)
    for p, wt in zip(hit_pts, hit_wts):
        acc += om.MVector(p) * (wt / total)

    _S.update({
        "fns": [r["fn"] for r in rows],
        "names": [r["name"] for r in rows],
        "orig": [r["pts"] for r in rows],
        "weights": [r["w"] for r in rows],
        "seglen": [r["lens"] for r in rows],
        "periodic": [r["periodic"] for r in rows],
        "targets": targets,
        "anchor": om.MPoint(acc),
        "normal": _camera_view_dir(view),
    })

    cmds.draggerContext(CTX, edit=True, drawString="{0}  r={1:.0f}".format(
        _S["mode"], radius))
    _overlays_refresh()
    if _WIN:
        _WIN.set_status("{0} stroke - {1} curve(s), {2} CVs".format(
            _S["mode"], len(rows), len(hit_wts)), live=True)


def _apply(delta, px_dist=0.0):
    mode = _S["mode"]
    keep_len = OPTS["preserve"] and mode in ("comb", "clump")
    amount = min(1.0, px_dist / CLUMP_TRAVEL) if mode == "clump" else 0.0

    for ci, fn in enumerate(_S["fns"]):
        orig = _S["orig"][ci]
        w = _S["weights"][ci]
        n = len(orig)

        new = om.MPointArray()
        if mode == "clump":
            tgt = _S["targets"][ci]
            for i in range(n):
                k = w[i] * amount
                a = om.MVector(orig[i])
                b = om.MVector(tgt[i])
                new.append(om.MPoint(a + (b - a) * k))
        else:
            for i in range(n):
                new.append(om.MPoint(om.MVector(orig[i]) + delta * w[i]))

        if keep_len and not _S["periodic"][ci]:
            lens = _S["seglen"][ci]
            for i in range(1, n):
                d = om.MVector(new[i]) - om.MVector(new[i - 1])
                L = d.length()
                if L < 1e-9:
                    d = om.MVector(orig[i]) - om.MVector(orig[i - 1])
                    L = d.length()
                    if L < 1e-9:
                        continue
                d = d / L * lens[i - 1]
                new[i] = om.MPoint(om.MVector(new[i - 1]) + d)

        fn.setCVPositions(new, om.MSpace.kWorld)
        fn.updateCurve()


def _drag():
    button = cmds.draggerContext(CTX, query=True, button=True)
    dx, dy, _z1 = cmds.draggerContext(CTX, query=True, dragPoint=True)
    ax, ay, _z2 = cmds.draggerContext(CTX, query=True, anchorPoint=True)

    if button == 2:
        OPTS["radius"] = max(2.0, _S["radius_start"] + (dx - ax))
        cmds.draggerContext(CTX, edit=True,
                            drawString="radius {0:.0f}".format(OPTS["radius"]))
        if _WIN:
            _WIN.sync_from_opts()
            _WIN.set_status("radius {0:.0f} px".format(OPTS["radius"]))
        _overlays_refresh()
        cmds.refresh(currentView=True)
        return

    if not _S["active"] or not _S["fns"]:
        return

    view = omui.M3dView.active3dView()
    p0 = _ray_plane(view, ax, ay, _S["anchor"], _S["normal"])
    p1 = _ray_plane(view, dx, dy, _S["anchor"], _S["normal"])
    px_dist = math.hypot(dx - ax, dy - ay)
    _apply(om.MVector(p1) - om.MVector(p0), px_dist)

    if _S["mode"] == "clump" and _WIN:
        _WIN.set_status("clump {0:.0f}%".format(
            min(1.0, px_dist / CLUMP_TRAVEL) * 100.0), live=True)
    cmds.refresh(currentView=True)


def _release():
    _HOVER["dirty"] = True
    if not _S["active"] or not _S["fns"]:
        _S["active"] = False
        _overlays_refresh()
        return

    final = [fn.cvPositions(om.MSpace.kWorld) for fn in _S["fns"]]

    for ci, fn in enumerate(_S["fns"]):
        fn.setCVPositions(_S["orig"][ci], om.MSpace.kWorld)
        fn.updateCurve()

    cmds.undoInfo(openChunk=True, chunkName="curveWrangler")
    try:
        for ci, name in enumerate(_S["names"]):
            pts = final[ci]
            orig = _S["orig"][ci]
            for i in range(len(pts)):
                p = pts[i]
                if (om.MVector(p) - om.MVector(orig[i])).length() < 1e-7:
                    continue
                cmds.xform("{0}.cv[{1}]".format(name, i),
                           worldSpace=True, translation=(p.x, p.y, p.z))
    finally:
        cmds.undoInfo(closeChunk=True)

    _S["active"] = False
    _S["fns"] = []
    _overlays_refresh()
    if _WIN:
        _WIN.set_status("stroke committed", live=False)


def make_context():
    if cmds.draggerContext(CTX, exists=True):
        cmds.deleteUI(CTX)
    cmds.draggerContext(
        CTX,
        pressCommand=_press,
        dragCommand=_drag,
        releaseCommand=_release,
        cursor="crossHair",
        space="screen",
        undoMode="step",
    )
    return CTX


# ------------------------------------------------------------------ hotkey --

RT_CMD = "GLCurveWranglerToggle"
NAME_CMD = "GLCurveWranglerToggleNameCommand"
HOTKEY_SET = "GL_Tools"
OPTVAR = "glCurveWranglerHotkey"


def toggle_brush():
    """Arm or disarm the brush. Target of the activation hotkey."""
    if _ctx_is_active():
        cmds.setToolTo("selectSuperContext")
    else:
        if not cmds.draggerContext(CTX, exists=True):
            make_context()
        if not _OVERLAYS:
            attach_overlays()
        cmds.setToolTo(CTX)
    if _WIN:
        _WIN.refresh_live()


def combo_label(combo):
    if not combo:
        return ""
    bits = []
    if combo.get("ctrl"):
        bits.append("CTRL")
    if combo.get("alt"):
        bits.append("ALT")
    if combo.get("shift"):
        bits.append("SHIFT")
    bits.append(str(combo.get("key", "")).upper())
    return "+".join(bits)


def _combo_encode(combo):
    return "{0},{1},{2},{3}".format(
        int(bool(combo["alt"])), int(bool(combo["ctrl"])),
        int(bool(combo["shift"])), combo["key"])


def _combo_decode(text):
    try:
        a, c, s, k = text.split(",")
        return {"alt": bool(int(a)), "ctrl": bool(int(c)),
                "shift": bool(int(s)), "key": k}
    except Exception:
        return None


def saved_combo():
    if cmds.optionVar(exists=OPTVAR):
        return _combo_decode(cmds.optionVar(query=OPTVAR))
    return None


def _ensure_command():
    """Create the runTimeCommand / nameCommand pair the hotkey binds to."""
    if cmds.runTimeCommand(RT_CMD, exists=True):
        try:
            cmds.runTimeCommand(RT_CMD, edit=True, delete=True)
        except Exception:
            pass
    cmds.runTimeCommand(
        RT_CMD,
        annotation="Toggle the GL Curve Wrangler brush",
        category="Custom Scripts",
        commandLanguage="python",
        command="import GL_CurveWrangler\nGL_CurveWrangler.toggle_brush()")
    return cmds.nameCommand(
        NAME_CMD,
        annotation="Toggle the GL Curve Wrangler brush",
        sourceType="mel",
        command=RT_CMD)


def _ensure_editable_set():
    """Maya's default hotkey set is locked; move to an editable one.

    Returns the name of the set now current, or None on failure.
    """
    try:
        current = cmds.hotkeySet(query=True, current=True)
        if current and current != "Maya_Default":
            return current
        if not cmds.hotkeySet(HOTKEY_SET, exists=True):
            cmds.hotkeySet(HOTKEY_SET, source="Maya_Default", current=True)
        else:
            cmds.hotkeySet(HOTKEY_SET, edit=True, current=True)
        return HOTKEY_SET
    except Exception:
        return None


def clear_hotkey(combo=None):
    """Unbind a combo. Defaults to the stored one."""
    combo = combo or saved_combo()
    if not combo:
        return
    try:
        cmds.hotkey(keyShortcut=combo["key"],
                    altModifier=combo["alt"],
                    ctrlModifier=combo["ctrl"],
                    shiftModifier=combo["shift"],
                    name="")
    except Exception:
        pass


def register_hotkey(combo):
    """Bind combo to the brush toggle. Returns (ok, message)."""
    if not combo or not combo.get("key"):
        return False, "no key captured"

    old = saved_combo()
    if old:
        clear_hotkey(old)

    hset = _ensure_editable_set()
    if hset is None:
        return False, "could not access a hotkey set"

    try:
        _ensure_command()
        cmds.hotkey(keyShortcut=combo["key"],
                    altModifier=bool(combo["alt"]),
                    ctrlModifier=bool(combo["ctrl"]),
                    shiftModifier=bool(combo["shift"]),
                    name=NAME_CMD)
    except Exception as exc:
        return False, str(exc)

    cmds.optionVar(stringValue=(OPTVAR, _combo_encode(combo)))
    note = "" if hset != HOTKEY_SET else "  (hotkey set: {0})".format(HOTKEY_SET)
    return True, "{0} bound{1}".format(combo_label(combo), note)


def apply_saved_hotkey():
    combo = saved_combo()
    if combo:
        register_hotkey(combo)
    return combo


def _key_name(key):
    """Qt key code -> Maya keyShortcut string."""
    if QtCore.Qt.Key_A <= key <= QtCore.Qt.Key_Z:
        return chr(key).lower()
    if QtCore.Qt.Key_0 <= key <= QtCore.Qt.Key_9:
        return chr(key)
    if QtCore.Qt.Key_F1 <= key <= QtCore.Qt.Key_F12:
        return "F{0}".format(key - QtCore.Qt.Key_F1 + 1)
    return None


class KeyCaptureEdit(QtWidgets.QLineEdit):
    """Click, then press a combination. Modifier-only presses are ignored."""

    captured = QtCore.Signal(object)

    def __init__(self, parent=None):
        super(KeyCaptureEdit, self).__init__(parent)
        self.setReadOnly(True)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setPlaceholderText("click here, then press a combo   e.g. ALT+G")
        self.setToolTip("Letters, digits and F1-F12 are supported.\n"
                        "Hold your modifiers and press the key.")
        self.combo = None

    def keyPressEvent(self, event):
        key = event.key()
        if key in (QtCore.Qt.Key_Control, QtCore.Qt.Key_Shift,
                   QtCore.Qt.Key_Alt, QtCore.Qt.Key_Meta):
            return
        name = _key_name(key)
        if not name:
            return
        mods = event.modifiers()
        self.combo = {
            "key": name,
            "alt": bool(mods & QtCore.Qt.AltModifier),
            "ctrl": bool(mods & QtCore.Qt.ControlModifier),
            "shift": bool(mods & QtCore.Qt.ShiftModifier),
        }
        self.setText(combo_label(self.combo))
        self.captured.emit(self.combo)

    def set_combo(self, combo):
        self.combo = combo
        self.setText(combo_label(combo) if combo else "")


class HotkeyDialog(QtWidgets.QDialog):
    """Small registrar for the brush activation shortcut."""

    def __init__(self, parent=None):
        super(HotkeyDialog, self).__init__(parent)
        self.setWindowTitle("Activation Shortcut")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.setFixedWidth(320)
        self.setStyleSheet(stylesheet())

        root = QtWidgets.QWidget(self)
        root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        lay = QtWidgets.QVBoxLayout(root)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(9)

        head = QtWidgets.QLabel("ACTIVATION SHORTCUT")
        head.setObjectName("section")
        lay.addWidget(head)

        blurb = QtWidgets.QLabel(
            "Binds a key to arm and disarm the brush.\nALT+G is a safe pick.")
        blurb.setObjectName("hint")
        lay.addWidget(blurb)

        self.field = KeyCaptureEdit()
        self.field.set_combo(saved_combo())
        lay.addWidget(self.field)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.assign = QtWidgets.QPushButton("ASSIGN")
        self.assign.setObjectName("go")
        self.assign.setCursor(QtCore.Qt.PointingHandCursor)
        self.assign.setToolTip("Bind the captured combination in Maya's "
                               "hotkey editor.")
        self.assign.clicked.connect(self._assign)

        self.unbind = QtWidgets.QPushButton("UNBIND")
        self.unbind.setObjectName("seg")
        self.unbind.setStyleSheet("border-radius:5px;")
        self.unbind.setCursor(QtCore.Qt.PointingHandCursor)
        self.unbind.setToolTip("Remove the current binding.")
        self.unbind.clicked.connect(self._unbind)

        row.addWidget(self.assign, 2)
        row.addWidget(self.unbind, 1)
        lay.addLayout(row)

        self.note = QtWidgets.QLabel(
            combo_label(saved_combo()) + " currently bound"
            if saved_combo() else "nothing bound yet")
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        self.note.setAlignment(QtCore.Qt.AlignCenter)
        lay.addWidget(self.note)

        warn = QtWidgets.QLabel(
            "Maya's default hotkey set is read only, so a set named "
            "'GL_Tools' is created and made current on first assign.")
        warn.setObjectName("hint")
        warn.setWordWrap(True)
        lay.addWidget(warn)

    def _assign(self):
        combo = self.field.combo
        if not combo:
            self.note.setText("press a combination first")
            return
        ok, msg = register_hotkey(combo)
        self.note.setText(msg)
        self.note.setStyleSheet(
            "color: {0};".format(C_SECOND_HI if ok else "#E8624F"))
        if ok and _WIN:
            _WIN.refresh_hotkey_label()

    def _unbind(self):
        clear_hotkey()
        cmds.optionVar(remove=OPTVAR)
        self.field.set_combo(None)
        self.note.setText("unbound")
        self.note.setStyleSheet("color: {0};".format(C_TEXT_DIM))
        if _WIN:
            _WIN.refresh_hotkey_label()


class AccentDialog(QtWidgets.QDialog):
    """Swap the two accent colours. Shell greys are left alone."""

    def __init__(self, parent=None):
        super(AccentDialog, self).__init__(parent)
        self.setWindowTitle("Accents")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.setFixedWidth(310)
        self.primary, self.secondary = current_accents()
        self._swatches = {"primary": [], "secondary": []}

        root = QtWidgets.QWidget(self)
        root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        self.lay = QtWidgets.QVBoxLayout(root)
        self.lay.setContentsMargins(14, 14, 14, 12)
        self.lay.setSpacing(10)

        head = QtWidgets.QLabel("ACCENT COLOURS")
        head.setObjectName("section")
        self.lay.addWidget(head)

        blurb = QtWidgets.QLabel(
            "Primary drives sliders, values and the viewport ring.\n"
            "Secondary drives the active-brush state.")
        blurb.setObjectName("hint")
        self.lay.addWidget(blurb)

        self._row("PRIMARY", "primary")
        self._row("SECONDARY", "secondary")

        reset = QtWidgets.QPushButton("RESET TO SALMON / BLUE")
        reset.setObjectName("seg")
        reset.setStyleSheet("border-radius:5px;")
        reset.setCursor(QtCore.Qt.PointingHandCursor)
        reset.clicked.connect(self._reset)
        self.lay.addWidget(reset)

        self.setStyleSheet(stylesheet())
        self._paint_swatches()

    def _row(self, title, slot):
        lab = QtWidgets.QLabel(title)
        lab.setObjectName("section")
        self.lay.addWidget(lab)

        grid = QtWidgets.QHBoxLayout()
        grid.setSpacing(5)
        for name, hexcol in ACCENT_PRESETS:
            b = QtWidgets.QPushButton()
            b.setObjectName("swatch")
            b.setFixedSize(26, 26)
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setToolTip("{0}  {1}".format(name, hexcol))
            b.setProperty("hexcol", hexcol)
            b.clicked.connect(
                lambda _c=False, s=slot, h=hexcol: self._pick(s, h))
            self._swatches[slot].append(b)
            grid.addWidget(b)

        custom = QtWidgets.QPushButton("\u2026")
        custom.setObjectName("swatch")
        custom.setFixedSize(26, 26)
        custom.setCursor(QtCore.Qt.PointingHandCursor)
        custom.setToolTip("Pick any colour")
        custom.clicked.connect(lambda _c=False, s=slot: self._custom(s))
        grid.addWidget(custom)
        grid.addStretch(1)
        self.lay.addLayout(grid)

    def _pick(self, slot, hexcol):
        setattr(self, slot, hexcol)
        self._commit()

    def _custom(self, slot):
        start = QtGui.QColor(getattr(self, slot))
        col = QtWidgets.QColorDialog.getColor(start, self, "Pick an accent")
        if col.isValid():
            setattr(self, slot, col.name().upper())
            self._commit()

    def _reset(self):
        self.primary, self.secondary = DEF_ACCENT, DEF_SECOND
        self._commit()

    def _commit(self):
        save_accents(self.primary, self.secondary)
        self.setStyleSheet(stylesheet())
        self._paint_swatches()
        retheme_all()

    def _paint_swatches(self):
        for slot, buttons in self._swatches.items():
            active = getattr(self, slot).upper()
            for b in buttons:
                hexcol = b.property("hexcol")
                on = hexcol.upper() == active
                b.setChecked(on)
                b.setStyleSheet(
                    "QPushButton{{background:{0};border:2px solid {1};"
                    "border-radius:4px;}}"
                    "QPushButton:hover{{border:2px solid {2};}}".format(
                        hexcol, C_TEXT if on else C_LINE, C_TEXT))


def retheme_all():
    """Push a palette change into every live piece of the tool."""
    if _WIN:
        try:
            _WIN.setStyleSheet(stylesheet())
            _WIN.repaint_theme()
        except Exception:
            pass
    _overlays_refresh()


# ----------------------------------------------------------- input widgets --

class ToggleSwitch(QtWidgets.QAbstractButton):
    """Animated iOS-style switch."""

    def __init__(self, parent=None, w=42, h=22):
        super(ToggleSwitch, self).__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(w, h)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self._t = 0.0
        self._anim = QtCore.QPropertyAnimation(self, b"slide", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self.toggled.connect(self._run)

    def _get(self):
        return self._t

    def _set(self, v):
        self._t = v
        self.update()

    slide = QtCore.Property(float, _get, _set)

    def _run(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._t)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on):
        super(ToggleSwitch, self).setChecked(on)
        self._t = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        h = self.height()
        r = h / 2.0

        off = QtGui.QColor(52, 56, 66)
        on = QtGui.QColor(QC_ACCENT)
        track = QtGui.QColor(
            int(off.red() + (on.red() - off.red()) * self._t),
            int(off.green() + (on.green() - off.green()) * self._t),
            int(off.blue() + (on.blue() - off.blue()) * self._t))

        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QtCore.QRectF(0, 0, self.width(), h), r, r)

        knob = 2 + self._t * (self.width() - h)
        p.setBrush(QtGui.QColor(240, 243, 248))
        p.drawEllipse(QtCore.QRectF(knob, 2, h - 4, h - 4))
        p.end()


class SliderRow(QtWidgets.QWidget):
    """Label + slider + value readout, float or int."""

    changed = QtCore.Signal(float)

    def __init__(self, label, lo, hi, value, decimals=0, suffix="",
                 tip="", parent=None):
        super(SliderRow, self).__init__(parent)
        self.lo, self.hi, self.dec, self.suffix = lo, hi, decimals, suffix
        if tip:
            self.setToolTip(tip)

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        self.name = QtWidgets.QLabel(label)
        self.name.setFixedWidth(74)
        self.name.setObjectName("rowlabel")

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(self._to_slider(value))
        self.slider.valueChanged.connect(self._on_slide)

        self.value = QtWidgets.QLabel(self._fmt(value))
        self.value.setFixedWidth(56)
        self.value.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.value.setObjectName("rowvalue")

        lay.addWidget(self.name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.value)

    def _to_slider(self, v):
        return int(round((float(v) - self.lo) / (self.hi - self.lo) * 1000.0))

    def _from_slider(self, s):
        return self.lo + (s / 1000.0) * (self.hi - self.lo)

    def _fmt(self, v):
        return "{0:.{1}f}{2}".format(v, self.dec, self.suffix)

    def _on_slide(self, s):
        v = self._from_slider(s)
        self.value.setText(self._fmt(v))
        self.changed.emit(v)

    def set_value(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(self._to_slider(v))
        self.slider.blockSignals(False)
        self.value.setText(self._fmt(v))


class ToggleRow(QtWidgets.QWidget):
    """Title + description + switch."""

    toggled = QtCore.Signal(bool)

    def __init__(self, title, desc, value, tip="", parent=None):
        super(ToggleRow, self).__init__(parent)
        if tip:
            self.setToolTip(tip)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(10)

        col = QtWidgets.QVBoxLayout()
        col.setSpacing(0)
        t = QtWidgets.QLabel(title)
        t.setObjectName("rowlabel")
        d = QtWidgets.QLabel(desc)
        d.setObjectName("hint")
        col.addWidget(t)
        col.addWidget(d)

        self.sw = ToggleSwitch()
        self.sw.setChecked(value)
        self.sw.toggled.connect(self.toggled.emit)

        lay.addLayout(col, 1)
        lay.addWidget(self.sw, 0, QtCore.Qt.AlignVCenter)


# ------------------------------------------------------------------ window --

def _maya_main():
    ptr = omui1.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget) if ptr else None


STYLE = """
QWidget#root {{ background: {bg}; }}
QLabel {{ color: {text}; font-family: 'Segoe UI'; font-size: 11px; }}
QLabel#section {{ color: {dim}; font-size: 9px; font-weight: 700;
                  letter-spacing: 1.4px; }}
QLabel#rowlabel {{ color: {text}; font-size: 11px; }}
QLabel#rowvalue {{ color: {accent}; font-size: 11px; font-weight: 600; }}
QLabel#hint {{ color: {dim}; font-size: 9px; }}
QLabel#status {{ color: {dim}; font-size: 10px; }}
QLabel#ver {{ color: {edge}; font-size: 9px; font-weight: 700;
              letter-spacing: 1px; }}

QToolTip {{ background: {line}; color: {text}; border: 1px solid {accent};
            padding: 6px 8px; font-family: 'Segoe UI'; font-size: 10px; }}

QFrame#card {{ background: {panel}; border: 1px solid {line};
               border-radius: 5px; }}
QFrame#statusbox {{ background: {line}; border: 1px solid {line};
                    border-radius: 4px; }}
QFrame#rule {{ background: {line}; max-height: 1px; border: none; }}

QLineEdit {{ background: {line}; color: {accent}; border: 1px solid {edge};
             border-radius: 4px; padding: 8px; font-family: 'Segoe UI';
             font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
QLineEdit:focus {{ border: 1px solid {accent}; }}

QPushButton#mini {{ background: {panel_hi}; color: {text};
                    border: 1px solid {line}; border-radius: 3px;
                    padding: 3px 9px; font-size: 9px; font-weight: 700;
                    letter-spacing: 1px; }}
QPushButton#mini:hover {{ color: {accent}; border: 1px solid {accent}; }}

QSlider::groove:horizontal {{ height: 4px; background: {line};
                              border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {text}; border: 1px solid {line};
                              width: 11px; height: 11px; margin: -5px 0;
                              border-radius: 6px; }}
QSlider::handle:horizontal:hover {{ background: {accent_hi}; }}

QPushButton#seg {{ background: {panel_hi}; color: {dim};
                   border: 1px solid {line}; padding: 7px 0;
                   font-size: 11px; font-weight: 600; }}
QPushButton#seg:hover {{ color: {text}; background: {edge}; }}
QPushButton#seg:checked {{ background: {accent}; color: {line};
                           border: 1px solid {accent}; }}

QPushButton#go {{ background: {panel_hi}; color: {text};
                  border: 1px solid {line}; border-radius: 4px;
                  padding: 11px 0; font-size: 12px; font-weight: 700;
                  letter-spacing: 0.6px; }}
QPushButton#go:hover {{ background: {edge}; border: 1px solid {accent}; }}
QPushButton#go[live="true"] {{ background: {second}; color: {light};
                               border: 1px solid {second_hi}; }}

QPushButton#swatch {{ border: 2px solid {line}; border-radius: 3px;
                      min-width: 26px; min-height: 26px; }}
QPushButton#swatch:hover {{ border: 2px solid {text}; }}
QPushButton#swatch:checked {{ border: 2px solid {text}; }}
"""


def stylesheet():
    return STYLE.format(
        bg=C_BG, panel=C_PANEL, panel_hi=C_PANEL_HI, line=C_LINE,
        edge=C_EDGE, text=C_TEXT, dim=C_TEXT_DIM, accent=C_ACCENT,
        accent_hi=C_ACCENT_HI, second=C_SECOND, second_hi=C_SECOND_HI,
        light=GRAY_LIGHT)


class CurveWranglerUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super(CurveWranglerUI, self).__init__(parent or _maya_main())
        self.setObjectName(WIN_OBJ)
        self.setWindowTitle("{0} {1}".format(TOOL_NAME, VERSION))
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.Tool)
        self.setMinimumWidth(330)
        self.setStyleSheet(stylesheet())
        self._build()
        self.sync_from_opts()
        self._hook_jobs()
        self.refresh_live()

    # -- construction ----------------------------------------------------

    def _card(self, title):
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        lay = QtWidgets.QVBoxLayout(frame)
        lay.setContentsMargins(14, 12, 14, 13)
        lay.setSpacing(9)
        if title:
            head = QtWidgets.QLabel(title)
            head.setObjectName("section")
            lay.addWidget(head)
        return frame, lay

    def _build(self):
        root = QtWidgets.QWidget(self)
        root.setObjectName("root")
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(root)

        main = QtWidgets.QVBoxLayout(root)
        main.setContentsMargins(14, 12, 14, 10)
        main.setSpacing(11)

        # header: no title, just the shortcut registrar and accent gear
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(6)
        head.addStretch(1)
        self.hk_btn = QtWidgets.QPushButton("SHORTCUT")
        self.hk_btn.setObjectName("mini")
        self.hk_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.hk_btn.clicked.connect(self._open_hotkey)
        head.addWidget(self.hk_btn)

        self.gear_btn = QtWidgets.QPushButton("\u2699")
        self.gear_btn.setObjectName("mini")
        self.gear_btn.setFixedWidth(26)
        self.gear_btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.gear_btn.setToolTip(
            "<b>Accents</b><br>Change the two accent colours. "
            "The grey shell stays as it is.")
        self.gear_btn.clicked.connect(self._open_accents)
        head.addWidget(self.gear_btn)
        main.addLayout(head)

        # mode
        card, lay = self._card("BRUSH MODE")
        seg = QtWidgets.QHBoxLayout()
        seg.setSpacing(0)
        self.grp = QtWidgets.QButtonGroup(self)
        self.grp.setExclusive(True)
        tips = (
            "<b>Grab</b><br>Drags CVs freely in the camera plane with a soft "
            "falloff. Segment lengths are not enforced, so strands can "
            "stretch. Best for reshaping.",
            "<b>Comb</b><br>Drags with the motion ramped from root to tip and "
            "the root pinned, then re-solves segment lengths so strands sweep "
            "instead of stretching. Best for grooming.",
            "<b>Clump</b><br>Gathers every strand under the brush toward a "
            "shared guide, ramped root to tip so bases stay planted and tips "
            "converge. Drag further to clump harder.<br><br>"
            "Needs two or more curves under the brush. <b>Auto-Mask</b> picks "
            "the nearest strand as the guide; with it off, strands clump "
            "toward the average of the group.",
        )
        for i, name in enumerate(("GRAB", "COMB", "CLUMP")):
            b = QtWidgets.QPushButton(name)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.setToolTip(tips[i])
            if i == 0:
                b.setStyleSheet("border-top-left-radius:4px;"
                                "border-bottom-left-radius:4px;")
            elif i == 2:
                b.setStyleSheet("border-top-right-radius:4px;"
                                "border-bottom-right-radius:4px;")
            self.grp.addButton(b, i)
            seg.addWidget(b)
        if hasattr(self.grp, "idClicked"):
            self.grp.idClicked.connect(self._on_mode)
        else:
            self.grp.buttonClicked.connect(
                lambda *_a: self._on_mode(self.grp.checkedId()))
        lay.addLayout(seg)
        main.addWidget(card)

        # falloff
        card, lay = self._card("FALLOFF")
        self.r_radius = SliderRow(
            "Radius", 2, 400, OPTS["radius"], 0, " px",
            tip="<b>Radius</b><br>Size of the brush in screen pixels. CVs "
                "outside it are untouched. Middle-mouse drag in the viewport "
                "adjusts this live.")
        self.r_radius.changed.connect(self._on_radius)

        self.r_strength = SliderRow(
            "Strength", 0.0, 1.0, OPTS["strength"], 2,
            tip="<b>Strength</b><br>Scales how much of your drag distance "
                "reaches the curve. 1.00 follows the cursor exactly; lower "
                "values let you build the shape up in passes.")
        self.r_strength.changed.connect(lambda v: self._set("strength", v))

        self.r_root = SliderRow(
            "Root lock", 0, 8, OPTS["root_lock"], 0,
            tip="<b>Root lock</b><br>Number of CVs at the start of each curve "
                "that never move, keeping strands planted on the surface. "
                "Raise to 2 to also hold the root tangent.")
        self.r_root.changed.connect(lambda v: self._set("root_lock", int(round(v))))

        self.r_tip = SliderRow(
            "Tip bias", 0.0, 4.0, OPTS["tip_bias"], 2,
            tip="<b>Tip bias</b><br>Comb and Clump. Shifts the effect toward the "
                "tip. 1.00 is a linear ramp; higher values leave the base "
                "stiff and sweep only the ends; 0 combs the whole strand "
                "evenly.")
        self.r_tip.changed.connect(lambda v: self._set("tip_bias", v))

        for w in (self.r_radius, self.r_strength, self.r_root, self.r_tip):
            lay.addWidget(w)
        main.addWidget(card)

        # behaviour
        card, lay = self._card("BEHAVIOUR")
        self.t_mask = ToggleRow(
            "Auto-Mask",
            "Stroke affects only the curve under the cursor",
            OPTS["automask"],
            tip="<b>Auto-Mask</b><br>Grab and Comb: restricts each stroke to "
                "the single curve nearest the brush centre, so you can keep a "
                "whole groom selected and still work one strand at a time. "
                "The viewport readout names the curve you would hit."
                "<br><br>Clump: chooses that nearest curve as the clump "
                "guide. With it off, strands gather toward the average of the "
                "group instead.")
        self.t_mask.toggled.connect(lambda v: self._set("automask", v))

        self.t_len = ToggleRow(
            "Preserve Length",
            "Hold segment lengths while combing",
            OPTS["preserve"],
            tip="<b>Preserve Length</b><br>Comb and Clump. After each stroke, "
                "re-solves every segment back to its original length from the "
                "root outward, so strands sweep and gather rather than "
                "stretch or shorten. Turn off to let strokes change length.")
        self.t_len.toggled.connect(lambda v: self._set("preserve", v))

        lay.addWidget(self.t_mask)
        lay.addWidget(self.t_len)
        main.addWidget(card)

        # activate + inline status
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(8)

        self.btn = QtWidgets.QPushButton("ACTIVATE BRUSH")
        self.btn.setObjectName("go")
        self.btn.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn.clicked.connect(self._on_activate)

        box = QtWidgets.QFrame()
        box.setObjectName("statusbox")
        box_lay = QtWidgets.QHBoxLayout(box)
        box_lay.setContentsMargins(10, 0, 10, 0)
        self.status = QtWidgets.QLabel("ready")
        self.status.setObjectName("status")
        self.status.setAlignment(QtCore.Qt.AlignCenter)
        self.status.setWordWrap(True)
        box_lay.addWidget(self.status)
        box.setToolTip("Live readout of what the brush is doing.")

        bottom.addWidget(self.btn, 1)
        bottom.addWidget(box, 1)
        main.addLayout(bottom)

        self.refresh_hotkey_label()

    # -- callbacks -------------------------------------------------------

    def _set(self, key, value):
        OPTS[key] = value
        _overlays_refresh()

    MODES = ("grab", "comb", "clump")

    def _on_mode(self, idx):
        OPTS["mode"] = self.MODES[idx] if 0 <= idx < len(self.MODES) else "grab"
        _overlays_refresh()

    def _on_radius(self, v):
        OPTS["radius"] = max(2.0, v)
        _overlays_refresh()

    def _on_activate(self):
        if _ctx_is_active():
            cmds.setToolTo("selectSuperContext")
        else:
            make_context()
            attach_overlays()
            cmds.setToolTo(CTX)
        self.refresh_live()

    def _open_hotkey(self):
        dlg = HotkeyDialog(self)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()

    def _open_accents(self):
        dlg = AccentDialog(self)
        dlg.exec_() if hasattr(dlg, "exec_") else dlg.exec()

    def repaint_theme(self):
        """Re-polish widgets whose colours are painted, not styled."""
        for w in (self.t_mask.sw, self.t_len.sw):
            w.update()
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.refresh_live()
        self.update()

    def refresh_hotkey_label(self):
        combo = saved_combo()
        self.hk_btn.setText(combo_label(combo) if combo else "SHORTCUT")
        self.hk_btn.setToolTip(
            "<b>Activation shortcut</b><br>"
            + ("Currently bound to <b>{0}</b>. ".format(combo_label(combo))
               if combo else "Nothing bound yet. ")
            + "Click to register a key that arms and disarms the brush.<br>"
              "<i>ALT+G is a safe pick.</i>")
        self._refresh_btn_tip()

    def _refresh_btn_tip(self):
        combo = saved_combo()
        shortcut = ("<br><br>Shortcut: <b>{0}</b>".format(combo_label(combo))
                    if combo else "")
        self.btn.setToolTip(
            "<b>Viewport controls</b><br>"
            "<b>LMB</b> drag &nbsp;&nbsp;brush the curves<br>"
            "<b>MMB</b> drag &nbsp;&nbsp;resize the brush radius<br>"
            "<b>Ctrl</b> + LMB &nbsp;&nbsp;flip Grab / Comb for one stroke"
            + shortcut)

    def sync_from_opts(self):
        self.r_radius.set_value(OPTS["radius"])
        self.r_strength.set_value(OPTS["strength"])
        self.r_root.set_value(OPTS["root_lock"])
        self.r_tip.set_value(OPTS["tip_bias"])
        try:
            idx = self.MODES.index(OPTS["mode"])
        except ValueError:
            idx = 0
        btn = self.grp.button(idx)
        if btn:
            btn.setChecked(True)

    def set_status(self, text, live=None):
        self.status.setText(text)
        if live is not None:
            self.status.setStyleSheet(
                "color: {0};".format(C_SECOND_HI if live else C_TEXT_DIM))

    def refresh_live(self):
        """Visual cue on the activate button while the context owns the mouse."""
        live = _ctx_is_active()
        self.btn.setText("ACTIVE  \u25cf" if live else "ACTIVATE")
        self.btn.setProperty("live", "true" if live else "false")
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
        self.set_status("armed - hover a viewport" if live else "ready",
                        live=live)
        if not live:
            for ov, _f, _w in _OVERLAYS.values():
                ov.hide()

    # -- script jobs -----------------------------------------------------

    def _hook_jobs(self):
        _kill_jobs()
        _JOBS.append(cmds.scriptJob(
            event=["ToolChanged", self.refresh_live], protected=False))
        _JOBS.append(cmds.scriptJob(
            event=["SelectionChanged", _on_selection_changed], protected=False))

    def closeEvent(self, event):
        global _WIN
        _kill_jobs()
        detach_overlays()
        if _ctx_is_active():
            cmds.setToolTo("selectSuperContext")
        _WIN = None
        super(CurveWranglerUI, self).closeEvent(event)


def _on_selection_changed():
    _HOVER["dirty"] = True
    _overlays_refresh()


def _kill_jobs():
    for j in _JOBS:
        try:
            if cmds.scriptJob(exists=j):
                cmds.scriptJob(kill=j, force=True)
        except Exception:
            pass
    del _JOBS[:]


# ------------------------------------------------------------------- entry --

def purge_legacy():
    """Remove viewport HUDs and contexts left behind by earlier versions.

    The pre-release 'Curvecomb' module created three headsUpDisplay blocks
    that nothing removes once that module is gone, so they stay pinned in
    the viewport corner forever.
    """
    try:
        for hud in (cmds.headsUpDisplay(listHeadsUpDisplays=True) or []):
            if hud.startswith("curveCombHUD"):
                cmds.headsUpDisplay(hud, remove=True)
    except Exception:
        pass

    for old_ctx in ("curveCombCtx",):
        try:
            if cmds.draggerContext(old_ctx, exists=True):
                cmds.deleteUI(old_ctx)
        except Exception:
            pass

    for old_win in ("curveCombWin",):
        try:
            if cmds.window(old_win, exists=True):
                cmds.deleteUI(old_win)
        except Exception:
            pass

    try:
        cmds.inViewMessage(clear="midCenterTop")
    except Exception:
        pass


def show():
    global _WIN
    purge_legacy()
    for w in QtWidgets.QApplication.allWidgets():
        if w.objectName() == WIN_OBJ:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
    make_context()
    attach_overlays()
    apply_saved_hotkey()
    _WIN = CurveWranglerUI()
    _WIN.show()
    return _WIN


def uninstall():
    global _WIN
    _kill_jobs()
    detach_overlays()
    purge_legacy()
    if _WIN:
        try:
            _WIN.close()
        except Exception:
            pass
        _WIN = None
    if cmds.draggerContext(CTX, exists=True):
        cmds.deleteUI(CTX)
